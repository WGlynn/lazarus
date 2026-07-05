# Contributing to Lazarus + Sonar

Thanks for looking. This is a small, dependency-light tool and it intends to stay that way.

## Setup

```
git clone https://github.com/WGlynn/lazarus
cd lazarus
pip install -e . pytest ruff
```

Sonar, the config loader, and the ledger are stdlib-only (plus the `tomli` TOML backport on Python 3.9-3.10). The Claude judge is the only part that needs the `anthropic` SDK and an API key; install it with `pip install -e ".[judge]"` when you want to run a real audit. None of the tests or demos need it.

## The green bar

Green is executable here, never model-judged. A change is green when all three pass:

```
ruff check src/lazarus_sonar tests
python -m pytest tests/ examples/async_demo/test_async_cycle.py -q
python examples/demo/run_demo.py         # v1: exactly 2 surfaced, 1 declined
python examples/async_demo/run_async_demo.py   # v2 async cycle, offline, no API key
```

CI runs the same across Python 3.9, 3.11, and 3.13. If the demos go red, a cross-module contract drifted; that is the signal, fix the contract.

## Principles worth knowing before you change things

- **Lazarus proposes, it never applies.** No code path may auto-edit a user's files. Applying a fix is always a separate, explicit human step.
- **Sonar stays cheap and offline.** Keep the perception stage stdlib-only with no network. If you want to add an embedding recall stage, it goes behind the existing `score_file` seam as an option, not a hard dependency.
- **The judge is the only expensive step.** Anything that changes how often it runs (see the `[async.trigger]` gate) must default to the current behavior and be opt-in.
- **Additive over forking.** The v2 async path reuses the v1 engine rather than duplicating it. Keep that discipline: one engine, modes on top.

## Pull requests

Open an issue first for anything beyond a small fix, so we can agree on shape before you build. Keep PRs focused, keep the green bar, and match the surrounding style (ruff is the arbiter). Conventional-commit-style titles (`feat:`, `fix:`, `docs:`, `chore:`) are appreciated but not required.
