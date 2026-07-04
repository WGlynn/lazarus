# Lazarus + Sonar

A retroactive-knowledge-audit tool for Claude Code and any agent that keeps its
rules, primitives, and memory in files. It re-reads the work an agent just
finished, finds the buried-but-still-valid rules that apply to it, and asks one
question about each: would this rule have changed the output? It surfaces the
survivors as proposed fixes. It never edits your files.

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

## What it does not do

It surfaces and it proposes. It does not apply anything. Lazarus writes each
retroactive fix to the ledger as `SURFACED` and stops. Nothing touches your files
or the finished work. Applying a fix is a separate, human action, recorded with
`lazarus ledger action`. There is no autoapply code path in v1, by design. Adding
one is your call, not the tool's.

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

## Examples

`examples/demo/` is a runnable, credential-free proof that the whole pipeline
holds together end to end. It ships a three-rule corpus, a sample diff, a
deterministic offline judge, and a runner:

```
examples/demo/
  corpus/no-secrets-in-logs.md         # rule 1 - will SURFACE
  corpus/timeout-on-external-calls.md   # rule 2 - will SURFACE
  corpus/prefer-f-strings.md            # rule 3 - will DECLINE
  work_unit.diff                        # the sample diff
  lazarus.config.toml                   # corpus.path=./corpus, ledger under .lazarus/
  stub_judge.py                         # deterministic, no-network, no-API-key judge
  run_demo.py                           # loads config, runs Sonar then Lazarus, prints the result
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
- **It proposes; it does not fix.** No autoapply path exists in v1. Every "fix" is
  a proposal you apply and record by hand.
- **Cost and latency.** Every audit is a Claude call. It is batched to one request
  per audit, but it is not free and it is not instant. The `Sonar`-only path
  (`lazarus sonar`) has neither cost.
- **Durability is flush, not fsync.** The ledger survives a process crash but can
  lose its last few lines to a full OS or power crash. This is the documented
  advisory-log trade, not an oversight. Multi-writer coordination is out of scope
  for v1 (single hook or CLI writer).
- **The GATE placement is guidance, not a hardened hook** in v1. Blocking a write
  on a judge call is the riskiest placement, so it ships documented and optional.
- Also deferred, named here so the boundary is explicit: multi-work-unit batch
  audits, and a corpus-quality linter.

## What's in the box

- `lazarus.config.example.toml` — annotated config to copy.
- `src/lazarus_sonar/` — `config`, `sonar`, `lazarus`, `judge`, `ledger`, `cli`.
- `hooks/` — `session_start_sweep.py`, `retro_audit.py`, `settings.snippet.json`.
- `INSTALL_HOOKS.md` — how to wire the hooks, what the two placeholders mean, and
  what the fail-loud contract is.
- `examples/demo/` — a runnable three-rule corpus, a sample diff, an offline stub
  judge, and `run_demo.py`. Running it produces two surfaced fixes and one
  Lazarus-killed candidate that lands in the ledger as `DECLINED`, with no API key
  and no network. Start there.

## License

MIT.
