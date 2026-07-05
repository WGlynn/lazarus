"""Integration test: the v2 trigger gate actually gates the judge in the runner.

Reuses the async-demo corpus + offline stub judge (no API key). Proves three things:
  1. default (gate disabled) -> the judge runs, exactly as before;
  2. gate enabled with an unreachable bar -> ZERO judge calls, empty result;
  3. gate enabled with a zero bar -> the judge runs and surfaces the same fixes.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SRC = REPO_ROOT / "src"
HOOKS = REPO_ROOT / "hooks"
DEMO = REPO_ROOT / "examples" / "demo"
ASYNC_DEMO = REPO_ROOT / "examples" / "async_demo"
for _extra in (str(SRC), str(HOOKS), str(DEMO)):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

from lazarus_sonar.config import TriggerConfig, load_config  # noqa: E402
from lazarus_sonar.async_.pending import PendingQueue  # noqa: E402
from lazarus_sonar.async_.runner import run_background_audit  # noqa: E402

from retro_audit import extract_work_unit  # noqa: E402
from stub_judge import stub_judge_fn as demo_stub  # noqa: E402

CONFIG_PATH = ASYNC_DEMO / "lazarus.config.toml"
WORK_UNIT_DIFF = DEMO / "work_unit.diff"
EXPECTED_SURFACED = {"no-secrets-in-logs.md", "timeout-on-external-calls.md"}


def _event() -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {
            "file_path": "service/upstream.py",
            "content": WORK_UNIT_DIFF.read_text(encoding="utf-8"),
        },
    }


@pytest.fixture()
def clean_config():
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


def _spy(inner):
    calls = {"n": 0}

    def judge(work_unit, kind, candidates):
        calls["n"] += 1
        return inner(work_unit, kind, candidates)

    return judge, calls


def _with_trigger(config, **trigger_kwargs):
    return replace(
        config,
        async_=replace(config.async_, trigger=TriggerConfig(**trigger_kwargs)),
    )


def _run(config, judge_fn, run_id="deadbeef"):
    kind, work_unit = extract_work_unit(_event())
    return run_background_audit(
        work_unit,
        config=config,
        kind=kind,
        judge_fn=judge_fn,
        queue=PendingQueue(config.pending_path),
        run_id=run_id,
    )


def test_gate_disabled_is_unchanged_default(clean_config):
    judge, calls = _spy(demo_stub)
    result = _run(clean_config, judge)  # default: trigger disabled
    assert calls["n"] == 1
    assert {f.rule_id for f in result.fixes} == EXPECTED_SURFACED


def test_gate_unreachable_bar_skips_the_judge(clean_config):
    gated = _with_trigger(clean_config, enabled=True, base_threshold=1e9, adaptive=False)
    judge, calls = _spy(demo_stub)
    result = _run(gated, judge)
    assert calls["n"] == 0            # the expensive judge never ran
    assert result.fixes == []
    assert result.candidates_in == 0  # an empty shortlist reached run_lazarus


def test_gate_zero_bar_judges(clean_config):
    gated = _with_trigger(clean_config, enabled=True, base_threshold=0.0, adaptive=False)
    judge, calls = _spy(demo_stub)
    result = _run(gated, judge)
    assert calls["n"] == 1
    assert {f.rule_id for f in result.fixes} == EXPECTED_SURFACED
