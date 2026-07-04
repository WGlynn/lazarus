#!/usr/bin/env python3
"""Stop / PostToolUse hook: run the LAZARUS retro-audit on the just-finished work-unit.

This hook reads a Claude Code hook event from stdin, extracts the work-unit that
just completed (a diff for PostToolUse:Edit|Write, or the last assistant response
for Stop), runs SONAR -> LAZARUS against your rules corpus, and prints the
surviving retroactive-fixes -- buried rules that would actually have changed the
finished work. It PROPOSES; it never edits your files or the finished work.

Placement (see hooks/settings.snippet.json and INSTALL_HOOKS.md):
  - Stop                       -> audit the last assistant turn
  - PostToolUse:Edit|Write     -> audit the diff of the write

Fail-loud contract:
  - Missing config, missing corpus, or an unparseable event is a visible stderr
    error and a non-zero exit. There is no silent no-op.
  - The one deliberate carve-out: a judge / model / network error must NOT wedge
    the session. It is printed loudly and the hook exits 0 without blocking the
    turn. On the Stop event we emit `{}` (never `hookSpecificOutput` /
    `additionalContext`, which the Stop schema rejects); on PostToolUse we exit 0.

Run standalone for debugging:
    echo '{"hook_event_name":"Stop", ...}' | python hooks/retro_audit.py
    python hooks/retro_audit.py --file examples/demo/work_unit.diff --kind diff

Exit codes:
    0  audit ran (fixes may or may not have surfaced), or judge error was
       swallowed to avoid wedging the turn.
    2  fail-loud: bad input, missing/unresolvable config, or missing corpus.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Import bootstrap
#
# Hooks are invoked as `python <abs path>/hooks/retro_audit.py` from an arbitrary
# working directory, so the package may not be importable yet. Add the repo's
# `src/` to sys.path before importing lazarus_sonar. Do this without swallowing a
# genuine "package is broken" ImportError -- only the "package not yet on the
# path" case triggers the fallback.
#
# We import the CONFIG-AWARE SONAR entry point `run_sonar_for_config` (not the
# pure `run_sonar`, whose signature takes explicit corpus_path/globs/scoring and
# does NOT accept a Config or a `kind`). `run_sonar_for_config` unpacks a Config
# and hands the pure sweeper what it needs, so this hook has exactly one obvious
# call site (see the canonical interface contract, section 2b / 7).
# --------------------------------------------------------------------------- #

_HOOK_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HOOK_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"

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

# JudgeUnavailable is the canonical "judge setup problem" error (missing the
# optional `anthropic` package or the API key). It lives in judge.py. We import
# it lazily and fall back to a never-matching sentinel so the `except` clause is
# always well-formed even when judge.py cannot be imported at all (e.g. a partial
# install). This mirrors cli.py's proven shim and section 3d of the contract.
# Importing judge.py is cheap: the Anthropic SDK is loaded lazily inside the
# module, not at import time, so this does not pull in the optional dependency.
try:  # pragma: no cover - trivial import shim
    from lazarus_sonar.judge import JudgeUnavailable
except Exception:  # noqa: BLE001 - judge module may be unimportable without extras

    class JudgeUnavailable(Exception):  # type: ignore[no-redef]
        """Fallback so the swallow-judge-error except clause is always valid.

        The real JudgeUnavailable lives in judge.py and is raised by the judge
        when the `anthropic` package or the API key is missing. If judge.py
        itself cannot be imported, this stand-in keeps the hook importable; the
        broad judge-fault swallow below still catches any judge exception and
        exits loud-but-non-blocking.
        """


HOOK_NAME = "lazarus.retro_audit"


# --------------------------------------------------------------------------- #
# Fail-loud plumbing
# --------------------------------------------------------------------------- #


def _err(msg: str) -> None:
    """Write a visible, attributable error to stderr."""
    sys.stderr.write(f"[{HOOK_NAME}] {msg}\n")


def _fail_loud(msg: str, *, exc: Optional[BaseException] = None) -> "NoReturn":  # type: ignore[name-defined]
    """Print a loud error and exit non-zero.

    Used for the input/config/corpus failures that must never pass silently.
    Not used for judge/model/network errors -- those go through
    `_swallow_judge_error` so the turn is never blocked.
    """
    _err("FAIL-LOUD: " + msg)
    if exc is not None:
        _err("cause: " + "".join(traceback.format_exception_only(type(exc), exc)).strip())
    sys.exit(2)


def _emit_nonblocking(event_name: str) -> None:
    """Emit the correct non-blocking payload for the event and exit 0.

    The Stop event schema rejects `hookSpecificOutput.additionalContext`, so for
    Stop we emit a bare `{}` (explicitly non-blocking). For every other event a
    clean exit 0 with no JSON body is the non-blocking signal.
    """
    if event_name == "Stop":
        sys.stdout.write(json.dumps({}))
        sys.stdout.flush()
    sys.exit(0)


def _swallow_judge_error(event_name: str, msg: str, exc: BaseException) -> "NoReturn":  # type: ignore[name-defined]
    """Loud-but-non-wedging: report a judge/model/network failure, then let the turn proceed.

    This is the single carve-out from fail-closed behavior. A transient API error
    or a missing API key at judge time must not block the user's session, so we
    print the error and exit 0 with a non-blocking payload.
    """
    _err("judge error (turn NOT blocked): " + msg)
    _err("cause: " + "".join(traceback.format_exception_only(type(exc), exc)).strip())
    _emit_nonblocking(event_name)


# --------------------------------------------------------------------------- #
# Input handling
# --------------------------------------------------------------------------- #


def _read_stdin() -> str:
    """Read the hook event JSON from stdin.

    Claude Code delivers hook input on stdin as a single JSON object. Empty stdin
    is a fail-loud condition -- a hook that silently no-ops on missing input hides
    real wiring bugs.
    """
    data = sys.stdin.read()
    if not data or not data.strip():
        _fail_loud(
            "no hook input on stdin. This hook expects a Claude Code hook event "
            "JSON object on stdin, or a --stdin/--file/--kind override for manual runs."
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
# Work-unit extraction
#
# A "work-unit" is the text LAZARUS audits. Its shape depends on the event:
#   - PostToolUse:Edit  -> a synthesized unified-style diff from old/new strings
#   - PostToolUse:Write -> the written file contents (an add)
#   - Stop              -> the last assistant response text
#
# The exact field names in Claude Code hook payloads have shifted across
# versions, so we probe a small set of likely locations rather than hard-coding
# one path. If none match, that is a fail-loud condition (an unparseable
# work-unit), NOT a silent skip.
# --------------------------------------------------------------------------- #


def _event_name(event: dict[str, Any]) -> str:
    """Best-effort event name, defaulting to Stop-style non-blocking behavior.

    We use this only to choose the non-blocking emit shape, so an unknown value
    is safe -- we just won't emit the Stop-specific `{}`.
    """
    for key in ("hook_event_name", "hookEventName", "event_name", "eventName"):
        val = event.get(key)
        if isinstance(val, str) and val:
            return val
    return "Stop"


def _tool_name(event: dict[str, Any]) -> str:
    for key in ("tool_name", "toolName"):
        val = event.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _tool_input(event: dict[str, Any]) -> dict[str, Any]:
    for key in ("tool_input", "toolInput"):
        val = event.get(key)
        if isinstance(val, dict):
            return val
    return {}


def _first_str(d: dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        val = d.get(key)
        if isinstance(val, str):
            return val
    return None


def _diff_from_edit(tool_input: dict[str, Any], path: str) -> Optional[str]:
    """Synthesize a minimal unified-style diff from an Edit tool's old/new strings.

    This is a readable approximation, not a byte-exact patch -- LAZARUS reads it
    as context for the judge, so line-level fidelity is what matters, not
    apply-ability.
    """
    old = _first_str(tool_input, "old_string", "oldString")
    new = _first_str(tool_input, "new_string", "newString")
    if old is None and new is None:
        return None
    old = old or ""
    new = new or ""

    header = f"--- a/{path}\n+++ b/{path}\n" if path else ""
    body_lines = []
    for line in old.splitlines():
        body_lines.append(f"-{line}")
    for line in new.splitlines():
        body_lines.append(f"+{line}")
    return header + "\n".join(body_lines)


def _content_from_write(tool_input: dict[str, Any], path: str) -> Optional[str]:
    content = _first_str(tool_input, "content", "file_text", "fileText", "text")
    if content is None:
        return None
    header = f"+++ b/{path} (new file)\n" if path else ""
    body = "\n".join(f"+{line}" for line in content.splitlines())
    return header + body


def _last_response_text(event: dict[str, Any]) -> Optional[str]:
    """Extract the last assistant response text from a Stop-style event.

    Different Claude Code versions surface this differently: a plain string, a
    list of content blocks, or a transcript path we can tail. Probe in order.
    """
    # 1. Direct string fields.
    direct = _first_str(
        event,
        "last_assistant_message",
        "lastAssistantMessage",
        "response",
        "last_response",
        "lastResponse",
        "message",
    )
    if direct:
        return direct

    # 2. A structured message object with content blocks.
    msg = event.get("last_message") or event.get("lastMessage")
    if isinstance(msg, dict):
        text = _text_from_content(msg.get("content"))
        if text:
            return text

    # 3. A transcript file we can read and pull the final assistant turn from.
    transcript = _first_str(event, "transcript_path", "transcriptPath")
    if transcript:
        text = _last_assistant_from_transcript(transcript)
        if text:
            return text

    return None


def _text_from_content(content: Any) -> Optional[str]:
    """Flatten a content value (string or list of blocks) into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") in (None, "text") and isinstance(block.get("text"), str):
                    parts.append(block["text"])
        if parts:
            return "\n".join(parts)
    return None


def _last_assistant_from_transcript(path_str: str) -> Optional[str]:
    """Read a JSONL transcript and return the text of the final assistant message.

    Best-effort: a missing or malformed transcript returns None (the caller then
    fails loud on an empty work-unit, which is the right signal).
    """
    path = Path(path_str).expanduser()
    if not path.is_file():
        return None
    last_text: Optional[str] = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                msg = rec.get("message") if isinstance(rec.get("message"), dict) else rec
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") != "assistant":
                    continue
                text = _text_from_content(msg.get("content"))
                if text:
                    last_text = text
    except OSError:
        return None
    return last_text


def extract_work_unit(event: dict[str, Any]) -> tuple[str, str]:
    """Return (kind, text) for the work-unit to audit.

    `kind` is one of "diff" or "response" and is passed through to the judge so
    it can frame the question appropriately. Raises nothing -- on a genuinely
    empty/unparseable work-unit the caller fails loud.
    """
    tool = _tool_name(event)

    if tool in ("Edit", "MultiEdit"):
        ti = _tool_input(event)
        path = _first_str(ti, "file_path", "filePath", "path") or ""
        diff = _diff_from_edit(ti, path)
        if diff:
            return "diff", diff

    if tool == "Write":
        ti = _tool_input(event)
        path = _first_str(ti, "file_path", "filePath", "path") or ""
        content = _content_from_write(ti, path)
        if content:
            return "diff", content

    # Stop (or any non-write event): audit the last response.
    response = _last_response_text(event)
    if response:
        return "response", response

    # Some Edit/Write payloads carry no usable diff (e.g. old==new, or a shape we
    # don't recognize). Fall through to fail-loud in the caller.
    return "response", ""


# --------------------------------------------------------------------------- #
# CLI overrides (for manual debugging outside a live hook)
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog=HOOK_NAME,
        description="LAZARUS retro-audit hook (Stop / PostToolUse).",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to lazarus.config.toml. Overrides discovery / LAZARUS_CONFIG.",
    )
    p.add_argument(
        "--file",
        default=None,
        help="Read the work-unit from this file instead of hook stdin (debugging).",
    )
    p.add_argument(
        "--kind",
        choices=("diff", "response"),
        default=None,
        help="Work-unit kind when using --file. Defaults to 'diff'.",
    )
    p.add_argument(
        "--stdin",
        action="store_true",
        help="Force reading the hook event from stdin even if --file is given.",
    )
    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _render_fixes(fixes: list[Any]) -> str:
    """Render surviving retroactive-fixes as human-readable stderr text.

    Each fix is a `lazarus.RetroFix` exposing: rule_id, where, patch, confidence,
    reason. We access these defensively via `_attr` so a schema tweak in
    run_lazarus does not crash the hook.
    """
    out = io.StringIO()
    out.write(f"[{HOOK_NAME}] LAZARUS surfaced {len(fixes)} retroactive-fix(es):\n")
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
        f"\n[{HOOK_NAME}] These are PROPOSALS, not applied changes. "
        "Review, then `lazarus ledger action <sig> <rule_id>` if you apply one, "
        "or `lazarus ledger decline <sig> <rule_id>` to suppress it for this work.\n"
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

    # ---- Determine the event + work-unit ---------------------------------- #
    if args.file and not args.stdin:
        # Manual/debug path: no hook event, just a file.
        event_name = "Stop"
        kind = args.kind or "diff"
        work_unit = _read_text_file(args.file)
    else:
        raw = _read_stdin()
        event = _parse_event(raw)
        event_name = _event_name(event)
        kind, work_unit = extract_work_unit(event)
        if args.kind:
            kind = args.kind

    if not work_unit.strip():
        _fail_loud(
            "extracted an empty work-unit. For an Edit/Write event this usually "
            "means the tool payload had no old_string/new_string/content; for a "
            "Stop event it means no last-response text or transcript was found. "
            "This is a wiring problem, not a no-op."
        )

    # ---- Load + validate config (fail-loud) ------------------------------- #
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

    # A resolved-but-missing corpus directory is a fail-loud condition: we never
    # silently fall back to scanning $HOME or the cwd. `config.corpus_path` is
    # validated at load time, but we re-check here so a corpus deleted after load
    # still fails loud with a hook-attributable message rather than deep inside
    # the SONAR sweep.
    corpus_path = Path(config.corpus_path)
    if not corpus_path.exists():
        _fail_loud(
            f"corpus.path does not exist: {corpus_path}. Set it to your rules / "
            "memory directory in lazarus.config.toml. There is no home/cwd fallback."
        )
    if not corpus_path.is_dir():
        _fail_loud(f"corpus.path is not a directory: {corpus_path}.")

    # ---- SONAR (perception) ---------------------------------------------- #
    # A SONAR failure is a real bug (bad glob, unreadable corpus) -> fail loud.
    # `run_sonar_for_config` unpacks corpus_path / globs / exclude / scoring off
    # the Config and calls the pure `run_sonar`; the hook never touches the pure
    # signature directly. `kind` is advisory (forward-compat), not a scoring input.
    try:
        candidates = run_sonar_for_config(work_unit, config, kind=kind)
    except Exception as exc:  # noqa: BLE001 -- surface any SONAR fault loudly
        _fail_loud("SONAR sweep failed while ranking corpus candidates", exc=exc)

    if not candidates:
        # No candidates cleared SONAR's threshold. That's a normal, quiet outcome
        # (nothing buried is relevant to this work). Non-blocking, exit clean.
        _emit_nonblocking(event_name)

    # ---- LAZARUS (cognition / precision) ---------------------------------- #
    # run_lazarus returns an AuditResult (not a list). Any judge/model/network
    # failure must NOT wedge the turn -- swallow it loudly. A JudgeUnavailable
    # (missing anthropic package or API key discovered at judge time) is the
    # expected non-wedging case: a retro-audit is advisory and should never block
    # a Stop/PostToolUse turn on a judge-setup problem. The broad Exception clause
    # below catches every other judge fault (refusal, network, unparseable
    # response) with the same non-wedging policy.
    try:
        result = run_lazarus(work_unit, candidates, config=config, kind=kind)
    except JudgeUnavailable as exc:
        _swallow_judge_error(
            event_name,
            "the judge is not available (e.g. missing ANTHROPIC_API_KEY or the "
            "[judge] extra is not installed). Install `pip install "
            "lazarus-sonar[judge]` and set ANTHROPIC_API_KEY to enable the "
            "precision filter. Perception (SONAR) still ran.",
            exc,
        )
    except Exception as exc:  # noqa: BLE001 -- deliberate: never block the turn on a judge fault
        _swallow_judge_error(
            event_name,
            "the LAZARUS judge pass raised. The retro-audit is advisory, so the "
            "turn is not blocked.",
            exc,
        )

    # ---- Report ----------------------------------------------------------- #
    fixes = result.fixes
    if fixes:
        sys.stderr.write(_render_fixes(fixes))
    # Whether or not anything surfaced, the audit ran cleanly. Emit the correct
    # non-blocking payload and exit 0 -- retro-audit never blocks the turn.
    _emit_nonblocking(event_name)


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
        # is broken, which the user needs to see and fix.
        _err("unexpected error in retro_audit hook")
        _err("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())
        sys.exit(2)
