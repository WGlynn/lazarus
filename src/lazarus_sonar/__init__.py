"""Lazarus + Sonar: a retroactive-knowledge-audit tool for file-based agent memory.

Two organs:

- SONAR is the perception layer. It globs a corpus of rules/primitives/memory
  files, tokenizes a work-unit (a diff, a finished response, a decision), and
  ranks the candidate files most likely to be relevant. It is wide, cheap, and
  high-recall: the firehose. Its raw output is never shown to a human.

- LAZARUS is the cognition layer. It consumes SONAR's shortlist and applies a
  precision filter via a judge model, answering one question per candidate:
  "would applying this buried rule actually have CHANGED the finished work?"
  It kills forced or on-topic-but-inert matches and emits a ranked list of
  retroactive fixes, each with the span it would improve and a proposed patch.

The value is precision, not recall. Passive relevance-surfacing already exists
in most setups and gets ignored because it is noisy. LAZARUS adds the filter,
the would-it-change-the-output test, and an append-only ledger that records
which (work-unit, rule) pairs were already judged so the same rule is never
re-surfaced after it was declined (the anti-nag property).

It PROPOSES, never auto-applies. Nothing here touches the user's files or the
finished work; applying a fix is a separate human action recorded via the CLI.

Perception and cognition stay separate, and that separation is visible in the
public surface. SONAR takes plain corpus arguments and knows nothing about
Config. LAZARUS takes the shortlist SONAR already produced; it does not run
SONAR itself. The caller runs SONAR, then hands the resulting candidates to
LAZARUS. A thin adapter, run_sonar_for_config, unpacks a loaded Config into the
plain SONAR arguments so config-holding callers (the CLI and the hooks) have one
obvious call instead of hand-unpacking corpus_path/globs/scoring each time.

Public API
----------

    from lazarus_sonar import (
        run_sonar, run_sonar_for_config, run_lazarus,
        Ledger, load_config, Config,
    )

- run_sonar(work_unit, *, corpus_path, globs, exclude=(), scoring=None,
  encoding="utf-8") -> list[Candidate].
  The portable perception core. Takes plain corpus arguments, no Config
  dependency, so it is reusable outside this package. Fails loud on an empty
  work-unit, a missing corpus, or a corpus that matches zero files.

- run_sonar_for_config(work_unit, config, *, kind="generic", top_n=None)
  -> list[Candidate].
  The Config adapter over run_sonar. Pulls corpus_path/globs/exclude/scoring off
  a loaded Config and calls run_sonar. This is the call every config-holding
  caller uses. `kind` is advisory in v1 (accepted for forward-compat, no scoring
  effect); `top_n` of None uses config.scoring.top_n.

- run_lazarus(work_unit, candidates, *, config, ledger=None, judge_fn=None,
  kind="diff", record=True) -> AuditResult.
  The precision filter over SONAR's shortlist. `candidates` is positional and
  required: LAZARUS does not run SONAR, the caller passes the shortlist in. It
  applies ledger suppression, calls the judge once (batched), keeps only
  confident would-change verdicts, and returns an AuditResult (result.fixes is
  the ranked, human-facing payload; result.as_dict() is the JSON view). Inject
  judge_fn to run offline with a stub and no API key.

- load_config(path=None, *, start=None, overrides=None) -> Config.
  Load, validate, and resolve a lazarus.config.toml. Config exposes both the
  structured sub-objects (scoring, judge, ledger) and flat read-only accessors
  (ledger_path, min_confidence, judge_model, api_key, top_n, min_score,
  max_candidates) that LAZARUS and the CLI read.

- Ledger(path): append-only JSONL store of SURFACED / ACTIONED / DECLINED
  verdicts, with is_declined() for anti-nag suppression and action()/decline()
  mutators. It takes a filesystem PATH (Ledger(config.ledger_path)), never a
  Config.

The judge (the `anthropic` SDK) is an optional [judge] extra. SONAR, the config
loader, and the ledger run stdlib-only and offline, so run_sonar,
run_sonar_for_config, load_config, and Ledger are importable and usable with zero
third-party dependencies and no API key. run_lazarus needs the judge only when no
judge_fn is injected, and imports it lazily; calling it that way without the
`anthropic` package installed (or without an API key) fails loud at call time,
not import time. An injected judge_fn (see examples/demo/stub_judge.py) needs
neither the package nor a key, which is the credential-free path the demo and
tests take.
"""

__version__ = "0.1.0"

from .config import Config, load_config
from .ledger import Ledger
from .lazarus import run_lazarus
from .sonar import run_sonar, run_sonar_for_config

__all__ = [
    "run_sonar",
    "run_sonar_for_config",
    "run_lazarus",
    "Ledger",
    "load_config",
    "Config",
    "__version__",
]
