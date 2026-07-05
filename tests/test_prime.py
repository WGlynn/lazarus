"""`lazarus prime` -- proactive recall surfaces relevant rules for UPCOMING work.

Offline, deterministic, no API key: runs the CLI end to end against the demo corpus.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lazarus_sonar.cli import main  # noqa: E402

DEMO = REPO / "examples" / "demo"
CFG = str(DEMO / "lazarus.config.toml")
DIFF = str(DEMO / "work_unit.diff")


def test_prime_surfaces_relevant_rules(capsys):
    rc = main(["prime", "--config", CFG, "--file", DIFF])
    out = capsys.readouterr().out
    assert rc == 0
    # The two rules the demo diff actually implicates should be recalled up front.
    assert "no-secrets-in-logs.md" in out
    assert "timeout-on-external-calls.md" in out
    # Priming framing: surfaced BEFORE the work, not a verdict.
    assert "before the work" in out.lower()


def test_prime_empty_input_is_clean_not_a_crash(capsys, tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n", encoding="utf-8")
    rc = main(["prime", "--config", CFG, "--file", str(empty)])
    err = capsys.readouterr().err
    # Empty upcoming-work is a clean input error (consistent with sonar/audit),
    # surfaced with a message, never a traceback.
    assert rc != 0
    assert "Traceback" not in err
