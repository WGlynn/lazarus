"""LAZARUS v2 background runner: the whole v1 pipeline, off the critical path.

v1 runs SONAR -> LAZARUS -> ledger as a BLOCKING Stop / PostToolUse hook, so
every finished turn pays the judge-latency tax even when nothing is caught. v2
moves that exact pipeline into a DETACHED child process spawned by the
non-blocking launcher hook. This module is that child's engine.

It is the ONLY new v2 code that calls the v1 engine, and it calls it with the
identical arguments the v1 ``retro_audit`` hook and the demo use:

    candidates = run_sonar_for_config(work_unit, config, kind=kind)
    result     = run_lazarus(work_unit, candidates, config=config,
                             ledger=ledger, judge_fn=judge_fn, kind=kind,
                             record=record)

That is the anti-drift property in DECISION D-1: there is exactly one place that
audits (``run_lazarus``), one signature function (``work_unit_signature``), one
``Candidate`` type, one ``ScoringConfig``. v2 adds a transport, not a second
engine. The runner is literally the sync hook's two engine lines plus a drain of
``result.fixes`` into the append-only pending queue, keyed on the SAME
``(work_unit_sig, rule_id)`` the ledger uses, so the next-turn injection hook can
surface last turn's findings without a live ``Config`` or a second lookup.

Placement in the package
------------------------
The canonical concurrency contract (section 2) names the runner module
``lazarus_sonar.async_.runner``, and this file lives there. Its imports of the v1
engine are two-dot parent imports (``from ..config import ...``); the pending
queue and the vendored offline stub are siblings inside this same ``async_``
subpackage (``from .pending import ...``, ``from .stub_judge import ...``). The
v1 engine files are imported, never edited.

Console entrypoint
------------------
``main`` is wired as the ``lazarus-audit-bg`` console script (pyproject
``[project.scripts]``: ``lazarus-audit-bg = "lazarus_sonar.async_.runner:main"``),
alongside the existing ``lazarus``. It is what the launcher spawns detached. It
reads the work-unit from ONE of three mutually exclusive sources -- a spool FILE
(the launcher's channel, because by the time the child reads it the parent hook
has already exited and a stdin pipe would be closed), stdin (manual/debug), or a
raw Claude Code hook-event JSON reusing the v1 extractor -- runs
``run_background_audit``, and exits with the runner's own fail-loud codes: 0 on a
clean audit (fixes may be 0), 2 on bad input / missing config / missing corpus, 3
on a judge fault.

Fail-loud vs fail-safe (DECISION D-9)
-------------------------------------
The runner is the DETACHED CHILD. It is off the critical path, so "fail loud"
here means "inspectable in the child's stderr / the launcher's per-run log file",
not "on the user's console". A misconfig or bad input exits 2; a judge fault
exits 3 and simply yields no new pending lines this run -- it degrades to
silence, which is exactly the async posture. The parent hook has already
returned, so there is nothing to un-block.

Offline / no-key is a first-class mode (DECISION D-6)
-----------------------------------------------------
``judge_fn`` is the SAME ``lazarus.JudgeFn`` seam ``run_lazarus`` already exposes.
Passing ``--stub`` (or ``[async].stub_judge = true`` in config, propagated by the
launcher) injects the deterministic offline judge the v1 demo ships, so the
entire async cycle runs with NO ``anthropic`` package and NO ``ANTHROPIC_API_KEY``.
From a checkout the runner prefers ``examples/demo/stub_judge.py`` (the source of
truth); when installed it falls back to the vendored
``lazarus_sonar.async_.stub_judge`` re-export.

Stdlib-only except where it lazily reaches the v1 judge, which the stub path
never forces.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import uuid
from pathlib import Path
from typing import NoReturn, Optional

# --------------------------------------------------------------------------- #
# v1 engine imports -- REUSED, never reimplemented.
#
# This module lives in the async_ subpackage, so the v1 engine modules are
# two-dot parent imports. The pending queue and the vendored stub are siblings
# inside async_ (one dot). Everything here is the exact same surface the v1 sync
# hook (hooks/retro_audit.py) and the demo (examples/demo/run_demo.py) import --
# there is no second engine.
# --------------------------------------------------------------------------- #
from ..config import Config, ConfigError, load_config          # REUSED
from ..sonar import run_sonar_for_config                       # REUSED (config-aware seam)
from ..lazarus import run_lazarus, AuditResult, JudgeFn        # REUSED
from ..ledger import Ledger, work_unit_signature               # REUSED
from .pending import PendingQueue, PendingFinding

__all__ = ["run_background_audit", "main"]


# The console-script / hook name, for attributable stderr. Mirrors the v1 hook's
# HOOK_NAME convention so a background crash is greppable.
RUNNER_NAME = "lazarus.audit_bg"

# Exit codes -- the runner's own fail-loud contract (DECISION D-9). Off the
# critical path, so "loud" means "in this child's stderr / the launcher log".
EXIT_OK = 0            # clean audit (fixes may be 0)
EXIT_FAIL_LOUD = 2     # bad input / missing config / missing corpus
EXIT_JUDGE_FAULT = 3   # judge / model / network fault, isolated

# Silence linters that flag work_unit_signature as unused: it is imported so the
# runner shares the exact signature function the ledger and pending queue use.
_SIGNATURE_REUSED = work_unit_signature


# --------------------------------------------------------------------------- #
# The async work-unit audit
# --------------------------------------------------------------------------- #


def run_background_audit(
    work_unit: str,
    *,
    config: Config,
    kind: str = "diff",
    judge_fn: Optional[JudgeFn] = None,
    queue: Optional[PendingQueue] = None,
    ledger: Optional[Ledger] = None,
    run_id: Optional[str] = None,
    record: bool = True,
) -> AuditResult:
    """Run the v1 pipeline on one work-unit and drain survivors to the queue.

    Identical engine to the v1 sync retro-audit:

        candidates = run_sonar_for_config(work_unit, config, kind=kind)
        result     = run_lazarus(work_unit, candidates, config=config,
                                 ledger=ledger, judge_fn=judge_fn, kind=kind,
                                 record=record)

    Then, NEW in v2, it drains ``result.fixes`` into the pending queue:

        sig = result.work_unit_sig     # == work_unit_signature(work_unit)
        rid = run_id or uuid.uuid4().hex[:8]
        queue.append_many(
            PendingFinding.from_retrofix(f, work_unit_sig=sig, kind=kind, run_id=rid)
            for f in result.fixes
        )

    The returned ``AuditResult`` is unchanged, so tests assert on it directly.

    Anti-nag is inherited, not reinvented (DECISION D-4): the SHARED
    ``Ledger(config.ledger_path)`` is passed straight into ``run_lazarus`` with
    ``record=True`` by default, so a rule already DECLINED for this signature is
    dropped before the judge -- exactly as v1. The pending queue's own dedup
    (keyed on the same ``(work_unit_sig, rule_id)``) is a second, independent
    layer against overlapping runner invocations; it lives in
    ``PendingQueue.append``.

    Args:
        work_unit: The finished work to audit (a diff, a response, a decision).
            Empty / blank propagates as the v1 ``run_lazarus`` fail-loud
            ``ValueError`` -- an async audit with nothing to audit is a caller
            bug, not a silent no-op.
        config: Loaded, validated ``Config``. Supplies the corpus/scoring for
            SONAR, ``min_confidence`` and the judge for LAZARUS, and the default
            queue (``config.pending_path``) and ledger (``config.ledger_path``)
            locations.
        kind: Work-unit kind label ("diff", "response", "decision"), threaded to
            SONAR (advisory) and the judge, carried onto each pending finding.
        judge_fn: Optional injected judge, the SAME ``JudgeFn`` contract
            ``run_lazarus`` exposes. ``None`` -> the real judge (needs the
            ``[judge]`` extra + an API key). The demo/tests inject
            ``stub_judge_fn`` and run with NO key.
        queue: Pending queue to drain into. Defaults to
            ``PendingQueue(config.pending_path)``.
        ledger: Shared anti-nag ledger. Defaults to
            ``Ledger(config.ledger_path)`` -- the same state the sync path and
            the CLI read, so async and sync suppression line up.
        run_id: 8-hex id of this runner invocation, stamped on each pending
            finding. Defaults to ``uuid.uuid4().hex[:8]``.
        record: Passed straight through to ``run_lazarus``. ``True`` (default)
            writes verdicts to the ledger, exactly as the sync path does.

    Returns:
        The ``AuditResult`` from ``run_lazarus``, unchanged.

    Raises:
        ValueError: on an empty/blank work-unit (from ``run_lazarus``).
        Any judge exception (missing anthropic pkg, missing key, refusal,
        network error) propagates unchanged; ``main`` catches it at the process
        boundary and exits 3 (loud, isolated, off the critical path). This
        function does not swallow it, so a programmatic caller / test can assert
        on it.
    """
    # Default the transport-side objects off the config, so a caller that only
    # holds a Config gets the same locations the launcher and inject hook use.
    if queue is None:
        queue = PendingQueue(config.pending_path)
    if ledger is None:
        ledger = Ledger(config.ledger_path)

    # --- the v1 engine, verbatim (perception -> cognition) ------------------
    # SONAR (perception): wide, cheap keyword sweep over the corpus. kind is
    # advisory in v1 and has no effect on scoring; it is threaded for the judge.
    candidates = run_sonar_for_config(work_unit, config, kind=kind)

    # --- NEW in v2: trigger gate (additive; only active when [async.trigger].enabled)
    # SONAR already ran (cheap, total coverage). This decides whether the EXPENSIVE
    # judge runs for THIS unit, so cost tracks risk density rather than a clock or a
    # token count. Below the risk-weighted bar we hand run_lazarus an EMPTY shortlist,
    # which short-circuits before any judge call (see lazarus.run_lazarus, the
    # "if not survivors" early return). Default OFF -> this block is skipped and every
    # unit is judged exactly as before, so the v1 / current path is unchanged.
    _trigger_threshold = None
    _trigger_path = None
    _shadow_this_run = False
    _shadow_path = None
    _trig = getattr(getattr(config, "async_", None), "trigger", None)
    if _trig is not None and _trig.enabled:
        from pathlib import Path

        from .trigger import RiskProfile, TriggerPolicy, load_threshold

        _trigger_threshold = _trig.base_threshold
        if _trig.adaptive:
            _trigger_path = Path(config.async_spool_dir) / "trigger_threshold.json"
            _trigger_threshold = load_threshold(
                _trigger_path, default=_trig.base_threshold
            )
        # RSI finding (2026-07-05): shadow sampling only earns its extra judge calls
        # when the adaptive controller is present to CONSUME the recall signal.
        # Without adaptive it is pure cost, so gate it on adaptive.
        _eff_shadow = _trig.shadow_epsilon if _trig.adaptive else 0.0
        _policy = TriggerPolicy(
            base_threshold=_trigger_threshold,
            risk=RiskProfile(high_risk_multiplier=_trig.high_risk_multiplier),
            max_judge_candidates=_trig.max_judge_candidates,
            shadow_epsilon=_eff_shadow,
        )
        _decision = _policy.decide(candidates, work_unit)
        _shadow_this_run = _decision.shadow
        _shadow_path = Path(config.async_spool_dir) / "trigger_shadow.json"
        candidates = (
            _policy.select_for_judge(candidates) if _decision.should_judge else []
        )

    # LAZARUS (cognition / precision): anti-nag suppression -> one batched judge
    # call -> confidence filter -> rank -> record. The shared ledger and the
    # injected judge_fn are the ONLY things that differ between a real and an
    # offline run; the pipeline is identical to the sync hook's.
    result = run_lazarus(
        work_unit,
        candidates,
        config=config,
        ledger=ledger,
        judge_fn=judge_fn,
        kind=kind,
        record=record,
    )

    # --- NEW in v2: drain survivors into the pending queue ------------------
    # Reuse the signature run_lazarus already computed rather than recomputing
    # it: result.work_unit_sig == work_unit_signature(work_unit). Storing each
    # finding as the verbatim RetroFix.as_dict() means the injection hook needs
    # no live Config and no second lookup to render it.
    sig = result.work_unit_sig
    rid = run_id or uuid.uuid4().hex[:8]
    queue.append_many(
        PendingFinding.from_retrofix(fix, work_unit_sig=sig, kind=kind, run_id=rid)
        for fix in result.fixes
    )

    # Shadow sampling: record whether this force-judged below-bar unit surfaced a real
    # catch, giving the controller the recall signal it is otherwise blind to.
    _shadow_surfaced = 0
    _shadow_total = 0
    if _shadow_path is not None:
        from .trigger import load_shadow_stats, record_shadow

        if _shadow_this_run:
            _shadow_surfaced, _shadow_total = record_shadow(
                _shadow_path, surfaced=bool(result.fixes), decay=0.98
            )
        else:
            _shadow_surfaced, _shadow_total = load_shadow_stats(_shadow_path)

    # Adaptive retune: fit the trigger bar to the ledger's recent accept-rate AND the
    # shadow-sample recall, so it self-corrects on both precision (too much DECLINED ->
    # raise) and false negatives (real catches below the bar -> lower). No-op unless
    # the gate is enabled with adaptive=true.
    if _trigger_path is not None and _trigger_threshold is not None:
        from .trigger import ThresholdController, save_threshold

        _new_thr, _reason = ThresholdController().update_from_records(
            _trigger_threshold,
            ledger.read_all(),
            window=200,
            shadow_surfaced=_shadow_surfaced,
            shadow_total=_shadow_total,
        )
        if _new_thr != _trigger_threshold:
            save_threshold(_trigger_path, _new_thr, reason=_reason)

    # Auto-apply (default ON): apply any fix that carries a concrete, uniquely-
    # locatable edit, reversibly (backup -> `lazarus undo`). Advisory-only fixes are
    # left surfaced. No human in the path; the backup is the safety net, not a gate.
    if getattr(config, "auto_apply", True):
        from pathlib import Path

        from ..apply import apply_fix

        _undo_dir = Path(config.async_spool_dir) / "undo"
        for _fix in result.fixes:
            _fd = _fix.as_dict() if hasattr(_fix, "as_dict") else _fix
            apply_fix(_fd, undo_dir=_undo_dir)

    return result


# --------------------------------------------------------------------------- #
# Console entrypoint: `lazarus-audit-bg`
# --------------------------------------------------------------------------- #


def _err(msg: str) -> None:
    """Write a visible, attributable error to the child's stderr.

    Off the critical path, this stderr is redirected by the launcher into the
    per-run log file under the async spool dir, so it is inspectable without ever
    landing on the parent's console (DECISION D-2 / D-9).
    """
    sys.stderr.write(f"[{RUNNER_NAME}] {msg}\n")


def _fail_loud(msg: str, *, exc: Optional[BaseException] = None) -> "NoReturn":  # type: ignore[name-defined]
    """Print a loud error and exit 2 (bad input / missing config / missing corpus)."""
    _err("FAIL-LOUD: " + msg)
    if exc is not None:
        _err(
            "cause: "
            + "".join(traceback.format_exception_only(type(exc), exc)).strip()
        )
    sys.exit(EXIT_FAIL_LOUD)


def _judge_fault(msg: str, exc: BaseException) -> "NoReturn":  # type: ignore[name-defined]
    """Report a judge/model/network fault and exit 3 (loud, isolated).

    The parent hook has already returned, so there is nothing to un-block. A
    judge fault simply means the pending queue gets no new lines this run: the
    async path degrades to silence, which is the intended posture.
    """
    _err("judge fault (no pending lines written this run): " + msg)
    _err("cause: " + "".join(traceback.format_exception_only(type(exc), exc)).strip())
    sys.exit(EXIT_JUDGE_FAULT)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="lazarus-audit-bg",
        description=(
            "LAZARUS v2 detached background runner. Runs SONAR -> LAZARUS -> "
            "ledger on one work-unit off the critical path and writes surviving "
            "retroactive-fix proposals to the pending queue for next-turn "
            "injection. Spawned detached by the async launcher hook."
        ),
    )

    # The three mutually-exclusive work-unit sources. argparse's mutually-
    # exclusive group enforces "at most one"; main() enforces "exactly one" and
    # checks them in the documented order (file -> stdin -> event-file).
    src = p.add_mutually_exclusive_group()
    src.add_argument(
        "--work-unit-file",
        default=None,
        help=(
            "Read the work-unit text/diff from this file. This is the launcher's "
            "channel: it spools the extracted unit to a file because by the time "
            "this detached child runs, the parent hook has exited and a stdin "
            "pipe would be closed."
        ),
    )
    src.add_argument(
        "--stdin",
        action="store_true",
        help="Read the work-unit from stdin (manual / debug).",
    )
    src.add_argument(
        "--event-file",
        default=None,
        help=(
            "Read a raw Claude Code hook-event JSON from this file and reuse the "
            "v1 extractor to get (kind, text). Same parsing as the sync hook."
        ),
    )

    p.add_argument(
        "--config",
        default=None,
        help=(
            "Path to lazarus.config.toml. Falls back to $LAZARUS_CONFIG, then a "
            "walk-up search, exactly like the v1 hooks."
        ),
    )
    p.add_argument(
        "--kind",
        choices=("diff", "response"),
        default=None,
        help="Work-unit kind. Defaults to 'diff' (or the extractor's kind for --event-file).",
    )
    p.add_argument(
        "--stub",
        action="store_true",
        help=(
            "Inject the deterministic offline stub judge (no anthropic package, "
            "no API key). Prefers examples/demo/stub_judge.py from a checkout; "
            "falls back to the vendored lazarus_sonar.async_.stub_judge when "
            "installed."
        ),
    )
    p.add_argument(
        "--run-id",
        default=None,
        help="8-hex run id stamped on each pending finding. Defaults to a fresh uuid4 prefix.",
    )
    return p.parse_args(argv)


def _read_text_file(path_str: str, *, label: str) -> str:
    path = Path(path_str).expanduser()
    if not path.is_file():
        _fail_loud(f"{label} path does not exist or is not a file: {path}")
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _fail_loud(f"could not read {label} {path}", exc=exc)


def _work_unit_from_event_file(path_str: str) -> tuple[str, str]:
    """Read a hook-event JSON file and reuse the v1 extractor -> (kind, text).

    Imports ``extract_work_unit`` from the v1 ``retro_audit`` hook so the async
    path parses a hook payload identically to the sync path (DECISION D-3): the
    same event yields the same ``work_unit_sig`` on both paths and the
    ledger/pending keys line up. The hooks dir is added to ``sys.path`` on demand
    (it is a scripts dir, not part of the installed package), mirroring the v1
    hooks' own import bootstrap.
    """
    raw = _read_text_file(path_str, label="--event-file")
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail_loud("--event-file did not contain valid JSON", exc=exc)
    if not isinstance(event, dict):
        _fail_loud(
            f"--event-file must contain a JSON object, got {type(event).__name__}"
        )

    extract = _import_extract_work_unit()
    kind, text = extract(event)
    return kind, text


def _import_extract_work_unit():
    """Locate and return the v1 ``retro_audit.extract_work_unit`` callable.

    The v1 extractor lives in ``hooks/retro_audit.py`` -- a scripts directory,
    not part of the installed package. Try a plain import first (in case the
    hooks dir is already importable), then fall back to putting the repo's
    ``hooks/`` dir on ``sys.path`` from this file's known location. Fail loud if
    it cannot be found: an event-file run with no extractor is unrecoverable.
    """
    try:
        from retro_audit import extract_work_unit  # type: ignore[import-not-found]
        return extract_work_unit
    except ImportError:
        pass

    # This file: src/lazarus_sonar/async_/runner.py -> repo root is parents[3].
    repo_root = Path(__file__).resolve().parents[3]
    hooks_dir = repo_root / "hooks"
    if hooks_dir.is_dir() and str(hooks_dir) not in sys.path:
        sys.path.insert(0, str(hooks_dir))
    try:
        from retro_audit import extract_work_unit  # type: ignore[import-not-found]
        return extract_work_unit
    except ImportError as exc:
        _fail_loud(
            "could not import the v1 work-unit extractor (retro_audit."
            "extract_work_unit) for --event-file. Run --event-file from a "
            "checkout that contains hooks/retro_audit.py, or feed the extracted "
            "work-unit directly via --work-unit-file / --stdin.",
            exc=exc,
        )


def _resolve_work_unit(args: argparse.Namespace) -> tuple[str, str]:
    """Return (kind, work_unit) from exactly one of the three input sources.

    Sources are mutually exclusive and checked in the documented order:
    --work-unit-file, then --stdin, then --event-file. Missing all three is a
    fail-loud caller/wiring bug. The default kind is "diff" (the launcher's
    common case); --event-file's kind comes from the v1 extractor unless --kind
    overrides it.
    """
    if args.work_unit_file is not None:
        work_unit = _read_text_file(args.work_unit_file, label="--work-unit-file")
        kind = args.kind or "diff"
        return kind, work_unit

    if args.stdin:
        work_unit = sys.stdin.read()
        kind = args.kind or "diff"
        return kind, work_unit

    if args.event_file is not None:
        kind, work_unit = _work_unit_from_event_file(args.event_file)
        if args.kind:
            kind = args.kind
        return kind, work_unit

    _fail_loud(
        "no work-unit source given. Pass exactly one of --work-unit-file "
        "<path>, --stdin, or --event-file <path>. The launcher spawns this "
        "runner with --work-unit-file."
    )


def _load_stub_judge_fn() -> JudgeFn:
    """Return the deterministic offline stub judge for ``--stub`` runs.

    Prefers ``examples/demo/stub_judge.py`` when running from a checkout (that
    demo file is the source of truth), exactly as the demo does; falls back to
    the vendored ``lazarus_sonar.async_.stub_judge`` re-export so ``--stub``
    still works from an installed wheel where the demo dir is not on the package
    path. A test asserts the two produce byte-identical verdicts so they can
    never drift (see verify_spec / DECISION D-6).
    """
    # Checkout-preferred: examples/demo/stub_judge.py, source of truth.
    # This file: src/lazarus_sonar/async_/runner.py -> repo root is parents[3].
    repo_root = Path(__file__).resolve().parents[3]
    demo_dir = repo_root / "examples" / "demo"
    if (demo_dir / "stub_judge.py").is_file():
        if str(demo_dir) not in sys.path:
            sys.path.insert(0, str(demo_dir))
        try:
            from stub_judge import stub_judge_fn  # type: ignore[import-not-found]
            return stub_judge_fn
        except ImportError:
            # Fall through to the vendored copy rather than failing: the vendored
            # re-export is a faithful, tested-identical fallback.
            pass

    # Installed fallback: the vendored re-export inside this async_ subpackage.
    try:
        from .stub_judge import stub_judge_fn  # type: ignore[import-not-found]
        return stub_judge_fn
    except ImportError as exc:
        _fail_loud(
            "could not load the offline stub judge for --stub (neither "
            "examples/demo/stub_judge.py from a checkout nor the vendored "
            "lazarus_sonar.async_.stub_judge is importable).",
            exc=exc,
        )


def main(argv: Optional[list[str]] = None) -> int:
    """Console entrypoint for ``lazarus-audit-bg`` (the detached runner).

    Parses the work-unit from one of --work-unit-file / --stdin / --event-file
    (mutually exclusive, checked in that order), loads and validates the config
    (fail-loud), runs ``run_background_audit``, and returns an exit code:

        0  clean audit (fixes may be 0)
        2  fail-loud: bad input, missing/unresolvable config, missing corpus
        3  judge fault (loud, isolated) -- no new pending lines this run

    Off the critical path: the parent hook has already returned, so nothing here
    needs to un-block a turn. "Loud" means "visible in this child's stderr / the
    launcher's per-run log", not "on the user's console".
    """
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # ---- Resolve the work-unit (fail-loud on bad/empty input) ------------- #
    kind, work_unit = _resolve_work_unit(args)
    if not work_unit.strip():
        _fail_loud(
            "extracted an empty work-unit. For --work-unit-file this means the "
            "spool file was empty; for --event-file it means the hook payload "
            "carried no usable diff/response. Nothing to audit -- this is a "
            "wiring problem, not a no-op."
        )

    # ---- Load + validate config (fail-loud) ------------------------------- #
    config_override = args.config or os.environ.get("LAZARUS_CONFIG")
    try:
        config = load_config(config_override)
    except ConfigError as exc:
        _fail_loud(
            "could not load a valid Lazarus config. Point --config (or "
            "$LAZARUS_CONFIG) at your lazarus.config.toml, which must set "
            "corpus.path and corpus.globs. See lazarus.config.example.toml.",
            exc=exc,
        )
    except FileNotFoundError as exc:
        _fail_loud(
            "config file not found. Copy lazarus.config.example.toml to "
            "lazarus.config.toml and set corpus.path / corpus.globs.",
            exc=exc,
        )

    # A resolved-but-missing corpus is fail-loud: never a silent home/cwd
    # fallback. Mirrors the v1 hook's explicit re-check after load.
    corpus_path = Path(config.corpus_path)
    if not corpus_path.exists():
        _fail_loud(
            f"corpus.path does not exist: {corpus_path}. Set it to your rules / "
            "memory directory in lazarus.config.toml. There is no home/cwd fallback."
        )
    if not corpus_path.is_dir():
        _fail_loud(f"corpus.path is not a directory: {corpus_path}.")

    # ---- Select the judge ------------------------------------------------- #
    # --stub (or the launcher-propagated [async].stub_judge) selects the offline
    # deterministic judge. Otherwise judge_fn stays None and run_lazarus binds
    # the real judge, needing the [judge] extra + a key -- the same seam the sync
    # path uses.
    judge_fn: Optional[JudgeFn] = _load_stub_judge_fn() if args.stub else None

    run_id = args.run_id or uuid.uuid4().hex[:8]

    # ---- Run the pipeline ------------------------------------------------- #
    # SONAR faults and empty/blank work-units are real bugs -> fail loud (exit 2).
    # Judge/model/network faults are isolated -> exit 3 with no pending lines.
    try:
        result = run_background_audit(
            work_unit,
            config=config,
            kind=kind,
            judge_fn=judge_fn,
            queue=PendingQueue(config.pending_path),
            ledger=Ledger(config.ledger_path),
            run_id=run_id,
            record=True,
        )
    except ValueError as exc:
        # Empty work-unit slipped past the guard above (defensive) -> fail loud.
        _fail_loud("the audit rejected the work-unit", exc=exc)
    except Exception as exc:  # noqa: BLE001 -- deliberate: isolate the judge fault
        # SONAR is called first inside run_background_audit and would surface as
        # a fault here too; both are engine faults we report loudly to this
        # child's log. The dominant case off the critical path is a judge/model/
        # network fault, which must not take the whole runner down noisily on the
        # user's console -- it just yields no pending lines. Exit 3, isolated.
        _judge_fault(
            "the background SONAR/LAZARUS pass raised. The retro-audit is "
            "advisory and off the critical path, so no pending findings were "
            "written this run.",
            exc,
        )

    # ---- Done ------------------------------------------------------------- #
    # Report the accounting to the child's stderr/log so a background run is
    # inspectable. Nothing is emitted on any harness channel: the launcher has
    # already returned and the injection hook is what surfaces findings next turn.
    _err(
        f"run_id={run_id} kind={kind} sig={result.work_unit_sig[:12]} "
        f"surfaced={len(result.fixes)} "
        f"candidates_in={result.candidates_in} judged={result.judged} "
        f"killed_by_judge={result.killed_by_judge} "
        f"below_confidence={result.below_confidence} "
        f"suppressed_declined={result.suppressed_declined} "
        f"pending_path={config.pending_path}"
    )
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        _err("interrupted")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 -- last-resort guard
        # An unexpected crash in the runner itself is fail-loud to this child's
        # log: it means the runner is broken, which needs to be seen and fixed.
        # Still off the critical path (the parent hook already returned).
        _err("unexpected error in the background runner")
        _err(
            "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ).rstrip()
        )
        sys.exit(EXIT_FAIL_LOUD)
