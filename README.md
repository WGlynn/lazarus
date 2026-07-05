# Lazarus + Sonar

Every memory, primitive, skill, rule, and hook you give an agent is a piece of hard-won wisdom. The trouble is what happens at scale. Once there are thousands of them, no agent can hold them all in mind before it acts, so the older ones get buried. They are still true and still relevant, they just never get retrieved. That is the data-availability problem, and it is one of the quietest killers of behavioral and performance consistency: the thousands of artifacts you invested in are forced to compete for a fixed slice of attention every turn, and most of what they should have contributed is lost. Acting becomes a shot in the dark, blind to the one rule that should have fired.

Lazarus takes the other side of the bet. Instead of trying to consult everything before the agent acts, it lets the work finish, then re-reads it and asks the buried rules a single question: would this one have changed the result? Only the rules that would actually change something ever surface.

A knowledge-audit tool for Claude Code and any agent that keeps its rules,
primitives, and memory in files. It works both ways from one corpus. Proactively,
it surfaces the buried-but-still-valid rules relevant to work you are about to do.
Retroactively, it re-reads the work just finished and asks of each buried rule:
would this have changed the output? It surfaces what matters as up-front context or
as proposals. It applies the fixes it can place unambiguously, reversibly (one
`lazarus undo` restores any edit), and surfaces the rest.

Two organs:

- **Sonar** is perception. It sweeps a large corpus of rule files, keyword-scores
  each one against a unit of work, and returns a ranked shortlist. Wide, cheap,
  high recall. This is the firehose stage and its raw output is never shown to a
  human.
- **Lazarus** is judgment. It takes Sonar's shortlist and applies one precision
  filter per candidate: "would applying this buried rule have changed the
  finished work?" It kills the on-topic-but-inert matches and emits a ranked list
  of retroactive fixes, each with the span it would improve and a concrete
  proposed patch.

Sonar runs with zero third-party dependencies and no API key. Lazarus needs a
Claude API key for the judge model.

**v2 adds concurrency.** v1 runs the audit as a blocking `Stop`/`PostToolUse`
hook, so it sits on the critical path and every operation pays the judge-latency
tax even when nothing is caught. v2 moves the audit off the critical path: a
non-blocking launcher spawns a detached background runner, and the findings are
surfaced on the next turn. The v1 sync path is untouched and stays authoritative
when selected. See [v2: Concurrency (async retro-audit)](#v2-concurrency-async-retro-audit).

## Proactive and retroactive

The same engine runs in both directions, from one corpus. That balance is the point:
preventative care and retroactive care, not half of either.

- **Proactive (prevent).** `lazarus prime` points the recall at work you are *about*
  to do (a prompt, a plan, a diff) and surfaces the relevant rules up front, so a
  buried rule is available before the work, not after. The opt-in PreToolUse pre-gate
  is the stricter form that warns on a high-confidence violation before the action runs.
- **Retroactive (cure).** `lazarus audit` re-reads finished work and asks whether a
  buried rule would have changed it, catching the miss after the fact.

The retroactive verdicts (the ledger) can inform what to prime next, so the two
directions feed each other rather than duplicating work.

Honest boundary: if your rules fit in `CLAUDE.md`, use `CLAUDE.md`. Static context is
simpler and just as good at small scale. Proactive Lazarus earns its place when the
corpus outgrows what you can statically load every turn (the point at which a fixed
context blob starts truncating), and its edge over a hand-rolled retrieval hook is
that one corpus, one judge, and one ledger serve both directions with a feedback loop
between them.

## The problem

Every serious agent's knowledge base grows past the point where all of it can sit
in context at once. Rules written a hundred days ago are still true, but they get
buried under newer material and never fire again. The agent silently repeats a
mistake that an old rule already solved, because nothing put that rule back in
front of it at the moment it mattered.

The usual response is passive relevance-surfacing: cosine similarity or keyword
matching that shows you "related" rules alongside your work. In a corpus of a few
hundred rules this produces a firehose. It is noisy, it is mostly wrong, and
people learn to ignore it. Recall was never the hard part.

## The crux: precision, not recall

The hard part is deciding which of the surfaced rules would actually have
mattered. That is the whole value of this tool, and it is worth being blunt about
where the value lives and where it does not.

- Sonar's keyword sweep is deliberately high-recall and noisy. On its own it is
  the same firehose everyone already has. It is not the product.
- Lazarus is the product. Its single test is "would applying this rule have
  changed the finished work?" The judge is instructed to default to NO and to
  reject rules that are on-topic but inert. That instruction is the precision
  filter. It is also the no-false-pattern-matching gate: a rule about logging that
  is thematically near a diff, but would not have changed a single line of it, is
  killed, not surfaced.
- The ledger is what makes the filter livable over time. Every verdict is
  recorded. Once a rule has been judged irrelevant for a given unit of work, or a
  surfaced fix has been dismissed by a human, it is never surfaced again for that
  same work. This is the anti-nag property. Without it, a retro-audit that runs on
  every turn would re-surface the same dead matches forever and get muted like
  everything before it.

Framed differently: this is a data-availability and liveness tool for agent
memory. It keeps buried-but-valid knowledge live, and it makes the agent
self-audit its finished work against that knowledge instead of trusting that the
right rule happened to be in context.

## Applying fixes

Lazarus applies fixes automatically, by default. When a fix carries a concrete,
uniquely-locatable edit, Lazarus applies it and backs up the original first, so a
single `lazarus undo` reverts it. Automatic application is the default because the
reward (a rule that would have been missed is instead honored, with no human in the
loop and no latency) far outweighs the risk when every edit is reversible and only
an exact, unambiguous match is ever touched. A fix with no concrete edit, or whose
target text is missing or ambiguous, is surfaced as a proposal instead of forced,
never guessed. Turn it off with `[apply] auto_apply = false` and auto-apply reverts
to surface-only. (How much gets applied vs surfaced tracks how often the judge emits
a concrete edit; that coverage is expanding.)

## Install

The base package (Sonar, config, and the ledger) is stdlib-first and works on any
supported interpreter with no extra steps. Python 3.11 and newer read TOML with
the standard-library `tomllib`. On 3.9 and 3.10 the base install pulls in a small
`tomli` backport automatically, because it is a marker-gated base dependency, not
an optional extra. A clean `pip install lazarus-sonar` therefore imports and runs
on a fresh 3.9 interpreter with no additional flags.

```bash
# Perception + ledger only. Stdlib-first, no API key, offline.
# On 3.9-3.10 this transparently pulls in the tomli backport.
pip install lazarus-sonar

# With the Claude judge (Lazarus retro-audit).
pip install "lazarus-sonar[judge]"
```

`requires-python` is `>=3.9`. `[judge]` pulls in the `anthropic` SDK and is the
only optional dependency: it is needed exclusively for the Lazarus judge call. If
you only want Sonar shortlists and the ledger, install the base package and you
never need a key or a network.

The v2 async layer adds no dependencies. It is stdlib-only (`subprocess`, `json`,
`pathlib`, `argparse`, `uuid`), and its offline mode reuses the same
credential-free stub judge the v1 demo ships. Installing the base package is
enough to run the entire async cycle with `--stub`.

## Configure

Copy the example config and point it at your own corpus. Nothing is hardcoded to
any project.

```bash
cp lazarus.config.example.toml lazarus.config.toml
```

```toml
[corpus]
# Where your rule / primitive / memory files live. Required, no default.
path = "~/.claude/memory"
# Which files count as rules. Required.
globs = ["**/*.md"]
# Optional: paths to skip.
exclude = ["**/archive/**", "**/_index.md"]

[sonar]
# Keyword-overlap floor. Candidates below this are dropped before ranking.
# This is a low positive floor, not a token count: because idf damping makes
# raw scores corpus-relative, a value near zero keeps Sonar maximally wide and
# lets Lazarus do the cutting. The in-code default is 0.0 (a single shared token
# or any structural boost admits a candidate); 0.05 here trims pure-noise
# zero-overlap files while staying permissive.
min_score = 0.05
# How many candidates Sonar hands to Lazarus.
top_n = 20
# Structural boosts, both configurable.
title_boost = 2.0   # work-unit tokens appearing in a rule's title/filename
path_boost  = 1.5   # work-unit tokens appearing in a rule's path
# Damping factor so tokens present in almost every file (e.g. a project name)
# do not dominate rank. This changes ordering, not membership, so recall is
# preserved. Default: true.
idf_damping = true
# Corpus-specific noise words to ignore, on top of the built-in stopword list.
extra_stopwords = []

[judge]
# The main quality knob. Precision tracks this model.
model = "claude-opus-4-8"
# Verdicts below this confidence are dropped from the fix list.
min_confidence = 0.6
# Upper bound on judge candidates considered per audit.
max_candidates = 15
# Token budget for the batched judge response.
max_tokens = 4096

[ledger]
# Append-only JSONL. Signatures and verdicts live here.
path = ".lazarus/ledger.jsonl"
# Drop already-declined rules before the judge runs (anti-nag). Default: true.
suppress_declined = true
```

Relative paths in the config are resolved against the config file's own
directory, so `.lazarus/ledger.jsonl` lands next to your `lazarus.config.toml`
regardless of where you invoke the CLI from.

`config.py` fails loud: a missing `corpus.path` or `corpus.globs` raises a clear
error. It never silently falls back to scanning your home directory or the
current working directory. If you point it at nothing, you get an error, not a
surprise sweep of your whole disk.

The v2 `[async]` and `[async.pregate]` tables are entirely optional and additive.
An absent `[async]` table means all defaults, so every existing v1
`lazarus.config.toml` loads byte-for-byte the same. Those keys are documented in
[v2 config: the `[async]` table](#v2-config-the-async-table).

## Usage

There are two ways to run it: the CLI, and the deterministic hook layer. The CLI
is the whole tool. The hooks are opt-in.

### CLI

```bash
# Perception only: what would Sonar surface for this diff? (no API call)
git diff | lazarus sonar --stdin --kind diff

# Full retro-audit: Sonar -> Lazarus judge -> ranked proposed fixes.
git diff | lazarus audit --stdin --kind diff
lazarus audit --file response.txt --kind response

# Ledger.
lazarus ledger show                                       # everything
lazarus ledger show --status SURFACED                     # filter by status
lazarus ledger show --work-unit-sig <sig>                 # scope to one work unit
lazarus ledger action --work-unit-sig <sig> --rule-id <id>   # record that you applied a fix
lazarus ledger decline --work-unit-sig <sig> --rule-id <id>  # dismiss; never re-surfaced for this work
```

`lazarus sonar` never calls the judge and needs no key. `lazarus audit` runs the
full pipeline and needs `ANTHROPIC_API_KEY`. `--kind` tells the tool how to read
the work unit (`diff`, `response`, `decision`, or the default `generic`); it
weights structural signals in the scorer and the judge prompt. `--stdin` and
`--file` are the two mutually exclusive input paths, and exactly one is required.
`--top-n` on `sonar` and `audit` caps the shortlist, overriding the configured
`top_n`. `--config`, `--corpus`, `--glob` (repeatable), and `--ledger-path` are
the config overrides available on every subcommand; `--json` emits machine
-readable output instead of the human text rendering.

The ledger subcommands take flags, not positional arguments: `--work-unit-sig`
and `--rule-id` are required on `action` and `decline`, `--note` is optional, and
`show` accepts `--work-unit-sig` and `--status` filters.

A retro-audit prints the survivors: for each rule that passed the
would-it-change-the-output test, the rule ID, where in the finished work it
applies, a proposed patch, the judge's confidence, and its one-line reason.
Killed candidates do not print; they land in the ledger as `DECLINED`.

v2 adds one more console script, `lazarus-audit-bg`, the detached background
runner. You do not normally invoke it by hand: the launcher hook spawns it. It is
documented under [The background runner](#the-background-runner) for debugging.

### Hooks (opt-in)

Placement is architecture, not a suggestion. The value of a retro-audit is that
it runs at the deterministic layer on the work that was just finished, not when a
human remembers to ask. Three placements map to real Claude Code events:

- **BOOT** — a `SessionStart` hook runs a Sonar sweep over the last session's
  work and prints the buried-rule candidates as boot context.
- **HOOK** — a `Stop` / `PostToolUse:Edit|Write` hook runs the Lazarus
  retro-audit on the just-finished work unit and prints the surviving fixes.
- **GATE** — an optional `PreToolUse:Edit|Write|Agent` hook surfaces and judges
  relevant buried rules *before* a write happens. This is the riskiest placement
  (it blocks an action on a judge call), so v1 ships it as documented guidance,
  not a hardened blocking hook.

Nothing installs itself into your `settings.json`. Merging the snippet is a
copy-paste step you do on purpose. The snippet sets `LAZARUS_CONFIG` to an
explicit, absolute path placeholder (`{{LAZARUS_CONFIG_FILE}}`) rather than
guessing a location relative to the hooks directory. `INSTALL_HOOKS.md` documents
exactly what to substitute for the two placeholders (`{{LAZARUS_HOME}}`, the
absolute `hooks/` directory, and `{{LAZARUS_CONFIG_FILE}}`, the absolute path of
your `lazarus.config.toml`). If `LAZARUS_CONFIG` is unset the hooks still
discover the config by walking up from the current directory, but the env var is
the reliable path and is documented as such. See `hooks/settings.snippet.json`
and `INSTALL_HOOKS.md`.

The hooks fail loud on missing input — no config, no corpus, no key, an
unparseable work unit: they print a visible error to stderr and exit non-zero.
The one deliberate exception: a judge, model, or network error inside a `Stop` or
`PostToolUse` hook must not wedge your session. It prints the error loudly and
exits without blocking the turn (emitting `{}` on the `Stop` event, which is the
only shape that event's schema accepts). Loud, but never wedging.

The v2 async hooks (`async_launcher.py`, `async_inject.py`, `async_pregate.py`)
are wired through a separate snippet, `hooks/settings.snippet.v2.json`, and are
described in [v2 hooks](#v2-hooks-launcher-injector-pre-gate). The v1 sync snippet
stays untouched; you run one path or the other, never both.

## How it works

### Sonar ranking

Local, explainable, no embeddings in v1. Sonar lowercases the work unit, splits
on non-word characters, drops stopwords, and does the same to each corpus file.
It scores each file by keyword overlap (a TF-lite intersection) plus two
structural boosts: tokens that also appear in the rule's title or filename, and
tokens that appear in its path. An idf damping factor keeps ubiquitous tokens
from dominating rank without cutting recall, since it changes ordering rather than
membership. It returns the top-N candidates above `min_score`. The scorer is a
single function, so swapping in embeddings later is a drop-in change behind the
same interface. Embeddings are deferred on purpose, to keep v1 dependency-light
and runnable offline.

### The judge

One Claude call per audit, batched across all surviving candidates rather than one
call per rule, to bound cost and latency. The model defaults to `claude-opus-4-8`
with adaptive thinking; the judge is precision-sensitive, so it gets the strong
model by default, and `judge_model` is the documented quality knob. Verdicts come
back as structured output, one record per candidate: `rule_id`, `would_change`
(bool), `where`, `patch`, `confidence`, `reason`. Parsing is schema-validated, not
regex-on-prose.

The prompt asks exactly one question — "would applying this buried rule have
changed the finished work?" — with an explicit instruction to default to NO and
to reject rules that are on-topic but inert. That instruction is the precision
filter.

### The ledger and anti-nag

Append-only JSONL, keyed on `(work_unit_sig, rule_id)`. The `work_unit_sig` is a
SHA-256 of the normalized work unit, so re-running the audit on the same diff
collapses to one signature. Before the judge runs, Lazarus drops any candidate
whose `(sig, rule_id)` is already `DECLINED` — so a rule judged irrelevant for
this work, or a surfaced fix a human dismissed, is never sent to the judge or
surfaced again for that same work.

This is signature-scoped, not a permanent per-rule mute. Substantially different
work produces a different signature and gets a fresh look at every rule. A rule
you dismissed for one diff will be reconsidered for the next, different diff.

The ledger is written on the hot path — once per finished turn and once per
`Edit`/`Write`. Each write flushes to the OS page cache, so a crash of the Python
process loses nothing, but it does not `fsync` to disk per record, so a full OS or
power crash can lose the last few unsynced lines. That trade is deliberate: the
ledger is advisory, append-only, single-writer, and reconstructable. Its only job
is anti-nag suppression, and the worst case from a lost trailing line is that one
already-judged rule re-surfaces once. Paying an `fsync` stall on every turn to
avoid that is the wrong trade for a per-turn hook.

## v2: Concurrency (async retro-audit)

v1 runs the whole audit on the critical path. The `Stop`/`PostToolUse` hook calls
Sonar, then the judge, then writes the ledger, and only then does the turn end.
The judge is a network round-trip, so every finished turn pays that latency even
on the common case where nothing survives. That is the tax v2 removes.

The Claude Code loop is sequential, so v2 does not invent parallelism inside a
turn. It borrows concurrency from the operating system. A non-blocking launcher
hook captures the finished work-unit, spawns a detached background process, and
returns in single-digit milliseconds. The detached process runs the identical v1
pipeline off the critical path, writes survivors to a pending-findings queue, and
exits. On the next prompt, an injection hook reads the queue, surfaces the
findings as context, and marks them consumed. The judge latency is now hidden
behind the agent's own thinking time, and only real catches ever reach the human.

### Anti-drift: v2 reuses the v1 engine, it does not fork it

This is the load-bearing property of the whole design. There is exactly one place
that audits (`run_lazarus`), one signature function (`work_unit_signature`), one
`Candidate` type, one scorer (`run_sonar_for_config`), one judge seam (`JudgeFn`),
and one result type (`RetroFix`/`AuditResult`). v2 imports every one of them and
calls them with the same arguments the v1 sync hook uses. The background runner is
literally the sync hook's two lines (Sonar then Lazarus) plus a queue drain. v2
adds a transport, not a second engine. None of the v1 engine files
(`sonar`, `lazarus`, `judge`, `ledger`, `config`) are edited; the async code lives
in a new namespace, `lazarus_sonar.async_` (trailing underscore, because `async`
is a Python keyword), plus three new `hooks/async_*.py` files.

### The two paths are mutually exclusive, selected by config mode

v2 is a config *mode*, not a rewrite. `config.async_enabled` decides which path
is live:

- **`mode = "sync"` (or unwired):** the v2 launcher is a no-op that emits the
  non-blocking payload and exits. You keep the v1 `retro_audit.py` on
  `Stop`/`PostToolUse`. Behavior is byte-for-byte v1.
- **`mode = "async"` (the default when the v2 hooks are wired, detected via
  `LAZARUS_ASYNC=1` in the v2 settings snippet):** the launcher dispatches the
  detached runner, and `async_launcher.py` *replaces* `retro_audit.py` on
  `Stop`/`PostToolUse`. You do not run both. Running both would double-audit the
  same work.

The mode auto-detects to `"async"` when wired and falls back to `"sync"`
otherwise; a bare `mode`/`enabled` key in the config overrides the auto-detect.

### The pending queue: the async twin of the ledger

The pending queue (`lazarus_sonar.async_.pending`) is an append-only JSONL file,
one finding per line, keyed on `(work_unit_sig, rule_id)` — the exact dedup key
the ledger uses. It is a deliberate mirror of the ledger, with a different job.
The ledger records judge *verdicts* for anti-nag suppression. The pending queue
records *surfaced findings awaiting injection* and whether they were consumed. Two
separate files, two separate jobs, one shared key shape and one shared durability
trade (flush to the page cache, no `fsync` per line, because the log is advisory,
single-writer-per-process, and reconstructable — identical rationale to the
ledger).

Each line carries the whole finding, including `fix`, which is a verbatim
`RetroFix.as_dict()` (rule_id, title, path, where, patch, reason, confidence,
sonar_score). Storing the full dict means the injection hook needs no second
lookup and no live `Config` to render: it reads the queue and formats. The line
schema:

```jsonc
{
  "schema": 1,                          // PENDING_SCHEMA_VERSION; readers ignore unknown fields
  "ts": 1751655000.123,                 // epoch seconds, round(.,3)
  "event": "SURFACED",                  // or "CONSUMED"; append-only, last line for a key wins
  "run_id": "a1b2c3d4",                 // 8-hex; the runner invocation that produced this finding
  "work_unit_sig": "<sha256>",          // work_unit_signature(work_unit); identical to the ledger sig
  "kind": "diff",                       // or "response"; the v1 work-unit kind, carried through
  "rule_id": "no-secrets-in-logs.md",   // POSIX-relative corpus id from Candidate.rule_id
  "fix": { "...RetroFix.as_dict()...": true }
}
```

A `CONSUMED` line carries the same `work_unit_sig` and `rule_id` with an
empty-or-omitted `fix` (the `SURFACED` line already holds the payload). State is
last-line-wins, exactly like `Ledger.state()`. The public surface is
`PendingQueue.append` / `append_many` (writer side, both dedup-safe),
`read_unconsumed` (reader side, newest run first, deterministic tiebreak on
`(confidence desc, rule_id)`, missing file returns `[]`), `mark_consumed`
(idempotent consume step), plus `state()` and `counts()` for introspection. Its
path comes from `config.pending_path`, never a hardcoded location.

### The background runner

`lazarus_sonar.async_.runner.run_background_audit` is the one function that calls
the engine. It is the v1 retro-audit's two lines, verbatim:

```python
candidates = run_sonar_for_config(work_unit, config, kind=kind)
result     = run_lazarus(work_unit, candidates, config=config,
                         ledger=ledger, judge_fn=judge_fn, kind=kind, record=record)
```

Then, new in v2, it drains `result.fixes` into the pending queue, reusing the
signature the engine already computed rather than recomputing it:

```python
sig = result.work_unit_sig            # == work_unit_signature(work_unit)
rid = run_id or uuid.uuid4().hex[:8]
queue.append_many(
    PendingFinding.from_retrofix(f, work_unit_sig=sig, kind=kind, run_id=rid)
    for f in result.fixes
)
```

It returns the `AuditResult` unchanged so tests can assert on it directly. The
`judge_fn` parameter is the same `JudgeFn` seam `run_lazarus` exposes: `None`
selects the real judge (needs `[judge]` and a key); the demo and tests inject the
stub and run with no key at all. The shared `Ledger(config.ledger_path)` is passed
in with `record=True`, so the async path inherits v1's anti-nag suppression
wholesale (a rule already declined for this signature is dropped before the
judge). Empty work-unit or missing corpus propagate as v1 fail-loud.

The console entrypoint is `lazarus-audit-bg`, added to `[project.scripts]`
alongside the existing `lazarus`:

```
lazarus = "lazarus_sonar.cli:main"
lazarus-audit-bg = "lazarus_sonar.async_.runner:main"
```

This is what the launcher spawns detached. It reads the work-unit from one of
three mutually exclusive sources, checked in order: `--work-unit-file <path>`
(the launcher's spool file, the normal path), `--stdin` (manual debug), or
`--event-file <path>` (a raw hook-event JSON re-parsed with the shared v1
extractor). Flags: `--config` (else `$LAZARUS_CONFIG` else walk-up), `--kind`,
`--stub` (offline deterministic judge), `--run-id`. Exit codes: 0 on a clean
audit (zero fixes is still clean), 2 fail-loud on bad input or missing
config/corpus, 3 on a judge fault. A judge fault is loud but isolated: the parent
turn already returned, so there is nothing to un-block, and the queue simply gets
no new lines this run.

Offline mode is first-class, not a test hack. `--stub` makes the runner import the
same deterministic judge the v1 demo ships. Because the demo directory is not on
the installed package path, v2 vendors a thin re-export at
`lazarus_sonar/async_/stub_judge.py` (a verbatim logic copy of
`examples/demo/stub_judge.py`), so `--stub` works from an installed wheel. The
demo file stays the source of truth, and a test asserts the two produce
byte-identical verdicts so they can never drift. Run from a checkout, `main`
prefers the demo file when present, exactly as the demo does; the vendored copy is
the installed fallback.

### v2 hooks: launcher, injector, pre-gate

**Launcher — `hooks/async_launcher.py` (`Stop` / `PostToolUse`, non-blocking).**
It reuses v1's `retro_audit.extract_work_unit`, `_event_name`,
`_emit_nonblocking`, and `_fail_loud` so it parses hook payloads identically to the
sync path. That shared extraction is what guarantees the same event yields the
same `work_unit_sig` on both paths, so the ledger and pending keys line up. It
resolves the config only to fail loud early on misconfig and to locate the spool
dir; it runs no Sonar and no judge. If the mode is `sync`, or the extraction is
empty, it emits the non-blocking payload and returns. Otherwise it writes the
work-unit to a spool file (`spool_dir/wu-<run_id>.txt`) and spawns the detached
runner with `--work-unit-file`. It passes a file, not a pipe, because by the time
the child reads it the parent hook has already exited and any stdin pipe would be
closed. The launcher never calls `.wait()`, `.communicate()`, or `.poll()` in a
loop. It does file I/O plus one `subprocess.Popen` and returns. The measured
budget is single-digit milliseconds, which is why the launcher's hook timeout is
10 seconds, not the 60-second judge budget that now lives inside the detached
child.

The detach is OS-level and cross-platform:

- **Windows:** `creationflags = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`. No
  console for the child; the new process group stops the parent's Ctrl-C from
  reaching it.
- **POSIX:** `start_new_session=True` (setsid), so the child leaves the parent's
  process group and survives the hook's exit.
- **Both:** `stdin=DEVNULL`; stdout/stderr redirected to a per-run log file under
  the spool dir (never `PIPE`, which would fill and could block the child, and
  reading which would block the parent); `close_fds=True`.

`hooks/async_runner_entry.py` is a three-line shim that puts `src/` on the path
and calls `lazarus_sonar.async_.runner.main()`, so the detached child works from a
plain checkout with no install, mirroring the v1 hooks' import bootstrap. When
installed, the launcher can spawn the `lazarus-audit-bg` console script directly;
the entry shim is the checkout fallback.

**Injector — `hooks/async_inject.py` (`UserPromptSubmit`, fail-safe).**
`UserPromptSubmit` is the one Claude Code event whose schema accepts
`additionalContext`, which is why the surfacing happens here. It reads
`read_unconsumed()` (current-state `SURFACED`, newest run first), formats them as
a human-readable block under a header that states clearly these are asynchronous
retro-audit findings from the previous turn that were surfaced rather than auto-applied, with a footer that
points at `lazarus ledger action/decline <sig> <rule_id>` in wording identical to
the v1 renderer so the human hears one voice. It emits the context, then marks the
findings consumed. This hook is fail-safe on everything: no findings, no queue,
misconfig, or any read error is a silent no-op. It is on the user's prompt path
and must never wedge a keystroke. It is the one hook that swallows all errors, on
purpose.

The consume protocol is emit-then-mark, which is deliberately at-most-once. A
second inject run reads zero unconsumed and stays silent, so a finding never nags
twice. If the harness drops the emitted context between emit and the model seeing
it, the marks are already written and the finding will not re-surface. v2 chooses
at-most-once over exactly-once because re-nagging violates the v1 anti-nag
contract, and a missed advisory finding is recoverable: the underlying rule is
still in the corpus and re-surfaces on the next related edit. A stricter,
ack-based consume is named and deferred. Findings that no prompt ever consumes
(the session just ends) stay `SURFACED` on disk and surface on the next session's
first prompt. Nothing is lost on the normal path.

**Pre-gate — `hooks/async_pregate.py` (`PreToolUse`, opt-in, synchronous).**
This is the one place v2 puts a judge call back on the critical path, so it is off
by default and triple-constrained. It extracts the *planned* work-unit from the
`PreToolUse` `tool_input` (reusing `retro_audit._diff_from_edit` /
`_content_from_write`), runs a bounded Sonar+Lazarus on it, and surfaces only the
highest-confidence rules to prevent rather than patch. It stays narrow by design:

1. **Default OFF** (`[async.pregate].enabled = false`), opt-in, commented out in
   the settings snippet.
2. **Candidate cap before the judge** (`pregate_max_candidates`, default 3): the
   Sonar shortlist is truncated to the top few by score *before* the judge, which
   bounds both latency and noise, since the deep tail of Sonar recall never
   reaches the judge here.
3. **High confidence floor after the judge** (`pregate_min_confidence`, default
   0.85, well above the judge's normal 0.6): only near-certain "this will change
   the output" verdicts survive.
4. **`record=False`**: the pre-gate does not write the ledger, so it cannot
   suppress or pre-empt the authoritative async retro-audit that runs on the same
   work moments later. The async path stays the source of truth.

It surfaces context as a `PreToolUse` decision but does not hard-block by default
(decision `allow` plus an `additionalContext` warning). A judge error defaults to
allow, never block, matching v1's optional-gate posture. A strict-blocking mode is
deferred. The pre-gate is a scalpel for the rare, unambiguous, high-value case
(about to write a logged secret), which is the only class where "prevent, do not
patch" pays for its latency.

### v2 config: the `[async]` table

Additive. A frozen `AsyncConfig` sub-object with flat accessors, built exactly
like the v1 `[ledger]` and `[judge]` tables (same `_optional_table`, `_bool`,
`_number`, `_positive_int`, `_resolve_path`, `_string_tuple` helpers, and the same
`_build_*` shape). A missing `[async]` table means all defaults, so v1 configs are
unaffected.

```toml
[async]
# Master switch. "async" runs launcher+runner+inject; "sync" makes the launcher a
# no-op so the v1 blocking retro-audit path is authoritative. The default is
# chosen at load time: "async" when the v2 hooks are wired (LAZARUS_ASYNC=1 from
# the v2 settings snippet), else "sync". A bare key overrides the auto-detect.
mode = "async"                       # "async" | "sync"   (default: auto)

# Convenience boolean equivalent to mode. If both are present they must agree, or
# load fails loud.
enabled = true                       # bool               (default: mode == "async")

# Pending-findings JSONL. Relative paths resolve against the config file's dir,
# same rule as ledger.path. Parent dir is created on first write.
pending_path = ".lazarus/pending.jsonl"          # str    (default shown)

# Spool dir for the launcher's wu-<run_id>.txt files and the detached runner's
# stdout/stderr logs. Relative -> config dir.
spool_dir = ".lazarus/async"                     # str    (default shown)

# Force the offline deterministic stub judge in the background runner (CI / no-key
# installs). Default false -> the runner uses the real judge like the sync path.
stub_judge = false                   # bool               (default false)

[async.pregate]
# The optional synchronous shift-left gate (PreToolUse). Default OFF.
enabled = false                      # bool               (default false)
# Only findings at or above this confidence are surfaced by the pre-gate. Kept
# high on purpose to dodge the deep-recall noise problem.
min_confidence = 0.85                # float 0..1         (default 0.85)
# Hard cap on candidates the pre-gate judges synchronously, to bound the on-path
# latency it deliberately reintroduces. Kept tiny.
max_candidates = 3                   # int >= 1           (default 3)
```

The flat accessors on `Config` mirror the v1 delegate style
(`ledger_path`, `min_confidence`, ...): `async_enabled`, `async_mode`,
`pending_path`, `async_spool_dir`, `async_stub_judge`, `pregate_enabled`,
`pregate_min_confidence`, `pregate_max_candidates`, all backed by the `async_`
sub-object (attribute name `async_`, keyword-safe). `_build_async` validates
`mode` is one of `{"async", "sync"}`, cross-checks `enabled` against `mode` and
fails loud if both are present and disagree, resolves the two paths via
`_resolve_path`, builds the nested `[async.pregate]` via `_optional_table`, and
applies the `LAZARUS_ASYNC` auto-detect only when neither `mode` nor `enabled` is
set. `_OVERRIDE_KEYS` gains `async_mode`, `pending_path`, and
`pregate_min_confidence` for CLI parity.

### v2 settings snippet

v2 ships a separate `hooks/settings.snippet.v2.json`; the v1 `settings.snippet.json`
is left untouched for sync installs. The v2 snippet sets `LAZARUS_ASYNC=1` so the
config mode auto-detects to `"async"`, wires `Stop` and `PostToolUse` to the
launcher (non-blocking, 10-second timeout because the launcher only
spawns-and-returns), wires `UserPromptSubmit` to the injector, and carries the
`PreToolUse` pre-gate commented out under `_optional_pretooluse_pregate` (opt-in,
also requires `[async.pregate].enabled = true` in your config). Timeouts are in
seconds, matching the v1 convention. As with v1, merging the snippet into your
`settings.json` is a deliberate copy-paste step; nothing self-installs, and you
wire the v1 sync snippet or the v2 async snippet, never both.

### Fail-loud vs fail-safe, per hook

The v1 fail-loud discipline is re-applied per hook, and each hook's boundary is a
deliberate choice about whether it can afford to be loud:

- **Launcher:** fail-loud on misconfig (bad or missing config, via `_fail_loud`),
  but still non-blocking for the turn. It runs no Sonar and no judge, so its
  failure surface is almost nothing. Empty extraction or a disabled mode is a
  quiet non-blocking no-op.
- **Runner (detached child):** fail-loud to its own log file (exit 2 misconfig,
  exit 3 judge fault). Off the critical path, "loud" means inspectable in
  `spool_dir/log-<run_id>.txt`, not on the user's console. A judge fault degrades
  to silence: no new pending lines this run.
- **Injector:** fail-safe always, silent no-op on any error. It is on the prompt
  path and must never wedge a keystroke.
- **Pre-gate:** fail-safe toward allow. A judge error defaults to allowing the
  action, matching v1's documented optional-gate contract.

### Offline async cycle: green with no key

The stub-judge seam makes the entire v2 transport testable with no `anthropic`
package and no `ANTHROPIC_API_KEY`. Launcher spawns runner, runner runs
Sonar+Lazarus with the stub and writes the pending queue, injector reads and
consumes, the second inject run reads zero. That end-to-end cycle is an executable
assertion over the v2 transport, exactly as `examples/demo/run_demo.py` is an
executable assertion over the v1 engine. Because the stub matches the real judge's
`JudgeFn` signature and verdict-dict shape, if any cross-module contract drifts,
the demo goes red.

Run it:

```
pip install -e .
python examples/async_demo/run_async_demo.py   # launcher -> runner -> pending queue -> inject -> consume, offline, no API key
```

## Examples

`examples/demo/` is a runnable, credential-free proof that the whole pipeline
holds together end to end. It ships a three-rule corpus, a sample diff, a
deterministic offline judge, and a runner:

```
examples/demo/
  corpus/no-secrets-in-logs.md          # rule 1 - will SURFACE
  corpus/timeout-on-external-calls.md    # rule 2 - will SURFACE
  corpus/prefer-f-strings.md             # rule 3 - will DECLINE
  work_unit.diff                         # the sample diff
  lazarus.config.toml                    # corpus.path=./corpus, ledger under .lazarus/
  stub_judge.py                          # deterministic, no-network, no-API-key judge
  run_demo.py                            # loads config, runs Sonar then Lazarus, prints the result
```

Run it with no API key and no network:

```bash
python examples/demo/run_demo.py
```

The stub judge is the objective oracle. It is a pure function of each candidate's
rule id against a fixed allowlist, with no model, key, or clock, so the outcome is
machine-checkable and identical on every run. All three rules score above the
`min_score` floor and reach the judge, so the one `DECLINED` verdict is a genuine
Lazarus kill, not a Sonar miss. That is the point the tool makes: precision, not
recall, does the cutting.

The exact, asserted outcome is two surfaced fixes and one declined candidate:

- `no-secrets-in-logs.md` — SURFACED (the diff logs a secret).
- `timeout-on-external-calls.md` — SURFACED (the diff adds a network call with no
  timeout).
- `prefer-f-strings.md` — DECLINED and recorded in the ledger (the diff already
  uses f-strings, so the rule is on-topic but inert).

Because the stub matches the real judge's function signature and verdict-dict
shape exactly, this demo exercises the entire cross-module contract (config load,
Sonar sweep, Lazarus judge, verdict typing, retro-fix assembly, and the ledger
write). If any signature drifts, the demo goes red. Start there.

The v2 async cycle reuses this same stub. The offline async demo drives the full
transport (launcher spool file, detached runner with `--stub`, pending-queue
write, next-turn injection, consume) with no key, and a test asserts the vendored
`lazarus_sonar/async_/stub_judge.py` and `examples/demo/stub_judge.py` return
byte-identical verdicts so the two copies can never drift.

## Limitations

Stated plainly, because the tool's honesty is the point:

- **Precision tracks the judge.** The would-it-change-the-output filter is only as
  good as `judge_model`. A weaker judge lets more inert matches through; a stronger
  one costs more per audit. This is the main tuning surface and it is a real
  tradeoff, not a solved problem.
- **Recall is keyword-shaped in v1.** Sonar finds rules that share vocabulary with
  the work. A rule that applies but uses entirely different words can be missed.
  Embedding-based recall behind the same scorer interface is designed but not
  built.
- **Anti-nag is signature-scoped, not semantic.** Two diffs that are conceptually
  the same but differ byte-for-byte are two signatures, so a rule declined for one
  can re-surface for the other. A semantic (not signature-scoped) mute is deferred.
- **It applies what it can place unambiguously; it surfaces the rest.** Auto-apply
  (default on) applies a fix only when its edit matches the target exactly once,
  backing up the original for `lazarus undo`. Advisory or ambiguous fixes are
  surfaced, never forced. How much is applied vs surfaced tracks how often the judge
  emits a concrete edit.
- **Cost and latency.** Every audit is a Claude call. It is batched to one request
  per audit, but it is not free and it is not instant. v2 hides that latency behind
  the next turn instead of blocking on it, but it does not eliminate the cost. The
  `Sonar`-only path (`lazarus sonar`) has neither cost.
- **Durability is flush, not fsync.** Both the ledger and the v2 pending queue
  survive a process crash but can lose their last few lines to a full OS or power
  crash. This is the documented advisory-log trade, not an oversight. Multi-writer
  coordination is out of scope: both logs are single-writer-per-process, and v2's
  concurrent runners are made safe by signature-keyed dedup, not by file locks.
- **v2 injection is at-most-once, by choice.** The injector emits before it marks
  consumed, so a finding surfaces at most once. If the harness drops the emitted
  context, the finding is not re-surfaced. Exactly-once, ack-based consume is
  deferred, because re-nagging would violate the anti-nag contract and a missed
  advisory finding re-surfaces on the next related edit anyway.
- **The GATE / pre-gate placement is opt-in and advisory** in v1 and v2. Blocking
  a write on a judge call is the riskiest placement, so it ships default-off,
  narrow, and surfacing-not-blocking. A hard-blocking pre-gate mode is deferred.
- Also deferred, named here so the boundary is explicit: multi-work-unit batch
  audits; a corpus-quality linter; a spool-file and CONSUMED-line GC/retention
  policy (a `lazarus async gc` command is future work); multi-writer file locking
  on the pending queue; semantic cross-signature dedup of findings; and a Windows
  Job Object to auto-kill orphaned runners (the POSIX/Windows detach is sufficient
  for v2, since runners are short-lived and self-terminating).

## What's in the box

- `lazarus.config.example.toml` — annotated config to copy, including the optional
  v2 `[async]` and `[async.pregate]` tables.
- `src/lazarus_sonar/` — `config`, `sonar`, `lazarus`, `judge`, `ledger`, `cli`,
  plus the v2 `async_/` package (`pending`, `runner`, `launcher`, `inject`,
  `stub_judge`). The v1 engine files are unedited.
- `hooks/` — v1: `session_start_sweep.py`, `retro_audit.py`,
  `settings.snippet.json`. v2: `async_launcher.py`, `async_inject.py`,
  `async_pregate.py`, `async_runner_entry.py`, `settings.snippet.v2.json`.
- Console scripts — `lazarus` (the CLI) and `lazarus-audit-bg` (the v2 detached
  background runner).
- `INSTALL_HOOKS.md` — how to wire the hooks, what the two placeholders mean, the
  fail-loud contract, and how to choose the sync vs async snippet.
- `examples/demo/` — a runnable three-rule corpus, a sample diff, an offline stub
  judge, and `run_demo.py`. Running it produces two surfaced fixes and one
  Lazarus-killed candidate that lands in the ledger as `DECLINED`, with no API key
  and no network. The same stub drives the offline v2 async cycle. Start there.

## License

MIT.
