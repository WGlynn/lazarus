"""Vendored offline stub judge -- installed-fallback twin of examples/demo/stub_judge.py.

The v1 demo's ``examples/demo/stub_judge.py`` is the SOURCE OF TRUTH for the
deterministic "green oracle" judge. But the demo directory is not on the package
import path when Lazarus is installed from a wheel, so ``lazarus-audit-bg --stub``
run from an installed package cannot ``import stub_judge`` from there. This module
is the vendored copy the runner falls back to in that case
(``lazarus_sonar.async_runner._load_stub_judge_fn``): from a checkout the runner
prefers ``examples/demo/stub_judge.py``; when installed it imports THIS.

Anti-drift contract (verify_spec step f / DECISION D-6)
------------------------------------------------------
The verdict logic here is a VERBATIM LOGIC COPY of the demo's stub: same ALLOW
allowlist, same would_change=True@0.9 / False@0.2 verdicts, same returned dict
shape. A test (``test_async_cycle.py`` step f, and the offline demo's stub-parity
guard) asserts that ``lazarus_sonar.async_.stub_judge.stub_judge_fn`` and
``examples.demo.stub_judge.stub_judge_fn`` return IDENTICAL verdict lists for the
demo fixture candidates, so ``--stub`` from an installed wheel can never diverge
from the demo oracle. If this file drifts from the demo file, that guard goes red.

Keep the two in lockstep by hand: the demo file stays canonical; any change to
the oracle is made there first and mirrored here, and the parity guard catches a
missed mirror. (The contract shows this module as a thin re-export placeholder
``from .stub_judge import stub_judge_fn``; that self-import is a documentation
stand-in -- the real vendored module must carry the logic so ``--stub`` works with
no demo dir on the path, which is exactly what it does below.)

Deterministic, offline: no ``anthropic`` package, no network, no
``ANTHROPIC_API_KEY``, no clock. The verdict is a pure function of each
candidate's ``rule_id``.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..sonar import Candidate


# rule_id substring -> (where, patch, reason). A candidate is judged
# would_change=True@0.9 iff its rule_id contains one of these keys; otherwise it
# is would_change=False@0.2. This allowlist IS the hand-encoded correct answer
# for the demo diff: the two rules the diff actually violates, and nothing else.
# VERBATIM copy of examples/demo/stub_judge.py::ALLOW.
ALLOW: dict[str, tuple[str, str, str]] = {
    "no-secrets-in-logs": (
        "the logger.info line that interpolates api_key",
        "redact the key before logging: log a fingerprint (last 4 chars) or "
        "drop the api_key from the message entirely.",
        "the diff logs a secret; the rule forbids writing keys/tokens to a log "
        "sink, so it would have changed this line.",
    ),
    "timeout-on-external-calls": (
        "the requests.get(url, headers=headers) call",
        "add an explicit timeout, e.g. requests.get(url, headers=headers, "
        "timeout=5), and decide what happens on timeout.",
        "the diff adds an outbound network call with no timeout, which can hang "
        "a worker forever; the rule would have changed this call.",
    ),
}


def stub_judge_fn(
    work_unit: str,
    kind: str,
    candidates: Sequence[Candidate],
) -> list[dict[str, Any]]:
    """A deterministic ``lazarus.JudgeFn``. No model, no network, no key, no clock.

    Returns one raw verdict dict per candidate, in the same order. The dict shape
    is exactly what ``lazarus.Verdict.from_judge`` consumes:
    ``{"rule_id", "would_change", "where", "patch", "confidence", "reason"}``.

    This is a verbatim logic copy of ``examples/demo/stub_judge.py::stub_judge_fn``
    so the installed-fallback oracle cannot diverge from the demo oracle (see the
    module docstring's anti-drift contract).
    """
    out: list[dict[str, Any]] = []
    for c in candidates:
        hit = next((v for k, v in ALLOW.items() if k in c.rule_id), None)
        if hit is not None:
            where, patch, reason = hit
            out.append(
                {
                    "rule_id": c.rule_id,
                    "would_change": True,
                    "where": where,
                    "patch": patch,
                    "confidence": 0.9,
                    "reason": reason,
                }
            )
        else:
            out.append(
                {
                    "rule_id": c.rule_id,
                    "would_change": False,
                    "where": "",
                    "patch": "",
                    "confidence": 0.2,
                    "reason": "on-topic but inert for this diff (the work already "
                    "satisfies the rule), so nothing would change.",
                }
            )
    return out
