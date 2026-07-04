"""Claude judge wrapper for LAZARUS.

This is the precision organ's model call. SONAR produces a wide, cheap
candidate shortlist (high recall); this module asks a strong model one
question per candidate, batched into a single API call:

    "Would applying this buried rule actually have CHANGED the finished work?"

The prompt instructs the model to default to NO and to reject rules that are
on-topic but inert. That default-NO instruction IS the precision filter and the
no-false-pattern-matching gate. Verdicts come back as typed structured output
(one object per candidate) so parsing is schema-validated, not regex-on-prose.

Design constraints (see the repo README and design notes):

  * One Claude call per audit, batched across the surviving candidates, to
    bound cost and latency. Not one call per rule.
  * Model defaults to claude-opus-4-8 with adaptive thinking. The judge is
    precision-sensitive, so it gets the strong model by default; ``judge_model``
    is the documented main quality knob.
  * Structured output via ``output_config.format`` with a json_schema. No
    prefill (removed on Opus 4.8), no ``budget_tokens`` (removed on Opus 4.8),
    no deprecated ``output_format`` parameter.
  * Fail-loud. A missing ``anthropic`` package, a missing API key, or a model
    refusal raises ``JudgeError`` with a clear, actionable message. Callers in
    a Stop / PostToolUse hook are expected to catch ``JudgeError``, print it
    loudly, and exit without wedging the turn (that non-wedging policy lives in
    the hook, not here -- this module's job is to fail loud, not to decide
    whether a failure should block).

Interface note: the public batch entry point is ``judge_batch``. It consumes
SONAR ``Candidate`` objects (from ``sonar.py``, reading each candidate's
``excerpt`` as the rule body) and returns one plain ``dict`` per verdict, which
is exactly what ``lazarus.Verdict.from_judge`` and the ``lazarus.JudgeFn`` type
consume. The typed ``Verdict`` dataclass and ``_parse_verdicts`` below stay
internal to keep parsing schema-validated; ``judge_batch`` converts to dicts
before returning.

This module PROPOSES verdicts. It never writes files, never touches the user's
work, and never applies a fix. LAZARUS turns these verdicts into a ranked
retroactive-fix list; a human decides what to do with it.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from .sonar import Candidate

# The Anthropic SDK is an optional [judge] extra so SONAR and the ledger stay
# stdlib-only. Import it lazily inside _load_anthropic() rather than at module
# top level, so that merely importing this module (e.g. for the Verdict type)
# does not require the package to be installed.

__all__ = [
    "DEFAULT_JUDGE_MODEL",
    "JudgeError",
    "JudgeUnavailable",
    "JudgeRefusal",
    "Verdict",
    "judge_batch",
    "build_judge_prompt",
]


# The strong model by default. The judge is the precision-sensitive step, so it
# does not get downgraded for cost -- that is the user's call via judge_model.
DEFAULT_JUDGE_MODEL = "claude-opus-4-8"

# Generous ceiling. One object per candidate plus reasons; batches are small
# (SONAR shortlists, minus already-declined items), so this is rarely hit. It
# is a hard cap the model is not aware of, not a thinking budget.
DEFAULT_MAX_TOKENS = 8000

# Environment variable the Anthropic SDK reads for authentication. We check for
# it explicitly so we can fail loud with a useful message instead of letting the
# SDK raise a less obvious error deep in a request.
API_KEY_ENV = "ANTHROPIC_API_KEY"


# ---------------------------------------------------------------------------
# Errors -- all fail-loud, all subclasses of JudgeError so a hook can catch one
# type.
# ---------------------------------------------------------------------------


class JudgeError(Exception):
    """Base class for every judge failure.

    A Stop / PostToolUse hook should catch this, print it to stderr, and exit
    without blocking the turn. A CLI invocation should let it propagate so the
    user sees the message and a non-zero exit code.
    """


class JudgeUnavailable(JudgeError):
    """The judge cannot run because the environment is not set up.

    Raised for a missing ``anthropic`` package or a missing API key -- problems
    the user fixes once, not per request. This is the class the CLI imports and
    catches (``from .judge import JudgeUnavailable``); its swallow-vs-propagate
    policy lives at the caller boundary, not here.
    """


class JudgeRefusal(JudgeError):
    """The model declined the request (``stop_reason == "refusal"``).

    Carries the refusal category and explanation from ``stop_details`` when the
    API provides them, so the caller can log why. A refusal means we have no
    verdicts for this batch; LAZARUS surfaces nothing rather than guessing.
    """

    def __init__(self, message: str, *, category: str | None = None) -> None:
        super().__init__(message)
        self.category = category


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
#
# The judged unit is the SONAR ``Candidate`` (imported above). The prompt reads
# ``candidate.rule_id``, ``candidate.title``, ``candidate.score``, and
# ``candidate.excerpt`` (the rule body). There is intentionally no separate
# judge-side Candidate type: a second type was the source of interface drift
# between SONAR and the judge. If richer full-text is wanted later, widen
# ``Candidate.excerpt`` upstream in sonar.py rather than reintroducing a type.


@dataclass(frozen=True)
class Verdict:
    """The model's typed judgment for one candidate.

    Internal to this module: it is the schema-validated parse target for the
    model's JSON. ``judge_batch`` converts these to plain dicts before returning,
    because both of its consumers (``lazarus.JudgeFn`` and
    ``lazarus.Verdict.from_judge``) are dict-shaped.

    Attributes:
        rule_id: Echoes the candidate's ``rule_id``. Used to match the verdict
            back to its candidate.
        would_change: The precision gate. True only if applying this rule would
            actually have changed the finished work. Default-NO: on-topic but
            inert rules come back False.
        where: Short description of the span in the finished work the rule
            bears on (a location, a quoted phrase, a symbol). Empty when
            ``would_change`` is False.
        patch: Concrete proposed change to that span. A proposal only -- nothing
            is applied. Empty when ``would_change`` is False.
        confidence: Model's confidence in the verdict, 0.0-1.0. LAZARUS filters
            surviving verdicts by a configurable ``min_confidence``.
        reason: One-sentence justification. For a False verdict this states why
            the rule is inert here; for a True verdict, what it would improve.
    """

    rule_id: str
    would_change: bool
    where: str
    patch: str
    confidence: float
    reason: str


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


# The single precision instruction. The default-NO framing and the explicit
# rejection of on-topic-but-inert rules is the whole point of LAZARUS -- it is
# what turns a noisy relevance firehose into a precise retroactive-fix list.
_SYSTEM_PROMPT = """\
You are the precision filter for a retroactive knowledge audit. You are given \
a finished WORK-UNIT (a diff, a written response, or a decision) and a small \
set of buried rules that a wide keyword search flagged as possibly relevant.

For EACH rule, answer one question and one question only:

    Would applying this rule have CHANGED the finished work?

Rules for answering:

  * Default to NO. Most surfaced rules are on-topic but inert: they mention the \
same words as the work without bearing on any concrete decision in it. Reject \
those. Being relevant is not enough; the rule must point at a specific thing in \
the work that would be different if the rule had been followed.
  * Answer YES only when you can name the exact span in the finished work that \
would change and describe the concrete change. If you cannot point at a span \
and write a concrete patch, the answer is NO.
  * Judge the finished work as given. Do not invent problems that are not in \
the work. Do not reward a rule for being wise in general.
  * A rule that the work already satisfies is a NO -- nothing would change.

You are proposing, not applying. Never assume your patch will be used; a human \
reviews every YES.
"""


def _candidate_body(candidate: Candidate) -> str:
    """The rule body the judge reads for a candidate.

    SONAR carries the leading slice of the rule file on ``Candidate.excerpt``.
    That is the text the judge reasons over. Falls back to empty when a
    candidate has no excerpt (e.g. a hand-built candidate in a test).
    """
    return (getattr(candidate, "excerpt", "") or "").strip()


def _format_candidate_block(index: int, candidate: Candidate) -> str:
    """Render one candidate for the user prompt."""
    title = (candidate.title or "").strip() or "(untitled)"
    body = _candidate_body(candidate)
    # A light SONAR-score hint, framed as weak context so the judge does not
    # treat it as authoritative.
    score_note = f" (keyword score {candidate.score:.2f}; not authoritative)"
    return (
        f"### Candidate {index}\n"
        f"rule_id: {candidate.rule_id}\n"
        f"title: {title}{score_note}\n"
        f"rule text:\n"
        f"{body}\n"
    )


def build_judge_prompt(work_unit: str, candidates: Sequence[Candidate]) -> str:
    """Build the user-turn prompt for a batch of candidates.

    The finished work goes first, then every candidate. Returned as a single
    string so the caller can log it verbatim.

    Raises:
        JudgeUnavailable: if ``candidates`` is empty -- there is nothing to
            judge, which is a caller bug, not a model call.
    """
    if not candidates:
        raise JudgeUnavailable(
            "build_judge_prompt called with no candidates; nothing to judge"
        )

    work = work_unit.strip()
    if not work:
        raise JudgeUnavailable(
            "build_judge_prompt called with an empty work-unit; refusing to "
            "ask the model to judge nothing"
        )

    candidate_blocks = "\n".join(
        _format_candidate_block(i, c) for i, c in enumerate(candidates)
    )

    return (
        "## Finished work-unit\n\n"
        "This is the completed work to audit. It has already been produced; "
        "you are checking it after the fact.\n\n"
        "```\n"
        f"{work}\n"
        "```\n\n"
        "## Candidate rules\n\n"
        f"{candidate_blocks}\n"
        "## Task\n\n"
        "Return one verdict object per candidate above, in the same order, "
        "each echoing its rule_id. For every candidate decide whether applying "
        "the rule would have changed the finished work-unit, following the "
        "rules you were given. Default to would_change = false."
    )


def _kind_task_line(kind: str) -> str:
    """A single sentence naming what kind of work-unit is being audited.

    Threaded into the prompt so the judge frames "would this change the work"
    correctly for a diff vs a written response vs a decision. Advisory: it
    scopes the judge's attention, it does not change the schema or the default.
    """
    label = (kind or "").strip() or "work-unit"
    return (
        f"The finished work-unit being audited is a {label}. Judge it as a "
        f"{label}: point only at spans that actually exist in this {label}."
    )


# ---------------------------------------------------------------------------
# Structured output schema
# ---------------------------------------------------------------------------


def _verdict_item_schema() -> dict[str, Any]:
    """JSON schema for a single verdict object.

    Kept within the structured-output supported subset: object with
    ``additionalProperties: false``, basic types only, no numeric/string
    length constraints (those are not enforced by structured outputs).
    """
    return {
        "type": "object",
        "properties": {
            "rule_id": {
                "type": "string",
                "description": "Echo the candidate's rule_id verbatim.",
            },
            "would_change": {
                "type": "boolean",
                "description": (
                    "True only if applying this rule would have changed the "
                    "finished work. Default to false for on-topic-but-inert "
                    "rules and rules the work already satisfies."
                ),
            },
            "where": {
                "type": "string",
                "description": (
                    "The exact span in the finished work the rule bears on "
                    "(a location, a quoted phrase, a symbol). Empty string "
                    "when would_change is false."
                ),
            },
            "patch": {
                "type": "string",
                "description": (
                    "A concrete proposed change to that span. A proposal only. "
                    "Empty string when would_change is false."
                ),
            },
            "confidence": {
                "type": "number",
                "description": "Confidence in this verdict, from 0.0 to 1.0.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "One sentence: for false, why the rule is inert here; for "
                    "true, what it would improve."
                ),
            },
        },
        "required": [
            "rule_id",
            "would_change",
            "where",
            "patch",
            "confidence",
            "reason",
        ],
        "additionalProperties": False,
    }


def _verdicts_schema() -> dict[str, Any]:
    """Top-level schema: an object wrapping the list of verdicts.

    A wrapping object (rather than a bare top-level array) keeps the shape
    conventional and leaves room to attach batch-level fields later without a
    breaking change.
    """
    return {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": _verdict_item_schema(),
                "description": (
                    "One verdict per candidate, in the same order the "
                    "candidates were given."
                ),
            }
        },
        "required": ["verdicts"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# Anthropic client loading -- lazy and fail-loud
# ---------------------------------------------------------------------------


def _load_anthropic() -> Any:
    """Import the anthropic SDK, failing loud if it is not installed.

    Returns the imported module. Raised as JudgeUnavailable so a hook can tell
    a setup problem (fixable once) apart from a per-request failure.
    """
    try:
        import anthropic  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised via the message
        raise JudgeUnavailable(
            "The 'anthropic' package is required to run the LAZARUS judge but "
            "is not installed. Install the judge extra:\n"
            "    pip install 'lazarus-sonar[judge]'\n"
            "SONAR and the ledger run without it; only the judge needs it."
        ) from exc
    return anthropic


def _build_client(anthropic_module: Any, api_key: str | None) -> Any:
    """Construct an Anthropic client, requiring an API key.

    We check the key ourselves so the failure message names the exact env var
    to set, rather than surfacing an SDK-internal error later.
    """
    key = api_key or os.environ.get(API_KEY_ENV)
    if not key:
        raise JudgeUnavailable(
            f"No Anthropic API key found. Set the {API_KEY_ENV} environment "
            "variable (or pass api_key=) so the LAZARUS judge can call the "
            "model. The judge never runs offline; SONAR and the ledger do."
        )
    return anthropic_module.Anthropic(api_key=key)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _extract_refusal_category(response: Any) -> str | None:
    """Best-effort read of the refusal category from stop_details."""
    details = getattr(response, "stop_details", None)
    if details is None:
        return None
    return getattr(details, "category", None)


def _first_text_block(response: Any) -> str:
    """Return the text of the first text block in the response.

    With ``output_config.format`` set, the model's answer arrives as a text
    block containing schema-valid JSON. We locate it by type rather than
    assuming ``content[0]``, since a thinking block can precede it.
    """
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "") or ""
    raise JudgeError(
        "The judge response contained no text block to parse. This usually "
        "means the model returned only thinking or a tool call; the request "
        "may need to be retried."
    )


def _coerce_verdict(raw: Any, *, index: int) -> Verdict:
    """Convert one parsed JSON object into a typed Verdict, defensively.

    Structured output guarantees the shape, but we still coerce types and
    clamp confidence so a malformed field cannot crash the caller.
    """
    if not isinstance(raw, dict):
        raise JudgeError(
            f"Verdict at position {index} was not a JSON object: {raw!r}"
        )

    rule_id = raw.get("rule_id")
    if not isinstance(rule_id, str) or not rule_id:
        raise JudgeError(
            f"Verdict at position {index} is missing a usable rule_id: {raw!r}"
        )

    would_change = bool(raw.get("would_change", False))

    where = raw.get("where") or ""
    patch = raw.get("patch") or ""
    reason = raw.get("reason") or ""

    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    # Clamp into range; the schema does not enforce numeric bounds.
    confidence = max(0.0, min(1.0, confidence))

    return Verdict(
        rule_id=rule_id,
        would_change=would_change,
        where=str(where),
        patch=str(patch),
        confidence=confidence,
        reason=str(reason),
    )


def _parse_verdicts(text: str) -> list[Verdict]:
    """Parse the model's JSON payload into a list of Verdicts."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JudgeError(
            "The judge returned text that was not valid JSON despite the "
            f"structured-output schema. First 200 chars: {text[:200]!r}"
        ) from exc

    if not isinstance(payload, dict) or "verdicts" not in payload:
        raise JudgeError(
            "The judge JSON did not contain a top-level 'verdicts' array: "
            f"{text[:200]!r}"
        )

    items = payload["verdicts"]
    if not isinstance(items, list):
        raise JudgeError(
            f"The 'verdicts' field was not a list: {items!r}"
        )

    return [_coerce_verdict(item, index=i) for i, item in enumerate(items)]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def judge_batch(
    work_unit: str,
    candidates: Sequence[Candidate],
    *,
    kind: str = "diff",
    model: str = DEFAULT_JUDGE_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    api_key: str | None = None,
    client: Any | None = None,
) -> list[dict[str, Any]]:
    """Ask the judge model whether each candidate would have changed the work.

    One batched Claude call for all candidates. Returns one RAW DICT per verdict
    -- shape ``{"rule_id","would_change","where","patch","confidence","reason"}``
    -- which is exactly what ``lazarus.Verdict.from_judge`` and the
    ``lazarus.JudgeFn`` type consume. It does NOT return ``Verdict`` objects; the
    typed ``Verdict`` is used only as the schema-validated parse target and is
    converted to dicts before returning. LAZARUS owns the min_confidence filter
    and the retroactive-fix construction.

    Args:
        work_unit: The finished work being audited (diff, response, decision).
        candidates: SONAR's surviving shortlist (``sonar.Candidate`` objects),
            already filtered against the DECLINED ledger by LAZARUS. Each
            candidate's ``excerpt`` is read as the rule body. Must be non-empty.
        kind: Work-unit kind label ("diff", "response", "decision"). Threaded
            into the prompt so the judge frames the question for the right kind
            of artifact. Advisory: it does not change the schema or the
            default-NO stance.
        model: Judge model ID. Defaults to claude-opus-4-8 (the strong model,
            on purpose). This is the main quality knob.
        max_tokens: Hard output ceiling for the response. The model is not
            aware of it; it is a safety cap, not a thinking budget.
        api_key: Explicit key. If omitted, the ANTHROPIC_API_KEY env var is
            used. Missing key raises JudgeUnavailable.
        client: An already-constructed Anthropic client, mainly for testing.
            When provided, the anthropic package need not be importable and the
            api_key check is skipped (the caller owns auth).

    Returns:
        A list of plain dicts, one per verdict the model returned. Order follows
        the model's response; callers match by rule_id rather than position.

    Raises:
        JudgeUnavailable: no candidates, empty work-unit, missing anthropic
            package, or missing API key.
        JudgeRefusal: the model declined the request.
        JudgeError: any other failure (empty response, unparseable JSON, API
            error).
    """
    if not candidates:
        raise JudgeUnavailable(
            "judge_batch called with no candidates. LAZARUS should skip the "
            "model call entirely when the shortlist is empty."
        )

    prompt = build_judge_prompt(work_unit, candidates)
    # Prepend the kind framing so the judge scopes its attention to the right
    # kind of artifact. Kept out of build_judge_prompt so that function stays a
    # pure candidate renderer; the kind line is a judge-call concern.
    prompt = f"{_kind_task_line(kind)}\n\n{prompt}"

    if client is None:
        anthropic_module = _load_anthropic()
        client = _build_client(anthropic_module, api_key)
        api_error_type = getattr(anthropic_module, "APIError", Exception)
    else:
        # Caller-supplied client: we cannot assume the package is importable,
        # so treat any exception from the call as an API error below.
        api_error_type = Exception

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            # Adaptive thinking: the model decides depth per request. Effort is
            # left at its default. No budget_tokens (removed on Opus 4.8).
            thinking={"type": "adaptive"},
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            # Typed verdicts via structured output. Not the deprecated
            # output_format parameter; not a prefill.
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": _verdicts_schema(),
                }
            },
        )
    except JudgeError:
        # Never wrap our own errors (e.g. from a stubbed client raising one).
        raise
    except api_error_type as exc:
        raise JudgeError(
            f"The judge model call failed: {exc}"
        ) from exc

    # A refusal is a successful HTTP 200 with stop_reason == "refusal". Check
    # it before touching content -- a pre-output refusal has empty content and a
    # mid-stream refusal has partial content that must be discarded.
    if getattr(response, "stop_reason", None) == "refusal":
        category = _extract_refusal_category(response)
        suffix = f" (category: {category})" if category else ""
        raise JudgeRefusal(
            "The judge model refused this audit request"
            f"{suffix}. No verdicts were produced; LAZARUS will surface "
            "nothing for this batch.",
            category=category,
        )

    text = _first_text_block(response)
    verdicts = _parse_verdicts(text)
    # Convert to plain dicts before returning: both consumers (the JudgeFn type
    # and lazarus.Verdict.from_judge) are dict-shaped. The typed Verdict is an
    # internal parse target only.
    return [asdict(v) for v in verdicts]
