#!/usr/bin/env python3
"""SessionStart hook: run a SONAR sweep over the last session's work-unit.

This hook runs at the deterministic boot layer of an agent session. On start it
reads the hook stdin JSON, locates the work-unit produced by the *previous*
session, runs the wide/cheap SONAR keyword sweep over the configured corpus, and
prints the ranked buried-rule candidates as boot context. The intent is
liveness: a rule that is old-but-still-true gets surfaced at the top of the next
session instead of staying buried by age.

What this hook does and does not do
-----------------------------------
- It runs SONAR only (perception/recall). It does NOT call the judge model, so
  it needs no API key and no third-party packages. The precision pass (LAZARUS,
  "would this buried rule have changed the output?") runs separately in the
  Stop / PostToolUse retro-audit hook. What you see here is a candidate
  shortlist, not a verdict.
- It suppresses candidates already recorded as DECLINED for this exact work-unit
  signature, so boot context stays quiet after you have judged a rule irrelevant
  for that work (the anti-nag property). Suppression is signature-scoped;
  substantially different work gets a fresh look.
- It PROPOSES nothing to be auto-applied and touches none of your files. It only
  prints text.

Fail-loud contract
------------------
- Missing config file, or a config whose corpus.path / corpus.globs is missing,
  is a configuration error: this hook prints a visible error to stderr and exits
  non-zero. There is no silent fallback to scanning home or cwd.
- No previous work-unit to sweep is NOT an error. On a fresh checkout, a first-
  ever boot, or a cleared work-unit, there is simply nothing to audit: this hook
  prints a short note to stderr and exits 0. Failing loud there would nag on
  every clean boot.
- A SONAR/corpus read error (unreadable corpus dir, all files skipped) is a
  configuration error and exits non-zero: a sweep that silently finds nothing
  because the corpus path is wrong is worse than one that refuses.

Configuration
-------------
Config path resolution, in order:
  1. --config <path> on the command line.
  2. LAZARUS_CONFIG in the environment.
  3. lazarus.config.toml in the hook's cwd (from stdin), then walking upward.
See lazarus.config.example.toml for the annotated schema.

Where the "last session's work-unit" comes from
----------------------------------------------
A SessionStart hook is not handed a diff, so the previous session's work-unit is
read from a file. There is no config key for this location in v1 (it is not part
of the config schema); resolution order is:
  1. --work-unit <path> on the command line.
  2. LAZARUS_LAST_WORK_UNIT in the environment.
  3. A conventional default: <cwd>/.lazarus/last_work_unit.txt, where a
     session-end hook is expected to have written the finished work. If none of
     these resolves to an existing, non-empty file, there is nothing to sweep
     (clean exit 0).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


# --------------------------------------------------------------------------- #
# Package import (repo layout: hooks/ next to src/lazarus_sonar/)
# --------------------------------------------------------------------------- #
def _import_api():
    """Import the public API, adding the repo's src/ to sys.path if needed.

    Returns the tuple
    ``(load_config, ConfigError, run_sonar_for_config, Ledger, work_unit_signature)``.
    Import failure is a fail-loud install error, not something to swallow.

    Note the canonical seams this hook binds to:
      - ``run_sonar_for_config`` (sonar.py): the config-aware adapter that unpacks
        corpus_path/globs/exclude/scoring off a Config and calls run_sonar. The
        hook never calls bare run_sonar with a ``config=`` kwarg.
      - ``ConfigError`` (config.py): the single exception load_config raises on a
        missing/malformed config or corpus; caught below instead of ValueError/
        KeyError.
      - ``work_unit_signature`` (ledger.py): the module-level signature function,
        used directly so DECLINED suppression here matches what the retro-audit
        hook and CLI recorded. No reflective probing.
    """

    def _load():
        from lazarus_sonar.config import ConfigError, load_config  # type: ignore
        from lazarus_sonar.ledger import Ledger, work_unit_signature  # type: ignore
        from lazarus_sonar.sonar import run_sonar_for_config  # type: ignore

        return (
            load_config,
            ConfigError,
            run_sonar_for_config,
            Ledger,
            work_unit_signature,
        )

    try:
        return _load()
    except ModuleNotFoundError:
        # Not installed on the path. Fall back to the sibling src/ dir so the
        # hook works from a plain checkout without `pip install -e .`.
        here = Path(__file__).resolve()
        src = here.parent.parent / "src"
        if src.is_dir():
            sys.path.insert(0, str(src))
        try:
            return _load()
        except ModuleNotFoundError as exc:
            _die(
                "cannot import the lazarus_sonar package.\n"
                f"  Tried the installed package and {src}.\n"
                "  Install it with `pip install -e .` from the repo root, or run\n"
                "  this hook from within the repo so hooks/ sits next to src/.\n"
                f"  Original import error: {exc}"
            )


# --------------------------------------------------------------------------- #
# stderr / exit helpers
# --------------------------------------------------------------------------- #
_PREFIX = "[lazarus:session_start_sweep]"


def _warn(msg: str) -> None:
    print(f"{_PREFIX} {msg}", file=sys.stderr, flush=True)


def _die(msg: str, code: int = 1) -> "None":
    """Print a visible error and exit non-zero. Used for real misconfiguration."""
    print(f"{_PREFIX} error: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def _clean_exit(msg: str) -> "None":
    """Print an informational note and exit 0. Used when there is simply nothing
    to sweep - a normal boot state, not an error."""
    print(f"{_PREFIX} {msg}", file=sys.stderr, flush=True)
    raise SystemExit(0)


# --------------------------------------------------------------------------- #
# stdin / argument parsing
# --------------------------------------------------------------------------- #
def _read_hook_stdin() -> dict:
    """Read and parse the hook payload from stdin.

    Claude Code delivers hook input as a JSON object on stdin. An empty stdin
    (e.g. the hook invoked by hand for a dry run) is tolerated and treated as an
    empty payload; malformed non-empty JSON is a fail-loud error, because a hook
    that silently misreads its input is exactly the class of quiet failure this
    tool exists to avoid.
    """
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    raw = raw.strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _die(f"hook stdin was not valid JSON: {exc}")
    if not isinstance(payload, dict):
        _die(f"hook stdin JSON must be an object, got {type(payload).__name__}")
    return payload


def _parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="session_start_sweep",
        description=(
            "SessionStart hook: SONAR sweep over the last session's work-unit, "
            "printed as boot context. Reads the hook payload from stdin."
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to lazarus.config.toml (overrides LAZARUS_CONFIG and discovery).",
    )
    parser.add_argument(
        "--work-unit",
        default=None,
        help="Path to the previous session's work-unit file (overrides env/default).",
    )
    parser.add_argument(
        "--max-show",
        type=int,
        default=8,
        help=(
            "Maximum candidates to print as boot context (default 8). SONAR may "
            "return more (up to [sonar].top_n); this caps the boot printout so it "
            "stays a shortlist, not a firehose."
        ),
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #
def _resolve_cwd(hook_payload: dict) -> Path:
    """The session's working directory. Claude Code puts this on the payload as
    `cwd`; fall back to the process cwd."""
    raw = hook_payload.get("cwd")
    if isinstance(raw, str) and raw.strip():
        p = Path(raw).expanduser()
        if p.is_dir():
            return p.resolve()
    return Path.cwd().resolve()


def _discover_config_path(cli_config, cwd: Path) -> Path:
    """Resolve the config file path, fail-loud if none is found.

    Order: --config, then LAZARUS_CONFIG, then lazarus.config.toml discovered by
    walking up from `cwd`.
    """
    if cli_config:
        p = Path(cli_config).expanduser()
        if not p.is_file():
            _die(f"--config path does not exist: {p}")
        return p.resolve()

    env_cfg = os.environ.get("LAZARUS_CONFIG", "").strip()
    if env_cfg:
        p = Path(env_cfg).expanduser()
        if not p.is_file():
            _die(f"LAZARUS_CONFIG points at a nonexistent file: {p}")
        return p.resolve()

    # Walk upward from cwd looking for lazarus.config.toml.
    for directory in [cwd, *cwd.parents]:
        candidate = directory / "lazarus.config.toml"
        if candidate.is_file():
            return candidate.resolve()

    _die(
        "no lazarus.config.toml found.\n"
        f"  Searched from {cwd} upward, and neither --config nor LAZARUS_CONFIG "
        "was set.\n"
        "  Copy lazarus.config.example.toml to lazarus.config.toml and point its "
        "[corpus].path\n"
        "  at your rules/memory directory, or pass --config <path>."
    )


def _resolve_work_unit_path(cli_work_unit, cwd: Path):
    """Resolve the previous session's work-unit file, or return None.

    A missing work-unit is NOT fail-loud - it just means nothing to sweep.

    There is no config key for this location in v1: ``[session_start].work_unit_path``
    is not part of the config schema, so it is not read here. Resolution order:
      1. --work-unit on the command line.
      2. LAZARUS_LAST_WORK_UNIT in the environment.
      3. The conventional <cwd>/.lazarus/last_work_unit.txt.
    """
    candidates: list = []

    if cli_work_unit:
        candidates.append(Path(cli_work_unit).expanduser())

    env_wu = os.environ.get("LAZARUS_LAST_WORK_UNIT", "").strip()
    if env_wu:
        candidates.append(Path(env_wu).expanduser())

    candidates.append(cwd / ".lazarus" / "last_work_unit.txt")

    for cand in candidates:
        try:
            if cand.is_file() and cand.stat().st_size > 0:
                return cand.resolve()
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------- #
# SONAR candidate normalization
# --------------------------------------------------------------------------- #
def _candidate_fields(cand) -> tuple:
    """Extract (rule_id, score, title/path) from a SONAR Candidate.

    run_sonar_for_config returns sonar.Candidate dataclasses, but we read
    defensively (attribute-or-dict) so a benign shape change here does not wedge
    boot. Returns ("", 0.0, "") for anything unreadable rather than raising.
    """

    def get(obj, name, default=None):
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    rule_id = get(cand, "rule_id") or get(cand, "id") or get(cand, "path") or ""
    score = get(cand, "score", 0.0)
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0

    # A human-readable locator: prefer a title, fall back to the path/id.
    title = get(cand, "title") or get(cand, "path") or rule_id or ""
    return (str(rule_id), score, str(title))


# --------------------------------------------------------------------------- #
# Boot-context rendering
# --------------------------------------------------------------------------- #
def _render_boot_context(work_unit_path: Path, shown: list, suppressed: int) -> str:
    lines: list = []
    lines.append("=" * 72)
    lines.append("LAZARUS/SONAR boot sweep - buried-rule candidates")
    lines.append("=" * 72)
    lines.append(
        f"Swept the last session's work-unit ({work_unit_path}) against the "
        "corpus."
    )
    lines.append(
        "These are SONAR candidates (wide keyword recall), NOT verdicts. The "
        "LAZARUS"
    )
    lines.append(
        'precision pass ("would this rule have changed the output?") runs in the '
    )
    lines.append("Stop / PostToolUse retro-audit, and it proposes - never auto-applies.")
    lines.append("")
    for i, (rule_id, score, title) in enumerate(shown, start=1):
        locator = title if title and title != rule_id else rule_id
        lines.append(f"  {i:>2}. {locator}  (rule_id={rule_id}, score={score:.3f})")
    if suppressed:
        lines.append("")
        lines.append(
            f"({suppressed} candidate(s) hidden: already DECLINED for this exact "
            "work-unit - anti-nag.)"
        )
    lines.append("")
    lines.append(
        "Run `lazarus audit` to get the filtered retroactive-fix proposals for "
        "this work-unit."
    )
    lines.append("=" * 72)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: list) -> int:
    args = _parse_args(argv)
    (
        load_config,
        ConfigError,
        run_sonar_for_config,
        Ledger,
        work_unit_signature,
    ) = _import_api()

    payload = _read_hook_stdin()
    cwd = _resolve_cwd(payload)

    # 1. Config - fail-loud on missing config / corpus. load_config raises
    #    ConfigError (a subclass of Exception) for a missing config file, a
    #    missing/malformed corpus.path, or an empty corpus.globs. Catch that, not
    #    ValueError/KeyError.
    config_path = _discover_config_path(args.config, cwd)
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        _die(f"invalid config ({config_path}): {exc}")
    except FileNotFoundError as exc:
        _die(f"config or corpus path missing: {exc}")
    except Exception as exc:  # noqa: BLE001 - surface anything else, loudly
        _die(f"failed to load config ({config_path}): {exc}")

    # config is a Config OBJECT from here on, never a dict. All access is by
    # attribute (config.ledger_path, config.ledger.suppress_declined, ...).

    # 2. Previous work-unit - a missing one is a clean, non-error exit.
    work_unit_path = _resolve_work_unit_path(args.work_unit, cwd)
    if work_unit_path is None:
        _clean_exit(
            "no previous work-unit found to sweep (fresh boot or nothing "
            "recorded); skipping. This is normal on first run."
        )

    try:
        work_unit_text = work_unit_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # We already confirmed the file exists and is non-empty; a read failure
        # now is a real, visible problem.
        _die(f"could not read work-unit file {work_unit_path}: {exc}")

    if not work_unit_text.strip():
        _clean_exit(f"work-unit file {work_unit_path} is effectively empty; skipping.")

    # 3. SONAR sweep - a corpus read failure here is fail-loud. run_sonar_for_config
    #    unpacks corpus_path/globs/exclude/scoring off the Config; kind is advisory
    #    in v1 and has no scoring effect.
    try:
        candidates = run_sonar_for_config(work_unit_text, config, kind="generic")
    except FileNotFoundError as exc:
        _die(f"corpus path is unreadable - check [corpus].path in config: {exc}")
    except Exception as exc:  # noqa: BLE001
        _die(f"SONAR sweep failed: {exc}")

    if not candidates:
        _clean_exit(
            "SONAR found no candidate rules above [sonar].min_score for this "
            "work-unit. Nothing buried to surface."
        )

    # 4. Anti-nag: drop candidates already DECLINED for this work-unit signature.
    suppress = bool(getattr(config.ledger, "suppress_declined", True))
    suppressed_count = 0
    kept = list(candidates)

    if suppress:
        try:
            ledger = Ledger(config.ledger_path)
        except Exception as exc:  # noqa: BLE001
            # A broken ledger must not wedge the boot sweep - surface it, then
            # continue without suppression. This is the one deliberate fail-open:
            # showing a possibly-nagging candidate is better than blocking boot.
            _warn(f"ledger unavailable, showing all candidates without anti-nag: {exc}")
            ledger = None

        if ledger is not None:
            # Compute the signature exactly as the ledger, retro-audit hook, and
            # CLI do, so DECLINED suppression keys line up across all writers.
            try:
                sig = work_unit_signature(work_unit_text)
            except Exception as exc:  # noqa: BLE001
                sig = ""
                _warn(
                    "could not compute work-unit signature "
                    f"({exc}); skipping anti-nag suppression for this boot."
                )

            if sig:
                filtered = []
                for cand in candidates:
                    rule_id, _score, _title = _candidate_fields(cand)
                    declined = False
                    if rule_id:
                        try:
                            declined = bool(ledger.is_declined(sig, rule_id))
                        except Exception:  # noqa: BLE001
                            declined = False
                    if declined:
                        suppressed_count += 1
                    else:
                        filtered.append(cand)
                kept = filtered

    if not kept:
        _clean_exit(
            f"all {suppressed_count} SONAR candidate(s) were already DECLINED for "
            "this work-unit (anti-nag); nothing new to surface."
        )

    # 5. Cap the boot printout to a readable shortlist.
    max_show = max(1, int(args.max_show))
    shown = [_candidate_fields(c) for c in kept[:max_show]]

    boot_context = _render_boot_context(work_unit_path, shown, suppressed_count)

    # SessionStart hooks inject boot context via stdout. Print there.
    print(boot_context, flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        _warn("interrupted")
        raise SystemExit(130)
