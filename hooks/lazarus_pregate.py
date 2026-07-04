#!/usr/bin/env python3
# hooks/lazarus_pregate.py
"""PreToolUse pre-gate hook (opt-in, SYNCHRONOUS): a tightly-bounded Sonar+Lazarus
on the PLANNED action, surfacing ONLY the highest-confidence rules to PREVENT
rather than patch. Deliberately narrow. Default OFF.

This is the one place LAZARUS v2 puts a judge call back ON the critical path, so
it is triple-constrained and gated behind a config opt-in (see canonical contract
section 6 and decision D-7):

  1. Default OFF          -- config.pregate_enabled. If false, this hook is an
                             immediate non-blocking no-op. The launcher/runner/
                             inject async path is authoritative; the pre-gate is a
                             thin, high-precision safety catch that only exists
                             when a human explicitly turns it on.
  2. Candidate cap        -- config.pregate_max_candidates (default 3). SONAR's
                             ranked shortlist is truncated to the top N BEFORE the
                             judge, so the synchronous judge batch is tiny. This
                             bounds both latency AND noise: the deep tail of SONAR
                             recall never reaches the judge here (narrowness knob #1).
  3. High confidence floor -- config.pregate_min_confidence (default 0.85, well
                             above the judge's normal min_confidence of 0.6). Only
                             near-certain "this WILL change the output" verdicts
                             surface (narrowness knob #2).
  4. record=False         -- the pre-gate never writes the ledger, so it cannot
                             suppress or pre-empt the authoritative async retro-
                             audit that legitimately sees the same work moments
                             later on Stop/PostToolUse.

The pre-gate never writes the pending queue either. It is a purely synchronous,
advisory, on-path check. Everything else is the async path's job.

Fail-safe boundary (decision D-9): the pre-gate is on the user's action path, so
its failure posture leans toward ALLOW. A judge/model/network error, an empty
extraction, or a disabled gate all resolve to allowing the action, never blocking
it. Genuine misconfiguration (bad/missing config, missing corpus) is still
surfaced loudly to stderr via the v1 `_fail_loud` plumbing, but even that path
defaults to allowing the action (`_fail_loud` writes stderr + exits 2; PreToolUse
does not treat exit 2 as a block, and we never emit a "deny" decision from the
pre-gate). This mirrors v1's documented optional-gate contract: "a judge error
defaults to allowing the action rather than blocking it".

The gate PROPOSES; it never applies, never edits files, never hard-blocks by
default. It emits a PreToolUse decision of "allow" carrying the high-confidence
findings as additionalContext. A strict-block mode is explicitly deferred
(decision D-8, item (c)).

Run standalone for debugging:
    echo '{"hook_event_name":"PreToolUse","tool_name":"Write", ...}' \
        | python hooks/lazarus_pregate.py --kind diff
    python hooks/lazarus_pregate.py --file examples/demo/work_unit.diff --kind diff

Exit codes:
    0  gate ran (findings may or may not have surfaced), gate disabled, empty
       extraction, or a judge error was swallowed to allow the action. In every
       case the action is ALLOWED (no deny decision is ever emitted).
    2  fail-loud: bad input, missing/unresolvable config, or missing corpus. The
       action is still allowed (PreToolUse does not treat this as a block); the
       error is visible to the operator so the misconfig gets fixed.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

# --------------------------------------------------------------------------- #
# Import bootstrap
#
# Hooks are invoked as `python <abs path>/hooks/lazarus_pregate.py` from an
# arbitrary working directory, so the package may not be importable yet. Add the
# repo's `src/` to sys.path before importing lazarus_sonar. This is the identical
# bootstrap the v1 retro_audit.py / session_start_sweep.py hooks use, so the
# pre-gate resolves the SAME engine from a plain checkout without an install.
#
# We also add the hooks/ directory itself so `import retro_audit` (the v1 hook we
# reuse for work-unit extraction and fail-loud plumbing) resolves. Reusing the v1
# extractor is deliberate: the pre-gate must extract a work-unit identically to
# the sync retro-audit and the async launcher, so the SAME planned/finished action
# yields the SAME work_unit_signature on every path (decision D-3).
# --------------------------------------------------------------------------- #

_HOOK_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HOOK_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"

if str(_HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOK_DIR))

try:
    from lazarus_sonar.config import ConfigError, load_config
    from lazarus_sonar.lazarus import run_lazarus
    from lazarus_sonar.sonar import run_sonar_for_config
except ImportError:
    if _SRC_DIR.is_dir() and str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))
    from lazarus_sonar.config import ConfigError, load_config
    from lazarus_sonar.lazarus import run_lazarus
    from lazarus_sonar.sonar import run_sonar_for_config

# Reuse the v1 retro_audit hook's work-unit extraction and fail-loud plumbing so
# the pre-gate and the sync/async paths parse hook payloads identically. Importing
# retro_audit is cheap: it does not pull in the anthropic SDK (the judge is loaded
# lazily inside lazarus.py / judge.py, not at retro_audit import time).
from retro_audit import (  # noqa: E402 -- import after the sys.path bootstrap above
    _content_from_write,
    _diff_from_edit,
    _err,
    _event_name,
    _fail_loud,
    _first_str,
    _tool_input,
    _tool_name,
)

# JudgeUnavailable is the canonical "judge setup problem" error (missing the
# optional `anthropic` package or the API key). It lives in judge.py. Import it
# lazily and fall back to a never-matching sentinel so the `except` clause is
# always well-formed even when judge.py cannot be imported at all (partial
# install). This mirrors retro_audit.py's proven shim. Importing judge.py is
# cheap: the Anthropic SDK is loaded lazily inside the module, not at import time.
try:  # pragma: no cover - trivial import shim
    from lazarus_sonar.judge import JudgeUnavailable
except Exception:  # noqa: BLE001 - judge module may be unimportable without extras

    class JudgeUnavailable(Exception):  # type: ignore[no-redef]
        """Fallback so the swallow-judge-error except clause is always valid.

        The real JudgeUnavailable lives in judge.py and is raised by the judge
        when the `anthropic` package or the API key is missing. If judge.py
        itself cannot be imported, this stand-in keeps the hook importable; the
        broad judge-fault swallow below still catches any judge exception and
        defaults to allowing the action.
        """


HOOK_NAME = "lazarus.pregate"


# --------------------------------------------------------------------------- #
# Fail-safe / decision plumbing
#
# The pre-gate never emits a deny decision. Its two outcomes are:
#   - allow-silent : emit a PreToolUse "allow" with no additionalContext.
#   - allow-context: emit a PreToolUse "allow" carrying the surfaced findings.
# `_fail_loud` (reused from retro_audit) writes stderr and exits 2 for genuine
# misconfig; PreToolUse does not treat exit 2 as a block, so the action still
# proceeds while the operator sees the error.
# --------------------------------------------------------------------------- #


def _allow(additional_context: Optional[str] = None) -> "NoReturn":  # type: ignore[name-defined]
    """Emit a non-blocking PreToolUse decision that ALLOWS the action, and exit 0.

    When `additional_context` is provided, it is attached on the PreToolUse
    additionalContext channel so the surfaced high-confidence findings reach the
    agent BEFORE it runs the planned tool. The decision is always "allow": the
    pre-gate surfaces context but, by default, does not hard-block (a strict-block
    mode is deferred, decision D-8 item (c)).

    Emitting `permissionDecision: "allow"` with an empty reason is the explicit
    non-blocking signal for PreToolUse. When there is nothing to surface we emit
    the bare allow shape so downstream tooling sees an intentional pass, not a
    missing hook.
    """
    hook_output: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
    }
    if additional_context:
        # The action is allowed; the findings ride along as context. Both channels
        # are populated so harness versions that read either one see the same text.
        hook_output["permissionDecisionReason"] = (
            "Lazarus pre-gate surfaced high-confidence buried rule(s) against the "
            "planned action. Advisory only; the action is not blocked."
        )
        hook_output["additionalContext"] = additional_context
    payload = {"hookSpecificOutput": hook_output}
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()
    sys.exit(0)


def _swallow_judge_error(msg: str, exc: BaseException) -> "NoReturn":  # type: ignore[name-defined]
    """Loud-but-allowing: report a judge/model/network failure, then ALLOW the action.

    This is the pre-gate's carve-out from fail-closed behavior and it points the
    same way v1's optional gate does: a judge error defaults to allowing the
    action rather than blocking it. The retro-audit that runs on Stop/PostToolUse
    moments later still gets a clean shot at the same work, so nothing is lost by
    letting the planned action through here.
    """
    _err("judge error (action ALLOWED): " + msg)
    _err("cause: " + "".join(traceback.format_exception_only(type(exc), exc)).strip())
    _allow()


# --------------------------------------------------------------------------- #
# Input handling
# --------------------------------------------------------------------------- #


def _read_stdin() -> str:
    """Read the hook event JSON from stdin.

    Empty stdin is a fail-loud condition -- a hook that silently no-ops on missing
    input hides real wiring bugs. `_fail_loud` writes stderr and exits 2; because
    this is PreToolUse, that exit does not block the action.
    """
    data = sys.stdin.read()
    if not data or not data.strip():
        _fail_loud(
            "no hook input on stdin. This pre-gate expects a Claude Code "
            "PreToolUse event JSON object on stdin, or a --stdin/--file/--kind "
            "override for manual runs."
        )
    return data


def _parse_event(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail_loud("hook input on stdin was not valid JSON", exc=exc)
    if not isinstance(obj, dict):
        _fail_loud(f"hook input must be a JSON object, got {type(obj).__name__}")
    return obj


def _read_text_file(path_str: str) -> str:
    path = Path(path_str).expanduser()
    if not path.is_file():
        _fail_loud(f"--file path does not exist or is not a file: {path}")
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _fail_loud(f"could not read --file {path}", exc=exc)


# --------------------------------------------------------------------------- #
# Planned-work-unit extraction
#
# For PreToolUse the work-unit is the PLANNED action, read from the event's
# tool_input (the pending Write/Edit args), NOT a finished diff or a last
# response. We reuse the v1 retro_audit synthesizers (`_diff_from_edit`,
# `_content_from_write`) so the planned action is rendered into the same diff
# shape the sync/async retro-audit will later see for the finished action. That
# keeps the work_unit_signature aligned across paths (decision D-3).
#
# A PreToolUse event whose tool is not a Write/Edit, or whose payload carries no
# usable content, yields an empty work-unit -> allow-silent. Unlike the sync
# retro-audit (which fails loud on empty), the pre-gate treats "nothing to judge"
# as a normal quiet pass: the gate must never wedge an action it cannot reason
# about.
# --------------------------------------------------------------------------- #


def extract_planned_work_unit(event: dict[str, Any]) -> tuple[str, str]:
    """Return (kind, text) for the PLANNED action to pre-judge.

    `kind` is "diff" for a Write/Edit (the only tools the pre-gate reasons over).
    Returns ("diff", "") when nothing usable is present, which the caller treats
    as a quiet allow-silent rather than a fail-loud condition.
    """
    tool = _tool_name(event)
    ti = _tool_input(event)
    path = _first_str(ti, "file_path", "filePath", "path") or ""

    if tool in ("Edit", "MultiEdit"):
        diff = _diff_from_edit(ti, path)
        if diff:
            return "diff", diff

    if tool == "Write":
        content = _content_from_write(ti, path)
        if content:
            return "diff", content

    # Some Edit payloads carry no usable old/new strings, or the tool is neither
    # Write nor Edit. Empty work-unit -> the caller allows silently.
    return "diff", ""


# --------------------------------------------------------------------------- #
# Stub-judge selection (offline / no-key mode)
#
# When config.async_stub_judge is set, the pre-gate uses the SAME deterministic
# stub the v1 demo and the async runner use, so the whole gate runs with no
# `anthropic` package and no ANTHROPIC_API_KEY (decision D-6). In a checkout we
# prefer examples/demo/stub_judge.py (the source of truth), exactly as the demo
# does; when installed we fall back to the vendored re-export
# lazarus_sonar.async_.stub_judge. If neither is importable we fail loud rather
# than silently reaching for the real (keyed) judge, because --stub was an
# explicit request for the offline path.
# --------------------------------------------------------------------------- #


def _load_stub_judge_fn() -> Callable[[str, str, Sequence[Any]], list[dict[str, Any]]]:
    """Return the deterministic stub JudgeFn, preferring the checkout demo copy.

    Resolution order:
      1. examples/demo/stub_judge.py in this repo checkout (the source of truth).
      2. the vendored re-export lazarus_sonar.async_.stub_judge (installed wheel).
    Fails loud if neither is available: a stub run that cannot find its stub is a
    wiring error, not something to paper over with the real judge.
    """
    demo_dir = _REPO_ROOT / "examples" / "demo"
    if (demo_dir / "stub_judge.py").is_file():
        if str(demo_dir) not in sys.path:
            sys.path.insert(0, str(demo_dir))
        try:
            from stub_judge import stub_judge_fn  # type: ignore[import-not-found]

            return stub_judge_fn
        except Exception:  # noqa: BLE001 - fall through to the vendored copy
            pass

    try:
        from lazarus_sonar.async_.stub_judge import stub_judge_fn  # type: ignore[import-not-found]

        return stub_judge_fn
    except Exception as exc:  # noqa: BLE001
        _fail_loud(
            "the offline stub judge could not be imported. Expected "
            "examples/demo/stub_judge.py in a checkout, or the vendored "
            "lazarus_sonar.async_.stub_judge in an installed package. --stub / "
            "[async].stub_judge requires one of them.",
            exc=exc,
        )


# --------------------------------------------------------------------------- #
# CLI overrides (for manual debugging outside a live hook)
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog=HOOK_NAME,
        description="LAZARUS pre-gate hook (PreToolUse, opt-in, synchronous).",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to lazarus.config.toml. Overrides discovery / LAZARUS_CONFIG.",
    )
    p.add_argument(
        "--file",
        default=None,
        help="Read the planned work-unit from this file instead of hook stdin "
        "(debugging).",
    )
    p.add_argument(
        "--kind",
        choices=("diff", "response"),
        default=None,
        help="Work-unit kind. The pre-gate reasons over planned diffs; defaults "
        "to 'diff'.",
    )
    p.add_argument(
        "--stdin",
        action="store_true",
        help="Force reading the hook event from stdin even if --file is given.",
    )
    p.add_argument(
        "--stub",
        action="store_true",
        help="Force the offline deterministic stub judge (no API key). Also "
        "enabled by [async].stub_judge in config.",
    )
    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# Rendering
#
# The findings render with the SAME RetroFix field names and PROPOSALS framing
# the v1 retro_audit renderer uses, so the human/agent sees one voice across the
# sync retro-audit, the async injection hook, and this pre-gate. The header states
# these are pre-action, high-confidence proposals; the footer points at the
# ledger commands, identical wording to retro_audit._render_fixes.
# --------------------------------------------------------------------------- #


def _render_high_confidence(fixes: list[Any], work_unit_sig: str) -> str:
    """Render surviving high-confidence pre-gate fixes as an additionalContext block."""
    out = io.StringIO()
    out.write(
        f"[{HOOK_NAME}] LAZARUS pre-gate surfaced {len(fixes)} high-confidence "
        "buried rule(s) that would change the PLANNED action:\n"
    )
    for i, fix in enumerate(fixes, 1):
        rule_id = _attr(fix, "rule_id") or "<unknown-rule>"
        confidence = _attr(fix, "confidence")
        where = _attr(fix, "where") or ""
        reason = _attr(fix, "reason") or ""
        patch = _attr(fix, "patch") or ""

        conf_str = f" (confidence {confidence})" if confidence is not None else ""
        out.write(f"\n  {i}. {rule_id}{conf_str}\n")
        if where:
            out.write(f"     where: {where}\n")
        if reason:
            out.write(f"     why:   {reason}\n")
        if patch:
            first = patch.strip().splitlines()
            preview = first[0] if first else ""
            more = "" if len(first) <= 1 else f"  (+{len(first) - 1} more line(s))"
            out.write(f"     patch: {preview}{more}\n")
    out.write(
        f"\n[{HOOK_NAME}] These are PROPOSALS surfaced BEFORE the action, not "
        "applied changes and not a block. Prefer to prevent rather than patch: "
        "adjust the planned write, then proceed. To record a decision for this "
        f"work: `lazarus ledger action {work_unit_sig} <rule_id>` if you apply "
        f"one, or `lazarus ledger decline {work_unit_sig} <rule_id>` to suppress "
        "it.\n"
    )
    return out.getvalue()


def _attr(obj: Any, name: str) -> Any:
    """Read `name` from an object or dict, returning None if absent."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # ---- Determine the event + planned work-unit -------------------------- #
    if args.file and not args.stdin:
        # Manual/debug path: no hook event, just a file.
        kind = args.kind or "diff"
        work_unit = _read_text_file(args.file)
    else:
        raw = _read_stdin()
        event = _parse_event(raw)
        # _event_name is read for parity with the other hooks and to keep the
        # debug output attributable; the pre-gate always emits the PreToolUse
        # allow shape regardless of the reported event name.
        _ = _event_name(event)
        kind, work_unit = extract_planned_work_unit(event)
        if args.kind:
            kind = args.kind

    # ---- Load + validate config (fail-loud on misconfig, but still allow) -- #
    config_override = args.config or os.environ.get("LAZARUS_CONFIG")
    try:
        config = load_config(config_override)
    except ConfigError as exc:
        _fail_loud(
            "could not load a valid Lazarus config. Point LAZARUS_CONFIG (or "
            "--config) at your lazarus.config.toml, which must set corpus.path "
            "and corpus.globs. See lazarus.config.example.toml.",
            exc=exc,
        )
    except FileNotFoundError as exc:
        _fail_loud(
            "config file not found. Copy lazarus.config.example.toml to "
            "lazarus.config.toml and set corpus.path / corpus.globs.",
            exc=exc,
        )

    # ---- Gate 0: opt-in switch -------------------------------------------- #
    # The pre-gate is OFF by default. When disabled it is an immediate
    # non-blocking allow -- no SONAR, no judge, no latency. This is the primary
    # narrowness control (decision D-7, knob #1).
    if not config.pregate_enabled:
        _allow()

    # An empty planned work-unit is a quiet allow, NOT a fail-loud condition. The
    # pre-gate only reasons over Write/Edit payloads; anything else (or an
    # old==new edit) simply passes. Never wedge an action we cannot audit.
    if not work_unit.strip():
        _allow()

    # A resolved-but-missing corpus is a fail-loud misconfig (visible to the
    # operator), but even that does not block the action -- _fail_loud exits 2 and
    # PreToolUse does not treat exit 2 as a deny. We never emit a deny decision.
    corpus_path = Path(config.corpus_path)
    if not corpus_path.exists():
        _fail_loud(
            f"corpus.path does not exist: {corpus_path}. Set it to your rules / "
            "memory directory in lazarus.config.toml. There is no home/cwd "
            "fallback."
        )
    if not corpus_path.is_dir():
        _fail_loud(f"corpus.path is not a directory: {corpus_path}.")

    # ---- Resolve the judge (real vs offline stub) ------------------------- #
    # --stub on the command line OR [async].stub_judge in config selects the
    # deterministic offline judge (no API key). Otherwise judge_fn stays None and
    # run_lazarus binds the real keyed judge from judge.py.
    use_stub = bool(args.stub) or bool(config.async_stub_judge)
    judge_fn = _load_stub_judge_fn() if use_stub else None

    # ---- SONAR (perception) ----------------------------------------------- #
    # A SONAR failure is a real bug (bad glob, unreadable corpus). It is surfaced
    # loudly, but -- consistent with the pre-gate's allow-by-default posture -- it
    # allows the action rather than blocking it. run_sonar_for_config unpacks
    # corpus_path / globs / exclude / scoring off the Config and calls the pure
    # run_sonar; the hook never touches the pure signature directly.
    try:
        candidates = run_sonar_for_config(work_unit, config, kind="diff")
    except Exception as exc:  # noqa: BLE001 -- surface loudly, but do not block the action
        _err("SONAR sweep failed while ranking corpus candidates (action ALLOWED)")
        _err("cause: " + "".join(traceback.format_exception_only(type(exc), exc)).strip())
        _allow()

    if not candidates:
        # No candidates cleared SONAR's threshold. Nothing buried is relevant to
        # the planned action. Quiet allow.
        _allow()

    # ---- Narrowness knob #1: cap candidates BEFORE the judge -------------- #
    # Truncate SONAR's ranked shortlist (already sorted by score desc) to the
    # pre-gate cap before any judge call. This bounds the synchronous judge batch
    # to a tiny size and keeps the deep tail of SONAR recall away from the on-path
    # judge (decision D-7, knob #1). Guard the cap to >= 1 defensively; config
    # validation already enforces this, but the slice must never be empty here.
    cap = max(1, config.pregate_max_candidates)
    capped = candidates[:cap]

    # ---- LAZARUS (cognition / precision), record=False -------------------- #
    # record=False: the pre-gate must NOT poison the ledger for the identical work
    # the Stop/PostToolUse retro-audit will legitimately see moments later. Any
    # judge/model/network failure defaults to ALLOW (never block). A
    # JudgeUnavailable (missing anthropic pkg or key at judge time) is the expected
    # non-blocking case; the broad Exception clause catches every other judge fault
    # with the same allow-by-default policy.
    try:
        result = run_lazarus(
            work_unit,
            capped,
            config=config,
            judge_fn=judge_fn,
            kind="diff",
            record=False,
        )
    except JudgeUnavailable as exc:
        _swallow_judge_error(
            "the judge is not available (e.g. missing ANTHROPIC_API_KEY or the "
            "[judge] extra is not installed). Install `pip install "
            "lazarus-sonar[judge]` and set ANTHROPIC_API_KEY to enable the "
            "pre-gate's precision filter, or set [async].stub_judge = true for "
            "the offline stub. The planned action is allowed.",
            exc,
        )
    except Exception as exc:  # noqa: BLE001 -- deliberate: never block the action on a judge fault
        _swallow_judge_error(
            "the LAZARUS judge pass raised. The pre-gate is advisory, so the "
            "planned action is allowed.",
            exc,
        )

    # ---- Narrowness knob #2: high confidence floor AFTER the judge -------- #
    # run_lazarus already applied the judge's would_change gate and the config
    # min_confidence (0.6 default). The pre-gate applies a SECOND, stricter floor
    # (pregate_min_confidence, default 0.85) so only near-certain verdicts surface
    # here. Merely-plausible matches are dropped -- that is precisely the class
    # that makes shift-left gates noisy (decision D-7, knob #2).
    floor = config.pregate_min_confidence
    high = [f for f in result.fixes if _attr(f, "confidence") is not None
            and float(_attr(f, "confidence")) >= floor]

    if not high:
        # Nothing cleared the high-confidence floor. Quiet allow: the async retro-
        # audit remains the source of truth for the merely-plausible findings.
        _allow()

    # ---- Surface as PreToolUse context, ALLOW the action ------------------ #
    context = _render_high_confidence(high, result.work_unit_sig)
    # Findings also go to stderr so they are visible in the hook log even if the
    # harness version does not surface additionalContext on PreToolUse.
    sys.stderr.write(context)
    _allow(context)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        _err("interrupted")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 -- last-resort guard
        # An unexpected crash in the hook itself is fail-loud: it means the hook
        # is broken, which the operator needs to see and fix. Even so, the exit
        # code (2) does not block the action under PreToolUse.
        _err("unexpected error in lazarus_pregate hook")
        _err("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())
        sys.exit(2)
