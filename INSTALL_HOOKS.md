# Installing the Lazarus/Sonar hooks

This document wires Lazarus/Sonar into Claude Code's deterministic hook layer. The
CLI (`lazarus sonar|audit|ledger`) is the whole tool and needs none of this; the
hooks are an opt-in that makes the retro-audit run automatically on the work an
agent just finished, instead of when a human remembers to ask.

There are two ways to wire the hooks, and they are mutually exclusive:

- **v1 sync path (default, shipped, GREEN).** The retro-audit runs as a
  **blocking** `Stop` / `PostToolUse` hook. It is on the critical path, so every
  turn pays the judge-latency tax even when nothing is caught. This is the whole
  of Steps 1 to 5 below and it is unchanged.
- **v2 async path (additive, opt-in).** The same audit runs **off** the critical
  path, in a detached background process, and its findings are injected into the
  *next* turn. The launcher returns in milliseconds, so the latency is hidden
  behind the agent's next turn and only real catches cost anything. This is the
  new "Async mode (v2)" section near the end.

The v2 async path reuses the v1 engine unchanged. There is exactly one place that
audits (`run_lazarus`), one signature function (`work_unit_signature`), one
`Candidate` type, one ledger. v2 adds a transport, not a second engine. You pick a
path with a config mode; you never run both, because running both would
double-audit the same work.

Three v1 placements map to three Claude Code events:

- **BOOT** (`SessionStart`) runs `session_start_sweep.py` — a pure Sonar keyword
  sweep over the last session's work-unit. No judge, no API key.
- **HOOK** (`Stop` and `PostToolUse:Edit|Write`) runs `retro_audit.py` — the full
  Sonar to Lazarus pipeline. Needs the Claude judge (an API key).
- **GATE** (`PreToolUse:Edit|Write|Agent`, optional) surfaces and judges buried
  rules *before* a write. It can block, so v1 ships it as documented guidance, not
  a hardened blocking hook.

Nothing installs itself. Merging the snippet is a copy-paste step you do on
purpose.

## Prerequisites

1. Install the package so the hooks can import `lazarus_sonar`. Either:

   ```bash
   pip install "lazarus-sonar[judge]"     # includes the Claude judge SDK
   ```

   or, from a checkout, an editable install from the repo root:

   ```bash
   pip install -e ".[judge]"
   ```

   The hooks also work from a plain checkout without any install: each hook adds
   the repo's `src/` to `sys.path` when `lazarus_sonar` is not already importable,
   as long as `hooks/` sits next to `src/`. Installing is the reliable path; the
   fallback exists for a bare clone.

2. Have a `lazarus.config.toml`. Copy the annotated example and point `[corpus].path`
   at your rules/memory directory:

   ```bash
   cp lazarus.config.example.toml lazarus.config.toml
   ```

   `corpus.path` and `corpus.globs` are required and have no default. A missing
   corpus is a hard error, never a silent scan of your home directory. See the
   example file for the full schema. The example also carries an annotated
   `[async]` table; it is all-defaults so it changes nothing until you turn v2 on.

## Step 1 — Copy the hooks somewhere stable

The hooks can run from the repo's `hooks/` directory or from any directory you
copy them to. Pick one absolute location and keep the two v1 hook files together:

```
<somewhere>/session_start_sweep.py
<somewhere>/retro_audit.py
```

Both files must be reachable at the absolute path you put in `settings.json`. If
you copy them out of the repo and you did NOT `pip install lazarus-sonar`, the
`src/`-fallback no longer applies (the copies are no longer next to `src/`), so
install the package in that case. Installing is the simplest choice either way.

Call the directory holding these files your **`{{LAZARUS_HOME}}`**. If you plan to
enable the v2 async path, copy the async hook files into the same directory (see
"Async mode (v2)"); keeping every hook under one `{{LAZARUS_HOME}}` is the whole
point of the placeholder.

## Step 2 — Set `{{LAZARUS_HOME}}` to the absolute hooks directory

Open `hooks/settings.snippet.json`. Replace every occurrence of `{{LAZARUS_HOME}}`
with the absolute path to the directory from Step 1. Use forward slashes on every
OS — Claude Code accepts them on Windows too.

Examples:

```
C:/tools/lazarus/hooks
/home/you/lazarus/hooks
```

Do not use a relative path and do not rely on the working directory. Claude Code
invokes hooks from an arbitrary cwd, so the command must be an absolute path to
the script.

## Step 3 — Point `LAZARUS_CONFIG` at your config file (explicitly)

This is the reliable way to tell every hook which `lazarus.config.toml` to load,
and it is the setting you should always set.

In `hooks/settings.snippet.json` there is an `env` block:

```json
"env": {
  "LAZARUS_CONFIG": "{{LAZARUS_CONFIG_FILE}}"
}
```

Replace `{{LAZARUS_CONFIG_FILE}}` with the **absolute** path to your
`lazarus.config.toml`. Forward slashes on every OS.

Examples:

```
C:/tools/lazarus/lazarus.config.toml
/home/you/lazarus/lazarus.config.toml
```

Do NOT assume the config sits one directory above `hooks/`. Earlier snippets used
a brittle relative expression like `{{LAZARUS_HOME}}/../lazarus.config.toml`; that
guess is wrong the moment you copy `hooks/` out of the repo or keep your config
anywhere else. The config can live anywhere. Point `LAZARUS_CONFIG` at it and the
guessing stops. The v2 hooks resolve config the same way (they call the same
`load_config`), so this one setting governs both paths.

### How discovery actually resolves

Each hook resolves its config in this order, and this is worth understanding
because it governs the failure modes:

1. `--config <path>` on the hook's command line (present on the v1 hooks for
   manual runs; the v2 launcher passes it to the detached child explicitly).
2. `LAZARUS_CONFIG` in the environment — the `env` value from this step.
3. Walk-up discovery: `lazarus.config.toml` in the hook's cwd, then each parent
   directory up to the filesystem root.

Steps 1 and 2 are strict: if you set `--config` or `LAZARUS_CONFIG` and the path
does not exist, that is a fail-loud error, not a fall-through to the walk-up
search. A typo in `LAZARUS_CONFIG` fails loudly with the bad path, which is what
you want. Step 3 is the only "search" and it never reaches outside your directory
tree; there is no fallback to `$HOME` or to scanning your whole disk.

If you leave `LAZARUS_CONFIG` unset, the hooks still work as long as walk-up
discovery finds a `lazarus.config.toml` above the cwd Claude Code launches them
from. That is convenient inside a single repo but fragile across projects, so the
env var is the path this document recommends and the snippet ships it pre-wired.

## Step 4 — Merge the `hooks` block into your `settings.json`

Merge the `hooks` object from the snippet into `~/.claude/settings.json` (global)
or a project `.claude/settings.json`. The rest of the snippet (the top-level
`_lazarus_snippet` documentation object and `_optional_pretooluse_gate`) is
inert — Claude Code ignores keys it does not recognize, so you can paste the
whole thing, but the load-bearing pieces are `env` and `hooks`.

If you already have a `hooks` key, merge per event. Each event value is an array,
so append the Lazarus matcher-group object to the existing `SessionStart` /
`Stop` / `PostToolUse` array rather than replacing it. Appending one more object
is always safe.

The merged block wires:

- `SessionStart` to `python {{LAZARUS_HOME}}/session_start_sweep.py`
- `Stop` to `python {{LAZARUS_HOME}}/retro_audit.py --kind response`
- `PostToolUse` (matcher `Edit|Write|NotebookEdit`) to
  `python {{LAZARUS_HOME}}/retro_audit.py --kind diff`

Timeouts in the snippet are in **seconds** (Claude Code convention): 20s for the
keyword-only boot sweep, 60s for the retro-audit's single batched judge call.
Tune to your corpus size and `judge_model`.

## Step 5 — Set `ANTHROPIC_API_KEY` for the retro-audit (not the sweep)

The `Stop` / `PostToolUse` retro-audit calls the Lazarus judge, so it needs
`ANTHROPIC_API_KEY` in the environment. The `SessionStart` sweep is pure Sonar
perception and needs no key.

The API key is normally an environment variable. The config also accepts an
optional `[judge].api_key`, but leaving it unset and using `ANTHROPIC_API_KEY` is
the primary path; the config key exists only so the value is always readable and
so a user who prefers config-file secrets has somewhere to put one.

If the key is missing, the retro-audit does not wedge your session — see the
fail-loud contract below. That is the offline-safe behavior: perception still
runs, the precision pass reports loudly that it could not run, and the turn
proceeds.

## Offline-stub vs real-judge modes

There are two ways the Lazarus judge can be satisfied, and the difference matters
for what you can run without credentials.

**Real-judge mode (production).** Install the judge extra
(`pip install "lazarus-sonar[judge]"`, which pulls the `anthropic` SDK) and set
`ANTHROPIC_API_KEY`. The `Stop` / `PostToolUse` hooks then make one batched Claude
call per audit and surface the surviving fixes. This is the mode the settings
snippet wires up. Precision tracks `judge_model` (default `claude-opus-4-8`), the
documented quality knob.

**Offline-stub mode (demo and tests).** The judge is just a `judge_fn` that maps
Sonar candidates to verdict dicts. The repo ships a deterministic, no-network,
no-API-key stub at `examples/demo/stub_judge.py` and injects it via
`run_lazarus(..., judge_fn=stub_judge_fn)`. It is the objective oracle for the
demo: on the demo corpus and demo diff it yields exactly two surfaced fixes and
one declined candidate, with no clock, no key, and no model call. The stub's
signature and its returned dict shape are identical to what the real judge
produces, so anything green against the stub is wired correctly for the real
judge. Use this to verify your install without spending a token.

The v1 hooks themselves always use the real judge (they call `run_lazarus` with no
`judge_fn`, so it binds to `judge.judge_batch`). The stub is a demo/test path, not
a v1 hook mode — but it is how you prove the pipeline before you turn the real
judge on. The v2 async path makes the stub a first-class runtime mode too, so the
whole background cycle runs with no key; see "Offline-testable, no key" below.

## Smoke test

Run the shipped demo. It loads its own config, runs Sonar, and runs Lazarus with
the offline stub judge — no key, no network:

```bash
python examples/demo/run_demo.py
```

Expected outcome: **2 surfaced, 1 declined**. The surfaced rules are
`no-secrets-in-logs.md` and `timeout-on-external-calls.md`; `prefer-f-strings.md`
is judged inert for the sample diff and lands in the ledger as `DECLINED`. If you
see that, your package install and the pipeline are sound and the only remaining
variable for the live hooks is your own `corpus.path`, `LAZARUS_CONFIG`, and (for
the retro-audit) `ANTHROPIC_API_KEY`.

You can also exercise the retro-audit hook by hand against the demo diff, which
routes through the real judge (needs the key), or against any file:

```bash
python {{LAZARUS_HOME}}/retro_audit.py --file examples/demo/work_unit.diff --kind diff
```

## Fail-loud contract

The hooks fail loud on anything that indicates a wiring problem, and stay quiet
only when there is genuinely nothing to do. The one deliberate carve-out is a
judge/model/network fault inside the retro-audit, which must never wedge a turn.

**`session_start_sweep.py` (SessionStart):**

- Missing config, or a config with a missing/malformed `corpus.path` or empty
  `corpus.globs`: visible stderr error, exit non-zero. No silent fallback.
- An unreadable corpus (bad `corpus.path`, all files skipped): fail-loud, exit
  non-zero — a sweep that silently finds nothing because the path is wrong is
  worse than one that refuses.
- No previous work-unit to sweep (fresh checkout, first boot, cleared work-unit):
  NOT an error. It prints a short note and exits 0. Failing loud here would nag on
  every clean boot.

  The previous session's work-unit is read from a file, resolved in order:
  `--work-unit <path>`, then `$LAZARUS_LAST_WORK_UNIT`, then the conventional
  `<cwd>/.lazarus/last_work_unit.txt` (where a session-end hook is expected to
  have written the finished work). None present means nothing to sweep — a clean
  exit, not a failure. There is no config key for this location in v1.

**`retro_audit.py` (Stop / PostToolUse):**

- No hook input on stdin, unparseable event JSON, or an empty extracted
  work-unit: fail-loud, exit code 2. A hook that silently no-ops on missing input
  hides real wiring bugs — that is the class of quiet failure this tool exists to
  avoid.
- Missing/unresolvable config, or a resolved-but-missing corpus directory:
  fail-loud, exit code 2. No home/cwd fallback.
- A Sonar sweep failure (bad glob, unreadable corpus): fail-loud, exit code 2.
- **The carve-out.** A judge/model/network error — including a missing
  `ANTHROPIC_API_KEY` at judge time or the `[judge]` extra not being installed —
  is printed loudly to stderr and the hook exits **0 without blocking the turn**.
  A retro-audit is advisory; it must never wedge your session on a judge-setup
  problem or a transient API error. Perception (Sonar) still ran.

  On the `Stop` event specifically, the non-blocking signal is an emitted `{}` on
  stdout. That is the only shape the Stop event's schema accepts — it rejects
  `hookSpecificOutput` / `additionalContext`. On every other event a clean exit 0
  with no JSON body is the non-blocking signal. This is loud on stderr, silent to
  the turn.

The result: config and corpus problems stop you immediately and visibly, so you
fix them once; a broken or absent judge degrades to Sonar-only and lets the
session continue.

## The optional PreToolUse gate

The gate is opt-in and ships commented-out. In `settings.snippet.json` it lives
under `_optional_pretooluse_gate`, not under `hooks`, so it does nothing until you
move it.

It runs `retro_audit.py` in gate mode before a `Write` / `Edit` / `Agent` action,
surfacing and judging relevant buried rules *before* the write happens. This is
the highest-value and highest-risk placement: it can block an action on a
would-change verdict, and it adds a judge call to the latency of every matched
write.

To enable it, cut the `PreToolUse` object out of `_optional_pretooluse_gate` and
paste it as a sibling of `SessionStart` / `Stop` / `PostToolUse` inside the real
`hooks` object. Its fail-loud policy differs from the retro-audit in one way: on a
judge error the gate defaults to **allowing** the action, never to blocking it —
a broken judge must not freeze your writes. In v1 this is guidance and a
ready-to-paste block, not a hardened, thoroughly-tested blocking hook. Enable it
deliberately, after you have run the retro-audit long enough to trust the judge's
precision on your corpus. (v2 ships a separate, narrower shift-left gate,
`async_pregate.py`, described below; the v1 gate above and the v2 pre-gate are two
different opt-in placements, not the same file.)

---

# Async mode (v2)

Everything above is v1 and is unchanged. This section is **additive**: turning it
on does not edit a single v1 file, and turning it off (or never touching it)
leaves the v1 sync path exactly as it was. If you never set the `[async]` table
and never wire the v2 hooks, your existing install keeps running byte-for-byte the
same.

## What v2 changes and why

v1 runs the audit as a **blocking** `Stop` / `PostToolUse` hook. It is on the
critical path, so every operation pays the judge-latency tax even when nothing is
caught. v2 moves the audit **off** the critical path so the latency is hidden
behind the main agent's next turn, and only real catches cost anything.

The Claude Code loop is sequential, so the concurrency is harness-native and
OS-level: a detached background process, file IPC, and the existing hook injection
points. Three moving parts plus one optional gate:

1. **Non-blocking launcher** (`Stop` / `PostToolUse`, `async_launcher.py`).
   Captures the finished work-unit from the hook's stdin payload, writes it to a
   spool file, spawns the background runner **detached** (no wait), and returns in
   milliseconds. It runs no Sonar and no judge, so it adds ~zero latency.
2. **Background runner** (`lazarus_sonar.async_.runner`, console entrypoint
   `lazarus-audit-bg`). Runs the entire v1 pipeline (Sonar -> Lazarus -> ledger)
   on the work-unit and writes surviving findings to a pending-findings queue. It
   runs concurrently with the main agent's next turn.
3. **Next-turn injection** (`UserPromptSubmit`, `async_inject.py`). Reads the
   unconsumed findings the runner produced during the previous turn, emits them as
   context for the main agent (the `additionalContext` channel), and marks them
   consumed. No findings -> silent no-op.
4. **Optional shift-left pre-gate** (`PreToolUse`, `async_pregate.py`, default
   OFF). Runs a tightly-bounded synchronous Sonar+Lazarus on the *planned* action,
   surfacing only the highest-confidence rules to prevent rather than patch.

It **proposes**, never auto-applies — the same contract as v1. And the whole cycle
is offline-testable with the same deterministic stub judge the v1 demo uses, so it
runs with no API key.

## The two modes, and why you never run both

The v1 sync path and the v2 async path audit the same work with the same engine.
Running both would double-audit every turn. They are mutually exclusive at runtime
via a single config switch, `[async].mode`:

- **`mode = "sync"`** (or the `[async]` table absent, or the v2 hooks not wired):
  the v2 launcher, if wired at all, is a no-op that emits the non-blocking payload
  and exits. The v1 `retro_audit.py` on `Stop` / `PostToolUse` remains
  authoritative. This is identical v1 behaviour.
- **`mode = "async"`** (the default *when the v2 hooks are wired* — detected via
  `LAZARUS_ASYNC=1`, which the v2 settings snippet exports): the launcher
  dispatches the detached runner, and `async_launcher.py` **replaces**
  `retro_audit.py` on `Stop` / `PostToolUse`. You wire the async launcher instead
  of the sync retro-audit, not in addition to it.

The rule of thumb: on `Stop` and `PostToolUse` you point the hook at exactly one
of `retro_audit.py` (sync) or `async_launcher.py` (async). The `SessionStart`
sweep is orthogonal and stays wired in both modes.

## Step A — Copy the v2 hook files into `{{LAZARUS_HOME}}`

Keep the async hooks next to the v1 hooks so one `{{LAZARUS_HOME}}` covers
everything:

```
{{LAZARUS_HOME}}/async_launcher.py        # Stop / PostToolUse launcher (non-blocking)
{{LAZARUS_HOME}}/async_runner_entry.py    # 3-line shim: puts src/ on the path, calls runner.main()
{{LAZARUS_HOME}}/async_inject.py          # UserPromptSubmit injection
{{LAZARUS_HOME}}/async_pregate.py         # optional PreToolUse pre-gate
```

`async_runner_entry.py` is the checkout fallback the launcher spawns: it adds
`src/` to `sys.path` and calls `lazarus_sonar.async_.runner.main()`, mirroring the
v1 hooks' import bootstrap, so the detached child works from a plain clone with no
install. When the package **is** installed, the launcher can instead spawn the
`lazarus-audit-bg` console script directly; the entry shim is only the
no-install fallback.

## Step B — Add the `[async]` table to your config

The `[async]` table is optional and every key has a default, so an existing v1
config loads unchanged. To turn v2 on, add:

```toml
[async]
# Master switch. "async" runs the launcher + runner + inject pipeline; "sync"
# makes the launcher a no-op so the v1 blocking retro-audit path is authoritative.
# The default is chosen at load time: "async" when the v2 hooks are wired
# (detected via LAZARUS_ASYNC=1, exported by the v2 settings snippet), else
# "sync". A bare `mode` key overrides the auto-detect.
mode = "async"                       # "async" | "sync"   (default: auto)

# Convenience boolean equivalent to mode. If BOTH mode and enabled are set they
# must agree, or the config fails loud. Exposed because a boolean reads cleanly
# in a settings file.
enabled = true                       # bool               (default: mode == "async")

# Where the pending-findings JSONL lives. A relative path resolves against the
# config file's directory (the same rule ledger.path uses). The parent directory
# is created on first write.
pending_path = ".lazarus/pending.jsonl"          # str    (default shown)

# Spool directory for the launcher's extracted work-unit files (wu-<run_id>.txt)
# and the detached runner's stdout/stderr logs (log-<run_id>.txt). Relative ->
# resolved against the config dir.
spool_dir = ".lazarus/async"                     # str    (default shown)

# Force the offline deterministic stub judge inside the background runner (CI or
# no-key installs). Default false -> the runner uses the real judge, like sync.
stub_judge = false                   # bool               (default false)

[async.pregate]
# The OPTIONAL, opt-in synchronous shift-left gate (PreToolUse). Default OFF.
enabled = false                      # bool               (default false)
# Only findings at or above this confidence are surfaced by the pre-gate. It is
# set high on purpose to stay narrow and dodge the deep-recall noise problem.
min_confidence = 0.85                # float 0..1         (default 0.85)
# Hard cap on candidates the pre-gate judges synchronously, to bound the on-path
# latency it deliberately reintroduces. Kept tiny.
max_candidates = 3                   # int >= 1           (default 3)
```

Notes on the switch:

- **Absent `[async]` table** means every key takes its default. With the v2 hooks
  unwired, `LAZARUS_ASYNC` is unset, so `mode` auto-detects to `"sync"` and
  nothing changes. This is what keeps every existing v1 config loading identically.
- **`mode` and `enabled` must agree** if you set both. Setting `mode = "async"`
  and `enabled = false` is a fail-loud config error, not a silent tie-break. Set
  one, or set both consistently.
- **`pending_path` and `spool_dir`** follow the same relative-to-config-dir
  resolution as `ledger.path`. Their parents are created on first write, so you do
  not pre-create `.lazarus/`.

These map to flat accessors on `Config`, built exactly like the v1 `[judge]` and
`[ledger]` tables: `config.async_enabled`, `config.async_mode`,
`config.pending_path`, `config.async_spool_dir`, `config.async_stub_judge`,
`config.pregate_enabled`, `config.pregate_min_confidence`,
`config.pregate_max_candidates`. `_build_async` validates `mode ∈ {"async",
"sync"}`, cross-checks `enabled` against `mode`, resolves the two paths, and
applies the `LAZARUS_ASYNC` auto-detect only when neither `mode` nor `enabled` is
set. The CLI override keys `async_mode`, `pending_path`, and
`pregate_min_confidence` are recognized for parity with the other tables.

## Step C — Wire the v2 hooks (use `settings.snippet.v2.json`)

v2 ships its own snippet, `hooks/settings.snippet.v2.json`. The v1
`settings.snippet.json` stays untouched for sync installs. The v2 snippet sets
`LAZARUS_ASYNC=1` (so the config mode auto-detects to `"async"`) and wires the
launcher, the injector, and the commented-out pre-gate. Replace `{{LAZARUS_HOME}}`
and `{{LAZARUS_CONFIG_FILE}}` exactly as in Steps 2 and 3.

```json
{
  "env": {
    "LAZARUS_CONFIG": "{{LAZARUS_CONFIG_FILE}}",
    "LAZARUS_ASYNC": "1"
  },
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command",
        "command": "python {{LAZARUS_HOME}}/async_launcher.py --kind response",
        "timeout": 10,
        "statusMessage": "Lazarus (async): dispatching retro-audit off the critical path..." } ] }
    ],
    "PostToolUse": [
      { "matcher": "Edit|Write|NotebookEdit",
        "hooks": [ { "type": "command",
        "command": "python {{LAZARUS_HOME}}/async_launcher.py --kind diff",
        "timeout": 10,
        "statusMessage": "Lazarus (async): dispatching diff retro-audit off the critical path..." } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
        "command": "python {{LAZARUS_HOME}}/async_inject.py",
        "timeout": 10,
        "statusMessage": "Lazarus: surfacing last turn's retro-audit findings..." } ] }
    ]
  },
  "_optional_pretooluse_pregate": {
    "note": "OPT-IN. Move into hooks.PreToolUse to enable the synchronous high-confidence shift-left gate. Set [async.pregate].enabled = true in your config too.",
    "PreToolUse": [
      { "matcher": "Write|Edit",
        "hooks": [ { "type": "command",
        "command": "python {{LAZARUS_HOME}}/async_pregate.py --kind diff",
        "timeout": 30,
        "statusMessage": "Lazarus pregate: judging the planned action against buried rules..." } ] }
    ]
  }
}
```

Wiring notes:

- **Do not also wire `retro_audit.py` on `Stop` / `PostToolUse`.** In async mode
  the launcher owns those two events. If you merge the v2 `hooks` block into a
  settings file that still points `Stop` / `PostToolUse` at `retro_audit.py`, you
  will double-audit every turn (once blocking, once detached). Replace the sync
  retro-audit entries; keep only one auditor per event.
- **`SessionStart` is unchanged.** The boot sweep (`session_start_sweep.py`) is
  orthogonal to sync-vs-async and stays wired in both modes. The v2 snippet omits
  it only because you keep your existing `SessionStart` entry; append the async
  events to your merged `hooks` object rather than replacing the whole thing.
- **Launcher timeouts are 10s, not 60s.** The launcher only spawns-and-returns; it
  runs no Sonar and no judge. The full 60s judge budget now lives inside the
  detached child, off the critical path, where a timeout on the hook process no
  longer matters. The 10s cap is generous headroom for one `subprocess.Popen`.
- **`UserPromptSubmit` is the one event whose schema accepts `additionalContext`.**
  That is why the injector lives there and not on `Stop`.
- **The pre-gate is opt-in.** It sits under `_optional_pretooluse_pregate` (inert),
  so it does nothing until you move it into `hooks.PreToolUse` *and* set
  `[async.pregate].enabled = true`. See "The optional shift-left pre-gate" below.

## The pending-findings queue

`async_launcher.py` and the runner communicate with the injector through an
append-only JSONL file at `config.pending_path` — the async twin of the v1 ledger.
The ledger records judge **verdicts** for anti-nag suppression; the pending queue
records **surfaced findings awaiting injection** and whether they were consumed.
Two files, two jobs, one shared key: both are keyed on `(work_unit_sig, rule_id)`,
computed by the same reused `work_unit_signature`, so a finding the runner surfaces
and a verdict `run_lazarus` records for the identical work-unit line up exactly.

Each line is one finding:

```json
{
  "schema": 1,
  "ts": 1751655000.123,
  "event": "SURFACED",
  "run_id": "a1b2c3d4",
  "work_unit_sig": "<sha256>",
  "kind": "diff",
  "rule_id": "no-secrets-in-logs.md",
  "fix": { "rule_id": "...", "title": "...", "path": "...", "where": "...",
           "patch": "...", "reason": "...", "confidence": 0.9, "sonar_score": 0.7 }
}
```

The `fix` object is verbatim `RetroFix.as_dict()`. Storing the whole dict on the
`SURFACED` line means the injection hook needs no second lookup and no live
`Config` to render the finding — it reads the queue and formats. A later
`CONSUMED` line carries the same `work_unit_sig` + `rule_id` with `fix` empty or
omitted; state for a key is the **last** line written (last-line-wins), exactly as
`Ledger.state()` resolves.

Durability follows the ledger's documented trade: writes append and `flush()` the
Python buffer to the OS page cache but do not `os.fsync()` per line. The queue is
advisory, single-writer-per-process, and fully reconstructable, so a normal
process crash loses nothing already flushed; only a full OS/power crash can drop
the last not-yet-synced lines, and the worst case is one finding missed once or
one re-injected once. Neither corrupts the file.

## Dedup — two independent layers, both signature-keyed

Both layers key on `(work_unit_sig, rule_id)`, the exact ledger key:

1. **Anti-nag (inherited, free).** The runner passes the shared
   `Ledger(config.ledger_path)` into `run_lazarus(record=True)`. A rule already
   `DECLINED` for this signature is dropped before the judge, exactly as in v1. The
   async path reuses v1's suppression wholesale; it does not reinvent it.
2. **Queue dedup (new).** `PendingQueue.append` is a no-op if `(sig, rule_id)`
   already has ANY line in the queue (SURFACED or CONSUMED). Overlapping runner
   invocations — a fast Edit -> Edit that spawns two runners on diffs whose
   whitespace-normalized signatures collide — queue the same fix once. Because the
   signature is the reused `work_unit_signature` (normalized sha256), cosmetically
   different spawns of the same work collapse to one key.

## Consume protocol — emit-then-mark, at-most-once

The injector reads `read_unconsumed()` (current-state `SURFACED`, newest run
first), emits the findings on `additionalContext`, then appends `CONSUMED` lines.
A second inject run in the same or a later turn reads zero unconsumed and is a
silent no-op.

The order is emit-then-mark deliberately. If the harness discards the emitted
context (a crash between emit and the model seeing it), the marks are already
written and the finding will not re-surface. v2 chooses **at-most-once** over
exactly-once because re-nagging violates the v1 anti-nag contract, and a missed
advisory finding is recoverable — the underlying rule is still in the corpus and
re-surfaces on the next related edit. A stricter ack-based consume is deferred.

Nothing is lost on the normal path. The runner writes findings to disk with flush;
they persist across the turn boundary until an inject hook consumes them. If no
prompt arrives (the session ends), they simply remain `SURFACED`-unconsumed on
disk and surface on the next session's first prompt.

## Offline-testable, no key

The runner's `judge_fn` parameter is the same `JudgeFn` seam `run_lazarus` already
exposes, and the v1 demo's `stub_judge_fn` already conforms to it. So the entire
async cycle — launcher spawns runner, runner runs Sonar + Lazarus with the stub,
writes pending, inject reads and consumes — runs with no `anthropic` package and
no `ANTHROPIC_API_KEY`. Select the stub in either of two ways:

- `[async].stub_judge = true` in config (the launcher propagates it to the child
  as `--stub`), or
- `--stub` directly on `lazarus-audit-bg` for a manual run.

The stub is vendored into the package at `lazarus_sonar/async_/stub_judge.py` so
`--stub` works from an installed wheel where the demo directory is not on the
package path. `examples/demo/stub_judge.py` stays the source of truth, and a test
asserts the two produce byte-identical verdicts so they cannot drift. Run from a
checkout, the runner prefers `examples/demo/stub_judge.py` when present, exactly as
the demo does; the vendored copy is the installed fallback.

This makes the green-with-no-key async demo an executable assertion over the whole
v2 transport, the same way v1's `run_demo.py` is an assertion over the engine.

## Running the background runner by hand

The console script `lazarus-audit-bg` (added alongside the existing `lazarus`) is
what the launcher spawns detached. You can also run it directly to debug the async
path. It reads the work-unit from exactly one of three sources, checked in order:

```bash
# From the spool file the launcher writes (the production channel):
lazarus-audit-bg --work-unit-file /path/to/wu-<run_id>.txt --kind diff --config /path/to/lazarus.config.toml

# From stdin (manual / debug):
cat some.diff | lazarus-audit-bg --stdin --kind diff --stub

# From a raw Claude Code hook-event JSON, reusing the v1 extractor:
lazarus-audit-bg --event-file /path/to/event.json --stub
```

Flags: `--config <path>` (else `$LAZARUS_CONFIG` else walk-up), `--kind
diff|response`, `--stub` (offline deterministic judge), `--run-id <hex>`. Exit `0`
on a clean audit (0 fixes is clean). Exit `2` fail-loud on bad input, missing
config, or missing corpus. A judge fault exits `3` — loud and isolated, because the
parent has already returned so there is nothing to un-block; the queue simply gets
no new lines that run.

The launcher reads the work-unit from a **file**, not a pipe, because by the time
the detached child runs the parent hook has already exited and a stdin pipe would
be closed. File IPC is the correct channel here.

## Cross-platform detach

The launcher spawns the runner with `subprocess.Popen` and never calls `.wait()`,
`.communicate()`, or `.poll()` in a loop. It creates the `Popen` object and
returns. The child re-parents to init/system and outlives the hook:

- **Windows:** `creationflags = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`
  (`0x00000200 | 0x00000008`). `DETACHED_PROCESS` gives the child no console; the
  new process group keeps a Ctrl-C in the parent's console from reaching it.
- **POSIX:** `start_new_session=True` (setsid), so the child is not in the parent's
  process group or session and is not killed when the hook process exits.
- **Both:** `stdin=DEVNULL`; `stdout` and `stderr` redirect to a per-run log file
  under `spool_dir` (`log-<run_id>.txt`), never to a `PIPE` (an unread PIPE could
  fill and block the child, and reading it would block the parent). `close_fds=True`.

Latency contract: the launcher does file I/O plus one `Popen` and returns.
Measured budget is single-digit milliseconds. A background crash lands in
`spool_dir/log-<run_id>.txt`, inspectable but never on the parent's console.

## The optional shift-left pre-gate

`async_pregate.py` is the one place v2 puts a judge call back **on** the critical
path, so it is triple-constrained and OFF by default. It runs a synchronous
Sonar+Lazarus on the *planned* `Write`/`Edit` action, surfacing only the
highest-confidence rules to prevent rather than patch. Enable it deliberately, and
only after the async retro-audit has earned your trust on your corpus.

To enable: move the `PreToolUse` block from `_optional_pretooluse_pregate` into
`hooks.PreToolUse` in your settings, and set `[async.pregate].enabled = true`.

Four constraints keep it a scalpel, not a firehose:

1. **Default OFF** — `[async.pregate].enabled = false`. Opt-in only, commented-out
   in the snippet, gated behind reading these docs.
2. **Candidate cap before the judge** — `pregate_max_candidates`, default **3**.
   Sonar's shortlist is truncated to the top 3 by score *before* `run_lazarus`, so
   the synchronous judge call is bounded to a tiny batch. This bounds latency and
   noise: the deep tail of Sonar recall never reaches the judge here.
3. **High confidence floor after the judge** — `pregate_min_confidence`, default
   **0.85**, well above the judge's normal `min_confidence` of 0.6. Only
   near-certain "this WILL change the output" verdicts surface; merely-plausible
   matches are dropped, which is precisely the class that makes shift-left gates
   noisy.
4. **`record=False`** — the pre-gate does not write the ledger, so it cannot
   suppress or pre-empt the authoritative async retro-audit that runs on the same
   work moments later. The async path stays the source of truth; the pre-gate is a
   thin, high-precision safety catch.

The pre-gate surfaces context but does **not** hard-block by default (decision
`allow` plus an `additionalContext` warning). A judge error defaults to **allow**,
never block — the same posture as the v1 optional gate. A strict-blocking mode is
deferred.

## v2 fail-loud vs fail-safe boundary

The v1 fail-loud discipline is re-applied per v2 hook, with the async path's prime
directive layered on top: **never wedge a turn**.

- **Launcher** (`async_launcher.py`): fail-**loud** on misconfig (bad or missing
  config) via v1's `_fail_loud` (stderr + exit 2), but this is still non-blocking
  for the turn (a `PostToolUse` exit 2 does not block; `Stop` already emitted no
  `additionalContext`). An empty extraction or a disabled mode is a quiet,
  non-blocking no-op — unlike the sync hook, which fails loud on empty, because an
  async miss is invisible to the user and a loud error on every keystroke-less
  `Stop` would be noise. The launcher runs no Sonar and no judge, so it has almost
  no failure surface.
- **Runner** (the detached child): fail-**loud to its own log file** — exit 2 on
  misconfig, exit 3 on a judge fault. Off the critical path, "loud" means
  "inspectable in `spool_dir/log-<run_id>.txt`", not "on the user's console". A
  judge fault simply yields no new pending lines: it degrades to silence.
- **Inject** (`async_inject.py`): fail-**safe** always. No findings, no queue, or
  any read error is a silent no-op with a clean exit 0. This is the one hook on the
  user's prompt path, so it must never wedge a keystroke; it is the one hook that
  deliberately swallows everything.
- **Pre-gate** (`async_pregate.py`): fail-**safe toward ALLOW**. A judge error
  defaults to allowing the action, never blocking it — matching the v1 optional
  gate contract.

## Async smoke test (no key)

Prove the whole v2 transport offline, the same way `run_demo.py` proves the engine:

```bash
# 1. Run the background runner on the demo diff with the stub judge, writing pending:
lazarus-audit-bg --work-unit-file examples/demo/work_unit.diff --kind diff --stub \
  --config examples/demo/lazarus.config.toml

# 2. Inspect the queue: two SURFACED findings, zero CONSUMED.
cat .lazarus/pending.jsonl    # or wherever [async].pending_path points

# 3. Simulate the injector reading + consuming (a second read is then silent):
python {{LAZARUS_HOME}}/async_inject.py < /dev/null
```

Expected: after step 1 the queue holds the same two findings the sync demo
surfaces (`no-secrets-in-logs.md`, `timeout-on-external-calls.md`), both
`SURFACED`; `prefer-f-strings.md` is `DECLINED` in the ledger and never queued.
After the injector runs once, a second run reads zero unconsumed and emits nothing
— the at-most-once consume in action. All of it runs with no `anthropic` package
and no `ANTHROPIC_API_KEY`.

## What is v2, and what is deferred

**v2 (buildable now, wired by the steps above):** the pending-findings queue; the
background runner plus the `lazarus-audit-bg` console entrypoint; the non-blocking
launcher; the injection hook; the opt-in pre-gate; the `[async]` /
`[async.pregate]` config; the v2 settings snippet; the vendored stub for offline
`--stub`; the offline async-cycle smoke test.

**Deferred (named, not built):**

- exactly-once, ack-based consume (mark only after the model confirms receipt) —
  v2 ships at-most-once by choice;
- a spool-file / pending-line GC or retention policy — v2 leaves `wu-*.txt` and
  `CONSUMED` lines on disk; a `lazarus async gc` command is future work;
- a hard-blocking pre-gate mode — v2's pre-gate surfaces context but defaults to
  allow;
- multi-writer file locking on the pending queue — v2 relies on the same
  single-writer-per-process, append-atomic, flush-not-fsync trade the v1 ledger
  documents; concurrent runners are made safe by dedup, not by locks;
- semantic (cross-signature) dedup of findings;
- a Windows Job Object to auto-kill orphaned runners — POSIX/Windows detach is
  sufficient for v2, and runners are short-lived and self-terminating.

Each deferral is a byproduct-of-building item, consistent with v1 having shipped
`max_file_kb` / `request_timeout_s` as accepted-but-ignored forward-compat knobs.
