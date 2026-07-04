# Lazarus + Sonar — runnable demo

This is the executable proof that the tool does what the top-level README claims.
It ships a tiny three-rule corpus, one sample diff, and a deterministic offline
"judge" so you can run the whole pipeline — perception, suppression, judgment,
ranking, ledger — with **no API key, no network, and nothing installed but
Python**. It always produces the same result:

> **2 surfaced retroactive-fixes, and 1 Lazarus-killed candidate that lands in
> the ledger as `DECLINED`.**

## Run it

From the repo root:

```bash
python examples/demo/run_demo.py
```

or use the one-line wrapper (same thing):

```bash
./examples/demo/run_demo.sh
```

You need Python 3.9+ (3.11+ uses the stdlib `tomllib`; on 3.9–3.10 the demo
needs the `tomli` backport, which a normal `pip install lazarus-sonar` provides).
The script puts the repo's `src/` on `sys.path` itself, so it also works from a
plain `git clone` with nothing installed.

The last line you should see is:

```
DEMO PASSED - 2 surfaced, 1 declined, exactly as expected.
```

and the process exits `0`. If any interface in the package drifts, the script
prints what it expected versus what it got and exits non-zero. The demo is an
executable assertion, not just a printout.

## What's in here

| File | Role |
| --- | --- |
| `corpus/no-secrets-in-logs.md` | Rule 1 — the diff violates it, so it **SURFACES**. |
| `corpus/timeout-on-external-calls.md` | Rule 2 — the diff violates it, so it **SURFACES**. |
| `corpus/prefer-f-strings.md` | Rule 3 — the diff already satisfies it, so it is **DECLINED**. |
| `work_unit.diff` | The finished work being audited: a diff that logs an API key and makes an external call with no timeout, and is otherwise f-string-clean. |
| `lazarus.config.toml` | A real config pointed at `./corpus`, writing its ledger to `.lazarus/ledger.jsonl`. |
| `stub_judge.py` | The deterministic, offline judge (the "green oracle"). Verdicts are a pure function of `rule_id`. |
| `run_demo.py` | Loads the config, runs Sonar, runs Lazarus with the stub judge, prints the `AuditResult`, and asserts the exact expected outcome. |
| `run_demo.sh` | One-command wrapper around `run_demo.py`. |

## What happens, step by step

1. **Config load** — `run_demo.py` reads `lazarus.config.toml` with the ordinary
   `config.load_config`. Relative paths resolve against this directory, so the
   corpus and ledger are found no matter where you run from.

2. **Sonar (perception)** — the wide, cheap keyword sweep scores all three rule
   files against the diff. Every rule shares vocabulary with the diff, so **all
   three clear `min_score` and reach the judge.** You can see this yourself with
   the real CLI, no key required:

   ```bash
   cd examples/demo
   python -m lazarus_sonar.cli sonar --file work_unit.diff --kind diff --config lazarus.config.toml
   ```

   which prints a 3-candidate shortlist (scores roughly `13.9`, `12.2`, `6.2`).
   That the f-strings rule is on the shortlist is the point: **the decline is a
   precision kill by Lazarus, not a recall miss by Sonar.**

3. **Lazarus (cognition)** — Lazarus drops anything already `DECLINED` for this
   work (nothing, on a fresh ledger), then asks the judge one question per
   candidate: *would applying this rule have changed the finished work?* The
   demo injects `stub_judge_fn` here instead of the Claude model:

   - `no-secrets-in-logs` → `would_change=true` @ `0.9` — the diff logs the key.
   - `timeout-on-external-calls` → `would_change=true` @ `0.9` — the diff's
     `requests.get(...)` has no `timeout=`.
   - `prefer-f-strings` → `would_change=false` @ `0.2` — the diff already uses
     f-strings, so the rule is inert; nothing would change.

4. **Filter + rank** — the two `would_change=true` verdicts clear the config's
   `min_confidence` of `0.6` and are surfaced, ranked by confidence. The
   `would_change=false` verdict is killed by the judge and recorded as
   `DECLINED`.

5. **Ledger** — the run writes to `.lazarus/ledger.jsonl`: two `SURFACED`
   records and one `DECLINED`. `run_demo.py` deletes any prior ledger first so
   the demo is reproducible; in normal use that `DECLINED` entry is exactly what
   stops the same dead match from being re-surfaced for the same work (the
   anti-nag property).

## The exact expected output

The script asserts all of the following (see `_check` in `run_demo.py`):

```
result.fixes             == [no-secrets-in-logs.md, timeout-on-external-calls.md]   # len 2, ranked
result.declined_rule_ids == [prefer-f-strings.md]                                   # len 1
result.killed_by_judge   == 1
result.below_confidence  == 0
candidates_in            == 3      # Sonar reached the judge with all three
judged                   == 3
```

The only nondeterministic field is the ledger timestamp, which is not asserted.
Everything else is byte-stable across runs and across machines.

## Why a stub judge?

Precision is the product; Sonar's recall is not. The real judge is a model, so
its verdicts are neither byte-reproducible nor free. For a demo a stranger can
run to green — and for CI — the stub encodes the *correct* answer for this diff
by hand and runs with no credentials. Because its signature and returned-dict
shape match the real `judge.judge_batch` exactly (`lazarus.JudgeFn` and
`lazarus.Verdict.from_judge`), swapping it in exercises the entire real
pipeline; only the model call is replaced. To run the demo against the actual
Claude judge instead, set `ANTHROPIC_API_KEY` and use the CLI:

```bash
cd examples/demo
export ANTHROPIC_API_KEY=sk-ant-...
python -m lazarus_sonar.cli audit --file work_unit.diff --kind diff --config lazarus.config.toml
```

A well-calibrated judge should reach the same verdicts the stub encodes; that it
is *not guaranteed* to, byte-for-byte, is exactly why the reproducible path uses
the stub.
