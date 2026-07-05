#!/usr/bin/env python3
"""Apply a concrete fix edit to a file, reversibly. The auto-apply mechanism.

Lazarus applies buried-rule fixes automatically. What makes automatic application
net-positive rather than reckless is that it is REVERSIBLE and CONSERVATIVE, not that
a human stands in the path:

- Reversible: every applied edit backs up the original first, so `lazarus undo`
  restores it in one step. The backup write is on the machine, off the critical
  path, and adds no human latency.
- Conservative: an edit is applied only if its `find` text occurs EXACTLY ONCE in
  the target file. Zero matches (the file moved on) or multiple matches (ambiguous)
  are SKIPPED and reported, never guessed. An advisory fix that carries no concrete
  edit is left as a proposal rather than forced.

This module is pure and stdlib-only: given a fix dict with an `edit`, it edits the
file. It does not decide WHEN to apply (that is the runner / config `auto_apply`),
and it never calls a model.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

__all__ = ["ApplyResult", "apply_fix", "undo_last", "edit_of"]


@dataclass(frozen=True)
class ApplyResult:
    applied: bool
    file: str
    reason: str
    backup: str = ""


def edit_of(fix: Any) -> Optional[dict]:
    """The concrete `{file, find, replace}` edit on a fix, or None if advisory-only."""
    edit = fix.get("edit") if isinstance(fix, dict) else getattr(fix, "edit", None)
    return edit if isinstance(edit, dict) else None


def apply_fix(fix: Any, *, undo_dir: "Path | str", root: "Path | str | None" = None) -> ApplyResult:
    """Apply a fix's concrete edit to its target file, backing up the original first.

    Returns an ApplyResult; never raises on an unapplyable fix (advisory-only,
    missing file, no/ambiguous match) -- those are reported as applied=False so the
    caller can leave the fix as a surfaced proposal.
    """
    edit = edit_of(fix)
    if not edit:
        return ApplyResult(False, "", "advisory-only fix (no concrete edit); left as a proposal")

    rel = edit.get("file") or (
        fix.get("path") if isinstance(fix, dict) else getattr(fix, "path", "")
    )
    find = edit.get("find", "")
    replace = edit.get("replace", "")
    if not rel or not find:
        return ApplyResult(False, str(rel or ""), "edit missing file or find text; skipped")

    p = Path(rel)
    if not p.is_absolute() and root is not None:
        p = Path(root) / rel
    if not p.exists():
        return ApplyResult(False, str(p), "target file not found; skipped")

    text = p.read_text(encoding="utf-8")
    count = text.count(find)
    if count == 0:
        return ApplyResult(False, str(p), "find text not present (the file moved on); skipped")
    if count > 1:
        return ApplyResult(
            False, str(p), f"find text is ambiguous ({count} matches); skipped, never guessed"
        )

    undo = Path(undo_dir)
    undo.mkdir(parents=True, exist_ok=True)
    stamp = f"{int(time.time() * 1000)}"
    backup = undo / f"{p.name}.{stamp}.bak"
    backup.write_text(text, encoding="utf-8")
    (undo / f"{p.name}.{stamp}.json").write_text(
        json.dumps({"file": str(p), "backup": str(backup)}), encoding="utf-8"
    )
    p.write_text(text.replace(find, replace, 1), encoding="utf-8")
    return ApplyResult(True, str(p), "applied", str(backup))


def undo_last(undo_dir: "Path | str") -> ApplyResult:
    """Revert the most recent auto-applied edit from its backup."""
    undo = Path(undo_dir)
    metas = sorted(undo.glob("*.json"))
    if not metas:
        return ApplyResult(False, "", "nothing to undo")
    meta = json.loads(metas[-1].read_text(encoding="utf-8"))
    dst = Path(meta["file"])
    src = Path(meta["backup"])
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    metas[-1].unlink()
    return ApplyResult(True, str(dst), "reverted", str(src))
