"""LAZARUS: the precision organ.

SONAR is recall. It sweeps a large rules/memory corpus and returns a wide,
cheap, deliberately noisy shortlist of candidate rules that *look* relevant to
a work-unit (a diff, a finished response, a decision). That shortlist is a
firehose and is never meant to be shown to a human as-is.

LAZARUS is the filter. It takes SONAR's shortlist plus the work-unit, drops
anything the DECLINED ledger has already judged irrelevant for this exact work
(the anti-nag property), then asks the judge model one batched question per
candidate:

    "Would applying this buried rule have CHANGED the finished work?"

The judge is instructed to default to NO and to reject rules that are merely
on-topic but inert. Only verdicts that say ``would_change`` is true AND clear
``min_confidence`` survive. Survivors become ranked retroactive-fix entries,
each carrying the span it would improve and a concrete proposed patch, and are
written to the ledger as SURFACED.

LAZARUS proposes; it never applies. Nothing in this module writes to the
user's files or mutates the finished work. Applying a fix is a separate,
human-initiated action recorded via ``lazarus ledger action``. There is no
autoapply code path here, by design.

This module is stdlib-only. The judge call and the Anthropic SDK live behind
``judge.py``; the ledger and its signatures live behind ``ledger.py``. LAZARUS
orchestrates the two and owns none of their third-party dependencies, so a
caller can import and reason about the pipeline without an API key installed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional, Sequence

from .apply import normalize_edit  # stdlib-only; the shared edit contract
from .ledger import Ledger, work_unit_signature
from .sonar import Candidate

if TYPE_CHECKING:  # pragma: no cover - imported lazily inside run_lazarus()
    from .config import Config

logger = logging.getLogger("lazarus_sonar.lazarus")

# The single question the judge is asked, restated here so callers and readers
# do not have to open judge.py to know what the precision filter actually tests.
# The literal prompt lives in judge.py; this is the human-facing summary.
JUDGE_QUESTION = (
    "Would applying this buried rule have CHANGED the finished work? "
    "Default to NO. Reject rules that are on-topic but inert."
)


# ---------------------------------------------------------------------------
# Verdict + retroactive-fix data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """One typed judgment about one candidate rule.

    This mirrors the per-candidate JSON schema the judge returns via
    ``output_config.format`` (see judge.py). It is reproduced as a dataclass so
    the rest of LAZARUS reasons over typed objects rather than raw dicts, and so
    a caller can construct verdicts directly in tests without invoking the model.

    Fields:
        rule_id: The candidate's stable id (path/slug from SONAR). The join key
            back to the corpus and to the ledger.
        would_change: The precision verdict. True only if applying the rule
            would have changed the finished work. The whole tool turns on this
            boolean being honest.
        confidence: Judge's confidence in ``would_change``, in [0.0, 1.0].
        where: Short description of the span in the finished work the rule bears
            on (a line, a hunk, a sentence). Empty string is allowed.
        patch: A concrete proposed change. Free text or a diff fragment. This is
            a PROPOSAL only; LAZARUS never applies it.
        reason: The judge's one-line justification, kept for the ledger and for
            human review.
        edit: The machine-applyable form of ``patch`` -- a ``{file, find,
            replace}`` dict, or ``None`` when the fix is advisory-only. Passed
            through to ``RetroFix.edit`` so the auto-applier (apply.py) can act on
            it; ``None`` fixes stay proposals. Normalized via
            ``apply.normalize_edit`` so an all-empty judge sentinel becomes None.
    """

    rule_id: str
    would_change: bool
    confidence: float
    where: str = ""
    patch: str = ""
    reason: str = ""
    edit: Optional[dict] = None

    @classmethod
    def from_judge(cls, raw: dict[str, Any]) -> "Verdict":
        """Build a Verdict from one judge-returned object.

        Tolerant of missing optional fields and out-of-range confidence, because
        the judge is a model and the schema constrains shape but not sanity.
        Raises on a missing ``rule_id`` because a verdict we cannot join back to
        a candidate is useless and silently dropping it would hide a judge bug.
        The ``edit`` is normalized through the same ``apply.normalize_edit`` the
        judge uses, so a stub or a hand-built raw verdict is held to the identical
        concrete-vs-advisory rule.
        """
        rule_id = raw.get("rule_id")
        if not rule_id or not isinstance(rule_id, str):
            raise ValueError(
                "judge verdict is missing a string 'rule_id'; cannot join it "
                f"back to a candidate. Raw verdict: {raw!r}"
            )
        confidence = _clamp_confidence(raw.get("confidence", 0.0))
        return cls(
            rule_id=rule_id,
            would_change=bool(raw.get("would_change", False)),
            confidence=confidence,
            where=str(raw.get("where", "") or ""),
            patch=str(raw.get("patch", "") or ""),
            reason=str(raw.get("reason", "") or ""),
            edit=normalize_edit(raw.get("edit")),
        )


@dataclass(frozen=True)
class RetroFix:
    """A surviving retroactive-fix entry, ready to surface to a human.

    Pairs the judge's Verdict with the SONAR Candidate it judged, so a caller
    rendering the result has the rule's title, path, and recall score alongside
    the precision verdict without a second lookup.
    """

    rule_id: str
    title: str
    path: str
    where: str
    patch: str
    reason: str
    confidence: float
    sonar_score: float
    edit: Optional[dict] = None

    def as_dict(self) -> dict[str, Any]:
        """Plain-dict view for JSON output and for the CLI/hook renderers.

        ``edit`` is the ``{file, find, replace}`` dict the auto-applier reads
        (via ``apply.edit_of``/``apply.apply_fix``), or ``None`` for an
        advisory-only fix. It rides in this dict verbatim so the async pending
        queue (which stores this exact payload) carries the applyable edit to the
        runner's auto-apply step with no second lookup.
        """
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "path": self.path,
            "where": self.where,
            "patch": self.patch,
            "reason": self.reason,
            "confidence": self.confidence,
            "sonar_score": self.sonar_score,
            "edit": self.edit,
        }


@dataclass
class AuditResult:
    """The full outcome of one LAZARUS retro-audit over one work-unit.

    ``fixes`` is the ranked, human-facing payload. The remaining fields are the
    accounting the CLI and hooks report so a run is explainable: how many
    candidates SONAR proposed, how many the ledger suppressed before the judge
    ran, how many the judge killed, and how many survived below the confidence
    bar. Nothing here is hidden state — a run should be fully reconstructable
    from these counts plus the ledger.
    """

    work_unit_sig: str
    kind: str
    fixes: list[RetroFix] = field(default_factory=list)
    candidates_in: int = 0
    suppressed_declined: int = 0
    judged: int = 0
    killed_by_judge: int = 0
    below_confidence: int = 0
    # rule_ids that were newly recorded as DECLINED this run (judged inert, or
    # judged relevant-but-below-confidence). Surfaced for transparency/tests.
    declined_rule_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "work_unit_sig": self.work_unit_sig,
            "kind": self.kind,
            "candidates_in": self.candidates_in,
            "suppressed_declined": self.suppressed_declined,
            "judged": self.judged,
            "killed_by_judge": self.killed_by_judge,
            "below_confidence": self.below_confidence,
            "surfaced": len(self.fixes),
            "declined_rule_ids": list(self.declined_rule_ids),
            "fixes": [f.as_dict() for f in self.fixes],
        }


# The callable a caller can inject in place of the real judge. Takes the
# work-unit text, its kind, and the surviving candidates; returns one raw dict
# verdict per candidate. This is exactly judge.judge_batch's signature, factored
# out so LAZARUS can be exercised offline with a stub and so judge.py stays the
# only module that imports the anthropic SDK.
JudgeFn = Callable[[str, str, Sequence[Candidate]], list[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_lazarus(
    work_unit: str,
    candidates: Sequence[Candidate],
    *,
    config: "Config",
    ledger: Optional[Ledger] = None,
    judge_fn: Optional[JudgeFn] = None,
    kind: str = "diff",
    record: bool = True,
) -> AuditResult:
    """Run the LAZARUS precision filter over SONAR's candidate shortlist.

    Pipeline, in order:

      1. Fail loud on an empty or blank work-unit. A retro-audit with nothing to
         audit is a caller bug, not a silent no-op.
      2. Compute the work-unit signature (sha256 of the normalized text). Reruns
         of the same work collapse to one signature.
      3. Drop any candidate whose ``(signature, rule_id)`` is already DECLINED in
         the ledger. This is the anti-nag suppression: a rule judged irrelevant
         for this work, or a surfaced item a human dismissed, is never
         re-surfaced for the same work.
      4. Call the judge ONCE, batched across the surviving candidates, asking the
         would-it-change-the-output question.
      5. Keep only verdicts where ``would_change`` is true AND confidence clears
         ``config.min_confidence``. Record every non-survivor as DECLINED so it
         is suppressed next time (killed-as-inert and below-confidence alike).
      6. Rank survivors and record them as SURFACED.

    LAZARUS proposes; it never writes files or mutates the finished work. The
    only side effects are ledger writes, and only when ``record`` is true.

    Note the division of labor: this orchestrator does NOT run SONAR and does not
    take a ``top_n``. The caller runs SONAR (or the ``run_sonar_for_config``
    adapter), caps the shortlist, and passes ``candidates`` in positionally. That
    separation of perception (SONAR) from cognition (LAZARUS) is the core design;
    keeping SONAR out of this function keeps LAZARUS a pure filter over a supplied
    shortlist.

    Args:
        work_unit: The finished work to audit (a diff, a response, a decision).
        candidates: SONAR's ranked shortlist. May be empty.
        config: Loaded, validated config. Supplies ``min_confidence`` and, when
            ``judge_fn`` is not given, the judge model, max_tokens, and api key
            for judge.py. All read via the flat accessors on config.Config
            (``config.ledger_path``, ``config.min_confidence``,
            ``config.judge_model``, ``config.api_key``) plus
            ``config.judge.max_tokens``.
        ledger: The ledger to read suppression from and write verdicts to. If
            None, one is opened from ``config.ledger_path``.
        judge_fn: Optional injected judge (for tests/offline). If None, the real
            batched judge from judge.py is used. The signature is exactly
            ``JudgeFn`` (work_unit, kind, candidates) -> list[dict]; the offline
            demo/test stub (examples/demo/stub_judge.py) conforms to it.
        kind: Work-unit kind label ("diff", "response", "decision"), passed to
            the judge and stored on the result. Descriptive only.
        record: When False, run the full pipeline but write nothing to the
            ledger. Useful for dry runs and previews.

    Returns:
        An AuditResult with the ranked surviving fixes and the run accounting.
        The return is an AuditResult object, not a dict and not a list; callers
        read ``result.fixes`` and serialize via ``result.as_dict()``.

    Raises:
        ValueError: If the work-unit is empty or blank.
        Any exception raised by the judge (missing anthropic pkg, missing API
        key, refusal, network error) propagates unchanged. Callers that must not
        wedge a session (Stop/PostToolUse hooks) catch it at their boundary and
        exit loud-but-non-blocking; LAZARUS itself does not swallow it, so the
        CLI can fail hard.
    """
    if work_unit is None or not work_unit.strip():
        # Fail loud: an empty work-unit almost always means the hook or CLI
        # failed to extract the diff/response and handed us nothing. Silently
        # returning "no fixes" would mask that upstream failure.
        raise ValueError(
            "LAZARUS received an empty work-unit. There is nothing to audit. "
            "This usually means the caller failed to extract the diff or "
            "finished response from its input."
        )

    sig = work_unit_signature(work_unit)
    result = AuditResult(work_unit_sig=sig, kind=kind, candidates_in=len(candidates))

    if ledger is None:
        ledger = Ledger(config.ledger_path)

    # --- Step 3: anti-nag suppression against the DECLINED ledger -----------
    survivors, suppressed = _drop_declined(candidates, sig, ledger)
    result.suppressed_declined = suppressed

    if not survivors:
        # Everything relevant was either never proposed or already declined for
        # this exact work. Nothing to judge; return a clean empty result. This
        # is a legitimate no-op (the anti-nag property working), NOT a silent
        # failure — the counts above make the reason explicit.
        logger.debug(
            "LAZARUS: no candidates survive DECLINED suppression for sig=%s "
            "(%d in, %d suppressed).",
            sig[:12],
            len(candidates),
            suppressed,
        )
        return result

    # --- Step 4: one batched judge call -------------------------------------
    if judge_fn is None:
        judge_fn = _default_judge_fn(config)

    raw_verdicts = judge_fn(work_unit, kind, survivors)
    verdicts = _parse_verdicts(raw_verdicts)
    result.judged = len(survivors)

    # Index survivors by rule_id so we can pair verdicts with their candidate.
    by_id = {c.rule_id: c for c in survivors}

    # --- Step 5 + 6: apply the precision filter, rank, and record -----------
    surfaced: list[RetroFix] = []
    declined_ids: list[str] = []
    min_conf = _clamp_confidence(config.min_confidence)

    for candidate in survivors:
        verdict = verdicts.get(candidate.rule_id)
        if verdict is None:
            # The judge returned no verdict for this candidate. Treat a missing
            # verdict as an implicit "would not change" — the default-NO stance
            # of the whole filter — and decline it so it is not re-judged for
            # this work. We do not silently forget it.
            logger.debug(
                "LAZARUS: judge returned no verdict for rule_id=%s; treating as "
                "would_change=false and declining.",
                candidate.rule_id,
            )
            declined_ids.append(candidate.rule_id)
            if record:
                ledger.decline(
                    sig,
                    candidate.rule_id,
                    reason="judge returned no verdict (default-NO)",
                    kind=kind,
                )
            continue

        if not verdict.would_change:
            # The judge killed it: on-topic-but-inert, or plainly irrelevant.
            # This is the no-false-pattern-matching gate doing its job.
            result.killed_by_judge += 1
            declined_ids.append(candidate.rule_id)
            if record:
                ledger.decline(
                    sig,
                    candidate.rule_id,
                    reason=verdict.reason or "judge: would_change=false",
                    kind=kind,
                )
            continue

        if verdict.confidence < min_conf:
            # Relevant in the judge's opinion, but not confidently enough to be
            # worth a human's attention. Decline it so we do not re-surface this
            # borderline call for the same work on every rerun.
            result.below_confidence += 1
            declined_ids.append(candidate.rule_id)
            if record:
                ledger.decline(
                    sig,
                    candidate.rule_id,
                    reason=(
                        f"judge: would_change=true but confidence "
                        f"{verdict.confidence:.2f} < min {min_conf:.2f}"
                    ),
                    kind=kind,
                )
            continue

        # Survivor: a confident would-change verdict. Pair it with its candidate
        # and build the human-facing retroactive-fix entry.
        surfaced.append(
            RetroFix(
                rule_id=candidate.rule_id,
                title=candidate.title,
                path=candidate.path,
                where=verdict.where,
                patch=verdict.patch,
                reason=verdict.reason,
                confidence=verdict.confidence,
                sonar_score=candidate.score,
                edit=verdict.edit,
            )
        )

    # Rank survivors: confidence first (that is the precision signal), SONAR
    # recall score as the tiebreak, rule_id last for a stable, deterministic
    # order so identical runs produce identical output.
    surfaced.sort(key=lambda f: (-f.confidence, -f.sonar_score, f.rule_id))

    if record and surfaced:
        for fix in surfaced:
            ledger.surface(
                sig,
                fix.rule_id,
                where=fix.where,
                patch=fix.patch,
                reason=fix.reason,
                confidence=fix.confidence,
                kind=kind,
            )

    result.fixes = surfaced
    result.declined_rule_ids = declined_ids
    # Guard against by_id going unused under linters; it documents the join and
    # is cheap to keep for callers that want candidate lookups off the result.
    del by_id
    return result


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _drop_declined(
    candidates: Sequence[Candidate],
    sig: str,
    ledger: Ledger,
) -> tuple[list[Candidate], int]:
    """Partition candidates into survivors and count of DECLINED-suppressed.

    A candidate is suppressed if ``(sig, rule_id)`` is already recorded as
    DECLINED for this work-unit signature. This is signature-scoped, not a
    permanent per-rule mute: substantially different work is a new signature and
    the same rule gets a fresh look there.
    """
    survivors: list[Candidate] = []
    suppressed = 0
    for candidate in candidates:
        if ledger.is_declined(sig, candidate.rule_id):
            suppressed += 1
            continue
        survivors.append(candidate)
    return survivors, suppressed


def _parse_verdicts(raw_verdicts: Iterable[dict[str, Any]]) -> dict[str, Verdict]:
    """Parse the judge's raw verdict objects into a {rule_id: Verdict} map.

    A raw object without a joinable ``rule_id`` is a judge/schema bug; it is
    logged and skipped rather than aborting the whole audit, because one
    malformed verdict should not sink the verdicts that parsed cleanly. A later
    duplicate rule_id wins (last verdict for a rule is authoritative).
    """
    verdicts: dict[str, Verdict] = {}
    for raw in raw_verdicts:
        if not isinstance(raw, dict):
            logger.warning(
                "LAZARUS: judge returned a non-object verdict; skipping: %r", raw
            )
            continue
        try:
            verdict = Verdict.from_judge(raw)
        except ValueError as exc:
            logger.warning("LAZARUS: dropping unparseable judge verdict: %s", exc)
            continue
        verdicts[verdict.rule_id] = verdict
    return verdicts


def _default_judge_fn(config: "Config") -> JudgeFn:
    """Bind the real batched judge from judge.py to this config.

    Imported lazily so that importing ``lazarus`` never pulls in judge.py (and
    therefore never requires the optional ``anthropic`` package). SONAR and the
    ledger stay usable, and importable, with zero third-party deps; only an
    actual judged audit reaches for the SDK.

    The returned callable conforms to ``JudgeFn`` and calls ``judge.judge_batch``
    with the model, max_tokens, and api key pulled off the config. ``judge_batch``
    returns one raw dict per verdict (not a typed Verdict); ``_parse_verdicts``
    above types them via ``Verdict.from_judge``. The config reads all resolve
    against config.Config's flat accessors (``judge_model``, ``api_key``) and the
    structured ``judge`` sub-object (``max_tokens``).
    """
    from . import judge  # local import: keeps anthropic out of this module

    def _run(work_unit: str, kind: str, candidates: Sequence[Candidate]) -> list[dict[str, Any]]:
        return judge.judge_batch(
            work_unit,
            candidates,
            kind=kind,
            model=config.judge_model,
            max_tokens=config.judge.max_tokens,
            api_key=config.api_key,
        )

    return _run


def _clamp_confidence(value: Any) -> float:
    """Coerce a confidence-like value into [0.0, 1.0].

    The judge is a model; a schema constrains a field to be a number but not to
    be in range. Coercing here keeps the ranking and the min_confidence gate
    well-defined instead of trusting the model to stay in bounds.
    """
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    if conf < 0.0:
        return 0.0
    if conf > 1.0:
        return 1.0
    return conf
