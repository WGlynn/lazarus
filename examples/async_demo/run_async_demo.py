#!/usr/bin/env python3
"""Runnable LAZARUS v2 async-cycle demo - the one-command proof the transport works.

Run it:

    python examples/async_demo/run_async_demo.py

or, from inside examples/async_demo/:

    python run_async_demo.py

This is the v2 analogue of the v1 ``examples/demo/run_demo.py``. Where the v1 demo
is an executable assertion over the ENGINE (config -> Sonar -> Lazarus -> ledger),
this one is an executable assertion over the async TRANSPORT: launcher spool ->
detached runner -> pending queue -> next-turn injection -> consume. It runs with
NO API key and NO network (the config forces the deterministic offline stub judge).

It asserts six steps in order and exits 0 with "ASYNC DEMO PASSED" only if every
one holds; any drift prints the mismatch and exits 1.

  (a) Pipe a synthetic PostToolUse(Write) event into hooks/async_launcher.py.
      Assert exit 0 fast (< 2.0s) and that a wu-<run_id>.txt spool file appeared,
      then JOIN on the pending queue reaching 2 SURFACED (a detached child cannot
      be .wait()-ed portably, so we poll the queue instead).
  (b) Call run_background_audit in-process with the stub judge. Assert the v1
      oracle (2 surfaced = no-secrets-in-logs.md + timeout-on-external-calls.md,
      killed_by_judge == 1, below_confidence == 0), that read_unconsumed() returns
      2 PendingFindings whose .fix round-trips RetroFix.as_dict() (8 keys) and
      whose .work_unit_sig == work_unit_signature(work_unit), and that this second
      pass adds 0 NEW surfaced lines (dedup, D-4) because it shares (a)'s signature.
  (c) Pipe UserPromptSubmit into hooks/async_inject.py. Assert valid JSON with
      hookEventName == 'UserPromptSubmit', additionalContext containing both
      rule_ids and 'PROPOSAL', then CONSUMED == 2 / current SURFACED == 0.
  (d) A second inject is silent (no additionalContext, exit 0).
  (e) Shell out to examples/demo/run_demo.py: assert exit 0 + 'DEMO PASSED', and
      that a plain v1 config with no [async] table loads with async_enabled False
      / async_mode == 'sync' (the additive-defaults contract).
  (f) Assert the vendored lazarus_sonar.async_.stub_judge.stub_judge_fn and the
      demo examples/demo/stub_judge.py stub_judge_fn agree verdict-for-verdict
      (anti-drift between the two stub copies).

Design note (why (a) and (b) agree): both the OS-spawn path (a) and the in-process
path (b) audit the SAME text the v1 Write-extractor produces (via
retro_audit.extract_work_unit on the same synthetic event), giving ONE identical
work_unit_signature, so the pending queue dedups to exactly 2 findings across both.
Step (b) wipes the SHARED ledger just before its pass so its judge-accounting
oracle stays deterministic regardless of whether (a)'s detached child already
recorded prefer-f-strings.md as DECLINED (which would otherwise suppress it before
the judge and change the `judged` count).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

# --- make the package + hooks + demo importable from a plain checkout ------
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]  # examples/async_demo -> examples -> repo root
SRC = REPO_ROOT / "src"
HOOKS = REPO_ROOT / "hooks"
DEMO = REPO_ROOT / "examples" / "demo"
for extra in (str(SRC), str(HOOKS), str(DEMO)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from lazarus_sonar.config import load_config  # noqa: E402
from lazarus_sonar.ledger import work_unit_signature  # noqa: E402
from lazarus_sonar.async_.pending import PendingQueue, SURFACED, CONSUMED  # noqa: E402
from lazarus_sonar.async_.runner import run_background_audit  # noqa: E402

from retro_audit import extract_work_unit  # noqa: E402  (hooks/ on path)
from stub_judge import stub_judge_fn as demo_stub  # noqa: E402  (examples/demo on path)


CONFIG_PATH = HERE / "lazarus.config.toml"
LAUNCHER = HOOKS / "async_launcher.py"
INJECT = HOOKS / "async_inject.py"
V1_DEMO = DEMO / "run_demo.py"
V1_DEMO_CONFIG = DEMO / "lazarus.config.toml"
WORK_UNIT_DIFF = DEMO / "work_unit.diff"

EXPECTED_SURFACED = {"no-secrets-in-logs.md", "timeout-on-external-calls.md"}
KIND = "diff"

# RetroFix.as_dict() field set (contract section 1).
RETROFIX_KEYS = {
    "rule_id", "title", "path", "where", "patch", "reason", "confidence", "sonar_score",
}


class DemoError(AssertionError):
    """A step assertion failed. Carries a human-readable mismatch."""


def _synthetic_write_event() -> dict:
    """A PostToolUse(Write) event whose written content is the demo diff.

    The v1 extractor turns this into a "+"-prefixed work-unit via
    `_content_from_write`. Both the launcher (step a) and step (b) run that same
    extractor on this same event, so they share one work_unit_signature.
    """
    content = WORK_UNIT_DIFF.read_text(encoding="utf-8")
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {
            "file_path": "service/upstream.py",
            "content": content,
        },
    }


def _clear_async_state(config) -> None:
    """Remove the demo-local pending queue + spool so each run starts clean."""
    pending = Path(config.pending_path)
    if pending.exists():
        pending.unlink()
    spool = Path(config.async_spool_dir)
    if spool.exists():
        for child in spool.glob("*"):
            try:
                child.unlink()
            except OSError:
                pass


def _wipe_ledger(config) -> None:
    """Wipe the shared ledger so step (b)'s accounting oracle is deterministic."""
    ledger = Path(config.ledger_path)
    if ledger.exists():
        ledger.unlink()


def _poll_surfaced(queue: PendingQueue, target: int, timeout_s: float) -> int:
    """Poll read_unconsumed() until it reaches `target` SURFACED or times out."""
    deadline = time.monotonic() + timeout_s
    n = 0
    while time.monotonic() < deadline:
        n = len(queue.read_unconsumed())
        if n >= target:
            return n
        time.sleep(0.05)
    return n


# --------------------------------------------------------------------------- #
# Step A - launcher spools + spawns the detached runner (OS path)
# --------------------------------------------------------------------------- #


def step_a(config, event: dict) -> None:
    spool_before = set(Path(config.async_spool_dir).glob("wu-*.txt")) \
        if Path(config.async_spool_dir).exists() else set()

    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(LAUNCHER), "--kind", "diff"],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        env=_env_with_config(),
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    elapsed = time.monotonic() - t0

    if proc.returncode != 0:
        raise DemoError(
            f"(a) launcher exited {proc.returncode}; stderr:\n{proc.stderr}"
        )
    if elapsed >= 2.0:
        raise DemoError(f"(a) launcher took {elapsed:.2f}s (>= 2.0s); it must return fast")

    spool_after = set(Path(config.async_spool_dir).glob("wu-*.txt"))
    new_spool = spool_after - spool_before
    if not new_spool:
        raise DemoError("(a) launcher did not write a wu-<run_id>.txt spool file")

    # Join on the detached child reaching 2 SURFACED (can't portably .wait() a
    # detached PID, so we poll the queue).
    queue = PendingQueue(config.pending_path)
    n = _poll_surfaced(queue, target=2, timeout_s=20.0)
    if n != 2:
        # Surface the child's log to explain a miss.
        logs = sorted(Path(config.async_spool_dir).glob("log-*.txt"))
        tail = logs[-1].read_text(encoding="utf-8") if logs else "(no log)"
        raise DemoError(
            f"(a) detached runner produced {n} SURFACED (expected 2). "
            f"child log:\n{tail}"
        )


# --------------------------------------------------------------------------- #
# Step B - in-process run_background_audit (deterministic core)
# --------------------------------------------------------------------------- #


def step_b(config, event: dict) -> None:
    # Same extractor the launcher used -> same signature -> same dedup key.
    kind, work_unit = extract_work_unit(event)
    sig = work_unit_signature(work_unit)

    # Wipe the shared ledger so suppression from (a)'s child does not perturb the
    # judged/killed accounting; the pending queue keeps (a)'s 2 SURFACED so this
    # pass can assert dedup.
    _wipe_ledger(config)

    queue = PendingQueue(config.pending_path)
    surfaced_before = len(queue.read_unconsumed())

    result = run_background_audit(
        work_unit,
        config=config,
        kind=kind,
        judge_fn=demo_stub,
        queue=queue,
        run_id="deadbeef",
    )

    surfaced_ids = {f.rule_id for f in result.fixes}
    if surfaced_ids != EXPECTED_SURFACED:
        raise DemoError(f"(b) surfaced {sorted(surfaced_ids)} != {sorted(EXPECTED_SURFACED)}")
    if result.killed_by_judge != 1:
        raise DemoError(f"(b) killed_by_judge == {result.killed_by_judge} (expected 1)")
    if result.below_confidence != 0:
        raise DemoError(f"(b) below_confidence == {result.below_confidence} (expected 0)")
    if result.work_unit_sig != sig:
        raise DemoError("(b) result.work_unit_sig != work_unit_signature(work_unit)")

    findings = queue.read_unconsumed()
    if len(findings) != 2:
        raise DemoError(f"(b) read_unconsumed() returned {len(findings)} (expected 2)")
    for f in findings:
        if set(f.fix.keys()) != RETROFIX_KEYS:
            raise DemoError(f"(b) finding.fix keys {sorted(f.fix)} != {sorted(RETROFIX_KEYS)}")
        if f.work_unit_sig != sig:
            raise DemoError("(b) finding.work_unit_sig != work_unit_signature(work_unit)")
        if not isinstance(f.fix["path"], str):
            raise DemoError("(b) finding.fix['path'] is not a str (JSON round-trip broke)")

    # Dedup (D-4): (a) already wrote these 2 SURFACED lines under the SAME sig, so
    # this in-process pass added 0 NEW surfaced keys.
    surfaced_after = len(findings)
    if surfaced_before == 2 and surfaced_after != 2:
        raise DemoError(
            f"(b) dedup broke: had {surfaced_before} SURFACED, now {surfaced_after}"
        )


# --------------------------------------------------------------------------- #
# Step C - inject emits + consumes
# --------------------------------------------------------------------------- #


def step_c(config) -> None:
    proc = subprocess.run(
        [sys.executable, str(INJECT)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit"}),
        text=True,
        capture_output=True,
        env=_env_with_config(),
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    if proc.returncode != 0:
        raise DemoError(f"(c) inject exited {proc.returncode}; stderr:\n{proc.stderr}")

    out = proc.stdout.strip()
    if not out:
        raise DemoError("(c) inject emitted nothing (expected additionalContext)")
    try:
        envelope = json.loads(out)
    except json.JSONDecodeError as exc:
        raise DemoError(f"(c) inject stdout was not valid JSON: {exc}\n{out}")

    hso = envelope.get("hookSpecificOutput", {})
    if hso.get("hookEventName") != "UserPromptSubmit":
        raise DemoError(f"(c) hookEventName != UserPromptSubmit: {hso.get('hookEventName')}")
    ctx = hso.get("additionalContext", "")
    for rid in EXPECTED_SURFACED:
        if rid not in ctx:
            raise DemoError(f"(c) additionalContext missing rule_id {rid}")
    if "PROPOSAL" not in ctx.upper():
        raise DemoError("(c) additionalContext missing PROPOSAL framing")

    # After the inject, the two keys are CONSUMED and nothing is currently SURFACED.
    queue = PendingQueue(config.pending_path)
    counts = queue.counts()
    if counts.get(CONSUMED, 0) != 2:
        raise DemoError(f"(c) CONSUMED == {counts.get(CONSUMED, 0)} (expected 2)")
    if counts.get(SURFACED, 0) != 0:
        raise DemoError(f"(c) current SURFACED == {counts.get(SURFACED, 0)} (expected 0)")


# --------------------------------------------------------------------------- #
# Step D - a second inject is silent
# --------------------------------------------------------------------------- #


def step_d() -> None:
    proc = subprocess.run(
        [sys.executable, str(INJECT)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit"}),
        text=True,
        capture_output=True,
        env=_env_with_config(),
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    if proc.returncode != 0:
        raise DemoError(f"(d) second inject exited {proc.returncode}; stderr:\n{proc.stderr}")
    if proc.stdout.strip():
        raise DemoError(f"(d) second inject was not silent; stdout:\n{proc.stdout}")


# --------------------------------------------------------------------------- #
# Step E - v1 sync demo still passes + additive defaults
# --------------------------------------------------------------------------- #


def step_e() -> None:
    proc = subprocess.run(
        [sys.executable, str(V1_DEMO)],
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    if proc.returncode != 0:
        raise DemoError(f"(e) v1 demo exited {proc.returncode}; stderr:\n{proc.stderr}")
    if "DEMO PASSED" not in proc.stdout:
        raise DemoError(f"(e) v1 demo did not print DEMO PASSED:\n{proc.stdout}")

    # Additive defaults: the plain v1 demo config has no [async] table, so it
    # loads as sync with async disabled.
    v1_config = load_config(V1_DEMO_CONFIG)
    if v1_config.async_enabled:
        raise DemoError("(e) plain v1 config has async_enabled True (expected False)")
    if v1_config.async_mode != "sync":
        raise DemoError(f"(e) plain v1 config async_mode == {v1_config.async_mode!r} (expected 'sync')")


# --------------------------------------------------------------------------- #
# Step F - the two stub judges agree (anti-drift)
# --------------------------------------------------------------------------- #


def step_f() -> None:
    from lazarus_sonar.async_.stub_judge import stub_judge_fn as vendored_stub
    from lazarus_sonar.sonar import Candidate

    # Build a small candidate set covering all three demo rules.
    cands = [
        Candidate(rule_id="no-secrets-in-logs.md", path=Path("x"), title="t", score=1.0, overlap=1),
        Candidate(rule_id="timeout-on-external-calls.md", path=Path("x"), title="t", score=1.0, overlap=1),
        Candidate(rule_id="prefer-f-strings.md", path=Path("x"), title="t", score=1.0, overlap=1),
    ]
    a = demo_stub("wu", "diff", cands)
    b = vendored_stub("wu", "diff", cands)
    if a != b:
        raise DemoError(f"(f) stub-judge drift:\n demo={a}\n vendored={b}")


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def _env_with_config() -> dict:
    import os

    env = dict(os.environ)
    env["LAZARUS_CONFIG"] = str(CONFIG_PATH)
    return env


def main() -> int:
    config = load_config(CONFIG_PATH)

    # Fresh async transport state each run so the demo is reproducible.
    _clear_async_state(config)
    _wipe_ledger(config)

    steps = [
        ("a", lambda: step_a(config, _synthetic_write_event())),
        ("b", lambda: step_b(config, _synthetic_write_event())),
        ("c", lambda: step_c(config)),
        ("d", step_d),
        ("e", step_e),
        ("f", step_f),
    ]

    for name, fn in steps:
        try:
            fn()
            print(f"  step ({name}) OK")
        except DemoError as exc:
            print("=" * 72)
            print(f"ASYNC DEMO FAILED at step ({name}):")
            print(f"  {exc}")
            print("=" * 72)
            return 1

    print("=" * 72)
    print("ASYNC DEMO PASSED - launcher -> detached runner -> pending queue -> "
          "inject -> consume, all green, no API key.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
