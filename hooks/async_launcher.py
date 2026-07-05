#!/usr/bin/env python3
# hooks/async_launcher.py
"""Canonical-contract-named alias for the v2 launcher hook.

The LAZARUS v2 concurrency contract (section 3) and the offline verify spec name
the launcher hook ``hooks/async_launcher.py``. The concrete implementation lives
in ``hooks/lazarus_async_launch.py`` (the name the installer wires). This file is
a thin executable alias so the contract's path resolves verbatim: piping a
PostToolUse / Stop hook-event JSON into ``python hooks/async_launcher.py`` runs
the exact same non-blocking launcher (extract work-unit -> spool to file -> spawn
detached runner -> return in milliseconds).

There is exactly one launcher implementation; this alias delegates to it so the
async demo and any operator following the contract by name both hit the same code.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HOOK_DIR = Path(__file__).resolve().parent
if str(_HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOK_DIR))

from lazarus_async_launch import main  # noqa: E402  (after sys.path bootstrap)

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
