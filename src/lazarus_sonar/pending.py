"""Append-only JSONL pending-findings queue for Lazarus v2 (async transport).

The pending queue is the async twin of the v1 ledger. Where the ledger records
judge VERDICTS about a (work-unit, rule) pair for anti-nag suppression, the
pending queue records SURFACED findings that a background runner produced during
a previous turn and that are AWAITING INJECTION into the next turn, plus whether
they were later CONSUMED. Two separate files, two separate jobs, one shared key.

Both are keyed on ``(work_unit_sig, rule_id)`` -- the exact same dedup key the
ledger uses (``ledger.LedgerRecord.key``). That shared key is what lets the two
files line up: the ledger's ``work_unit_signature`` and this queue's are the
same function, so a finding surfaced by the runner and a verdict recorded by
``run_lazarus`` on the identical work-unit refer to the same key.

Why a second file at all? The ledger answers "has this rule already been judged
for this work?" (suppression, written on the runner's hot path via the reused
``run_lazarus(record=True)``). The pending queue answers "what did the last
turn's background audit surface that the next turn should be shown, and have we
shown it yet?" (transport + at-most-once delivery). Storing the whole
``RetroFix.as_dict()`` on the SURFACED line means the UserPromptSubmit injection
hook needs no second lookup and no live Config to render: it reads the queue and
formats the dict directly.

Durability trade (flush yes, fsync no)
--------------------------------------
Writes are append-only and flush the Python buffer to the OS page cache
(``fh.flush()``) but deliberately do NOT ``os.fsync()`` per line. This is the
same trade the v1 ledger documents and for the same reasons: the queue is
advisory, single-writer-per-process, append-only, and fully reconstructable. A
Python-process crash (the common case) loses nothing already flushed; only a
full OS/power crash can drop the last not-yet-synced lines, and the worst
consequence is that one surfaced finding is missed once (recoverable: the
underlying corpus rule re-surfaces on the next related edit) or, symmetrically,
a CONSUMED mark is lost and a finding re-injects once. Neither corrupts the
file. Concurrent multi-writer coordination remains out of scope for v2; the
writer side is made safe by dedup, not by locks (see the runner's D-4).

State for a key is the LAST line written for that key (last-line-wins), mirroring
``Ledger.state()``. A SURFACED line carries the full ``fix`` payload; a later
CONSUMED line for the same key carries an empty/omitted ``fix`` because the
SURFACED line already holds it.

Stdlib-only. This module reaches into the v1 engine only for the shared signature
helper and the RetroFix type (for typing), so the queue is usable offline with no
judge extra installed.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from ..ledger import work_unit_signature  # REUSED, not reimplemented
from ..lazarus import RetroFix  # for typing only

__all__ = [
    "PENDING_SCHEMA_VERSION",
    "SURFACED",
    "CONSUMED",
    "PendingError",
    "PendingFinding",
    "PendingQueue",
]

# Silence linters that flag work_unit_signature as unused: it is re-exported as
# part of this module's namespace so callers (the runner) can import the shared
# signature helper from here alongside the queue, guaranteeing they use the same
# function the ledger uses. Referencing it here documents the reuse contract.
_SIGNATURE_REUSED = work_unit_signature

# ---------------------------------------------------------------------------
# Schema / event constants
# ---------------------------------------------------------------------------

# Current on-disk line-schema version. Bumped only on a breaking change to the
# line shape; readers tolerate older/newer versions and ignore unknown fields so
# a mixed-version queue still loads. Mirrors ledger.SCHEMA_VERSION discipline.
PENDING_SCHEMA_VERSION = 1

# Append-only event states. A key's current state is the last line written for it
# (last-line-wins), exactly like the ledger's verdict resolution.
SURFACED = "SURFACED"
CONSUMED = "CONSUMED"

_EVENTS = (SURFACED, CONSUMED)


class PendingError(Exception):
    """Raised on an unwritable or unrecoverable pending queue.

    Fail-loud by design on the WRITER side (the runner): a queue we cannot write
    is a real defect the background runner surfaces to its log, never swallows.
    The READER side (the injection hook) treats a missing file as a legitimate
    empty (not an error) so it can never wedge a prompt; that fail-safe behaviour
    lives in ``read_unconsumed``, not here.
    """


# ---------------------------------------------------------------------------
# Pending finding record (one JSONL line)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingFinding:
    """One line in the pending queue: a surfaced finding awaiting injection.

    Identity (the dedup/state key, same shape as ``LedgerRecord.key``):
        work_unit_sig   sha256 of the normalized work-unit (``work_unit_signature``)
        rule_id         POSIX-relative corpus id, from ``Candidate.rule_id``

    Carried-through context:
        kind            v1 work-unit kind ("diff" | "response"), passed through
        run_id          8-hex id of the runner invocation that produced this line
        ts              unix epoch seconds (float) when written, rounded to 3 dp
                        on serialization (matching the ledger)
        event           SURFACED (payload-bearing) or CONSUMED (payload-empty)
        schema          line-schema version

    Payload:
        fix             exactly ``RetroFix.as_dict()`` (rule_id, title, path,
                        where, patch, reason, confidence, sonar_score; path is a
                        str). Stored whole so the injection hook renders without a
                        second lookup and without a live Config. Empty on a
                        CONSUMED line (the SURFACED line already holds it).
    """

    work_unit_sig: str
    rule_id: str
    kind: str
    fix: dict  # RetroFix.as_dict()
    run_id: str = ""
    ts: float = field(default_factory=time.time)
    event: str = SURFACED
    schema: int = PENDING_SCHEMA_VERSION

    @property
    def key(self) -> Tuple[str, str]:
        """The dedup/state key: (work_unit_sig, rule_id).

        Same shape as ``LedgerRecord.key`` so the pending queue and the ledger
        speak the same identity for the same work.
        """
        return (self.work_unit_sig, self.rule_id)

    @property
    def confidence(self) -> float:
        """Judge confidence, read off the stored ``fix`` dict.

        The reader (``read_unconsumed``) sorts on this as the primary tiebreak,
        so exposing it here keeps that sort key from reaching into ``fix``
        directly. Missing/unparseable confidence sorts as 0.0.
        """
        try:
            return float(self.fix.get("confidence", 0.0))
        except (TypeError, ValueError, AttributeError):
            return 0.0

    def to_json(self) -> str:
        """Serialize to a single JSONL line (no trailing newline).

        ``sort_keys`` for stable, diffable lines; ``ensure_ascii`` off so unicode
        in rules/patches survives round-trip. Matches ``LedgerRecord.to_json``.
        A CONSUMED line omits the ``fix`` payload entirely (the SURFACED line
        already carries it; last-line-wins resolves state).
        """
        payload = {
            "schema": self.schema,
            "ts": round(self.ts, 3),
            "event": self.event,
            "run_id": self.run_id,
            "work_unit_sig": self.work_unit_sig,
            "kind": self.kind,
            "rule_id": self.rule_id,
        }
        if self.event == SURFACED and self.fix:
            payload["fix"] = self.fix
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict) -> "PendingFinding":
        """Build a finding from a parsed JSON object.

        Tolerant of missing optional keys and unknown extra keys (forward-compat:
        readers ignore fields they do not know), mirroring
        ``LedgerRecord.from_dict``. Raises ``PendingError`` only on the two fields
        the key is built from, because a line we cannot key is unusable and
        silently dropping it would hide a writer bug.
        """
        try:
            work_unit_sig = data["work_unit_sig"]
            rule_id = data["rule_id"]
        except KeyError as exc:
            raise PendingError(
                f"pending line missing required field: {exc}"
            ) from exc

        event = str(data.get("event", SURFACED))
        raw_fix = data.get("fix")
        fix = raw_fix if isinstance(raw_fix, dict) else {}

        return cls(
            work_unit_sig=str(work_unit_sig),
            rule_id=str(rule_id),
            kind=str(data.get("kind", "")),
            fix=fix,
            run_id=str(data.get("run_id", "")),
            ts=float(data.get("ts", time.time())),
            event=event,
            schema=int(data.get("schema", PENDING_SCHEMA_VERSION)),
        )

    @classmethod
    def from_retrofix(
        cls,
        fix: RetroFix,
        *,
        work_unit_sig: str,
        kind: str,
        run_id: str,
    ) -> "PendingFinding":
        """Build a SURFACED finding from a v1 ``RetroFix``.

        ``fix.as_dict()`` is stored verbatim as the payload, so the injection
        hook has the whole RetroFix view (rule_id/title/path/where/patch/reason/
        confidence/sonar_score) with no second lookup. ``rule_id`` is taken from
        the RetroFix so the pending key matches the ledger key for the same work.
        """
        return cls(
            work_unit_sig=work_unit_sig,
            rule_id=fix.rule_id,
            kind=kind,
            fix=fix.as_dict(),
            run_id=run_id,
            event=SURFACED,
        )


# ---------------------------------------------------------------------------
# Pending queue
# ---------------------------------------------------------------------------


class PendingQueue:
    """Append-only JSONL queue of surfaced-but-not-yet-injected findings.

    Two roles share one file:
      - the background RUNNER appends SURFACED findings (writer side);
      - the UserPromptSubmit INJECTION hook reads unconsumed findings and then
        appends CONSUMED lines (reader + consume side).

    The file is created lazily on first write. Reads tolerate a missing file
    (empty queue) because "no queue yet" is a legitimate empty on the injection
    path, not an error. State for a key is the last line written for that key;
    the full append-only history is preserved.

    The path is supplied by the caller, resolved from ``config.pending_path``.
    No location is hardcoded here.
    """

    def __init__(self, path: os.PathLike | str) -> None:
        self.path = Path(path).expanduser()

    # -- low-level io -----------------------------------------------------

    def _ensure_parent(self) -> None:
        parent = self.path.parent
        if parent and not parent.exists():
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PendingError(
                    f"cannot create pending-queue directory {parent}: {exc}"
                ) from exc

    def _append_line(self, finding: PendingFinding) -> PendingFinding:
        """Append one finding as a JSONL line. Append-only: never rewrites
        existing lines.

        Flushes the Python buffer to the OS page cache but does NOT ``os.fsync``
        per write. Identical durability trade to the v1 ledger's ``_append``:
        the queue is advisory, single-writer-per-process, append-only, and
        reconstructable, so an fsync-per-line stall is not warranted. See the
        module docstring for the full rationale.
        """
        self._ensure_parent()
        line = finding.to_json()
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
                fh.write("\n")
                fh.flush()
        except OSError as exc:
            raise PendingError(
                f"cannot write to pending queue {self.path}: {exc}"
            ) from exc
        return finding

    def read_all(self, *, skip_corrupt: bool = True) -> List[PendingFinding]:
        """Read every finding in file order.

        Blank lines are ignored. A corrupt line (unparseable JSON, or a line
        missing the key fields) is skipped when ``skip_corrupt`` is True (the
        default, so one bad line never bricks the queue) and raises otherwise.
        A missing file is an empty queue, not an error.
        """
        if not self.path.exists():
            return []

        findings: List[PendingFinding] = []
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for lineno, raw in enumerate(fh, start=1):
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        data = json.loads(stripped)
                        findings.append(PendingFinding.from_dict(data))
                    except (json.JSONDecodeError, PendingError) as exc:
                        if skip_corrupt:
                            continue
                        raise PendingError(
                            f"corrupt pending line {lineno} in {self.path}: {exc}"
                        ) from exc
        except OSError as exc:
            raise PendingError(
                f"cannot read pending queue {self.path}: {exc}"
            ) from exc
        return findings

    # -- writer side (runner) ---------------------------------------------

    def append(self, finding: PendingFinding) -> PendingFinding:
        """Append one SURFACED finding.

        Dedup-safe: if ``(work_unit_sig, rule_id)`` already has ANY line in the
        queue (SURFACED or CONSUMED), this is a no-op that returns the EXISTING
        finding for that key rather than writing a duplicate. The same surfaced
        fix is never queued twice, even across overlapping runner invocations
        whose whitespace-normalized signatures collide (see the runner's D-4).

        Fail-loud (``PendingError``) on an unwritable path.
        """
        existing = self._latest_for_key(finding.key)
        if existing is not None:
            # Already present in some state; do not append a duplicate SURFACED
            # line. Return the line we already have for this key so callers can
            # treat append as idempotent.
            return existing
        return self._append_line(finding)

    def append_many(
        self, findings: Iterable[PendingFinding]
    ) -> List[PendingFinding]:
        """Append a batch (the runner's whole ``AuditResult.fixes``).

        Per-item dedup: each finding goes through the same key check as
        ``append``. Returns the resulting finding per input (the newly written
        one, or the pre-existing one if the key was already queued), preserving
        input order. The dedup set is refreshed from disk once up front and then
        updated in-memory as we write, so a batch that itself contains two
        findings with the same key writes the first and dedups the second within
        the same call.
        """
        # Snapshot current keys once (one read), then track keys we write during
        # this batch so intra-batch duplicates also collapse.
        seen: Dict[Tuple[str, str], PendingFinding] = {}
        for existing in self.read_all():
            # last-line-wins: keep the most recent line per key as the canonical
            # existing finding, matching state() resolution.
            seen[existing.key] = existing

        results: List[PendingFinding] = []
        for finding in findings:
            prior = seen.get(finding.key)
            if prior is not None:
                results.append(prior)
                continue
            written = self._append_line(finding)
            seen[finding.key] = written
            results.append(written)
        return results

    # -- reader side (injection hook) -------------------------------------

    def read_unconsumed(
        self, *, work_unit_sig: Optional[str] = None
    ) -> List[PendingFinding]:
        """Return findings whose CURRENT state is SURFACED (never CONSUMED).

        Ordering is deterministic: newest run first, then a stable tiebreak on
        (confidence descending, rule_id ascending). "Newest run first" is by the
        latest SURFACED line's timestamp, so the most recently produced batch
        surfaces at the top.

        An optional ``work_unit_sig`` filter restricts to one signature.

        A missing file returns ``[]`` -- FAIL-SAFE: no queue yet is a legitimate
        empty, NOT an error. This is the exact read path the UserPromptSubmit
        injection hook takes, and it must never wedge a prompt, so it never
        raises for a missing file. (A genuinely unreadable/corrupt-and-strict
        file is a separate, opt-in condition; the default here is tolerant.)
        """
        if not self.path.exists():
            return []

        # Resolve current state (last-line-wins) and keep the payload-bearing
        # SURFACED line for each key that is currently SURFACED.
        current_event: Dict[Tuple[str, str], str] = {}
        latest_surfaced: Dict[Tuple[str, str], PendingFinding] = {}

        for finding in self.read_all():
            current_event[finding.key] = finding.event
            if finding.event == SURFACED:
                # Keep the most recent SURFACED line (it carries the fix payload).
                latest_surfaced[finding.key] = finding

        unconsumed: List[PendingFinding] = []
        for key, event in current_event.items():
            if event != SURFACED:
                continue
            finding = latest_surfaced.get(key)
            if finding is None:
                # Current state is SURFACED but we somehow have no payload line;
                # skip rather than emit a payload-less proposal.
                continue
            if work_unit_sig is not None and finding.work_unit_sig != work_unit_sig:
                continue
            unconsumed.append(finding)

        # Newest run first (by ts desc), then confidence desc, then rule_id asc.
        # ts is negated for descending; confidence negated likewise; rule_id
        # kept ascending for a stable, deterministic final order.
        unconsumed.sort(key=lambda f: (-f.ts, -f.confidence, f.rule_id))
        return unconsumed

    # -- consume protocol (injection hook, after emitting) ----------------

    def mark_consumed(self, findings: Iterable[PendingFinding]) -> int:
        """Append one CONSUMED line per finding key. Returns the count newly
        consumed.

        Idempotent: marking a key whose current state is already CONSUMED appends
        nothing and is not counted. This is the atomic "consume" step -- after it
        runs, ``read_unconsumed`` returns zero for those keys, so a second inject
        run is a silent no-op (see verify_spec step d). A CONSUMED line omits the
        fix payload; the SURFACED line already holds it and last-line-wins
        resolves the state to CONSUMED.

        The ordering contract at the hook level is emit-then-mark: the injection
        hook emits the findings on additionalContext FIRST, then calls this. That
        yields at-most-once delivery (never nag twice), matching the v1 anti-nag
        posture.
        """
        state = self.state()
        newly = 0
        # De-dup input keys within this call so passing the same finding twice
        # marks it once.
        marked_this_call: set = set()
        for finding in findings:
            key = finding.key
            if key in marked_this_call:
                continue
            if state.get(key) == CONSUMED:
                # Already consumed; idempotent no-op, not counted.
                marked_this_call.add(key)
                continue
            consumed_line = PendingFinding(
                work_unit_sig=finding.work_unit_sig,
                rule_id=finding.rule_id,
                kind=finding.kind,
                fix={},  # CONSUMED lines carry no payload
                run_id=finding.run_id,
                event=CONSUMED,
            )
            self._append_line(consumed_line)
            marked_this_call.add(key)
            newly += 1
        return newly

    # -- state / introspection --------------------------------------------

    def state(self) -> Dict[Tuple[str, str], str]:
        """Return the current state map: key -> latest event string.

        Last-line-wins over the append-only history, mirroring ``Ledger.state``.
        A CONSUMED line for a key overrides an earlier SURFACED line for the same
        key.
        """
        state: Dict[Tuple[str, str], str] = {}
        for finding in self.read_all():
            state[finding.key] = finding.event
        return state

    def counts(self) -> Dict[str, int]:
        """Return a current-state tally: {"SURFACED": n, "CONSUMED": m}.

        Counts are over the resolved current state per key (last-line-wins), not
        over raw lines, so a key that was SURFACED then CONSUMED counts once, as
        CONSUMED.
        """
        tally = {SURFACED: 0, CONSUMED: 0}
        for event in self.state().values():
            tally[event] = tally.get(event, 0) + 1
        return tally

    # -- internals --------------------------------------------------------

    def _latest_for_key(
        self, key: Tuple[str, str]
    ) -> Optional[PendingFinding]:
        """Return the last line written for ``key``, or None if the key has never
        appeared in the queue. Used by ``append`` for its any-line dedup check."""
        latest: Optional[PendingFinding] = None
        for finding in self.read_all():
            if finding.key == key:
                latest = finding
        return latest
