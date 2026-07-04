#!/usr/bin/env python3
# hooks/lazarus_async_launch.py
"""LAZARUS v2 launcher hook (Stop / PostToolUse) -- NON-BLOCKING.

This is the async transport's front door. It captures the just-finished work-unit
from the hook's stdin payload, writes it to a spool FILE, spawns the detached
background runner (`lazarus-audit-bg` / async_runner_entry.py), and RETURNS in
single-digit milliseconds. It runs no SONAR and no judge -- all of that latency
now lives inside the detached child, off the critical path (see the v2 canonical
concurrency contract, sections 3 and D-1..D-9).

Placement (see hooks/settings.snippet.v2.json):
  - Stop                       -> dispatch a "response" retro-audit
  - PostToolUse:Edit|Write     -> dispatch a "diff" retro-audit

Relationship to the v1 sync path (D-1, additive strategy):
  - The v1 engine is REUSED, never rewritten. This launcher imports v1's own
    work-unit extractor and fail/emit helpers from `retro_audit.py`, so the async
    path parses hook payloads identically to the sync path -- the same event
    yields the same work-unit signature on both paths, and the ledger / pending
    keys line up.
  - It is a runtime MODE, not a fork. When `config.async_enabled` is false
    (mode == "sync", or the v2 hooks are not wired) this launcher is a no-op that
    emits the correct non-blocking payload and returns, leaving v1's blocking
    `retro_audit.py` authoritative. You wire ONE of the two on Stop/PostToolUse;
    running both would double-audit.

Fail-loud vs fail-safe (D-9), re-applied for this hook:
  - Misconfig (bad/missing config) is fail-LOUD via v1's `_fail_loud` (stderr +
    exit 2). That is still non-blocking for the turn: PostToolUse exit 2 does not
    block, and Stop has already emitted no additionalContext.
  - An empty extraction, or a disabled async mode, is a QUIET non-blocking no-op
    -- unlike the sync hook which fails loud on an empty work-unit. The async
    path's prime directive is "never wedge a turn"; a loud error on every
    keystroke-less Stop would be pure noise, and an async miss is invisible.
  - This hook never runs SONAR or the judge, so its failure surface is almost
    entirely "could not spawn / could not spool", which it surfaces loudly but
    still without blocking.

Latency contract:
  - File I/O + one `subprocess.Popen` + return. NO `.wait()`, NO `.communicate()`,
    NO PIPE this process reads. The child re-parents and outlives the hook.

Run standalone for debugging:
    echo '{"hook_event_name":"Stop","last_assistant_message":"..."}' \
        | python hooks/lazarus_async_launch.py --kind response
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Import bootstrap
#
# Hooks are invoked as `python <abs path>/hooks/lazarus_async_launch.py` from an
# arbitrary working directory, so neither the `lazarus_sonar` package nor the
# sibling `retro_audit` module is guaranteed importable yet. We must be able to
# import BOTH:
#   * `retro_audit`               -- a sibling module in this same hooks/ dir; we
#                                     reuse its work-unit extractor + fail/emit
#                                     helpers verbatim (D-3), so extraction never
#                                     forks between the sync and async paths.
#   * `lazarus_sonar.config`      -- the v1 package under ../src.
# Add the hooks dir and ../src to sys.path (idempotently) before importing. This
# mirrors retro_audit.py's own bootstrap; we do not swallow a genuine "package is
# broken" ImportError, only the "not yet on the path" case.
# --------------------------------------------------------------------------- #

_HOOK_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HOOK_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"

for _p in (_HOOK_DIR, _SRC_DIR):
    _ps = str(_p)
    if _p.is_dir() and _ps not in sys.path:
        sys.path.insert(0, _ps)

# Reuse v1's extractor and fail/emit plumbing verbatim (D-3). `extract_work_unit`
# returns (kind, text); `_event_name` picks the non-blocking emit shape;
# `_emit_nonblocking` emits `{}` for Stop / clean exit 0 otherwise; `_fail_loud`
# writes a loud stderr line and exits 2; `_err` is the attributable stderr writer.
from retro_audit import (  # noqa: E402  (import after sys.path bootstrap)
    _emit_nonblocking,
    _err,
    _event_name,
    _fail_loud,
    extract_work_unit,
)

# Config is loaded only to (a) fail-loud early on misconfig and (b) locate the
# work-unit spool dir + the runner's config path + the async mode switch. No
# SONAR / judge is imported here -- keeping this hook's import cost and failure
# surface minimal is the whole point.
from lazarus_sonar.config import ConfigError, load_config  # noqa: E402


HOOK_NAME = "lazarus.async_launcher"


# --------------------------------------------------------------------------- #
# CLI (for manual debugging outside a live hook)
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog=HOOK_NAME,
        description=(
            "LAZARUS v2 non-blocking launcher (Stop / PostToolUse). Extracts the "
            "work-unit, spawns the detached background runner, and returns."
        ),
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to lazarus.config.toml. Overrides discovery / $LAZARUS_CONFIG.",
    )
    p.add_argument(
        "--kind",
        choices=("diff", "response"),
        default=None,
        help=(
            "Work-unit kind hint. Normally inferred from the event "
            "(Edit/Write -> diff, Stop -> response); this overrides it."
        ),
    )
    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# Detached spawn (D-2): cross-platform spawn-and-forget, verified no-wait
# --------------------------------------------------------------------------- #


def _spawn_detached(argv: list[str], *, log_path: Path) -> None:
    """OS-level detach so the child outlives this hook and runs concurrently with
    the main agent's next turn.

    The Claude Code loop is sequential, so concurrency here is purely OS-level:
    the child re-parents to init/system and keeps running after this hook returns.

    Discipline (D-2):
      * NO `.wait()`, NO `.communicate()`, NO `.poll()` loop, NO PIPE this
        process reads. We construct the `Popen` and return. An unread PIPE would
        fill its OS buffer and could block the child; reading it would block us.
      * `stdin = DEVNULL`. `stdout`/`stderr` are redirected to a per-run log file
        under the spool dir, so a background crash is inspectable
        (`spool/log-<run_id>.txt`) but never lands on the parent's console.
      * `close_fds = True`.
      * Windows: `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`
        (0x00000200 | 0x00000008). DETACHED_PROCESS gives the child no console;
        the new process group stops a Ctrl-C in the parent's console from
        reaching it.
      * POSIX: `start_new_session=True` (setsid), so the child is not in the
        parent's process group / session and is not killed when this hook exits.

    Failure to spawn is loud (it means the async path is misconfigured or the
    interpreter path is wrong) but still non-blocking -- the caller decides the
    emit shape.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)

    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        # Run the child from the repo root so its own src/ bootstrap and any
        # relative config resolution behave predictably regardless of the cwd the
        # hook was invoked from.
        "cwd": str(_REPO_ROOT),
    }

    if os.name == "nt":
        # DETACHED_PROCESS (0x00000008) may not be exposed as a named constant on
        # older Pythons; CREATE_NEW_PROCESS_GROUP is. Reference both defensively.
        detached_process = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        popen_kwargs["creationflags"] = detached_process | new_group
    else:
        popen_kwargs["start_new_session"] = True

    # Open the log handle and hand it to the child. We deliberately do NOT keep a
    # reference to it beyond the Popen call: the child inherits the fd; this
    # process is about to exit, so leaving it to GC is fine and we never read it.
    log_handle = open(log_path, "ab", buffering=0)
    try:
        subprocess.Popen(  # noqa: S603  -- argv is fully constructed by us, no shell
            argv,
            stdout=log_handle,
            stderr=log_handle,
            **popen_kwargs,
        )
    finally:
        # The child has inherited (dup'd) the fd; closing our copy here is correct
        # and keeps this process from holding the handle open past return.
        try:
            log_handle.close()
        except OSError:
            pass


def _child_argv(
    *,
    config: Any,
    wu_file: Path,
    kind: str,
    run_id: str,
    config_path: Optional[str],
) -> list[str]:
    """Build the detached runner's argv.

    The child reads the work-unit from a FILE (`--work-unit-file`), not from a
    pipe: by the time it runs, this hook has already exited and any stdin pipe
    would be closed. File IPC is the correct channel here (D-2).

    We prefer the checkout shim `async_runner_entry.py` (a 3-line bootstrap that
    puts src/ on the path and calls `lazarus_sonar.async_.runner.main()`), so the
    detached child works from a plain checkout with no install, mirroring the v1
    hooks' import bootstrap. If that shim is absent (an installed layout without
    the hooks dir shipped), fall back to the `lazarus-audit-bg` console script,
    which the runner registers in pyproject `[project.scripts]`.
    """
    entry_shim = _HOOK_DIR / "async_runner_entry.py"
    if entry_shim.is_file():
        argv = [
            sys.executable,
            str(entry_shim),
            "--work-unit-file",
            str(wu_file),
            "--kind",
            kind,
            "--run-id",
            run_id,
        ]
    else:
        # Installed fallback: the console entrypoint. It shares the same flags.
        argv = [
            "lazarus-audit-bg",
            "--work-unit-file",
            str(wu_file),
            "--kind",
            kind,
            "--run-id",
            run_id,
        ]

    # Pass the resolved config path through so the child does not re-run discovery
    # from a different cwd and pick a different config. `config.source_path` can be
    # None (programmatic Config); fall back to the explicit override we were given.
    if config_path:
        argv += ["--config", config_path]

    # Offline / no-key mode (D-6): when the config asks for the deterministic stub
    # judge, tell the child to use it. The launcher itself never touches the judge.
    if getattr(config, "async_stub_judge", False):
        argv.append("--stub")

    return argv


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # ---- Read + parse the hook event (fail-loud on genuinely absent input) --- #
    raw = sys.stdin.read()
    if not raw or not raw.strip():
        # A launcher with no stdin is a wiring bug the operator needs to see. This
        # is the one loud-on-empty case: it is not a normal turn outcome, it means
        # the hook was invoked wrong. Still exit non-blocking-safe via _fail_loud
        # (exit 2 does not block PostToolUse; Stop got no additionalContext).
        _fail_loud("no hook input on stdin")

    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail_loud("hook input on stdin was not valid JSON", exc=exc)
    if not isinstance(event, dict):
        _fail_loud(f"hook input must be a JSON object, got {type(event).__name__}")

    event_name = _event_name(event)  # "Stop" / "PostToolUse" / ...

    # ---- Extract the finished work-unit (REUSED v1 extractor, D-3) ---------- #
    kind, work_unit = extract_work_unit(event)
    if args.kind:
        kind = args.kind

    if not work_unit.strip():
        # Nothing extracted is a NORMAL quiet outcome for the launcher (unlike the
        # sync hook, which fails loud here). The async path must never wedge a
        # turn: e.g. a Stop with no last-response text, or an Edit whose old==new.
        _emit_nonblocking(event_name)
        return

    # ---- Resolve config: fail-loud on misconfig, else locate spool + mode --- #
    # No SONAR, no judge. We load the config only to (a) surface a real misconfig
    # early and loudly, and (b) find the spool dir, the async mode switch, and the
    # config path to hand the child.
    config_override = args.config or os.environ.get("LAZARUS_CONFIG")
    try:
        config = load_config(config_override)
    except ConfigError as exc:
        # Misconfig IS loud (D-9), but still non-blocking: _fail_loud writes stderr
        # and exits 2, which does not block PostToolUse and is inert for Stop.
        _fail_loud(f"invalid config: {exc}", exc=exc)
    except FileNotFoundError as exc:
        _fail_loud("config file not found", exc=exc)

    # ---- Mode gate (D-1): sync mode -> launcher no-ops -------------------- #
    # When async is disabled (mode == "sync", or the v2 hooks are not wired via
    # LAZARUS_ASYNC=1) the operator is running v1's blocking retro_audit.py on
    # these events. This launcher must do nothing but emit the non-blocking
    # payload, so the two paths never double-audit.
    if not getattr(config, "async_enabled", False):
        _emit_nonblocking(event_name)
        return

    run_id = uuid.uuid4().hex[:8]

    # ---- Spool the work-unit to a FILE (D-2 file IPC) ---------------------- #
    # The child reads this file, not our stdin -- we are about to exit, so a pipe
    # would be closed. Any I/O failure here is loud (the async path cannot run
    # without a readable spool) but still non-blocking.
    try:
        spool = Path(config.async_spool_dir)
        spool.mkdir(parents=True, exist_ok=True)
        wu_file = spool / f"wu-{run_id}.txt"
        wu_file.write_text(work_unit, encoding="utf-8")
    except OSError as exc:
        _fail_loud(
            "could not write the work-unit spool file. Check that "
            "[async].spool_dir is writable.",
            exc=exc,
        )

    # ---- Build argv + spawn detached, then return immediately ------------- #
    # Prefer the resolved config source_path (a stable absolute path) so the child
    # resolves the identical config regardless of its cwd; fall back to whatever
    # override string we were given. source_path may be None for a programmatic
    # Config, hence the guard.
    resolved_source = getattr(config, "source_path", None)
    config_path = str(resolved_source) if resolved_source else (config_override or None)

    argv_child = _child_argv(
        config=config,
        wu_file=wu_file,
        kind=kind,
        run_id=run_id,
        config_path=config_path,
    )
    log_path = spool / f"log-{run_id}.txt"

    try:
        _spawn_detached(argv_child, log_path=log_path)
    except OSError as exc:
        # Spawn failure is loud but non-blocking: the async audit simply does not
        # run this turn, and the operator sees why. We do NOT retry or wait.
        _fail_loud(
            "could not spawn the detached background runner. Check that the "
            "runner entrypoint (async_runner_entry.py or the lazarus-audit-bg "
            "console script) is present and the interpreter path is valid.",
            exc=exc,
        )

    # RETURN IMMEDIATELY. For Stop this emits `{}`; for every other event it is a
    # clean exit 0. No .wait(), no .communicate(): single-digit-ms budget met.
    _emit_nonblocking(event_name)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        # _fail_loud / _emit_nonblocking exit via SystemExit -- let it propagate
        # with its chosen code (2 loud, 0 non-blocking).
        raise
    except KeyboardInterrupt:
        _err("interrupted")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 -- last-resort guard
        # An unexpected crash in the launcher itself is fail-loud: it means the
        # hook is broken and the operator must see it. It is still non-blocking in
        # practice (exit 2 does not block PostToolUse; Stop emitted no context),
        # but we surface the full traceback so the async path can be repaired.
        _err(f"[{HOOK_NAME}] unexpected error in async launcher hook")
        _err(
            "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ).rstrip()
        )
        sys.exit(2)
