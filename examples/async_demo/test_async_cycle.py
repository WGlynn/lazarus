"""pytest wrapper for the LAZARUS v2 async cycle (CI-friendly).

8 tests, all deterministic on a bare checkout (stdlib + tomli only, no API key,
no network). Steps (b)-(f) run in-process. Step (a) MOCKS the OS detach
(monkeypatch on ``lazarus_sonar.async_.launcher._spawn_detached``) so no real
detached child runs on locked-down CI; it keeps only the launcher's contract:
fast return, a wu-<run_id>.txt spool file written, and the child argv reading the
work-unit from that FILE (``--work-unit-file``).

Run:

    pytest examples/async_demo/test_async_cycle.py
    pytest -k async_cycle

Tests:
    test_async_cycle_a_launcher_spool_and_fast_return
    test_async_cycle_b_runner_writes_pending
    test_async_cycle_b_dedup_second_run_adds_zero
    test_async_cycle_c_inject_emits_and_consumes
    test_async_cycle_d_second_inject_is_silent
    test_async_cycle_e_v1_sync_demo_still_passes
    test_async_cycle_e_additive_defaults_plain_v1_config_is_sync
    test_async_cycle_f_stub_parity
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
SRC = REPO_ROOT / "src"
HOOKS = REPO_ROOT / "hooks"
DEMO = REPO_ROOT / "examples" / "demo"
for _extra in (str(SRC), str(HOOKS), str(DEMO)):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

from lazarus_sonar.config import load_config  # noqa: E402
from lazarus_sonar.ledger import work_unit_signature  # noqa: E402
from lazarus_sonar.async_ import launcher as launcher_mod  # noqa: E402
from lazarus_sonar.async_.pending import PendingQueue, SURFACED, CONSUMED  # noqa: E402
from lazarus_sonar.async_.runner import run_background_audit  # noqa: E402
from lazarus_sonar.sonar import Candidate  # noqa: E402

from retro_audit import extract_work_unit  # noqa: E402
from stub_judge import stub_judge_fn as demo_stub  # noqa: E402

CONFIG_PATH = HERE / "lazarus.config.toml"
INJECT = HOOKS / "async_inject.py"
V1_DEMO = DEMO / "run_demo.py"
V1_DEMO_CONFIG = DEMO / "lazarus.config.toml"
WORK_UNIT_DIFF = DEMO / "work_unit.diff"

EXPECTED_SURFACED = {"no-secrets-in-logs.md", "timeout-on-external-calls.md"}
RETROFIX_KEYS = {
    "rule_id", "title", "path", "where", "patch", "reason", "confidence", "sonar_score",
}


def _event() -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {
            "file_path": "service/upstream.py",
            "content": WORK_UNIT_DIFF.read_text(encoding="utf-8"),
        },
    }


def _env() -> dict:
    import os

    env = dict(os.environ)
    env["LAZARUS_CONFIG"] = str(CONFIG_PATH)
    return env


@pytest.fixture()
def clean_config():
    """Load the async demo config and wipe transport state + ledger before each test."""
    config = load_config(CONFIG_PATH)
    for p in (Path(config.pending_path), Path(config.ledger_path)):
        if p.exists():
            p.unlink()
    spool = Path(config.async_spool_dir)
    if spool.exists():
        for child in spool.glob("*"):
            try:
                child.unlink()
            except OSError:
                pass
    return config


def _run_in_process(config, run_id="deadbeef"):
    kind, work_unit = extract_work_unit(_event())
    result = run_background_audit(
        work_unit,
        config=config,
        kind=kind,
        judge_fn=demo_stub,
        queue=PendingQueue(config.pending_path),
        run_id=run_id,
    )
    return work_unit, result


# --- step a (mocked detach) ------------------------------------------------ #


def test_async_cycle_a_launcher_spool_and_fast_return(clean_config, monkeypatch):
    captured = {}

    def fake_spawn(argv, *, log_path):
        captured["argv"] = argv
        captured["log_path"] = log_path

    monkeypatch.setattr(launcher_mod, "_spawn_detached", fake_spawn)
    monkeypatch.setenv("LAZARUS_CONFIG", str(CONFIG_PATH))

    # Drive main() directly with the synthetic event on stdin.
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_event())))
    with pytest.raises(SystemExit) as ei:
        launcher_mod.main(["--kind", "diff"])
    assert ei.value.code in (0, None)

    # A spool file was written, and the child argv reads it via --work-unit-file.
    spool = Path(clean_config.async_spool_dir)
    wu_files = list(spool.glob("wu-*.txt"))
    assert len(wu_files) == 1
    argv = captured["argv"]
    assert "--work-unit-file" in argv
    idx = argv.index("--work-unit-file")
    assert Path(argv[idx + 1]) == wu_files[0]


# --- step b ---------------------------------------------------------------- #


def test_async_cycle_b_runner_writes_pending(clean_config):
    work_unit, result = _run_in_process(clean_config)
    sig = work_unit_signature(work_unit)

    assert {f.rule_id for f in result.fixes} == EXPECTED_SURFACED
    assert result.killed_by_judge == 1
    assert result.below_confidence == 0
    assert result.work_unit_sig == sig

    findings = PendingQueue(clean_config.pending_path).read_unconsumed()
    assert len(findings) == 2
    for f in findings:
        assert set(f.fix.keys()) == RETROFIX_KEYS
        assert f.work_unit_sig == sig
        assert isinstance(f.fix["path"], str)


def test_async_cycle_b_dedup_second_run_adds_zero(clean_config):
    _run_in_process(clean_config, run_id="run1aaaa")
    q = PendingQueue(clean_config.pending_path)
    first = len(q.read_unconsumed())
    assert first == 2

    # Second pass, same signature -> append_many dedups every key -> 0 new.
    _run_in_process(clean_config, run_id="run2bbbb")
    assert len(q.read_unconsumed()) == 2


# --- step c ---------------------------------------------------------------- #


def test_async_cycle_c_inject_emits_and_consumes(clean_config):
    _run_in_process(clean_config)

    proc = subprocess.run(
        [sys.executable, str(INJECT)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit"}),
        text=True, capture_output=True, env=_env(), cwd=str(REPO_ROOT), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    envelope = json.loads(proc.stdout.strip())
    hso = envelope["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    ctx = hso["additionalContext"]
    for rid in EXPECTED_SURFACED:
        assert rid in ctx
    assert "PROPOSAL" in ctx.upper()

    counts = PendingQueue(clean_config.pending_path).counts()
    assert counts.get(CONSUMED, 0) == 2
    assert counts.get(SURFACED, 0) == 0


# --- step d ---------------------------------------------------------------- #


def test_async_cycle_d_second_inject_is_silent(clean_config):
    _run_in_process(clean_config)
    # First inject consumes.
    subprocess.run(
        [sys.executable, str(INJECT)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit"}),
        text=True, capture_output=True, env=_env(), cwd=str(REPO_ROOT), timeout=30,
    )
    # Second inject is silent.
    proc = subprocess.run(
        [sys.executable, str(INJECT)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit"}),
        text=True, capture_output=True, env=_env(), cwd=str(REPO_ROOT), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


# --- step e ---------------------------------------------------------------- #


def test_async_cycle_e_v1_sync_demo_still_passes():
    proc = subprocess.run(
        [sys.executable, str(V1_DEMO)],
        text=True, capture_output=True, cwd=str(REPO_ROOT), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "DEMO PASSED" in proc.stdout


def test_async_cycle_e_additive_defaults_plain_v1_config_is_sync():
    v1 = load_config(V1_DEMO_CONFIG)
    assert v1.async_enabled is False
    assert v1.async_mode == "sync"


# --- step f ---------------------------------------------------------------- #


def test_async_cycle_f_stub_parity():
    from lazarus_sonar.async_.stub_judge import stub_judge_fn as vendored_stub

    cands = [
        Candidate(rule_id="no-secrets-in-logs.md", path=Path("x"), title="t", score=1.0, overlap=1),
        Candidate(rule_id="timeout-on-external-calls.md", path=Path("x"), title="t", score=1.0, overlap=1),
        Candidate(rule_id="prefer-f-strings.md", path=Path("x"), title="t", score=1.0, overlap=1),
    ]
    assert demo_stub("wu", "diff", cands) == vendored_stub("wu", "diff", cands)
