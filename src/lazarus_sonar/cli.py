"""Command-line entry point for LAZARUS + SONAR.

`lazarus` is the shared entry point for humans and for Claude Code hooks. It
wraps the organs of the tool:

    lazarus sonar    -- run the SONAR sweep only and print the raw candidate
                        shortlist. This is the high-recall firehose stage; it is
                        useful for debugging the corpus and the scorer, but its
                        output is deliberately noisy and is not meant to be acted
                        on directly.

    lazarus audit    -- run the full SONAR -> LAZARUS retro-audit on a finished
                        work-unit. SONAR gathers candidates, the DECLINED ledger
                        suppresses anything already judged irrelevant for this
                        work, and the judge model applies the precision filter
                        (would applying this buried rule have CHANGED the finished
                        work?). Surviving retroactive-fixes are printed and
                        recorded in the ledger as SURFACED. It proposes fixes; it
                        never edits your files.

    lazarus ledger   -- inspect and mutate the append-only ledger:
                          show     list entries (optionally scoped to a work-unit)
                          action   record that a surfaced fix was applied
                          decline  record that a surfaced fix was dismissed

    lazarus async    -- inspect the v2 asynchronous PENDING-FINDINGS queue (the
                        off-critical-path transport added in v2). Read-only:
                          show     list findings still awaiting injection
                          counts   tally SURFACED vs CONSUMED
                        This is the async twin of `ledger show`: the ledger records
                        judge VERDICTS (anti-nag); the pending queue records
                        SURFACED findings AWAITING INJECTION and whether they were
                        consumed. Two separate files, two separate jobs. This CLI
                        group only READS the queue -- it never audits, never spawns
                        the background runner (that is `lazarus-audit-bg`), never
                        marks findings consumed (that is the UserPromptSubmit inject
                        hook), and never applies a fix.

Design contract (see the repo README):

  * Portable and parameterized. The corpus location and file globs come from
    config, never from hardcoded paths. This module resolves config once and
    passes it down. The v2 pending-queue location likewise comes from config
    (``config.pending_path``); there is no hardcoded async location.

  * Fail-loud on missing input. A missing config, a missing corpus, an empty
    or unreadable work-unit, or (for `audit`) a missing API key produces a
    visible stderr message and a non-zero exit. There is no silent no-op. The
    ONE deliberate exception is the async read path: a not-yet-created pending
    queue is a legitimate empty, not an error (it mirrors the inject hook, which
    must never wedge a prompt), so `lazarus async show/counts` prints an empty
    result rather than failing when no queue exists yet.

  * Proposes, never auto-applies. Nothing in this CLI writes to the corpus or
    to the finished work. Applying a proposed fix is a separate, explicit human
    action recorded via `lazarus ledger action`. The async group is read-only
    and does not even mutate the pending queue.

  * Additive v2. The v1 sync surface below (`sonar`, `audit`, `ledger`) is
    unchanged. `async` is a new, purely additive inspection group; it imports the
    v2 pending module lazily so `sonar` and `ledger` keep their zero-dependency,
    offline load. Every existing invocation behaves byte-for-byte as it did in v1.

Perception vs cognition stays separated exactly as the rest of the package
draws it. This CLI runs SONAR itself (via the config adapter), then hands the
resulting candidate shortlist to LAZARUS; SONAR does not know about the ledger
or the judge, and LAZARUS does not run the sweep. The two never collapse into a
single "do everything" call, so each organ remains independently testable.

The functions here are stdlib-only. The judge (invoked by `audit`) lives in
lazarus.py / judge.py and only imports the optional `anthropic` extra when it
actually runs, so `sonar`, `ledger`, and `async` work with zero third-party
dependencies and offline.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Sequence, TextIO

from . import __version__

# These imports are stdlib-only at module load. The judge dependency (anthropic)
# is imported lazily inside lazarus.py / judge.py, and only when `audit` actually
# calls the model, so importing this CLI never requires the optional extra. The
# v2 async pending module is likewise imported lazily inside the `async` handlers
# (see _load_pending), so `sonar` / `audit` / `ledger` never pay for it.
from .config import Config, ConfigError, load_config
from .ledger import Ledger, LedgerError
from .lazarus import run_lazarus
from .sonar import run_sonar_for_config

# Exit codes. The convention below is used consistently so hooks can branch on
# the specific failure mode rather than a generic non-zero.
EXIT_OK = 0
EXIT_USAGE = 2          # argparse / bad invocation
EXIT_CONFIG = 3         # missing or invalid config, missing corpus
EXIT_INPUT = 4          # missing / empty / unreadable work-unit
EXIT_JUDGE = 5          # judge unavailable (no API key, missing pkg) or judge error
EXIT_LEDGER = 6         # ledger read/write failure
EXIT_PENDING = 7        # pending-queue read/write failure (v2 async)

# The kinds of work-unit `audit` understands. This is passed through to the
# scorer and the judge prompt so both can weight structural signals correctly
# (a diff is scored differently from a finished prose response). It is advisory:
# an unknown kind is accepted and treated as "generic".
WORK_UNIT_KINDS = ("diff", "response", "decision", "generic")


# --------------------------------------------------------------------------- #
# small I/O helpers                                                           #
# --------------------------------------------------------------------------- #

def _eprint(msg: str) -> None:
    """Write a visible, prefixed error to stderr.

    Used for every fail-loud path so that hook logs make the failure obvious
    rather than swallowing it.
    """
    print(f"lazarus: error: {msg}", file=sys.stderr)


def _read_work_unit(
    *,
    from_stdin: bool,
    file_path: Optional[str],
    stdin: TextIO,
) -> str:
    """Read the work-unit text from stdin or a file, fail-loud on empty input.

    Exactly one source is expected. The caller (via argparse) guarantees that
    `--stdin` and `--file` are mutually exclusive, and that at least one is set.
    A source that yields only whitespace is treated as missing input, because an
    empty work-unit is never a legitimate thing to audit and silently returning
    "no candidates" would hide a broken hook.
    """
    if from_stdin:
        try:
            text = stdin.read()
        except OSError as exc:
            raise _InputError(f"could not read work-unit from stdin: {exc}")
        source = "stdin"
    else:
        # file_path is guaranteed non-None here by the argument wiring.
        assert file_path is not None
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except FileNotFoundError:
            raise _InputError(f"work-unit file not found: {file_path}")
        except OSError as exc:
            raise _InputError(f"could not read work-unit file {file_path}: {exc}")
        source = file_path

    if not text.strip():
        raise _InputError(f"work-unit from {source} is empty")
    return text


def _extract_work_unit_from_hook_json(payload: str) -> Optional[str]:
    """Best-effort extraction of a work-unit from a Claude Code hook stdin blob.

    Hooks receive a JSON object on stdin. Depending on the event, the finished
    work-unit lives under a different key. This helper is a convenience for the
    dedicated hook scripts; the primary CLI path treats stdin as raw text so a
    human can pipe a diff directly. It returns None when the payload is not JSON
    or carries no recognizable work-unit field, letting the caller fall back to
    treating the input as raw text.

    Recognized shapes (first match wins):
      * {"diff": "..."}                      -- a raw diff
      * {"tool_input": {"content": "..."}}   -- a Write payload
      * {"tool_input": {"new_string": "..."}}-- an Edit payload
      * {"last_response": "..."}             -- a finished assistant turn
      * {"response": "..."}                  -- alias for the above
      * {"work_unit": "..."}                 -- explicit passthrough
    """
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None

    for key in ("work_unit", "diff", "last_response", "response"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value

    tool_input = obj.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("content", "new_string"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return value

    return None


# --------------------------------------------------------------------------- #
# internal exceptions -> exit codes                                           #
# --------------------------------------------------------------------------- #

class _InputError(Exception):
    """Raised when the work-unit cannot be read or is empty. Maps to EXIT_INPUT."""


# --------------------------------------------------------------------------- #
# rendering                                                                    #
# --------------------------------------------------------------------------- #

def _render_candidates_text(candidates: Sequence[Dict[str, Any]]) -> str:
    """Render the SONAR shortlist as plain text for a human or a boot-context log.

    Each candidate is expected to expose at least `rule_id`, `score`, and
    `path`; a `title` and `matched_terms` are shown when present. Callers pass
    the JSON-ready dict view (``Candidate.as_dict()``), so this renderer never
    reaches into a dataclass. This is the raw firehose view; the header says so,
    so nobody mistakes it for a judged result.
    """
    if not candidates:
        return "SONAR: no candidates above threshold."

    lines: List[str] = [
        f"SONAR shortlist ({len(candidates)} candidate"
        f"{'s' if len(candidates) != 1 else ''}, high-recall -- unjudged):",
    ]
    for rank, cand in enumerate(candidates, start=1):
        rule_id = cand.get("rule_id", "<unknown>")
        score = cand.get("score")
        score_str = f"{score:.3f}" if isinstance(score, (int, float)) else str(score)
        title = cand.get("title")
        path = cand.get("path", "")
        head = f"  {rank:>2}. [{score_str}] {rule_id}"
        if title:
            head += f" -- {title}"
        lines.append(head)
        if path:
            lines.append(f"      {path}")
        matched = cand.get("matched_terms")
        if isinstance(matched, (list, tuple)) and matched:
            preview = ", ".join(str(t) for t in list(matched)[:8])
            lines.append(f"      terms: {preview}")
    return "\n".join(lines)


def _render_fixes_text(fixes: Sequence[Dict[str, Any]]) -> str:
    """Render the surviving LAZARUS retroactive-fixes for a human or a hook log.

    Each fix exposes `rule_id`, `where` (the span it would improve), `patch`
    (the concrete proposed change), `confidence`, and `reason`. Callers pass the
    dict view (``RetroFix.as_dict()``). The rendering is explicit that these are
    proposals: nothing has been applied.
    """
    if not fixes:
        return (
            "LAZARUS: no buried rule would have changed this work. "
            "Nothing to propose."
        )

    lines: List[str] = [
        f"LAZARUS retroactive-fixes ({len(fixes)} proposed -- not applied):",
    ]
    for rank, fix in enumerate(fixes, start=1):
        rule_id = fix.get("rule_id", "<unknown>")
        confidence = fix.get("confidence")
        conf_str = (
            f"{confidence:.2f}" if isinstance(confidence, (int, float)) else str(confidence)
        )
        lines.append(f"  {rank:>2}. {rule_id}  (confidence {conf_str})")
        where = fix.get("where")
        if where:
            lines.append(f"      where:  {where}")
        reason = fix.get("reason")
        if reason:
            lines.append(f"      why:    {reason}")
        patch = fix.get("patch")
        if patch:
            # Indent multi-line patches so they read as a block under the entry.
            for i, patch_line in enumerate(str(patch).splitlines()):
                prefix = "      patch:  " if i == 0 else "              "
                lines.append(f"{prefix}{patch_line}")
    lines.append("")
    lines.append(
        "These are proposals. To apply one, edit the work yourself, then record "
        "it with: lazarus ledger action --work-unit-sig <sig> --rule-id <id>"
    )
    return "\n".join(lines)


def _render_ledger_entries_text(entries: Sequence[Dict[str, Any]]) -> str:
    """Render ledger rows as a fixed-width table for `lazarus ledger show`.

    Rows are the dict view returned by ``Ledger.entries`` (``status`` carries
    the verdict, ``timestamp`` the write time), so the renderer stays a pure
    dict consumer.
    """
    if not entries:
        return "ledger: no entries."

    lines: List[str] = ["ledger entries:"]
    for entry in entries:
        status = str(entry.get("status", "?"))
        rule_id = str(entry.get("rule_id", "?"))
        sig = str(entry.get("work_unit_sig", ""))
        sig_short = sig[:12] if sig else ""
        ts = str(entry.get("timestamp", ""))
        lines.append(f"  {status:<8}  {sig_short:<12}  {rule_id:<32}  {ts}")
        note = entry.get("note")
        if note:
            lines.append(f"            note: {note}")
    return "\n".join(lines)


def _render_pending_findings_text(findings: Sequence[Any]) -> str:
    """Render the v2 pending-queue's UNCONSUMED findings for `lazarus async show`.

    ``findings`` are ``PendingFinding`` objects (as returned by
    ``PendingQueue.read_unconsumed``): newest run first, deterministic tiebreak
    on (confidence desc, rule_id). Each carries a ``fix`` dict that is exactly a
    ``RetroFix.as_dict()``, so the per-finding body reuses the SAME field names
    the ledger/audit renderers use (rule_id, where, reason, confidence) and the
    human sees one voice across sync and async paths.

    The header is explicit that these are asynchronous retro-audit PROPOSALS from
    a previous turn, still awaiting injection and never applied. The footer points
    at the same `lazarus ledger action/decline` verbs the sync renderer names, so
    there is a single documented way to close the loop.
    """
    if not findings:
        return (
            "async: no unconsumed findings. The pending queue is empty (or every "
            "surfaced finding has already been injected and consumed)."
        )

    lines: List[str] = [
        f"async pending-findings ({len(findings)} awaiting injection -- "
        f"proposals, not applied):",
    ]
    for rank, finding in enumerate(findings, start=1):
        fix = getattr(finding, "fix", None) or {}
        rule_id = fix.get("rule_id", getattr(finding, "rule_id", "<unknown>"))
        confidence = fix.get("confidence")
        conf_str = (
            f"{confidence:.2f}" if isinstance(confidence, (int, float)) else str(confidence)
        )
        run_id = getattr(finding, "run_id", "") or ""
        kind = getattr(finding, "kind", "") or ""
        sig = str(getattr(finding, "work_unit_sig", "") or "")
        sig_short = sig[:12] if sig else ""
        head = f"  {rank:>2}. {rule_id}  (confidence {conf_str})"
        meta_bits = [b for b in (
            f"run {run_id}" if run_id else "",
            f"kind {kind}" if kind else "",
            f"sig {sig_short}" if sig_short else "",
        ) if b]
        if meta_bits:
            head += "  [" + ", ".join(meta_bits) + "]"
        lines.append(head)
        title = fix.get("title")
        if title:
            lines.append(f"      title:  {title}")
        where = fix.get("where")
        if where:
            lines.append(f"      where:  {where}")
        reason = fix.get("reason")
        if reason:
            lines.append(f"      why:    {reason}")
        patch = fix.get("patch")
        if patch:
            for i, patch_line in enumerate(str(patch).splitlines()):
                prefix = "      patch:  " if i == 0 else "              "
                lines.append(f"{prefix}{patch_line}")
    lines.append("")
    lines.append(
        "These are asynchronous proposals surfaced off the critical path. They "
        "will be injected on the next prompt, then marked consumed. To apply one, "
        "edit the work yourself, then record it with: lazarus ledger action "
        "--work-unit-sig <sig> --rule-id <id>  (or `lazarus ledger decline ...` "
        "to dismiss it)."
    )
    return "\n".join(lines)


def _render_pending_counts_text(counts: Dict[str, int]) -> str:
    """Render the pending-queue current-state tally for `lazarus async counts`.

    ``counts`` is ``PendingQueue.counts()`` -> {"SURFACED": n, "CONSUMED": m},
    the last-line-wins state tally (mirrors ``Ledger.counts``). SURFACED is the
    number still awaiting injection; CONSUMED is the number already injected.
    """
    surfaced = int(counts.get("SURFACED", 0))
    consumed = int(counts.get("CONSUMED", 0))
    return (
        "async pending-queue state:\n"
        f"  SURFACED (awaiting injection): {surfaced}\n"
        f"  CONSUMED (already injected):   {consumed}"
    )


# --------------------------------------------------------------------------- #
# config resolution shared by subcommands                                     #
# --------------------------------------------------------------------------- #

def _resolve_config(args: argparse.Namespace) -> Config:
    """Load and validate config, applying CLI overrides. Fail-loud on error.

    `--config`, `--corpus`, and `--glob` (repeatable) are the overrides the CLI
    exposes; everything else lives in the TOML file. config.load_config raises
    ConfigError on a missing corpus path or empty globs -- there is no silent
    fallback to scanning home or cwd -- and this function turns that into a
    visible message plus EXIT_CONFIG. The exception is re-raised as a typed
    control-flow signal handled by the dispatch wrapper.

    `--glob` maps to the ``globs`` override key, which config._apply_overrides
    knows how to apply (it rebuilds corpus_globs from the patterns). A single
    `--glob` replaces the config globs entirely; an all-blank set is rejected by
    config, not silently accepted.
    """
    overrides: Dict[str, Any] = {}
    corpus = getattr(args, "corpus", None)
    if corpus:
        overrides["corpus_path"] = corpus
    globs = getattr(args, "glob", None)
    if globs:
        overrides["globs"] = list(globs)
    judge_model = getattr(args, "judge_model", None)
    if judge_model:
        overrides["judge_model"] = judge_model

    return load_config(path=getattr(args, "config", None), overrides=overrides)


# --------------------------------------------------------------------------- #
# subcommand handlers                                                          #
# --------------------------------------------------------------------------- #

def _cmd_sonar(args: argparse.Namespace, *, stdin: TextIO, stdout: TextIO) -> int:
    """`lazarus sonar` -- print the raw SONAR shortlist for a work-unit.

    Perception only. Runs the keyword+structural scorer over the corpus and
    prints the ranked candidates. It does not consult the ledger and does not
    call the judge, so it is cheap, offline, and dependency-free. The output is
    the firehose; treat it as a debugging view of what SONAR can see, not as a
    recommendation.

    The scan goes through ``run_sonar_for_config``, the thin adapter that pulls
    corpus_path / globs / exclude / scoring off the resolved Config and calls the
    pure ``run_sonar`` core. ``--top-n`` overrides the configured shortlist cap.
    """
    config = _resolve_config(args)

    raw = _read_work_unit(
        from_stdin=args.stdin,
        file_path=args.file,
        stdin=stdin,
    )
    # `sonar` treats stdin as raw text (a human piping a diff). If the input
    # happens to be hook JSON, extract the work-unit from it so the same command
    # works from a hook too.
    if args.stdin:
        extracted = _extract_work_unit_from_hook_json(raw)
        if extracted is not None:
            raw = extracted

    candidates = run_sonar_for_config(
        raw,
        config,
        kind=getattr(args, "kind", "generic"),
        top_n=getattr(args, "top_n", None),
    )

    if getattr(args, "json", False):
        json.dump(
            {"candidates": [c.as_dict() for c in candidates]},
            stdout,
            indent=2,
            sort_keys=True,
        )
        stdout.write("\n")
    else:
        stdout.write(_render_candidates_text([c.as_dict() for c in candidates]))
        stdout.write("\n")
    return EXIT_OK


def _cmd_audit(args: argparse.Namespace, *, stdin: TextIO, stdout: TextIO) -> int:
    """`lazarus audit` -- full SONAR -> LAZARUS retro-audit on a finished work-unit.

    Cognition. This handler runs the two organs in order: it first calls SONAR
    (via ``run_sonar_for_config``) to gather the candidate shortlist, then hands
    that shortlist to ``run_lazarus``. LAZARUS drops any candidate whose
    (work-unit signature, rule) is already DECLINED in the ledger, batches the
    survivors to the judge model with the single would-it-change-the-output
    question, keeps only the would_change verdicts above min_confidence, and
    returns an AuditResult carrying the ranked retroactive-fix list plus the run
    accounting. Surviving fixes are recorded as SURFACED so they are both
    auditable and never re-surfaced for the same work.

    Separation of concerns is deliberate: SONAR does not know about the ledger or
    the judge, and LAZARUS does not run the sweep. ``--top-n`` caps the SONAR
    shortlist before it ever reaches LAZARUS.

    This is the one subcommand that needs the judge (and thus the optional
    `anthropic` extra plus an API key). If the judge is unavailable, LAZARUS
    raises a typed error which we surface loudly and turn into EXIT_JUDGE. In a
    Stop / PostToolUse hook context the wrapper (see the hooks/ scripts) is
    responsible for not wedging the session -- this CLI's job is simply to fail
    loud and non-zero.

    Note on v2: this synchronous `audit` is the blocking path. The v2 async
    transport runs the identical engine off the critical path via the detached
    `lazarus-audit-bg` runner and the launcher/inject hooks; it is a transport,
    not a second engine, so `audit` here is untouched.
    """
    config = _resolve_config(args)

    raw = _read_work_unit(
        from_stdin=args.stdin,
        file_path=args.file,
        stdin=stdin,
    )
    # From a hook, stdin is JSON; from a human, stdin is the raw work-unit.
    # Extract from JSON when possible, otherwise use the text verbatim.
    if args.stdin:
        extracted = _extract_work_unit_from_hook_json(raw)
        if extracted is not None:
            raw = extracted

    ledger = _open_ledger(config, getattr(args, "ledger_path", None))
    kind = getattr(args, "kind", "generic")

    # SONAR first (perception): gather the candidate shortlist, capped to --top-n
    # if given. Then LAZARUS (cognition) takes that shortlist positionally, does
    # DECLINED suppression against this ledger, one batched judge call, the
    # precision filter, and (unless --dry-run) records SURFACED entries. LAZARUS
    # proposes only -- it never edits files or the finished work.
    candidates = run_sonar_for_config(
        raw,
        config,
        kind=kind,
        top_n=getattr(args, "top_n", None),
    )
    result = run_lazarus(
        raw,
        candidates,
        config=config,
        ledger=ledger,
        kind=kind,
        record=not getattr(args, "dry_run", False),
    )

    if getattr(args, "json", False):
        json.dump(result.as_dict(), stdout, indent=2, sort_keys=True, default=str)
        stdout.write("\n")
    else:
        stdout.write(_render_fixes_text([f.as_dict() for f in result.fixes]))
        stdout.write("\n")
    return EXIT_OK


def _cmd_ledger_show(args: argparse.Namespace, *, stdout: TextIO) -> int:
    """`lazarus ledger show` -- list ledger entries, optionally scoped.

    With `--work-unit-sig`, only entries for that signature are shown, which is
    the useful view when checking whether a rule was already judged for a given
    diff. `--status` filters by SURFACED / ACTIONED / DECLINED. Read-only.

    ``Ledger.entries`` returns JSON-ready dicts (``status`` for the verdict,
    ``timestamp`` for the write time), so both the JSON and text paths consume
    the rows directly.
    """
    config = _resolve_config(args)
    ledger = _open_ledger(config, getattr(args, "ledger_path", None))

    entries = ledger.entries(
        work_unit_sig=getattr(args, "work_unit_sig", None),
        status=getattr(args, "status", None),
    )
    if getattr(args, "json", False):
        json.dump({"entries": list(entries)}, stdout, indent=2, sort_keys=True, default=str)
        stdout.write("\n")
    else:
        stdout.write(_render_ledger_entries_text(entries))
        stdout.write("\n")
    return EXIT_OK


def _cmd_ledger_action(args: argparse.Namespace, *, stdout: TextIO) -> int:
    """`lazarus ledger action` -- record that a proposed fix was applied.

    This is the human step that closes the loop: LAZARUS proposes, you apply the
    change to your files yourself, then you record it here. Recording ACTIONED is
    append-only and, like DECLINED, suppresses re-surfacing of that (signature,
    rule) for the same work.

    ``Ledger.action`` returns a LedgerRecord; we take its dict view so the JSON
    output and the human summary read the same key set the ``show`` renderer
    uses (``status`` / ``timestamp``).
    """
    config = _resolve_config(args)
    ledger = _open_ledger(config, getattr(args, "ledger_path", None))

    entry = ledger.action(
        work_unit_sig=args.work_unit_sig,
        rule_id=args.rule_id,
        note=getattr(args, "note", None),
    ).as_dict()
    if getattr(args, "json", False):
        json.dump(entry, stdout, indent=2, sort_keys=True, default=str)
        stdout.write("\n")
    else:
        stdout.write(
            f"ledger: recorded ACTIONED for {entry.get('rule_id')} "
            f"on {str(entry.get('work_unit_sig', ''))[:12]}\n"
        )
    return EXIT_OK


def _cmd_ledger_decline(args: argparse.Namespace, *, stdout: TextIO) -> int:
    """`lazarus ledger decline` -- record that a proposed fix was dismissed.

    This is the anti-nag primitive. Once a (signature, rule) is DECLINED, LAZARUS
    drops that candidate before the judge ever runs for the same work-unit, so
    the same buried rule is never re-surfaced after you have already judged it
    irrelevant here. It is signature-scoped, not a permanent per-rule mute:
    substantially different work is a new signature and gets a fresh look.

    ``Ledger.decline`` returns a LedgerRecord; we take its dict view for the same
    reason ``action`` does.
    """
    config = _resolve_config(args)
    ledger = _open_ledger(config, getattr(args, "ledger_path", None))

    entry = ledger.decline(
        work_unit_sig=args.work_unit_sig,
        rule_id=args.rule_id,
        note=getattr(args, "note", None),
    ).as_dict()
    if getattr(args, "json", False):
        json.dump(entry, stdout, indent=2, sort_keys=True, default=str)
        stdout.write("\n")
    else:
        stdout.write(
            f"ledger: recorded DECLINED for {entry.get('rule_id')} "
            f"on {str(entry.get('work_unit_sig', ''))[:12]}\n"
        )
    return EXIT_OK


def _cmd_async_show(args: argparse.Namespace, *, stdout: TextIO) -> int:
    """`lazarus async show` -- list UNCONSUMED findings in the v2 pending queue.

    Read-only inspection of the off-critical-path transport. The background runner
    (`lazarus-audit-bg`) writes surfaced fixes here; the UserPromptSubmit inject
    hook reads and marks them consumed on the next turn. This command lets a human
    look at what is queued between those two events without touching the queue.

    It opens ``PendingQueue(config.pending_path)`` and calls ``read_unconsumed``
    (findings whose current state is SURFACED, newest run first, deterministic
    tiebreak). ``--work-unit-sig`` scopes to one work-unit, mirroring
    ``ledger show --work-unit-sig``.

    Fail-SAFE on a missing queue: a not-yet-created pending file is a legitimate
    empty (no background audit has produced anything yet), NOT an error. This is
    the same read-path posture the inject hook takes; the command prints an empty
    result and exits 0 rather than failing loud. A genuinely corrupt/unwritable
    queue still raises PendingError -> EXIT_PENDING.

    This command never audits, never spawns the runner, and never marks anything
    consumed. It is a pure reader.
    """
    config = _resolve_config(args)
    _require_async_configured(config)

    queue = _open_pending(config)
    findings = queue.read_unconsumed(
        work_unit_sig=getattr(args, "work_unit_sig", None)
    )

    if getattr(args, "json", False):
        json.dump(
            {
                "pending_path": str(config.pending_path),
                "findings": [_pending_finding_as_dict(f) for f in findings],
            },
            stdout,
            indent=2,
            sort_keys=True,
            default=str,
        )
        stdout.write("\n")
    else:
        stdout.write(_render_pending_findings_text(findings))
        stdout.write("\n")
    return EXIT_OK


def _cmd_async_counts(args: argparse.Namespace, *, stdout: TextIO) -> int:
    """`lazarus async counts` -- tally SURFACED vs CONSUMED in the pending queue.

    Read-only. Opens ``PendingQueue(config.pending_path)`` and reports
    ``counts()`` (current-state, last-line-wins): how many surfaced findings are
    still awaiting injection versus already consumed. A missing queue reports all
    zeros rather than failing, matching `async show` and the inject hook.
    """
    config = _resolve_config(args)
    _require_async_configured(config)

    queue = _open_pending(config)
    counts = queue.counts()

    if getattr(args, "json", False):
        json.dump(
            {"pending_path": str(config.pending_path), "counts": dict(counts)},
            stdout,
            indent=2,
            sort_keys=True,
        )
        stdout.write("\n")
    else:
        stdout.write(_render_pending_counts_text(counts))
        stdout.write("\n")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# shared openers                                                               #
# --------------------------------------------------------------------------- #

def _open_ledger(config: Config, ledger_path_override: Optional[str]) -> Ledger:
    """Open the ledger at the configured (or overridden) path.

    A ledger open failure is a hard error: the anti-nag property and the
    SURFACED/ACTIONED/DECLINED audit trail depend on it, so a broken ledger must
    not be silently ignored. Raised LedgerError is turned into EXIT_LEDGER by the
    dispatch wrapper. ``config.ledger_path`` is the flat accessor over
    ``config.ledger.path``; the Ledger constructor takes a path, never a Config.
    """
    path = ledger_path_override or config.ledger_path
    return Ledger(path)


def _open_pending(config: Config) -> Any:
    """Open the v2 pending-findings queue at ``config.pending_path``.

    The pending module is imported lazily here (not at module top) so that
    `sonar`, `audit`, and `ledger` keep their zero-import-cost, offline load and
    never pull the async subpackage. ``PendingQueue`` is stdlib-only, so this
    import cannot fail for want of a third-party package; a genuine ImportError
    would mean a broken install and is surfaced as EXIT_PENDING by the dispatch
    wrapper (via PendingError re-raise below).

    The queue path is read from config only (``config.pending_path``); there is
    no hardcoded async location, matching the ledger's config-only path rule.
    """
    try:
        from .async_.pending import PendingQueue  # lazy: async subpackage
    except Exception as exc:  # noqa: BLE001 - broken/partial install
        raise _PendingImportError(
            f"could not import the v2 async pending module: {exc}. "
            f"This is a v2 feature; ensure the lazarus_sonar.async_ package is "
            f"installed."
        ) from exc
    return PendingQueue(config.pending_path)


def _require_async_configured(config: Config) -> None:
    """Warn (non-fatally) when `async` is inspected while the config mode is sync.

    The pending queue is populated by the async transport. If the operator is in
    `mode = "sync"` (or the v2 hooks are not wired), the queue will normally be
    empty because nothing writes to it. Inspecting it is still legitimate (an
    older async run may have left findings), so this does NOT fail; it emits a
    one-line note to stderr so a confused-empty result is explained. The command
    still reads the queue and exits 0.

    ``config.async_enabled`` is the v2 flat accessor (True when mode == "async").
    Guarded with getattr so a config object built before the v2 [async] additions
    (a defensive belt-and-braces case) does not crash this reader.
    """
    enabled = getattr(config, "async_enabled", None)
    if enabled is False:
        _eprint(
            "note: async mode is 'sync' in this config, so the pending queue is "
            "not being populated by the background runner. Showing whatever is on "
            "disk."
        )


def _pending_finding_as_dict(finding: Any) -> Dict[str, Any]:
    """Best-effort JSON view of a PendingFinding for `async show --json`.

    Prefers the finding's own serializer when present (``to_json`` parsed back,
    or a ``to_dict``); otherwise assembles the documented field set by attribute.
    Keeping this tolerant means the CLI does not hard-couple to a single
    serialization method name on PendingFinding while still emitting the canonical
    keys (work_unit_sig, rule_id, kind, event, run_id, ts, fix).
    """
    to_json = getattr(finding, "to_json", None)
    if callable(to_json):
        try:
            return json.loads(to_json())
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    to_dict = getattr(finding, "to_dict", None)
    if callable(to_dict):
        try:
            return dict(to_dict())
        except (TypeError, ValueError):
            pass
    return {
        "work_unit_sig": getattr(finding, "work_unit_sig", ""),
        "rule_id": getattr(finding, "rule_id", ""),
        "kind": getattr(finding, "kind", ""),
        "event": getattr(finding, "event", ""),
        "run_id": getattr(finding, "run_id", ""),
        "ts": getattr(finding, "ts", None),
        "schema": getattr(finding, "schema", None),
        "fix": getattr(finding, "fix", {}) or {},
    }


# --------------------------------------------------------------------------- #
# argument parser                                                              #
# --------------------------------------------------------------------------- #

def _add_config_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the shared config-override flags to a subparser.

    These are duplicated onto every subcommand rather than placed on the top
    parser so that `lazarus <sub> --corpus ...` works regardless of ordering,
    which is friendlier for hook command strings.
    """
    parser.add_argument(
        "--config",
        metavar="PATH",
        help=(
            "Path to lazarus.config.toml. If omitted, the default search order "
            "is used (./lazarus.config.toml, then $LAZARUS_CONFIG). Missing "
            "corpus.path or globs is a hard error -- there is no fallback to "
            "scanning home or cwd."
        ),
    )
    parser.add_argument(
        "--corpus",
        metavar="DIR",
        help="Override the corpus directory from config.",
    )
    parser.add_argument(
        "--glob",
        action="append",
        metavar="PATTERN",
        help=(
            "Override the corpus file globs (repeatable). Replaces the globs "
            "from config when given at least once."
        ),
    )
    parser.add_argument(
        "--ledger-path",
        metavar="PATH",
        help="Override the ledger JSONL path from config.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human text rendering.",
    )


def _add_work_unit_source_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the mutually-exclusive work-unit source flags.

    Exactly one of --stdin / --file is required. `--stdin` is the hook path
    (Claude Code pipes hook JSON in) and also lets a human pipe a diff directly;
    `--file` points at a saved work-unit on disk. Requiring one of them keeps the
    fail-loud contract: there is no implicit source.
    """
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--stdin",
        action="store_true",
        help=(
            "Read the work-unit from stdin. Accepts either raw text (a piped "
            "diff or response) or a Claude Code hook JSON payload, from which "
            "the work-unit is extracted."
        ),
    )
    source.add_argument(
        "--file",
        metavar="PATH",
        help="Read the work-unit from a file on disk.",
    )
    parser.add_argument(
        "--kind",
        choices=WORK_UNIT_KINDS,
        default="generic",
        help=(
            "The kind of work-unit being audited (default: generic). Weights "
            "structural signals in the scorer and the judge prompt."
        ),
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=None,
        metavar="N",
        help="Cap the SONAR shortlist to the top N candidates (overrides config).",
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct the full argparse tree for the `lazarus` CLI.

    Kept as a standalone function so tests can introspect the parser and so the
    console-script entry point and `python -m lazarus_sonar.cli` share one
    definition.
    """
    parser = argparse.ArgumentParser(
        prog="lazarus",
        description=(
            "LAZARUS + SONAR: retroactive knowledge-audit for a file-based rules "
            "corpus. SONAR surfaces buried-but-relevant rules for a finished "
            "work-unit; LAZARUS applies the precision filter -- would applying "
            "this rule have CHANGED the work? -- and proposes fixes. It proposes; "
            "it never auto-applies."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    # required=True is set after add so the "no command" case yields our own
    # usage message on Python versions where the kwarg is unavailable at add
    # time; see main().
    subparsers.required = False

    # -- sonar ------------------------------------------------------------- #
    sonar_parser = subparsers.add_parser(
        "sonar",
        help="Run the SONAR sweep only and print the raw candidate shortlist.",
        description=(
            "Perception only. Scores the corpus against a work-unit and prints "
            "the ranked candidates. High-recall and deliberately noisy -- this "
            "is the firehose, not a recommendation. No ledger, no judge, no "
            "third-party dependencies."
        ),
    )
    _add_config_arguments(sonar_parser)
    _add_work_unit_source_arguments(sonar_parser)
    sonar_parser.set_defaults(_handler="sonar")

    # -- audit ------------------------------------------------------------- #
    audit_parser = subparsers.add_parser(
        "audit",
        help="Run the full SONAR -> LAZARUS retro-audit and propose fixes.",
        description=(
            "Cognition. SONAR gathers candidates, the DECLINED ledger suppresses "
            "already-judged-irrelevant ones, the judge applies the precision "
            "filter, and surviving retroactive-fixes are printed and recorded as "
            "SURFACED. Requires the judge (the optional [judge] extra and an API "
            "key). Proposes fixes; never edits your files."
        ),
    )
    _add_config_arguments(audit_parser)
    _add_work_unit_source_arguments(audit_parser)
    audit_parser.add_argument(
        "--judge-model",
        metavar="MODEL",
        help=(
            "Override the judge model from config. The judge model is the main "
            "precision knob."
        ),
    )
    audit_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run the audit and print proposed fixes but do NOT record SURFACED "
            "entries in the ledger."
        ),
    )
    audit_parser.set_defaults(_handler="audit")

    # -- ledger ------------------------------------------------------------ #
    ledger_parser = subparsers.add_parser(
        "ledger",
        help="Inspect and mutate the append-only SURFACED/ACTIONED/DECLINED ledger.",
        description=(
            "The ledger is the anti-nag and audit surface. `show` lists entries; "
            "`action` records that a proposed fix was applied; `decline` records "
            "that one was dismissed. ACTIONED and DECLINED both suppress "
            "re-surfacing of that (work-unit signature, rule) for the same work."
        ),
    )
    ledger_sub = ledger_parser.add_subparsers(dest="ledger_command", metavar="<action>")
    ledger_sub.required = False

    show_parser = ledger_sub.add_parser(
        "show",
        help="List ledger entries (optionally scoped to a work-unit or status).",
    )
    _add_config_arguments(show_parser)
    show_parser.add_argument(
        "--work-unit-sig",
        metavar="SIG",
        help="Show only entries for this work-unit signature.",
    )
    show_parser.add_argument(
        "--status",
        choices=("SURFACED", "ACTIONED", "DECLINED"),
        help="Show only entries with this status.",
    )
    show_parser.set_defaults(_handler="ledger_show")

    action_parser = ledger_sub.add_parser(
        "action",
        help="Record that a proposed fix was applied (ACTIONED).",
    )
    _add_config_arguments(action_parser)
    action_parser.add_argument(
        "--work-unit-sig",
        required=True,
        metavar="SIG",
        help="The work-unit signature the fix belongs to.",
    )
    action_parser.add_argument(
        "--rule-id",
        required=True,
        metavar="ID",
        help="The rule that was applied.",
    )
    action_parser.add_argument(
        "--note",
        metavar="TEXT",
        help="Optional free-text note recorded with the entry.",
    )
    action_parser.set_defaults(_handler="ledger_action")

    decline_parser = ledger_sub.add_parser(
        "decline",
        help="Record that a proposed fix was dismissed (DECLINED, anti-nag).",
    )
    _add_config_arguments(decline_parser)
    decline_parser.add_argument(
        "--work-unit-sig",
        required=True,
        metavar="SIG",
        help="The work-unit signature the fix belongs to.",
    )
    decline_parser.add_argument(
        "--rule-id",
        required=True,
        metavar="ID",
        help="The rule that was dismissed as irrelevant for this work.",
    )
    decline_parser.add_argument(
        "--note",
        metavar="TEXT",
        help="Optional free-text note recorded with the entry.",
    )
    decline_parser.set_defaults(_handler="ledger_decline")

    ledger_parser.set_defaults(_ledger_parser=ledger_parser)

    # -- async ------------------------------------------------------------- #
    # v2 additive group: read-only inspection of the off-critical-path pending
    # queue. It is the async twin of `ledger show`. It never audits, never spawns
    # the background runner (`lazarus-audit-bg`), and never marks findings
    # consumed (the UserPromptSubmit inject hook does that). Pure reader.
    async_parser = subparsers.add_parser(
        "async",
        help="Inspect the v2 asynchronous pending-findings queue (read-only).",
        description=(
            "v2 async transport inspection. The background runner writes surfaced "
            "fixes to a pending queue off the critical path; the inject hook "
            "surfaces and consumes them on the next prompt. `show` lists findings "
            "still awaiting injection; `counts` tallies SURFACED vs CONSUMED. "
            "Read-only: this group never audits, never spawns the runner, and "
            "never marks anything consumed. A not-yet-created queue is a "
            "legitimate empty, not an error."
        ),
    )
    async_sub = async_parser.add_subparsers(dest="async_command", metavar="<action>")
    async_sub.required = False

    async_show_parser = async_sub.add_parser(
        "show",
        help="List unconsumed pending findings (optionally scoped to a work-unit).",
    )
    _add_config_arguments(async_show_parser)
    async_show_parser.add_argument(
        "--work-unit-sig",
        metavar="SIG",
        help="Show only pending findings for this work-unit signature.",
    )
    async_show_parser.set_defaults(_handler="async_show")

    async_counts_parser = async_sub.add_parser(
        "counts",
        help="Tally SURFACED vs CONSUMED findings in the pending queue.",
    )
    _add_config_arguments(async_counts_parser)
    async_counts_parser.set_defaults(_handler="async_counts")

    async_parser.set_defaults(_async_parser=async_parser)

    parser.set_defaults(
        _root_parser=parser,
        _ledger_root=ledger_parser,
        _async_root=async_parser,
    )

    return parser


# --------------------------------------------------------------------------- #
# dispatch                                                                      #
# --------------------------------------------------------------------------- #

def _dispatch(
    args: argparse.Namespace,
    *,
    stdin: TextIO,
    stdout: TextIO,
) -> int:
    """Route a parsed namespace to its handler, translating errors to exit codes.

    All fail-loud paths funnel through here: config, input, judge, ledger, and
    pending errors each print a visible stderr message and return their dedicated
    exit code. This keeps every handler free of exit-code plumbing and guarantees
    the CLI never silently no-ops on a failure.
    """
    handler = getattr(args, "_handler", None)
    if handler is None:
        # No subcommand (or a bare `ledger` / `async` with no action). Print
        # usage for the relevant parser and signal a usage error.
        command = getattr(args, "command", None)
        if command == "ledger":
            args._ledger_root.print_help(file=sys.stderr)
        elif command == "async":
            args._async_root.print_help(file=sys.stderr)
        else:
            args._root_parser.print_help(file=sys.stderr)
        return EXIT_USAGE

    try:
        if handler == "sonar":
            return _cmd_sonar(args, stdin=stdin, stdout=stdout)
        if handler == "audit":
            return _cmd_audit(args, stdin=stdin, stdout=stdout)
        if handler == "ledger_show":
            return _cmd_ledger_show(args, stdout=stdout)
        if handler == "ledger_action":
            return _cmd_ledger_action(args, stdout=stdout)
        if handler == "ledger_decline":
            return _cmd_ledger_decline(args, stdout=stdout)
        if handler == "async_show":
            return _cmd_async_show(args, stdout=stdout)
        if handler == "async_counts":
            return _cmd_async_counts(args, stdout=stdout)
    except ConfigError as exc:
        _eprint(str(exc))
        return EXIT_CONFIG
    except _InputError as exc:
        _eprint(str(exc))
        return EXIT_INPUT
    except LedgerError as exc:
        _eprint(str(exc))
        return EXIT_LEDGER
    except PendingError as exc:
        _eprint(str(exc))
        return EXIT_PENDING
    except _PendingImportError as exc:
        _eprint(str(exc))
        return EXIT_PENDING
    except JudgeUnavailable as exc:
        _eprint(str(exc))
        return EXIT_JUDGE

    # Unreachable if the handler table above stays in sync with build_parser().
    _eprint(f"internal error: unknown handler {handler!r}")
    return EXIT_USAGE


# JudgeUnavailable is defined in judge.py; importing it here would pull the judge
# module (and, transitively, an optional-dependency check) into every invocation
# including `sonar` and `ledger`. Instead we import it lazily and fall back to a
# never-matching sentinel so the except clause above is well-formed even when the
# judge module cannot be imported. run_lazarus raises the real class.
try:  # pragma: no cover - trivial import shim
    from .judge import JudgeUnavailable
except Exception:  # noqa: BLE001 - judge module may be unimportable without extras
    class JudgeUnavailable(Exception):  # type: ignore[no-redef]
        """Fallback so the dispatch except clause is always valid.

        The real JudgeUnavailable lives in judge.py and is raised by the judge
        when the `anthropic` package or the API key is missing. If judge.py
        itself cannot be imported (e.g. a partial install), this stand-in keeps
        the CLI importable; `audit` will still fail loud with a clear message
        from run_lazarus.
        """


# PendingError is defined in async_/pending.py; importing it here would pull the
# v2 async subpackage into every invocation including `sonar` and `ledger`, which
# would defeat the zero-cost, offline load of the v1 sync surface. So, exactly as
# with JudgeUnavailable, it is imported lazily with a never-matching sentinel
# fallback. The `async` handlers raise the real class (or _PendingImportError on
# a broken install); both map to EXIT_PENDING in _dispatch above.
try:  # pragma: no cover - trivial import shim
    from .async_.pending import PendingError
except Exception:  # noqa: BLE001 - async subpackage may be unavailable
    class PendingError(Exception):  # type: ignore[no-redef]
        """Fallback so the dispatch except clause is always valid.

        The real PendingError lives in async_/pending.py and is raised on an
        unwritable/corrupt pending queue. If that module cannot be imported, this
        stand-in keeps the CLI importable; the `async` handlers surface the import
        failure as _PendingImportError instead, also mapped to EXIT_PENDING.
        """


class _PendingImportError(Exception):
    """Raised when the v2 async pending module cannot be imported at call time.

    Distinct from PendingError (a runtime queue failure) so the message can point
    the user at the v2 install rather than a corrupt queue. Maps to EXIT_PENDING.
    """


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for the `lazarus` console script and `python -m`.

    Parses `argv` (defaults to sys.argv[1:]), dispatches, and returns an integer
    exit code. Kept thin so it can be called directly from tests. stdin/stdout
    are threaded through explicitly so hooks and tests can substitute streams.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    return _dispatch(args, stdin=sys.stdin, stdout=sys.stdout)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
