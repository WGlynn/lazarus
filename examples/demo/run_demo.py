#!/usr/bin/env python3
"""Runnable Lazarus + Sonar demo — the one-command proof that the tool works.

Run it:

    python examples/demo/run_demo.py

or, from inside examples/demo/:

    python run_demo.py

Expected result (deterministic, no API key, no network):

    2 SURFACED retroactive-fixes  -> no-secrets-in-logs.md, timeout-on-external-calls.md
    1 DECLINED (killed by judge)  -> prefer-f-strings.md

The script exits 0 and prints "DEMO PASSED" when it sees exactly that, and exits
non-zero with a diff if anything drifts. That green/red exit is the point: this
demo is an executable assertion over the whole cross-module pipeline, so if any
interface in the package changes shape, this run goes red.

What it exercises, end to end
-----------------------------
    load_config                 read examples/demo/lazarus.config.toml
      -> run_sonar_for_config   keyword-score the 3-rule corpus against the diff
      -> run_lazarus            drop DECLINED, judge, filter by confidence, rank
         (judge_fn = stub)      the offline green oracle (stub_judge.py)
      -> AuditResult            the ranked fixes + accounting
      -> Ledger                 SURFACED / DECLINED written to .lazarus/ledger.jsonl

Every stage is the real code from src/lazarus_sonar/. Only the judge is
substituted — with the deterministic stub — so the demo is credential-free and
reproducible while still running the genuine perception, suppression, filtering,
ranking, and ledger logic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# --- make the package importable from a plain checkout ---------------------
# A stranger who just cloned the repo has not necessarily run `pip install -e .`.
# Put the repo's src/ on sys.path (and this demo dir, for stub_judge) so
# `python examples/demo/run_demo.py` works with nothing installed but Python
# itself (plus `tomli` on 3.9-3.10; tomllib is stdlib on 3.11+).
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]  # examples/demo -> examples -> repo root
SRC = REPO_ROOT / "src"
for extra in (str(SRC), str(HERE)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from lazarus_sonar.config import load_config  # noqa: E402
from lazarus_sonar.lazarus import run_lazarus  # noqa: E402
from lazarus_sonar.ledger import Ledger  # noqa: E402
from lazarus_sonar.sonar import run_sonar_for_config  # noqa: E402

from stub_judge import stub_judge_fn  # noqa: E402  (added to sys.path above)


CONFIG_PATH = HERE / "lazarus.config.toml"
WORK_UNIT_PATH = HERE / "work_unit.diff"
KIND = "diff"

# The exact green oracle (see DECISION D-5 in the interface contract). These are
# the rule_ids — POSIX-relative to the corpus root — the pipeline must surface
# and decline for the demo diff.
EXPECTED_SURFACED = ["no-secrets-in-logs.md", "timeout-on-external-calls.md"]
EXPECTED_DECLINED = ["prefer-f-strings.md"]


def run() -> "AuditResult":  # type: ignore[name-defined]  # noqa: F821
    """Run the full demo pipeline and return the AuditResult.

    Wipes any prior ledger first so the run is reproducible: a DECLINED entry
    left over from an earlier run would suppress its candidate before the judge
    (the anti-nag property working as designed, but not what you want when you
    re-run a demo and expect the same output every time).
    """
    config = load_config(CONFIG_PATH)

    # Fresh ledger every run so the demo is deterministic.
    ledger_path = config.ledger_path
    if ledger_path.exists():
        ledger_path.unlink()
    ledger = Ledger(ledger_path)

    work_unit = WORK_UNIT_PATH.read_text(encoding="utf-8")

    # SONAR (perception): wide, cheap keyword sweep over the corpus.
    candidates = run_sonar_for_config(work_unit, config, kind=KIND)

    # LAZARUS (cognition): suppression -> judge (the offline stub) -> confidence
    # filter -> rank -> record. judge_fn is the credential-free green oracle.
    result = run_lazarus(
        work_unit,
        candidates,
        config=config,
        ledger=ledger,
        judge_fn=stub_judge_fn,
        kind=KIND,
        record=True,
    )
    return result


def _check(result) -> list[str]:
    """Return a list of human-readable assertion failures (empty == all passed).

    Asserts the exact green oracle from the contract:
      - exactly 2 SURFACED fixes, and they are the two actionable rules
      - exactly 1 DECLINED rule, and it is prefer-f-strings.md
      - the decline was a judge kill (killed_by_judge == 1), not a
        below-confidence drop (below_confidence == 0)
    """
    failures: list[str] = []

    surfaced_ids = [f.rule_id for f in result.fixes]
    if surfaced_ids != EXPECTED_SURFACED:
        failures.append(
            f"SURFACED rule_ids {surfaced_ids!r} != expected {EXPECTED_SURFACED!r}"
        )
    if len(result.fixes) != 2:
        failures.append(f"expected 2 surfaced fixes, got {len(result.fixes)}")

    declined = sorted(result.declined_rule_ids)
    if declined != EXPECTED_DECLINED:
        failures.append(
            f"DECLINED rule_ids {declined!r} != expected {EXPECTED_DECLINED!r}"
        )

    if result.killed_by_judge != 1:
        failures.append(f"expected killed_by_judge == 1, got {result.killed_by_judge}")
    if result.below_confidence != 0:
        failures.append(
            f"expected below_confidence == 0, got {result.below_confidence}"
        )

    return failures


def main() -> int:
    result = run()

    # The machine-readable view. `default=str` because RetroFix.path carries a
    # pathlib.Path, exactly as the CLI's JSON path does.
    print("=" * 72)
    print("AuditResult (as_dict):")
    print("=" * 72)
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True, default=str))
    print()

    # The human view: what a person running the demo should see.
    print("=" * 72)
    print(f"SURFACED ({len(result.fixes)} retroactive-fix proposals):")
    print("=" * 72)
    for i, fix in enumerate(result.fixes, start=1):
        print(f"  {i}. {fix.rule_id}  (confidence {fix.confidence:.2f})")
        print(f"       where: {fix.where}")
        print(f"       patch: {fix.patch}")
        print(f"       why:   {fix.reason}")
    print()
    print(f"DECLINED (killed by the judge, {len(result.declined_rule_ids)}): "
          f"{', '.join(result.declined_rule_ids) or '(none)'}")
    print()
    print("Accounting: "
          f"candidates_in={result.candidates_in} "
          f"judged={result.judged} "
          f"killed_by_judge={result.killed_by_judge} "
          f"below_confidence={result.below_confidence} "
          f"suppressed_declined={result.suppressed_declined}")
    print()

    failures = _check(result)
    if failures:
        print("=" * 72)
        print("DEMO FAILED - the pipeline did not produce the expected oracle:")
        for f in failures:
            print(f"  - {f}")
        print("=" * 72)
        return 1

    print("=" * 72)
    print("DEMO PASSED - 2 surfaced, 1 declined, exactly as expected.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
