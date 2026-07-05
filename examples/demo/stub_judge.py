"""Deterministic, offline stub judge for the Lazarus + Sonar demo (and tests).

This is the "green oracle" the demo and the test suite run against. It is a
drop-in replacement for the real Claude judge (`judge.judge_batch` wired through
`lazarus._default_judge_fn`) that needs no `anthropic` package, no network, no
`ANTHROPIC_API_KEY`, and no clock. Its verdict is a pure function of each
candidate's `rule_id`, so the same corpus + the same diff always produce exactly
the same result: two SURFACED retroactive-fixes and one DECLINED.

Why a stub at all
-----------------
The whole point of Lazarus is precision: SONAR is high-recall and hands over a
noisy shortlist; the judge is the precision gate that answers "would applying
this buried rule have CHANGED the finished work?". The real judge is a model, so
its answer is not byte-for-byte reproducible and it costs an API call. For a demo
that a stranger can run to see green — and for tests that must be deterministic in
CI — we substitute a judge whose answer is fixed and inspectable.

This stub encodes the *correct* answer for the demo diff by hand:

- ``no-secrets-in-logs``      -> would_change = True  (the diff logs the api key)
- ``timeout-on-external-calls`` -> would_change = True  (the diff's requests.get
                                    has no timeout)
- everything else (here, ``prefer-f-strings``) -> would_change = False (the diff
                                    already uses f-strings, so the rule is inert)

The signature and the returned-dict shape match the real contract exactly
(`lazarus.JudgeFn` and `lazarus.Verdict.from_judge`), so wiring this stub into
`run_lazarus(..., judge_fn=stub_judge_fn)` exercises the entire cross-module
pipeline — config load -> run_sonar_for_config -> run_lazarus -> judge_fn dicts
-> Verdict.from_judge -> RetroFix -> AuditResult -> Ledger — end to end. If any
signature in the interface contract is violated, this stub's run goes red.

Each True verdict also carries a concrete ``edit`` (``{file, find, replace}``) so
the auto-applier (apply.py) has a real, uniquely-locatable change to apply and
revert — the stub is the offline proof that the whole judge -> RetroFix -> apply
wire works, not just the surfacing half. The ``find`` strings are exact
substrings of the demo diff (examples/demo/work_unit.diff), so an apply against
the file that diff represents lands cleanly and `lazarus undo` restores it.
"""

from __future__ import annotations

from typing import Any, Sequence

from lazarus_sonar.sonar import Candidate


# rule_id substring -> (where, patch, reason, edit). A candidate is judged
# would_change=True@0.9 iff its rule_id contains one of these keys; otherwise it
# is would_change=False@0.2. This allowlist IS the hand-encoded correct answer
# for the demo diff: the two rules the diff actually violates, and nothing else.
# ``edit`` is the concrete {file, find, replace} the auto-applier can execute;
# each ``find`` is an exact, single-occurrence substring of the demo diff.
ALLOW: dict[str, tuple[str, str, str, dict]] = {
    "no-secrets-in-logs": (
        "the logger.info line that interpolates api_key",
        "redact the key before logging: log a fingerprint (last 4 chars) or "
        "drop the api_key from the message entirely.",
        "the diff logs a secret; the rule forbids writing keys/tokens to a log "
        "sink, so it would have changed this line.",
        {
            "file": "service/upstream.py",
            "find": 'logger.info(f"fetching profile for user {user_id} with api key {api_key}")',
            "replace": 'logger.info(f"fetching profile for user {user_id}")',
        },
    ),
    "timeout-on-external-calls": (
        "the requests.get(url, headers=headers) call",
        "add an explicit timeout, e.g. requests.get(url, headers=headers, "
        "timeout=5), and decide what happens on timeout.",
        "the diff adds an outbound network call with no timeout, which can hang "
        "a worker forever; the rule would have changed this call.",
        {
            "file": "service/upstream.py",
            "find": "requests.get(url, headers=headers)",
            "replace": "requests.get(url, headers=headers, timeout=5)",
        },
    ),
}


def stub_judge_fn(
    work_unit: str,
    kind: str,
    candidates: Sequence[Candidate],
) -> list[dict[str, Any]]:
    """A deterministic `lazarus.JudgeFn`. No model, no network, no key, no clock.

    Returns one raw verdict dict per candidate, in the same order. The dict shape
    is exactly what `lazarus.Verdict.from_judge` consumes:
    ``{"rule_id", "would_change", "where", "patch", "confidence", "reason",
    "edit"}`` (``edit`` is a ``{file, find, replace}`` dict on a True verdict,
    ``None`` on a False one).

    Args:
        work_unit: the finished work being audited. Accepted to match the
            `JudgeFn` signature; the verdict here does not depend on it, which is
            what makes the demo reproducible.
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
            where, patch, reason, edit = hit
            out.append(
                {
                    "rule_id": c.rule_id,
                    "would_change": True,
                    "where": where,
                    "patch": patch,
                    "confidence": 0.9,
                    "reason": reason,
                    "edit": edit,
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
                    "edit": None,
                }
            )
    return out
