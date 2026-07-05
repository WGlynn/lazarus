"""The auto-apply mechanism: conservative (unique-match) and reversible (undo)."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lazarus_sonar.apply import apply_fix, undo_last  # noqa: E402


def test_advisory_only_fix_is_left_as_proposal(tmp_path):
    r = apply_fix({"rule_id": "x", "patch": "do a thing (no concrete edit)"}, undo_dir=tmp_path / "undo")
    assert r.applied is False
    assert "advisory-only" in r.reason


def test_unique_match_is_applied_and_backed_up(tmp_path):
    f = tmp_path / "svc.py"
    f.write_text("resp = requests.get(url, headers=headers)\n", encoding="utf-8")
    fix = {
        "rule_id": "timeout-on-external-calls.md",
        "edit": {
            "file": str(f),
            "find": "requests.get(url, headers=headers)",
            "replace": "requests.get(url, headers=headers, timeout=5)",
        },
    }
    r = apply_fix(fix, undo_dir=tmp_path / "undo")
    assert r.applied is True
    assert "timeout=5" in f.read_text(encoding="utf-8")
    assert Path(r.backup).exists()


def test_ambiguous_match_is_skipped_never_guessed(tmp_path):
    f = tmp_path / "svc.py"
    f.write_text("x = 1\nx = 1\n", encoding="utf-8")
    r = apply_fix({"edit": {"file": str(f), "find": "x = 1", "replace": "x = 2"}}, undo_dir=tmp_path / "undo")
    assert r.applied is False
    assert "ambiguous" in r.reason
    assert f.read_text(encoding="utf-8") == "x = 1\nx = 1\n"  # untouched


def test_absent_match_is_skipped(tmp_path):
    f = tmp_path / "svc.py"
    f.write_text("hello\n", encoding="utf-8")
    r = apply_fix({"edit": {"file": str(f), "find": "goodbye", "replace": "hi"}}, undo_dir=tmp_path / "undo")
    assert r.applied is False
    assert "not present" in r.reason


def test_missing_file_is_skipped(tmp_path):
    r = apply_fix(
        {"edit": {"file": str(tmp_path / "nope.py"), "find": "a", "replace": "b"}},
        undo_dir=tmp_path / "undo",
    )
    assert r.applied is False
    assert "not found" in r.reason


def test_undo_reverts_the_last_edit(tmp_path):
    f = tmp_path / "svc.py"
    f.write_text("orig\n", encoding="utf-8")
    apply_fix({"edit": {"file": str(f), "find": "orig", "replace": "changed"}}, undo_dir=tmp_path / "undo")
    assert "changed" in f.read_text(encoding="utf-8")
    r = undo_last(tmp_path / "undo")
    assert r.applied is True
    assert f.read_text(encoding="utf-8") == "orig\n"


def test_undo_with_nothing_to_revert(tmp_path):
    r = undo_last(tmp_path / "undo")
    assert r.applied is False
    assert "nothing to undo" in r.reason


def test_root_relative_path(tmp_path):
    (tmp_path / "svc.py").write_text("a\n", encoding="utf-8")
    r = apply_fix(
        {"edit": {"file": "svc.py", "find": "a", "replace": "b"}},
        undo_dir=tmp_path / "undo",
        root=tmp_path,
    )
    assert r.applied is True
    assert (tmp_path / "svc.py").read_text(encoding="utf-8") == "b\n"
