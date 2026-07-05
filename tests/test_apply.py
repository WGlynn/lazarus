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


def test_two_edits_same_file_undo_both_restores_original(tmp_path):
    # Two edits to the SAME file applied back-to-back (same clock tick on coarse
    # Windows resolution) must get DISTINCT backups, so undoing both restores the
    # original byte-for-byte. Regression for the millisecond-stamp collision that
    # silently broke `lazarus undo` when the auto-applier landed >1 edit per file.
    f = tmp_path / "svc.py"
    original = "a = 1\nb = 2\n"
    f.write_text(original, encoding="utf-8")
    undo_dir = tmp_path / "undo"

    r1 = apply_fix({"edit": {"file": str(f), "find": "a = 1", "replace": "a = 10"}}, undo_dir=undo_dir)
    r2 = apply_fix({"edit": {"file": str(f), "find": "b = 2", "replace": "b = 20"}}, undo_dir=undo_dir)
    assert r1.applied and r2.applied
    assert Path(r1.backup) != Path(r2.backup)  # distinct backups, no collision
    assert f.read_text(encoding="utf-8") == "a = 10\nb = 20\n"

    # LIFO: first undo reverts the second edit, second undo reverts the first.
    assert undo_last(undo_dir).applied is True
    assert f.read_text(encoding="utf-8") == "a = 10\nb = 2\n"
    assert undo_last(undo_dir).applied is True
    assert f.read_text(encoding="utf-8") == original
    assert undo_last(undo_dir).applied is False  # nothing left


def test_undo_orders_by_numeric_stamp_not_filename(tmp_path):
    # undo_last must revert the NUMERICALLY-largest stamp (newest), not the
    # lexicographically-largest filename. When stamps cross a digit-width boundary
    # (13 -> 14 digits, ~year 2286) a filename sort would pick "9999999999999"
    # over "10000000000000" and revert the OLDER edit. Hand-build both backups.
    import json as _json

    f = tmp_path / "svc.py"
    f.write_text("current\n", encoding="utf-8")
    undo = tmp_path / "undo"
    undo.mkdir()

    older = "9999999999999"      # 13 digits
    newer = "10000000000000"     # 14 digits, numerically newer, lexicographically SMALLER
    (undo / f"svc.py.{older}.bak").write_text("v-old\n", encoding="utf-8")
    (undo / f"svc.py.{older}.json").write_text(
        _json.dumps({"file": str(f), "backup": str(undo / f"svc.py.{older}.bak")}), encoding="utf-8"
    )
    (undo / f"svc.py.{newer}.bak").write_text("v-new\n", encoding="utf-8")
    (undo / f"svc.py.{newer}.json").write_text(
        _json.dumps({"file": str(f), "backup": str(undo / f"svc.py.{newer}.bak")}), encoding="utf-8"
    )

    r = undo_last(undo)
    assert r.applied is True
    # Reverts the newest (14-digit) backup, not the lexicographically-last (13-digit) one.
    assert f.read_text(encoding="utf-8") == "v-new\n"


def test_root_relative_path(tmp_path):
    (tmp_path / "svc.py").write_text("a\n", encoding="utf-8")
    r = apply_fix(
        {"edit": {"file": "svc.py", "find": "a", "replace": "b"}},
        undo_dir=tmp_path / "undo",
        root=tmp_path,
    )
    assert r.applied is True
    assert (tmp_path / "svc.py").read_text(encoding="utf-8") == "b\n"
