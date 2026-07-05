# Lazarus v2 async cycle - runnable demo

The one-command proof that the v2 **async transport** holds together end to end:
launcher spool -> detached runner -> pending queue -> next-turn injection -> consume.
It runs with **no API key and no network** - the config forces the same
deterministic offline stub judge the v1 demo uses.

```bash
python examples/async_demo/run_async_demo.py
```

Expected tail on success (exit 0):

```
ASYNC DEMO PASSED - launcher -> detached runner -> pending queue -> inject -> consume, all green, no API key.
```

Any drift prints `ASYNC DEMO FAILED at step (x): ...` and exits 1. That green/red
exit is the point: this demo is an executable assertion over the whole v2
transport, exactly as `examples/demo/run_demo.py` is over the v1 engine.

## What's in this directory

```
examples/async_demo/
  run_async_demo.py       # the executable assertion (steps a-f)
  test_async_cycle.py     # pytest wrapper (8 tests; step (a) mocks the OS detach)
  lazarus.config.toml     # reuses the v1 demo corpus + ledger, adds an [async] table
  run_async_demo.sh       # python3/python shell wrapper
  .lazarus/               # demo-local pending queue + spool dir (created on first run)
```

It reuses the v1 demo's three-rule corpus and its ledger **verbatim** (there is no
new corpus here), so the async transport is exercised against the identical oracle.

## The four transport pieces

| piece | on disk | job |
|-------|---------|-----|
| Launcher | `hooks/async_launcher.py` -> `lazarus_sonar.async_.launcher` | Stop/PostToolUse; spool the work-unit + spawn the detached runner, return in ms |
| Background runner | `lazarus_sonar.async_.runner` (console `lazarus-audit-bg`) | run the v1 engine off the critical path, drain survivors to the pending queue |
| Pending queue | `lazarus_sonar.async_.pending` | append-only JSONL twin of the ledger; SURFACED -> CONSUMED |
| Injection hook | `hooks/async_inject.py` -> `lazarus_sonar.async_.inject` | UserPromptSubmit; read unconsumed, emit as `additionalContext`, mark consumed |

## The six asserted steps

- **(a)** Pipe a synthetic `PostToolUse(Write)` event into `hooks/async_launcher.py`.
  Assert exit 0 in < 2.0s and a `wu-<run_id>.txt` spool file, then join on the
  pending queue reaching 2 SURFACED (a detached child can't be `.wait()`-ed
  portably, so we poll the queue).
- **(b)** Call `run_background_audit` in-process with the stub judge. Assert the v1
  oracle (2 surfaced = `no-secrets-in-logs.md` + `timeout-on-external-calls.md`,
  `killed_by_judge == 1`, `below_confidence == 0`), that `read_unconsumed()`
  returns 2 findings whose `.fix` round-trips `RetroFix.as_dict()` (8 keys) and
  whose `.work_unit_sig == work_unit_signature(work_unit)`, and that this pass adds
  **0 new** surfaced lines (dedup, D-4).
- **(c)** Pipe `UserPromptSubmit` into `hooks/async_inject.py`. Assert valid JSON
  with `hookEventName == "UserPromptSubmit"`, `additionalContext` carrying both
  rule_ids and the PROPOSAL framing, then `CONSUMED == 2` / current `SURFACED == 0`.
- **(d)** A second inject is silent (no `additionalContext`, exit 0).
- **(e)** Shell out to `examples/demo/run_demo.py`: assert exit 0 + `DEMO PASSED`,
  and that a plain v1 config with no `[async]` table loads with `async_enabled`
  False / `async_mode == "sync"` (the additive-defaults contract).
- **(f)** Assert the vendored `lazarus_sonar.async_.stub_judge` and the demo
  `examples/demo/stub_judge.py` agree verdict-for-verdict (anti-drift).

## One extractor, one oracle (D-3)

Both the OS-spawn path (a) and the in-process path (b) audit the **same text** the
v1 Write-extractor produces - they call `retro_audit.extract_work_unit` on the
same synthetic event - so they share **one** `work_unit_signature`. That is why the
pending queue dedups across both to exactly 2 findings. Step (b) wipes the shared
ledger just before its pass so its judge-accounting oracle stays deterministic
regardless of whether (a)'s detached child already recorded `prefer-f-strings.md`
as DECLINED (which would otherwise suppress it before the judge and change the
`judged` count).

## pytest

```bash
pytest examples/async_demo/test_async_cycle.py
# or
pytest -k async_cycle
```

Steps (b)-(f) run in-process and are fully deterministic. Step (a) mocks the OS
detach (`monkeypatch` on `lazarus_sonar.async_.launcher._spawn_detached`) so no
real detached child runs on locked-down CI; it keeps only the launcher's fast
return + spool-file + reads-from-FILE contract.

## Why a stub judge, and swapping in the real one

The stub is the credential-free green oracle: its verdict is a pure function of
each candidate's `rule_id`, so the cycle is reproducible with no model, key, or
clock. To run the async cycle against the **real** Claude judge instead, set
`[async].stub_judge = false` in the config, install `pip install lazarus-sonar[judge]`,
and set `ANTHROPIC_API_KEY`. The transport is identical; only the judge seam changes.
