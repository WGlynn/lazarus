#!/usr/bin/env python3
"""UserPromptSubmit injector: surface the previous turn's async retro-audit findings.

This is the LAZARUS v2 "next-turn injection" logic (canonical contract section 4).
While the main agent was answering the user's *previous* prompt, a detached
background runner (spawned by the async launcher) ran the full v1 pipeline
(SONAR -> LAZARUS -> ledger) off the critical path and wrote any surviving
retroactive-fixes to an append-only pending-findings queue. This runs on the NEXT
``UserPromptSubmit``, reads the findings the runner produced but that have not yet
been shown, emits them to the main agent as ``additionalContext``, and marks them
consumed so a later prompt does not re-surface the same finding.

Where this lives, and how it is invoked
---------------------------------------
The logic lives here, in the package (``lazarus_sonar.async_.inject``). The thin
``hooks/async_inject.py`` entrypoint is what Claude Code wires on
``UserPromptSubmit``; it puts ``src/`` on ``sys.path`` and calls ``main()`` here.

Fail-SAFE, always
------------------
Unlike the launcher (fail-loud on misconfig) and the detached runner (fail-loud
to its own log), THIS hook is on the user's prompt path. It must NEVER wedge a
keystroke. Every failure mode -- no config, misconfigured config, a v1 install
whose async surface is not present yet, an unreadable/corrupt queue, an unexpected
crash -- degrades to a SILENT no-op: emit nothing on ``additionalContext`` and
exit 0. This is the one hook in the system that deliberately swallows everything
(contract D-9). The cost of a swallowed error here is a missed advisory finding,
which is recoverable: the underlying rule is still in the corpus and re-surfaces
on the next related edit.

Consume protocol: emit-then-mark, at-most-once (contract D-5)
-------------------------------------------------------------
We read ``read_unconsumed()`` (current-state SURFACED, last-line-wins over any
CONSUMED), emit the block, THEN append CONSUMED lines. A second inject run reads
zero unconsumed and is a silent no-op. If the harness drops the emitted context
between the emit and the model seeing it, the marks are already down and the
finding will not re-surface. We choose at-most-once over exactly-once because
re-nagging violates the v1 anti-nag contract; a stricter ack-based consume is
deferred (contract D-8). The emit is a single ``print`` of the JSON envelope, so
the marks only land after that write has been handed to stdout.

Exit codes:
    0  always. This hook never blocks a prompt, so it never exits non-zero.
"""

from __future__ import annotations

import io
import json
import os
import sys
import traceback
from typing import Any, List

HOOK_NAME = "lazarus.inject"
HOOK_EVENT_NAME = "UserPromptSubmit"


# --------------------------------------------------------------------------- #
# Fail-SAFE plumbing
#
# This hook never fails loud. `_silent_noop` is the universal exit: emit nothing
# on additionalContext and return a clean 0. A one-line diagnostic goes to stderr
# only when LAZARUS_DEBUG is set, so a wiring problem is inspectable during setup
# without ever polluting a normal prompt turn.
# --------------------------------------------------------------------------- #


def _debug(msg: str) -> None:
    """Emit a diagnostic to stderr only when LAZARUS_DEBUG is truthy.

    Kept off by default: this hook must be visually silent on the prompt path.
    During install/debugging, `LAZARUS_DEBUG=1` surfaces why nothing injected
    (no queue, sync mode, corrupt line) without changing the fail-safe behaviour.
    """
    if os.environ.get("LAZARUS_DEBUG"):
        sys.stderr.write(f"[{HOOK_NAME}] {msg}\n")


def _silent_noop() -> "NoReturn":  # type: ignore[name-defined]
    """The fail-safe exit: emit nothing on additionalContext, exit 0.

    Used for every non-surfacing outcome -- no findings, no queue yet, disabled
    (sync) mode, misconfig, a partial install missing the async surface, or any
    read error. UserPromptSubmit treats a clean exit with no JSON body as "no
    added context", so this never blocks or alters the user's prompt.
    """
    sys.exit(0)


# --------------------------------------------------------------------------- #
# Rendering
#
# The block we inject mirrors the v1 retro_audit `_render_fixes` renderer so the
# human (and the agent) see ONE voice across the sync and async paths: the same
# PROPOSALS framing and the same `lazarus ledger action/decline <sig> <rule_id>`
# footer. The only added line is the header stating these came from the PREVIOUS
# turn's asynchronous audit, so their provenance is unambiguous.
#
# Each finding carries the whole `RetroFix.as_dict()` payload verbatim under
# `finding.fix` (contract section 1), so we format straight off that dict with no
# second lookup and no live Config.
# --------------------------------------------------------------------------- #


def _fix_field(finding: Any, name: str) -> Any:
    """Read a RetroFix field off a PendingFinding's `fix` dict, defensively.

    `finding.fix` is exactly `RetroFix.as_dict()` (rule_id, title, path, where,
    patch, reason, confidence, sonar_score). We read via `.get` on the dict and
    fall back to a top-level attribute so a schema tweak or a hand-built finding
    in a test does not raise on the prompt path.
    """
    fix = getattr(finding, "fix", None)
    if isinstance(fix, dict) and name in fix:
        return fix.get(name)
    # Fall back to a top-level attribute (e.g. work_unit_sig / rule_id live on the
    # PendingFinding itself, not inside `fix`).
    return getattr(finding, name, None)


def _format(findings: List[Any]) -> str:
    """Render surfaced findings as a human-readable additionalContext block.

    Uses RetroFix field names straight off `finding.fix`. The header states these
    are asynchronous retro-audit PROPOSALS produced during the previous turn and
    never applied; the footer points at the same `lazarus ledger` commands the v1
    hook prints, so the operator has one consistent way to action or dismiss a
    finding regardless of which path surfaced it.
    """
    out = io.StringIO()
    n = len(findings)
    out.write(
        f"[{HOOK_NAME}] LAZARUS surfaced {n} retroactive-fix{('' if n == 1 else 'es')} "
        f"from the PREVIOUS turn's asynchronous retro-audit (run off the critical "
        f"path while you were being answered). These are PROPOSALS, not applied "
        f"changes -- nothing in your files or the finished work was modified.\n"
    )
    for i, finding in enumerate(findings, 1):
        rule_id = _fix_field(finding, "rule_id") or "<unknown-rule>"
        title = _fix_field(finding, "title") or ""
        path = _fix_field(finding, "path") or ""
        where = _fix_field(finding, "where") or ""
        reason = _fix_field(finding, "reason") or ""
        patch = _fix_field(finding, "patch") or ""
        confidence = _fix_field(finding, "confidence")
        sig = getattr(finding, "work_unit_sig", "") or ""

        conf_str = ""
        if confidence is not None:
            try:
                conf_str = f" (confidence {float(confidence):.2f})"
            except (TypeError, ValueError):
                conf_str = f" (confidence {confidence})"

        heading = title or rule_id
        out.write(f"\n  {i}. {heading}{conf_str}\n")
        out.write(f"     rule:  {rule_id}\n")
        if path:
            out.write(f"     file:  {path}\n")
        if where:
            out.write(f"     where: {where}\n")
        if reason:
            out.write(f"     why:   {reason}\n")
        if patch:
            patch_lines = patch.strip().splitlines()
            preview = patch_lines[0] if patch_lines else ""
            more = "" if len(patch_lines) <= 1 else f"  (+{len(patch_lines) - 1} more line{'s' if len(patch_lines) - 1 != 1 else ''})"
            out.write(f"     patch: {preview}{more}\n")
        if sig:
            out.write(
                f"     apply: lazarus ledger action {sig[:12]} {rule_id}   "
                f"(or `decline` to suppress it for this work)\n"
            )

    out.write(
        f"\n[{HOOK_NAME}] These are PROPOSALS, not applied changes. Review, then "
        "`lazarus ledger action <sig> <rule_id>` if you apply one, or "
        "`lazarus ledger decline <sig> <rule_id>` to suppress it for this work.\n"
    )
    return out.getvalue()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> None:
    """Read the queue, emit unconsumed findings, mark them consumed.

    Every step is guarded: any failure -> silent no-op. The one place we emit is
    a single `print` of the additionalContext envelope, and `mark_consumed` runs
    only AFTER that print (emit-then-mark, at-most-once; contract D-5).
    """
    # Draining stdin keeps the harness from seeing a broken pipe; the event body
    # is not needed (UserPromptSubmit carries no work-unit for us to audit -- we
    # only read the queue the background runner already populated). A read error
    # is non-fatal.
    try:
        sys.stdin.read()
    except Exception:  # noqa: BLE001 -- draining stdin must never wedge the prompt
        pass

    # ---- Load config (fail-SAFE) ----------------------------------------- #
    # A misconfigured or missing config MUST NOT block the user's prompt, so
    # unlike the launcher we swallow ConfigError (and any import failure) here.
    try:
        from ..config import ConfigError, load_config
    except Exception as exc:  # noqa: BLE001 -- partial/broken install -> no-op
        _debug(f"cannot import lazarus_sonar.config ({exc}); no-op")
        _silent_noop()

    try:
        config = load_config(os.environ.get("LAZARUS_CONFIG"))
    except ConfigError as exc:
        _debug(f"config invalid ({exc}); no-op (never block a prompt)")
        _silent_noop()
    except FileNotFoundError as exc:
        _debug(f"config file not found ({exc}); no-op")
        _silent_noop()
    except Exception as exc:  # noqa: BLE001 -- any load fault is non-blocking here
        _debug(f"config load raised ({exc}); no-op")
        _silent_noop()

    # ---- Respect the async/sync mode switch ------------------------------ #
    # On a v1-style install (mode "sync", or a config.py that predates the [async]
    # table and has no `async_enabled` accessor) this hook is a no-op and the v1
    # sync retro-audit stays authoritative. `getattr(..., False)` keeps us safe
    # against a config object without the v2 surface.
    if not getattr(config, "async_enabled", False):
        _debug("async mode disabled (sync path authoritative); no-op")
        _silent_noop()

    # ---- Resolve the pending queue --------------------------------------- #
    # The pending-queue module is part of the v2 async surface. Import it lazily
    # so a partial install (v1 engine present, async_ package not yet dropped in)
    # degrades to a silent no-op instead of crashing the prompt.
    try:
        from .pending import PendingQueue
    except Exception as exc:  # noqa: BLE001 -- async surface absent -> no-op
        _debug(f"cannot import lazarus_sonar.async_.pending ({exc}); no-op")
        _silent_noop()

    pending_path = getattr(config, "pending_path", None)
    if pending_path is None:
        _debug("config has no pending_path accessor; no-op")
        _silent_noop()

    try:
        queue = PendingQueue(pending_path)
    except Exception as exc:  # noqa: BLE001 -- constructing the queue must not throw here
        _debug(f"cannot open pending queue at {pending_path} ({exc}); no-op")
        _silent_noop()

    # ---- Read unconsumed findings ---------------------------------------- #
    # `read_unconsumed` returns current-state SURFACED findings (never CONSUMED),
    # newest run first. A missing queue file is a legitimate empty (no background
    # run has produced anything yet), NOT an error -- it returns []. Any read
    # fault is swallowed to the fail-safe no-op.
    try:
        findings = queue.read_unconsumed()
    except Exception as exc:  # noqa: BLE001 -- unreadable/corrupt queue -> no-op
        _debug(f"reading pending queue failed ({exc}); no-op")
        _silent_noop()

    if not findings:
        # The common quiet outcome: the background runner surfaced nothing last
        # turn (nothing buried was relevant), or everything was already consumed.
        _debug("no unconsumed findings; no-op")
        _silent_noop()

    # ---- Format + emit --------------------------------------------------- #
    try:
        context = _format(findings)
    except Exception as exc:  # noqa: BLE001 -- a rendering fault must not block the prompt
        _debug(f"formatting findings raised ({exc}); no-op")
        _silent_noop()

    # UserPromptSubmit is the ONE event whose schema accepts additionalContext.
    # Emit the envelope. This single print is the "emit" half of emit-then-mark.
    envelope = {
        "hookSpecificOutput": {
            "hookEventName": HOOK_EVENT_NAME,
            "additionalContext": context,
        }
    }
    try:
        print(json.dumps(envelope))
        sys.stdout.flush()
    except Exception as exc:  # noqa: BLE001 -- if we cannot emit, do NOT consume
        # If the emit itself failed, we must not mark consumed -- otherwise the
        # finding is lost without ever being shown. Leave it SURFACED for the
        # next prompt and no-op.
        _debug(f"emitting additionalContext failed ({exc}); leaving unconsumed")
        _silent_noop()

    # ---- Consume (mark AFTER emit; at-most-once) ------------------------- #
    # Now that the context has been written to stdout, append one CONSUMED line
    # per finding so a subsequent inject run reads zero unconsumed and stays
    # silent. `mark_consumed` is idempotent (last-line-wins), so even a double
    # UserPromptSubmit is safe. A consume failure is non-fatal: at worst the
    # finding re-surfaces once next prompt, which is the recoverable direction.
    try:
        queue.mark_consumed(findings)
    except Exception as exc:  # noqa: BLE001 -- consume best-effort; already emitted
        _debug(f"marking findings consumed failed ({exc}); may re-surface once")

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        # Even an interrupt on the prompt path must not surface a traceback that
        # could wedge the turn. Exit clean.
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001 -- last-resort fail-SAFE guard
        # An unexpected crash in THIS hook must never block the user's prompt.
        # Swallow to a clean exit and only whisper the cause under LAZARUS_DEBUG.
        if os.environ.get("LAZARUS_DEBUG"):
            sys.stderr.write(
                f"[{HOOK_NAME}] unexpected error (turn NOT blocked):\n"
                + "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ).rstrip()
                + "\n"
            )
        sys.exit(0)
