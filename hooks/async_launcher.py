#!/usr/bin/env python3
"""LAZARUS v2 launcher hook entrypoint (Stop / PostToolUse) -- NON-BLOCKING.

Thin wiring shim. The launcher LOGIC lives in the package at
``lazarus_sonar.async_.launcher`` (importable, testable). This file is only what
Claude Code wires on Stop / PostToolUse: it puts the repo's ``src/`` (and this
``hooks/`` dir, for the reused v1 ``retro_audit`` extractor) on ``sys.path`` so
the hook runs from a plain checkout with no install, then calls ``main()``.

Wiring (see hooks/settings.snippet.v2.json):
  - Stop                    -> python {{LAZARUS_HOME}}/async_launcher.py --kind response
  - PostToolUse:Edit|Write  -> python {{LAZARUS_HOME}}/async_launcher.py --kind diff

Run standalone for debugging:
    echo '{"hook_event_name":"Stop","last_assistant_message":"..."}' \
        | python hooks/async_launcher.py --kind response
"""

from __future__ import annotations

import sys
from pathlib import Path

_HOOK_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HOOK_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"

# Bootstrap: put src/ (for the lazarus_sonar package) and hooks/ (for the v1
# retro_audit extractor the launcher reuses) on the path, idempotently, before
# importing the package logic.
for _p in (_SRC_DIR, _HOOK_DIR):
    _ps = str(_p)
    if _p.is_dir() and _ps not in sys.path:
        sys.path.insert(0, _ps)

from lazarus_sonar.async_.launcher import main  # noqa: E402


if __name__ == "__main__":
    main()
