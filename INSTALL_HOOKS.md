# Installing the Lazarus/Sonar hooks

This document wires Lazarus/Sonar into Claude Code's deterministic hook layer. The
CLI (`lazarus sonar|audit|ledger`) is the whole tool and needs none of this; the
hooks are an opt-in that makes the retro-audit run automatically on the work an
agent just finished, instead of when a human remembers to ask.

Three placements map to three Claude Code events:

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
   example file for the full schema.

## Step 1 — Copy the hooks somewhere stable

The hooks can run from the repo's `hooks/` directory or from any directory you
copy them to. Pick one absolute location and keep the two hook files together:

```
<somewhere>/session_start_sweep.py
<somewhere>/retro_audit.py
```

Both files must be reachable at the absolute path you put in `settings.json`. If
you copy them out of the repo and you did NOT `pip install lazarus-sonar`, the
`src/`-fallback no longer applies (the copies are no longer next to `src/`), so
install the package in that case. Installing is the simplest choice either way.

Call the directory holding these two files your **`{{LAZARUS_HOME}}`**.

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
guessing stops.

### How discovery actually resolves

Each hook resolves its config in this order, and this is worth understanding
because it governs the failure modes:

1. `--config <path>` on the hook's command line (present on both hooks for manual
   runs; not set by the snippet).
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

The hooks themselves always use the real judge (they call `run_lazarus` with no
`judge_fn`, so it binds to `judge.judge_batch`). The stub is a
demo/test path, not a hook mode — but it is how you prove the pipeline before you
turn the real judge on.

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
precision on your corpus.
