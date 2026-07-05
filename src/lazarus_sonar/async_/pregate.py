#!/usr/bin/env python3
"""PreToolUse pre-gate (opt-in, SYNCHRONOUS): a tightly-bounded Sonar+Lazarus on
the PLANNED action, surfacing ONLY the highest-confidence rules to PREVENT rather
than patch. Deliberately narrow. Default OFF.

This is the one place LAZARUS v2 puts a judge call back ON the critical path, so
it is triple-constrained and gated behind a config opt-in (see canonical contract
section 6 and decision D-7):

  1. Default OFF          -- config.pregate_enabled. If false, this is an
                             immediate non-blocking no-op.
  2. Candidate cap        -- config.pregate_max_candidates (default 3). SONAR's
                             ranked shortlist is truncated to the top N BEFORE the
                             judge, so the synchronous judge batch is tiny.
  3. High confidence floor -- config.pregate_min_confidence (default 0.85, well
                             above the judge's normal min_confidence of 0.6).
  4. record=False         -- the pre-gate never writes the ledger, so it cannot
                             suppress or pre-empt the authoritative async retro-
                             audit that legitimately sees the same work moments
                             later on Stop/PostToolUse.

Where this lives, and how it is invoked
---------------------------------------
The logic lives here, in the package (``lazarus_sonar.async_.pregate``). The thin
``hooks/async_pregate.py`` entrypoint is what Claude Code wires (opt-in) on
``PreToolUse``; it puts ``src/`` and ``hooks/`` on ``sys.path`` and calls
``main()`` here.

Fail-safe boundary (decision D-9): the pre-gate is on the user's action path, so
its failure posture leans toward ALLOW. A judge/model/network error, an empty
extraction, or a disabled gate all resolve to allowing the action, never blocking
it. Genuine misconfiguration (bad/missing config, missing corpus) is still
surfaced loudly to stderr via the v1 ``_fail_loud`` plumbing, but even that path
defaults to allowing the action.

The gate PROPOSES; it never applies, never edits files, never hard-blocks by
default. It emits a PreToolUse decision of "allow" carrying the high-confidence
findings as additionalContext. A strict-block mode is explicitly deferred.

Exit codes:
    0  gate ran (findings may or may not have surfaced), gate disabled, empty
       extraction, or a judge error was swallowed to allow the action. In every
       case the action is ALLOWED (no deny decision is ever emitted).
    2  fail-loud: bad input, missing/unresolvable config, or missing corpus. The
       action is still allowed (PreToolUse does not treat this as a block).
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
# Repo layout anchors + import bootstrap.
#
# This module lives at src/lazarus_sonar/async_/pregate.py:
#   parents[0] = async_/  parents[1] = lazarus_sonar/  parents[2] = src/
#   parents[3] = repo root
# The v1 engine is imported package-relative (two-dot parent). The v1 retro_audit
# hook (reused for work-unit extraction + fail-loud plumbing) lives in hooks/, a
# scripts dir, so we add it to sys.path before importing it.
# --------------------------------------------------------------------------- #

_PKG_FILE = Path(__file__).resolve()
_REPO_ROOT = _PKG_FILE.parents[3]
_HOOK_DIR = _REPO_ROOT / "hooks"

if _HOOK_DIR.is_dir() and str(_HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOK_DIR))

from ..config import ConfigError, load_config  # noqa: E402
from ..lazarus import run_lazarus  # noqa: E402
from ..sonar import run_sonar_for_config  # noqa: E402

# Reuse the v1 retro_audit hook's work-unit extraction and fail-loud plumbing so
# the pre-gate and the sync/async paths parse hook payloads identically.
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
# optional `anthropic` package or the API key). Import it lazily and fall back to
# a never-matching sentinel so the `except` clause is always well-formed even
# when judge.py cannot be imported at all (partial install).
try:  # pragma: no cover - trivial import shim
    from ..judge import JudgeUnavailable
except Exception:  # noqa: BLE001 - judge module may be unimportable without extras

    class JudgeUnavailable(Exception):  # type: ignore[no-redef]
        """Fallback so the swallow-judge-error except clause is always valid."""


HOOK_NAME = "lazarus.pregate"


# --------------------------------------------------------------------------- #
# Fail-safe / decision plumbing
# --------------------------------------------------------------------------- #


def _allow(additional_context: Optional[str] = None) -> "NoReturn":  # type: ignore[name-defined]
    """Emit a non-blocking PreToolUse decision that ALLOWS the action, and exit 0.

    When `additional_context` is provided, it is attached on the PreToolUse
    additionalContext channel so the surfaced high-confidence findings reach the
    agent BEFORE it runs the planned tool. The decision is always "allow": the
    pre-gate surfaces context but, by default, does not hard-block.
    """
    hook_output: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
    }
    if additional_context:
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
    """Loud-but-allowing: report a judge/model/network failure, then ALLOW the action."""
    _err("judge error (action ALLOWED): " + msg)
    _err("cause: " + "".join(traceback.format_exception_only(type(exc), exc)).strip())
    _allow()


# --------------------------------------------------------------------------- #
# Input handling
# --------------------------------------------------------------------------- #


def _read_stdin() -> str:
    """Read the hook event JSON from stdin. Empty stdin is a fail-loud condition."""
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
# tool_input (the pending Write/Edit args). We reuse the v1 retro_audit
# synthesizers so the planned action renders into the same diff shape the
# sync/async retro-audit will later see for the finished action (decision D-3).
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

    return "diff", ""


# --------------------------------------------------------------------------- #
# Stub-judge selection (offline / no-key mode)
# --------------------------------------------------------------------------- #


def _load_stub_judge_fn() -> Callable[[str, str, Sequence[Any]], list[dict[str, Any]]]:
    """Return the deterministic stub JudgeFn, preferring the checkout demo copy.

    Resolution order:
      1. examples/demo/stub_judge.py in this repo checkout (the source of truth).
      2. the vendored re-export lazarus_sonar.async_.stub_judge (installed wheel).
    Fails loud if neither is available.
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
        from .stub_judge import stub_judge_fn  # type: ignore[import-not-found]

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
    # non-blocking allow -- no SONAR, no judge, no latency.
    if not config.pregate_enabled:
        _allow()

    # An empty planned work-unit is a quiet allow, NOT a fail-loud condition.
    if not work_unit.strip():
        _allow()

    # A resolved-but-missing corpus is a fail-loud misconfig (visible to the
    # operator), but even that does not block the action.
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
    use_stub = bool(args.stub) or bool(config.async_stub_judge)
    judge_fn = _load_stub_judge_fn() if use_stub else None

    # ---- SONAR (perception) ----------------------------------------------- #
    try:
        candidates = run_sonar_for_config(work_unit, config, kind="diff")
    except Exception as exc:  # noqa: BLE001 -- surface loudly, but do not block the action
        _err("SONAR sweep failed while ranking corpus candidates (action ALLOWED)")
        _err("cause: " + "".join(traceback.format_exception_only(type(exc), exc)).strip())
        _allow()

    if not candidates:
        _allow()

    # ---- Narrowness knob #1: cap candidates BEFORE the judge -------------- #
    cap = max(1, config.pregate_max_candidates)
    capped = candidates[:cap]

    # ---- LAZARUS (cognition / precision), record=False -------------------- #
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
    floor = config.pregate_min_confidence
    high = [f for f in result.fixes if _attr(f, "confidence") is not None
            and float(_attr(f, "confidence")) >= floor]

    if not high:
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
        _err("unexpected error in the pre-gate hook")
        _err("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())
        sys.exit(2)
