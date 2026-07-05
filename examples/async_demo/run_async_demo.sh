#!/usr/bin/env bash
# One-command runner for the Lazarus v2 async-cycle demo.
#
#   ./examples/async_demo/run_async_demo.sh
#
# It just invokes run_async_demo.py with whatever Python is on PATH (preferring
# python3). No API key, no network, no install required beyond Python itself
# (plus `tomli` on Python 3.9-3.10; tomllib is stdlib on 3.11+). Expected output
# ends with "ASYNC DEMO PASSED".
set -euo pipefail

# Resolve this script's own directory so the demo runs correctly regardless of
# the caller's current working directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "run_async_demo.sh: no python3 or python found on PATH." >&2
    exit 127
fi

exec "$PY" "${SCRIPT_DIR}/run_async_demo.py"
