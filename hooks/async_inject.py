#!/usr/bin/env python3
"""LAZARUS v2 injection hook entrypoint (UserPromptSubmit) -- FAIL-SAFE.

Thin wiring shim. The injection LOGIC lives in the package at
``lazarus_sonar.async_.inject`` (importable, testable). This file is only what
Claude Code wires on ``UserPromptSubmit``: it puts the repo's ``src/`` on
``sys.path`` so the hook runs from a plain checkout with no install, then calls
``main()`` (read unconsumed pending findings -> emit additionalContext with the
PROPOSALS framing -> mark consumed).

Fail-safe: every failure mode inside ``main()`` degrades to a silent no-op with a
clean exit, because this hook is on the user's prompt path and must never wedge a
keystroke. This shim keeps its own bootstrap equally defensive.

Wiring (see hooks/settings.snippet.v2.json):
  - UserPromptSubmit -> python {{LAZARUS_HOME}}/async_inject.py

Run standalone for debugging (prints the JSON envelope it would emit):
    echo '{"hook_event_name":"UserPromptSubmit"}' | python hooks/async_inject.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_HOOK_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HOOK_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"

if _SRC_DIR.is_dir() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

try:
    from lazarus_sonar.async_.inject import main
except Exception:  # noqa: BLE001 -- partial/broken install must not wedge the prompt
    # This hook is on the keystroke path. If even the import fails, exit clean
    # (no additionalContext) rather than surfacing a traceback that could wedge
    # the turn. A LAZARUS_DEBUG whisper aids setup without polluting a real turn.
    import os

    if os.environ.get("LAZARUS_DEBUG"):
        import traceback

        sys.stderr.write(
            "[lazarus.inject] entrypoint import failed (turn NOT blocked):\n"
            + traceback.format_exc()
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
