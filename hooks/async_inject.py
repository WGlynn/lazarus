#!/usr/bin/env python3
# hooks/async_inject.py
"""Canonical-contract-named alias for the v2 next-turn injection hook.

The LAZARUS v2 concurrency contract (section 4) and the offline verify spec name
the injection hook ``hooks/async_inject.py``. The concrete implementation lives in
``hooks/lazarus_inject.py`` (the name the installer wires). This file is a thin
executable alias so the contract's path resolves verbatim: piping a
UserPromptSubmit hook-event JSON into ``python hooks/async_inject.py`` runs the
exact same fail-SAFE hook (read unconsumed pending findings -> emit them on
``additionalContext`` with the PROPOSALS framing -> mark them consumed).

There is exactly one injection hook; this alias delegates to it so the async demo
and any operator following the contract by name both hit the same code.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HOOK_DIR = Path(__file__).resolve().parent
if str(_HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOK_DIR))

from lazarus_inject import main  # noqa: E402  (after sys.path bootstrap)

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
