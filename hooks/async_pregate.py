#!/usr/bin/env python3
"""LAZARUS v2 pre-gate hook entrypoint (PreToolUse, opt-in, SYNCHRONOUS).

Thin wiring shim. The pre-gate LOGIC lives in the package at
``lazarus_sonar.async_.pregate`` (importable, testable). This file is only what
Claude Code wires (opt-in) on ``PreToolUse``: it puts the repo's ``src/`` (and
this ``hooks/`` dir, for the reused v1 ``retro_audit`` extractor) on ``sys.path``
so the hook runs from a plain checkout with no install, then calls ``main()``.

The pre-gate is OFF by default (``[async.pregate].enabled = false``) and, even
when enabled, only surfaces the highest-confidence findings and always ALLOWS the
action (advisory, never hard-blocks). See the package module docstring for the
triple-constraint rationale.

Wiring (see hooks/settings.snippet.v2.json, under _optional_pretooluse_pregate):
  - PreToolUse:Write|Edit -> python {{LAZARUS_HOME}}/async_pregate.py --kind diff

Run standalone for debugging:
    echo '{"hook_event_name":"PreToolUse","tool_name":"Write", ...}' \
        | python hooks/async_pregate.py --kind diff
    python hooks/async_pregate.py --file examples/demo/work_unit.diff --kind diff
"""

from __future__ import annotations

import sys
from pathlib import Path

_HOOK_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HOOK_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"

for _p in (_SRC_DIR, _HOOK_DIR):
    _ps = str(_p)
    if _p.is_dir() and _ps not in sys.path:
        sys.path.insert(0, _ps)

from lazarus_sonar.async_.pregate import main  # noqa: E402


if __name__ == "__main__":
    main()
