#!/usr/bin/env python3
# hooks/async_runner_entry.py
"""Checkout entry shim for the detached LAZARUS v2 background runner.

The launcher (``hooks/lazarus_async_launch.py`` / its ``async_launcher.py`` alias)
spawns THIS file detached instead of the ``lazarus-audit-bg`` console script so
the async path works from a plain git checkout with nothing installed -- exactly
the way the v1 hooks bootstrap ``src/`` onto ``sys.path`` before importing the
package. When the package IS installed, the launcher may spawn the console script
directly; this shim is the checkout fallback.

It does three things and nothing else:
  1. put the repo's ``src/`` on ``sys.path`` (so ``import lazarus_sonar`` works
     without ``pip install -e .``),
  2. call the runner's ``main()`` with the argv the launcher passed
     (``--work-unit-file`` / ``--kind`` / ``--config`` / ``--run-id`` / ``--stub``),
  3. propagate its exit code.

The runner module lives at ``lazarus_sonar.async_runner`` (directly in the
package, so its v1-engine imports are plain sibling imports). The canonical
contract names it ``lazarus_sonar.async_.runner``; this shim imports the concrete
on-disk location and is tolerant if a future layout moves it into the subpackage.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HOOK_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HOOK_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"

if _SRC_DIR.is_dir() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# The hooks dir must be importable too: the runner's --event-file path reuses the
# v1 extractor from retro_audit.py, and this keeps that import working when the
# child is spawned with cwd elsewhere.
if _HOOK_DIR.is_dir() and str(_HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOK_DIR))


def _load_runner_main():
    """Return the runner's ``main`` callable from whichever location holds it.

    Concrete on-disk location first (``lazarus_sonar.async_runner``), then the
    canonical-contract subpackage location (``lazarus_sonar.async_.runner``) as a
    forward-compatible fallback.
    """
    try:
        from lazarus_sonar.async_runner import main  # type: ignore[import-not-found]
        return main
    except ImportError:
        from lazarus_sonar.async_.runner import main  # type: ignore[import-not-found]
        return main


if __name__ == "__main__":
    _main = _load_runner_main()
    sys.exit(_main(sys.argv[1:]))
