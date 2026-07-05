"""End-to-end proof that a judge `edit` threads all the way to an applied file.

The auto-apply mechanism (apply.py) is unit-tested in test_apply.py, but that
proves only that GIVEN a fix carrying a concrete edit, the file is changed. This
test closes the other half: that the pipeline actually PRODUCES such a fix. It
runs the real engine offline with the deterministic stub judge --

    run_sonar_for_config -> run_lazarus(judge_fn=stub) -> RetroFix.edit
      -> RetroFix.as_dict()["edit"] -> apply_fix -> file mutated -> undo_last

-- against the demo corpus and the file the demo diff represents. If any link in
that chain drops the edit (a missing dataclass field, a schema/normalizer gap, an
as_dict() omission), the surfaced fix is advisory-only and `apply_fix` reports
`applied=False`, so this test goes red. That is the regression guard the wire
lacked: `edit` is threaded, not just defined.

Deterministic and offline: stdlib + tomli only, no API key, no network. The
committed target file (examples/demo/target/service/upstream.py) is copied into a
tmp scratch dir and mutated there, so the repo file is never touched.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SRC = REPO_ROOT / "src"
DEMO = REPO_ROOT / "examples" / "demo"
for _extra in (str(SRC), str(DEMO)):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

from lazarus_sonar.apply import apply_fix, undo_last  # noqa: E402
from lazarus_sonar.config import load_config  # noqa: E402
from lazarus_sonar.ledger import Ledger  # noqa: E402
from lazarus_sonar.lazarus import Verdict, run_lazarus  # noqa: E402
from lazarus_sonar.sonar import run_sonar_for_config  # noqa: E402

from stub_judge import stub_judge_fn  # noqa: E402  (examples/demo on path)

CONFIG_PATH = DEMO / "lazarus.config.toml"
WORK_UNIT_DIFF = DEMO / "work_unit.diff"
# The committed file the demo diff represents; the stub's edit `find`s are exact
# substrings of it. Copied into tmp before applying so the original is untouched.
TARGET_FIXTURE = DEMO / "target" / "service" / "upstream.py"
TARGET_REL = Path("service") / "upstream.py"

TIMEOUT_EDIT = {
    "file": "service/upstream.py",
    "find": "requests.get(url, headers=headers)",
    "replace": "requests.get(url, headers=headers, timeout=5)",
}


def _run(tmp_path):
    """Run the real pipeline offline with the stub judge; return the AuditResult.

    A fresh ledger in tmp_path guarantees no stale DECLINED entry from a prior
    demo run suppresses a candidate. record=False keeps the run side-effect-free.
    """
    config = load_config(CONFIG_PATH)
    work_unit = WORK_UNIT_DIFF.read_text(encoding="utf-8")
    candidates = run_sonar_for_config(work_unit, config, kind="diff")
    return run_lazarus(
        work_unit,
        candidates,
        config=config,
        ledger=Ledger(tmp_path / "ledger.jsonl"),
        judge_fn=stub_judge_fn,
        kind="diff",
        record=False,
    )


def test_edit_threads_from_verdict_to_retrofix(tmp_path):
    result = _run(tmp_path)
    fixes = {f.rule_id: f for f in result.fixes}
    assert set(fixes) == {"no-secrets-in-logs.md", "timeout-on-external-calls.md"}

    # The concrete edit survived verdict -> RetroFix unchanged...
    timeout = fixes["timeout-on-external-calls.md"]
    assert timeout.edit == TIMEOUT_EDIT
    # ...and rides in the dict view the pending queue / auto-applier read.
    assert timeout.as_dict()["edit"] == TIMEOUT_EDIT
    # The other surfaced rule also carries a concrete edit (not advisory).
    assert fixes["no-secrets-in-logs.md"].edit is not None


def test_auto_apply_then_undo_end_to_end(tmp_path):
    result = _run(tmp_path)
    fix = next(f for f in result.fixes if f.rule_id == "timeout-on-external-calls.md")

    # Build the file the diff represents in a scratch tree (repo fixture untouched).
    target = tmp_path / TARGET_REL
    target.parent.mkdir(parents=True)
    original = TARGET_FIXTURE.read_text(encoding="utf-8")
    target.write_text(original, encoding="utf-8")
    undo_dir = tmp_path / "undo"

    # Apply: the edit's find occurs exactly once -> the file is really changed.
    r = apply_fix(fix.as_dict(), undo_dir=undo_dir, root=tmp_path)
    assert r.applied, r.reason
    changed = target.read_text(encoding="utf-8")
    assert "requests.get(url, headers=headers, timeout=5)" in changed
    assert changed != original

    # Reversible: undo restores the original byte-for-byte.
    u = undo_last(undo_dir)
    assert u.applied, u.reason
    assert target.read_text(encoding="utf-8") == original


def test_advisory_verdict_normalizes_empty_edit_to_none():
    # The real judge always returns an `edit` object; an all-empty one is the
    # "no concrete edit" sentinel and must normalize to None so the fix stays a
    # proposal rather than a guaranteed no-op apply.
    v = Verdict.from_judge(
        {
            "rule_id": "x",
            "would_change": True,
            "confidence": 0.9,
            "edit": {"file": "", "find": "", "replace": ""},
        }
    )
    assert v.edit is None
