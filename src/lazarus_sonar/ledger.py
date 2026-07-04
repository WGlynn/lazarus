"""Append-only JSONL ledger for Lazarus/Sonar.

The ledger records every verdict Lazarus reaches about a (work-unit, rule)
pair so the same rule is never re-surfaced after it has already been judged
irrelevant for the same work. This is the anti-nag property.

Records are keyed on (work_unit_sig, rule_id):

  work_unit_sig  sha256 of the normalized work-unit text. Reruns of the same
                 diff or response collapse to one signature, so an audit that
                 runs twice does not double-count.
  rule_id        the corpus file's stable identifier (usually its relative
                 path or slug), assigned by Sonar.

Three verdict states are written:

  SURFACED   Lazarus judged the rule would have changed the finished work and
             emitted a proposed retroactive fix. It is a proposal only. No
             file is touched.
  ACTIONED   a human applied the proposed fix (recorded via `lazarus ledger
             action`). Applying is always a separate human step; there is no
             autoapply path in v1.
  DECLINED   the rule was judged irrelevant for this work, or a SURFACED item
             was dismissed by the human (via `lazarus ledger decline`). A
             DECLINED (sig, rule_id) is suppressed on future audits of the
             same signature.

Suppression is signature-scoped, not a permanent per-rule mute. Substantially
different work produces a different signature and gets a fresh look. That is a
deliberate limit: a rule you decline for one diff will resurface on an
unrelated diff. There is no semantic mute in v1.

The file format is one JSON object per line (JSONL). Writes are append-only:
mutators append a new record rather than editing prior lines, so the ledger is
a full audit trail and never rewrites history. State for a given key is the
last record written for that key.

Durability trade (flush yes, fsync no)
--------------------------------------
Each write flushes the Python buffer to the OS page cache (`fh.flush()`) but
does NOT call `os.fsync()` per record. The rationale: this ledger is written on
the Stop / PostToolUse hot path -- once per finished turn and once per
Edit/Write -- and an fsync-per-line would add a synchronous disk-sync stall to
every one of those calls, on the critical path of the user's session.

The ledger is advisory anti-nag state: append-only, single-writer, and fully
reconstructable. Flushing to the OS page cache means a crash of the Python
process (the common case) loses nothing. The only loss window is a full OS or
power crash, which can drop the last few not-yet-synced lines; the worst
consequence is that one already-judged rule re-surfaces once on the next audit,
which is self-healing and never corrupts the file. This is the standard "flush,
don't fsync" trade for an advisory append log. Concurrent multi-writer
coordination remains out of scope for v1 (single hook/CLI writer).

Stdlib-only. This module has no third-party dependencies so the ledger and
Sonar are usable offline, without the judge extra installed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

__all__ = [
    "SURFACED",
    "ACTIONED",
    "DECLINED",
    "VERDICTS",
    "work_unit_signature",
    "LedgerRecord",
    "Ledger",
    "LedgerError",
]

# ---------------------------------------------------------------------------
# Verdict constants
# ---------------------------------------------------------------------------

SURFACED = "SURFACED"
ACTIONED = "ACTIONED"
DECLINED = "DECLINED"

VERDICTS = (SURFACED, ACTIONED, DECLINED)

# Current on-disk record schema version. Bumped only on a breaking change to
# the record shape; readers tolerate older/newer versions and ignore unknown
# fields so a mixed-version ledger still loads.
SCHEMA_VERSION = 1


class LedgerError(Exception):
    """Raised on unrecoverable ledger problems (unwritable path, corrupt
    line that cannot be skipped, invalid verdict). Fail-loud by design: the
    caller surfaces this rather than silently continuing."""


# ---------------------------------------------------------------------------
# Work-unit signature
# ---------------------------------------------------------------------------

# Collapse runs of whitespace so cosmetic reflow of the same diff/response
# does not change the signature. We intentionally do NOT strip content, only
# normalize spacing and trailing whitespace, so meaningfully different work
# yields a different signature.
_WS_RUN = re.compile(r"[ \t]+")
_BLANK_RUN = re.compile(r"\n{3,}")


def normalize_work_unit(text: str) -> str:
    """Normalize a work-unit into a stable canonical string for hashing.

    - normalize line endings to \\n
    - strip trailing whitespace on each line
    - collapse runs of spaces/tabs within a line to a single space
    - collapse 3+ blank lines to a single blank line
    - strip leading/trailing blank lines

    This keeps the signature stable across trivial reformatting while leaving
    real content changes (a new dependency, a changed log line) distinct.
    """
    if text is None:
        raise LedgerError("cannot normalize a None work-unit")
    if not isinstance(text, str):
        raise LedgerError(f"work-unit must be str, got {type(text).__name__}")

    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WS_RUN.sub(" ", line).rstrip() for line in unified.split("\n")]
    joined = "\n".join(lines)
    joined = _BLANK_RUN.sub("\n\n", joined)
    return joined.strip("\n")


def work_unit_signature(text: str) -> str:
    """Return the sha256 hex digest of the normalized work-unit.

    Reruns of the same diff or response collapse to one signature, which is
    what lets the ledger suppress a rule already judged for this work.
    """
    normalized = normalize_work_unit(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


@dataclass
class LedgerRecord:
    """One line in the ledger.

    Required identity fields:
        verdict         one of SURFACED / ACTIONED / DECLINED
        work_unit_sig   sha256 of the normalized work-unit
        rule_id         corpus file identifier
        ts              unix epoch seconds (float) when written

    Proposal payload (present on SURFACED, carried forward on ACTIONED/DECLINED
    when known so the trail is self-contained):
        where           span/location in the finished work the rule would touch
        patch           the proposed retroactive fix (text; a proposal, never
                        auto-applied)
        confidence      judge confidence 0.0..1.0
        reason          judge's one-line rationale

    Context (optional, best-effort provenance):
        kind            work-unit kind: "diff", "response", "decision", ...
        source          short label for where the work-unit came from
        note            free-form note attached by a mutator (e.g. decline
                        reason supplied by the human)
        schema          record schema version
    """

    verdict: str
    work_unit_sig: str
    rule_id: str
    ts: float = field(default_factory=time.time)
    where: str = ""
    patch: str = ""
    confidence: Optional[float] = None
    reason: str = ""
    kind: str = ""
    source: str = ""
    note: str = ""
    schema: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise LedgerError(
                f"invalid verdict {self.verdict!r}; expected one of {VERDICTS}"
            )
        if not self.work_unit_sig:
            raise LedgerError("ledger record requires a non-empty work_unit_sig")
        if not self.rule_id:
            raise LedgerError("ledger record requires a non-empty rule_id")

    @property
    def key(self) -> Tuple[str, str]:
        """The dedup/state key: (work_unit_sig, rule_id)."""
        return (self.work_unit_sig, self.rule_id)

    def to_json(self) -> str:
        """Serialize to a single JSONL line (no trailing newline)."""
        payload = {
            "schema": self.schema,
            "ts": round(self.ts, 3),
            "verdict": self.verdict,
            "work_unit_sig": self.work_unit_sig,
            "rule_id": self.rule_id,
            "where": self.where,
            "patch": self.patch,
            "confidence": self.confidence,
            "reason": self.reason,
            "kind": self.kind,
            "source": self.source,
            "note": self.note,
        }
        # sort_keys for stable, diffable lines; ensure_ascii off so unicode in
        # rules/patches survives round-trip.
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def as_dict(self) -> dict:
        """Return a JSON-ready dict keyed for the CLI renderer.

        This is the shape the `lazarus ledger` subcommands emit: the verdict is
        exposed as ``status`` and the timestamp as ``timestamp`` (the keys the
        table renderer and JSON path read), with the proposal payload carried
        alongside. It matches the row shape produced by ``Ledger.entries`` so a
        single record and a list of rows serialize identically.
        """
        return {
            "status": self.verdict,
            "rule_id": self.rule_id,
            "work_unit_sig": self.work_unit_sig,
            "timestamp": self.ts,
            "note": self.note,
            "where": self.where,
            "patch": self.patch,
            "confidence": self.confidence,
            "reason": self.reason,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LedgerRecord":
        """Build a record from a parsed JSON object, tolerating unknown fields
        and older records that omit newer keys."""
        try:
            verdict = data["verdict"]
            work_unit_sig = data["work_unit_sig"]
            rule_id = data["rule_id"]
        except KeyError as exc:
            raise LedgerError(f"ledger line missing required field: {exc}") from exc

        conf = data.get("confidence")
        if conf is not None:
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                conf = None

        return cls(
            verdict=verdict,
            work_unit_sig=work_unit_sig,
            rule_id=rule_id,
            ts=float(data.get("ts", time.time())),
            where=str(data.get("where", "")),
            patch=str(data.get("patch", "")),
            confidence=conf,
            reason=str(data.get("reason", "")),
            kind=str(data.get("kind", "")),
            source=str(data.get("source", "")),
            note=str(data.get("note", "")),
            schema=int(data.get("schema", SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class Ledger:
    """Append-only JSONL ledger with anti-nag suppression.

    Usage:

        led = Ledger(".lazarus/ledger.jsonl")
        if not led.is_declined(sig, rule_id):
            ... send to judge ...
        led.surface(record)          # write a proposed fix
        led.decline(sig, rule_id)    # human dismissed it
        led.action(sig, rule_id)     # human applied it

    The file is created lazily on first write. Reads tolerate a missing file
    (empty ledger). State for a key is the last record written for that key;
    the full line history is preserved as an audit trail.
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
                raise LedgerError(
                    f"cannot create ledger directory {parent}: {exc}"
                ) from exc

    def _append(self, record: LedgerRecord) -> LedgerRecord:
        """Append one record as a JSONL line. Append-only: never rewrites
        existing lines."""
        self._ensure_parent()
        line = record.to_json()
        try:
            # Line-buffered append. Each record is a single write of one line,
            # which is atomic enough for the single-writer hook/CLI usage here;
            # concurrent multi-writer coordination is out of scope for v1.
            #
            # We flush the Python buffer to the OS page cache but deliberately
            # do NOT os.fsync() per write: this runs on the Stop/PostToolUse hot
            # path and an fsync-per-line would stall every finished turn and
            # every Edit/Write on a synchronous disk sync. The ledger is
            # advisory, append-only, and reconstructable, so the flush-not-fsync
            # trade is correct here. See the module docstring for the full
            # durability rationale.
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
                fh.write("\n")
                fh.flush()
        except OSError as exc:
            raise LedgerError(
                f"cannot write to ledger {self.path}: {exc}"
            ) from exc
        return record

    def read_all(self, *, skip_corrupt: bool = True) -> List[LedgerRecord]:
        """Read every record in file order.

        Blank lines are ignored. A corrupt line (unparseable JSON or a record
        missing required fields) is skipped when skip_corrupt is True (the
        default, so one bad line never bricks the ledger) and raises otherwise.
        """
        if not self.path.exists():
            return []

        records: List[LedgerRecord] = []
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for lineno, raw in enumerate(fh, start=1):
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        data = json.loads(stripped)
                        records.append(LedgerRecord.from_dict(data))
                    except (json.JSONDecodeError, LedgerError) as exc:
                        if skip_corrupt:
                            continue
                        raise LedgerError(
                            f"corrupt ledger line {lineno} in {self.path}: {exc}"
                        ) from exc
        except OSError as exc:
            raise LedgerError(
                f"cannot read ledger {self.path}: {exc}"
            ) from exc
        return records

    def __iter__(self) -> Iterator[LedgerRecord]:
        return iter(self.read_all())

    # -- state ------------------------------------------------------------

    def state(self) -> Dict[Tuple[str, str], LedgerRecord]:
        """Return the current state map: key -> last record written for key.

        Because the file is append-only and time-ordered, the last line wins.
        This is how a later DECLINE overrides an earlier SURFACE for the same
        (sig, rule_id), and how an ACTION overrides a prior SURFACE.
        """
        state: Dict[Tuple[str, str], LedgerRecord] = {}
        for record in self.read_all():
            state[record.key] = record
        return state

    def get(self, work_unit_sig: str, rule_id: str) -> Optional[LedgerRecord]:
        """Return the current record for (sig, rule_id), or None if the key
        has never been written."""
        target = (work_unit_sig, rule_id)
        latest: Optional[LedgerRecord] = None
        for record in self.read_all():
            if record.key == target:
                latest = record
        return latest

    def verdict_of(self, work_unit_sig: str, rule_id: str) -> Optional[str]:
        """Return the current verdict string for a key, or None if unseen."""
        record = self.get(work_unit_sig, rule_id)
        return record.verdict if record is not None else None

    # -- anti-nag ---------------------------------------------------------

    def is_declined(self, work_unit_sig: str, rule_id: str) -> bool:
        """True if this (sig, rule_id) has been DECLINED.

        Lazarus calls this before sending a candidate to the judge and drops
        any declined pair, so a rule already judged irrelevant for this work
        is never re-surfaced for the same work. Signature-scoped: a different
        work-unit has a different signature and is not suppressed.
        """
        return self.verdict_of(work_unit_sig, rule_id) == DECLINED

    def declined_rule_ids(self, work_unit_sig: str) -> set[str]:
        """All rule_ids currently DECLINED for a given work-unit signature.

        Convenience for Lazarus to filter a whole candidate shortlist in one
        pass instead of calling is_declined per candidate.
        """
        declined: set[str] = set()
        state = self.state()
        for (sig, rule_id), record in state.items():
            if sig == work_unit_sig and record.verdict == DECLINED:
                declined.add(rule_id)
        return declined

    def suppress(
        self, work_unit_sig: str, candidate_rule_ids: Iterable[str]
    ) -> List[str]:
        """Given a work-unit signature and candidate rule_ids, return the
        subset that is NOT currently declined, preserving input order.

        This is the anti-nag filter Lazarus applies to Sonar's shortlist.
        """
        declined = self.declined_rule_ids(work_unit_sig)
        return [rid for rid in candidate_rule_ids if rid not in declined]

    # -- writes / mutators ------------------------------------------------

    def write(self, record: LedgerRecord) -> LedgerRecord:
        """Append an already-built record. Prefer the typed helpers below."""
        return self._append(record)

    def surface(
        self,
        work_unit_sig: str,
        rule_id: str,
        *,
        where: str = "",
        patch: str = "",
        confidence: Optional[float] = None,
        reason: str = "",
        kind: str = "",
        source: str = "",
    ) -> LedgerRecord:
        """Record a SURFACED proposed retroactive fix.

        This is a proposal only. Nothing in the user's files or the finished
        work is modified. Applying the patch is a separate human action logged
        with `action()`.
        """
        record = LedgerRecord(
            verdict=SURFACED,
            work_unit_sig=work_unit_sig,
            rule_id=rule_id,
            where=where,
            patch=patch,
            confidence=confidence,
            reason=reason,
            kind=kind,
            source=source,
        )
        return self._append(record)

    def action(
        self,
        work_unit_sig: str,
        rule_id: str,
        *,
        note: Optional[str] = "",
    ) -> LedgerRecord:
        """Record that a human APPLIED the proposed fix for (sig, rule_id).

        Called by `lazarus ledger action`. Carries the prior proposal payload
        forward (where/patch/reason/confidence) if a SURFACED record exists,
        so the ACTIONED line is self-contained. Raises if the key was never
        surfaced, since actioning an unseen proposal is almost always a typo
        in the rule_id or signature.

        `note` accepts None (the CLI passes `getattr(args, "note", None)`) and
        coerces it to the empty string so the record always stores a str.
        """
        note = note or ""
        prior = self.get(work_unit_sig, rule_id)
        if prior is None:
            raise LedgerError(
                f"cannot action ({work_unit_sig[:12]}..., {rule_id}): "
                "no prior ledger entry for this key"
            )
        record = LedgerRecord(
            verdict=ACTIONED,
            work_unit_sig=work_unit_sig,
            rule_id=rule_id,
            where=prior.where,
            patch=prior.patch,
            confidence=prior.confidence,
            reason=prior.reason,
            kind=prior.kind,
            source=prior.source,
            note=note,
        )
        return self._append(record)

    def decline(
        self,
        work_unit_sig: str,
        rule_id: str,
        *,
        note: Optional[str] = "",
        where: str = "",
        patch: str = "",
        confidence: Optional[float] = None,
        reason: str = "",
        kind: str = "",
        source: str = "",
    ) -> LedgerRecord:
        """Record a DECLINED verdict for (sig, rule_id).

        Two callers:
          - Lazarus, when the judge says the rule would NOT have changed the
            work (a killed candidate). It passes where/reason/etc. from the
            judge verdict.
          - `lazarus ledger decline`, when a human dismisses a SURFACED item.
            It typically passes only a note.

        After this, is_declined() returns True for the pair and Lazarus will
        not re-surface it for the same work-unit signature. The suppression is
        signature-scoped, not a permanent per-rule mute.

        `note` accepts None (the CLI passes `getattr(args, "note", None)`) and
        coerces it to the empty string so the record always stores a str.
        """
        note = note or ""
        prior = self.get(work_unit_sig, rule_id)
        record = LedgerRecord(
            verdict=DECLINED,
            work_unit_sig=work_unit_sig,
            rule_id=rule_id,
            where=where or (prior.where if prior else ""),
            patch=patch or (prior.patch if prior else ""),
            confidence=confidence if confidence is not None
            else (prior.confidence if prior else None),
            reason=reason or (prior.reason if prior else ""),
            kind=kind or (prior.kind if prior else ""),
            source=source or (prior.source if prior else ""),
            note=note,
        )
        return self._append(record)

    # -- reporting --------------------------------------------------------

    def show(
        self,
        *,
        work_unit_sig: Optional[str] = None,
        verdict: Optional[str] = None,
        latest_only: bool = True,
    ) -> List[LedgerRecord]:
        """Return records for `lazarus ledger show`.

        Filters:
          work_unit_sig   restrict to one signature (or a unique prefix of it)
          verdict         restrict to SURFACED / ACTIONED / DECLINED
          latest_only     when True (default) return the current state per key;
                          when False return the full append-only history.

        Results are sorted by timestamp.
        """
        if verdict is not None and verdict not in VERDICTS:
            raise LedgerError(
                f"invalid verdict filter {verdict!r}; expected one of {VERDICTS}"
            )

        if latest_only:
            source_records = list(self.state().values())
        else:
            source_records = self.read_all()

        def sig_matches(rec: LedgerRecord) -> bool:
            if work_unit_sig is None:
                return True
            return rec.work_unit_sig.startswith(work_unit_sig)

        selected = [
            rec
            for rec in source_records
            if sig_matches(rec) and (verdict is None or rec.verdict == verdict)
        ]
        selected.sort(key=lambda r: r.ts)
        return selected

    def entries(
        self,
        *,
        work_unit_sig: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[dict]:
        """CLI-facing read alias over `show`, returning JSON-ready dicts.

        This is what `lazarus ledger show` consumes. `status` is the CLI's name
        for the verdict filter and maps onto `show(verdict=...)`; it is
        validated against VERDICTS (raising LedgerError on a bad value, the same
        as `show`). Each row is `LedgerRecord.as_dict()`, so the verdict is
        exposed as ``status`` and the timestamp as ``timestamp`` -- the keys the
        table renderer and JSON path read.
        """
        if status is not None and status not in VERDICTS:
            raise LedgerError(
                f"invalid status filter {status!r}; expected one of {VERDICTS}"
            )
        records = self.show(work_unit_sig=work_unit_sig, verdict=status)
        return [record.as_dict() for record in records]

    def counts(self) -> Dict[str, int]:
        """Return current-state counts per verdict, for a one-line summary."""
        tally = {SURFACED: 0, ACTIONED: 0, DECLINED: 0}
        for record in self.state().values():
            tally[record.verdict] = tally.get(record.verdict, 0) + 1
        return tally
