#!/usr/bin/env python3
"""LAZARUS v2 detached-runner entry shim (checkout fallback).

This is the small executable the launcher spawns DETACHED when running from a
plain checkout (no ``pip install``). It puts the repo's ``src/`` on ``sys.path``
(so ``import lazarus_sonar`` resolves) and ``hooks/`` (so the runner's
``--event-file`` path can reach the v1 ``retro_audit`` extractor), then calls
``lazarus_sonar.async_.runner.main()`` and propagates its exit code.

Why a shim and not the console script directly: from a plain clone the
``lazarus-audit-bg`` console script does not exist (nothing was installed), so
the launcher (``lazarus_sonar.async_.launcher._child_argv``) spawns
``python hooks/async_runner_entry.py ...`` instead. When the package IS installed,
the launcher spawns the ``lazarus-audit-bg`` console script directly and this shim
is not used. It mirrors the v1 hooks' import bootstrap so the detached child works
with no install.

It forwards ``sys.argv[1:]`` to the runner unchanged, so the launcher's
``--work-unit-file / --kind / --run-id / --config / --stub`` flags pass straight
through.
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

from lazarus_sonar.async_.runner import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
