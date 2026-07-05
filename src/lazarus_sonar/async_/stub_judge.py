"""Vendored offline stub judge for the Lazarus v2 async transport.

This is the installed-fallback twin of ``examples/demo/stub_judge.py``. The
background runner's ``_load_stub_judge_fn`` prefers the demo file when running
from a checkout (that file is the source of truth), and falls back to THIS
vendored copy when the package is installed and the demo dir is not on the path,
so ``--stub`` / ``[async].stub_judge = true`` works from a plain wheel with no
``anthropic`` package, no network, and no ``ANTHROPIC_API_KEY``.

Its logic is a VERBATIM copy of the demo stub: the same ALLOW allowlist, the same
``would_change = True @ 0.9`` / ``False @ 0.2`` verdicts, and the same returned
dict shape (``lazarus.Verdict.from_judge``'s contract). A parity test
(``test_async_cycle.py::test_f_stub_parity`` and step (f) of the async demo)
asserts this module and the demo file return byte-identical verdicts, so the two
copies can never drift. The only difference from the demo file is the import
anchor: ``Candidate`` comes from ``..sonar`` (this module lives inside the
``lazarus_sonar`` package) rather than the top-level ``lazarus_sonar.sonar``.

Encoded correct answer for the demo diff (identical to the demo stub):

- ``no-secrets-in-logs``        -> would_change = True  (the diff logs the api key)
- ``timeout-on-external-calls`` -> would_change = True  (the requests.get has no
                                    timeout)
- everything else (here, ``prefer-f-strings``) -> would_change = False (the diff
                                    already uses f-strings, so the rule is inert)
"""

from __future__ import annotations

from typing import Any, Sequence

from ..sonar import Candidate


# rule_id substring -> (where, patch, reason). A candidate is judged
# would_change=True@0.9 iff its rule_id contains one of these keys; otherwise it
# is would_change=False@0.2. This allowlist IS the hand-encoded correct answer
# for the demo diff: the two rules the diff actually violates, and nothing else.
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

    Args:
        work_unit: the finished work being audited. Accepted to match the
            ``JudgeFn`` signature; the verdict here does not depend on it, which
            is what makes the demo reproducible.
        kind: the advisory work-unit kind ("diff", ...). Accepted for the same
            reason; unused by this stub.
        candidates: SONAR's surviving shortlist. The verdict for each is a pure
            function of ``candidate.rule_id``.

    Returns:
        A list of verdict dicts, one per candidate.
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
