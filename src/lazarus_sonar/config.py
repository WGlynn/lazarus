"""Configuration loading and validation for Lazarus/Sonar.

Config is a single TOML file (see ``lazarus.config.example.toml``). This module
reads it with the standard library (``tomllib`` on 3.11+, ``tomli`` on 3.9-3.10),
validates it, resolves relative paths against the config file's directory, and
merges CLI overrides.

Fail-loud contract
------------------
There is NO silent fallback to scanning the home directory or the current working
directory. If ``corpus.path`` or ``corpus.globs`` is missing, empty, or the wrong
type, loading raises :class:`ConfigError` with a message that names the offending
key and points at the config file. A hook that can't find its corpus should stop
loudly, not quietly no-op over an empty file set. This mirrors the same
fail-on-missing-input discipline the hooks themselves apply.

Nothing here talks to the network or the judge model; that lives in ``judge.py``.
The only judge-related thing config carries is the model id string (and, if the
user sets it, an optional API key that is normally left unset in favour of the
``ANTHROPIC_API_KEY`` environment variable).

One SONAR-knob type
-------------------
The SONAR scoring parameters live in exactly one place: ``sonar.ScoringConfig``.
This module imports that type, stores an instance on ``Config.scoring``, and does
NOT define its own copy. Keeping a single source of truth for the scoring knobs
prevents the two-divergent-copies drift that this contract exists to kill.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

# The ONE SONAR-knob type. Defined in sonar.py (next to the scorer that actually
# consumes it) and re-exported here so callers that only import config still see
# it. config.py must never define a second copy of these knobs.
from .sonar import ScoringConfig

# tomllib is stdlib on 3.11+. On 3.9-3.10 it isn't, so fall back to the `tomli`
# backport, which exposes the same `load(fp)` / `loads(str)` API. `tomli` is
# declared as a BASE, version-gated dependency in pyproject.toml
# (`tomli>=1.1.0; python_version < "3.11"`), so this import succeeds on every
# supported interpreter after a plain `pip install lazarus-sonar`. If it somehow
# doesn't, we fail loud with an actionable message rather than limping on.
try:  # pragma: no cover - trivial import shim
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - only on 3.9/3.10
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise ImportError(
            "No TOML parser available. On Python 3.11+ this uses the stdlib "
            "`tomllib`; on 3.9-3.10 install the backport with "
            "`pip install lazarus-sonar` (which pulls in `tomli` as a base "
            "dependency) or `pip install tomli`."
        ) from exc


__all__ = [
    "ConfigError",
    "ScoringConfig",
    "JudgeConfig",
    "LedgerConfig",
    "Config",
    "DEFAULT_CONFIG_FILENAME",
    "DEFAULT_JUDGE_MODEL",
    "DEFAULT_LEDGER_PATH",
    "find_config_path",
    "load_config",
]


# The judge is precision-sensitive, so it gets the strong model by default.
# `judge_model` is the documented main quality knob; override it in config or via
# a CLI flag. Bare model id, no date suffix.
DEFAULT_JUDGE_MODEL = "claude-opus-4-8"

# Config file the CLI and hooks look for when no explicit --config is passed.
DEFAULT_CONFIG_FILENAME = "lazarus.config.toml"

# The ONE canonical default ledger location, used verbatim here, in
# lazarus.config.example.toml, and in the README. Relative, so it resolves
# against the config file's directory (see _resolve_path). Do not reintroduce
# the old "lazarus.ledger.jsonl" / "./lazarus-ledger.jsonl" spellings.
DEFAULT_LEDGER_PATH = ".lazarus/ledger.jsonl"

# Environment variable that can point at a config file, checked after an explicit
# path and before the walk-up search. Lets a hook export one location once.
CONFIG_PATH_ENV_VAR = "LAZARUS_CONFIG"

# Documented example glob shape. Globs are REQUIRED; this constant exists solely
# to make error messages concrete and is never applied as a silent fallback
# (see _require_globs).
_EXAMPLE_GLOBS = ("**/*.md",)

# Keys that are documented in the config schema but unimplemented in v1. They are
# parsed-and-ignored: present so a forward-looking config does not error, but not
# read by any module this release. Grouped by table for clarity.
_ACCEPTED_IGNORED_CORPUS = ("max_file_kb",)
_ACCEPTED_IGNORED_JUDGE = ("request_timeout_s", "work_unit_char_limit")


class ConfigError(Exception):
    """Raised when configuration is missing, malformed, or fails validation.

    The message is meant to be printed straight to stderr by the CLI or a hook
    and read by a human. It always names the offending key and, where relevant,
    the config file it came from.
    """


# --------------------------------------------------------------------------- #
# Config value objects
# --------------------------------------------------------------------------- #
#
# NOTE: there is no SonarConfig here. The SONAR scoring knobs are
# ``sonar.ScoringConfig`` (imported above), and Config stores an instance of it
# on the ``scoring`` attribute. Config only defines the judge/ledger sub-configs
# and the top-level Config aggregate.


@dataclass(frozen=True)
class JudgeConfig:
    """LAZARUS judge parameters (the precision / reasoning stage)."""

    # The model that answers "would this rule have changed the output?".
    # This is the main quality knob.
    model: str = DEFAULT_JUDGE_MODEL

    # A surviving verdict must clear this to be surfaced as a retroactive fix.
    min_confidence: float = 0.6

    # Cap on how many shortlisted candidates go into a single batched judge call.
    # Bounds per-audit cost/latency; excess candidates below this line are not
    # judged (they were already the weakest by Sonar score).
    max_candidates: int = 15

    # Token budget handed to the judge for a single batched call. Passed straight
    # through to judge.judge_batch(..., max_tokens=...).
    max_tokens: int = 4096

    # Optional API key. Normally left None: judge.py falls back to the
    # ANTHROPIC_API_KEY environment variable, which is the primary path. This
    # exists only so ``config.api_key`` is always readable by the default
    # judge-fn factory in lazarus.py, and so a user who prefers config-file
    # secrets over env vars has a place to put one.
    api_key: str | None = None


@dataclass(frozen=True)
class LedgerConfig:
    """Append-only ledger location (anti-nag suppression state)."""

    path: Path

    # When True (default), the session-start sweep suppresses candidates whose
    # rule ids are already recorded as DECLINED for the same work-unit, so the
    # user is not re-nagged about a rule they already dismissed.
    suppress_declined: bool = True


@dataclass(frozen=True)
class Config:
    """Fully-resolved, validated configuration.

    Paths are absolute. ``corpus_path`` is guaranteed to exist as a directory at
    load time; ``corpus_globs`` is guaranteed non-empty. The config file that
    produced this is kept as ``source_path`` for error messages and for resolving
    anything lazily later.

    Two access styles coexist deliberately:

    - Structured sub-objects (``scoring``, ``judge``, ``ledger``) mirror the
      config file's tables and are what ``_build_config`` populates.
    - Flat read-only properties (``ledger_path``, ``min_confidence``,
      ``judge_model``, ``max_candidates``, ``api_key``, ``top_n``, ``min_score``)
      are thin delegates the CLI, LAZARUS, and the hooks read by name. They add
      no state and keep Config frozen and validated.

    ``scoring`` is a ``sonar.ScoringConfig`` (the one SONAR-knob type). The name
    ``scoring`` matches ``run_sonar``'s ``scoring=`` parameter, so there is
    exactly one name for the knob object across the codebase.
    """

    # --- corpus ---
    corpus_path: Path
    corpus_globs: tuple[str, ...]
    corpus_exclude: tuple[str, ...]

    # --- sub-configs (structured) ---
    scoring: ScoringConfig
    judge: JudgeConfig
    ledger: LedgerConfig

    source_path: Path | None = field(default=None)

    # --- flat read-only accessors that lazarus.py, cli.py, and the hooks read --
    #     Implemented as properties delegating to the sub-objects; no new state.

    @property
    def ledger_path(self) -> Path:
        return self.ledger.path

    @property
    def min_confidence(self) -> float:
        return self.judge.min_confidence

    @property
    def judge_model(self) -> str:
        return self.judge.model

    @property
    def max_candidates(self) -> int:
        return self.judge.max_candidates

    @property
    def api_key(self) -> str | None:
        return self.judge.api_key

    @property
    def top_n(self) -> int:
        return self.scoring.top_n

    @property
    def min_score(self) -> float:
        return self.scoring.min_score

    def with_overrides(self, **overrides: Any) -> "Config":
        """Return a copy with CLI/programmatic overrides applied.

        Recognized keys mirror ``--flag`` names on the CLI. ``None`` values are
        ignored so callers can pass through un-set argparse defaults without
        clobbering config. Unknown keys raise :class:`ConfigError` so a typo'd
        flag fails loud instead of silently doing nothing.
        """
        return _apply_overrides(self, overrides)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def find_config_path(
    explicit: str | os.PathLike[str] | None = None,
    start: str | os.PathLike[str] | None = None,
) -> Path:
    """Locate the config file, failing loud if none is found.

    Resolution order:
      1. ``explicit`` (a ``--config`` flag) if given.
      2. ``$LAZARUS_CONFIG`` if set.
      3. ``lazarus.config.toml`` walking up from ``start`` (default: cwd) to the
         filesystem root.

    Raises :class:`ConfigError` if an explicit/env path does not exist, or if the
    walk-up search finds nothing. There is no built-in default that silently
    "works" without a config file.
    """
    if explicit is not None:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise ConfigError(
                f"Config file not found: {p}\n"
                f"(passed explicitly). Create it or point --config at an "
                f"existing {DEFAULT_CONFIG_FILENAME}."
            )
        return p.resolve()

    env_val = os.environ.get(CONFIG_PATH_ENV_VAR)
    if env_val:
        p = Path(env_val).expanduser()
        if not p.is_file():
            raise ConfigError(
                f"Config file not found: {p}\n"
                f"(from ${CONFIG_PATH_ENV_VAR}). Fix the variable or unset it."
            )
        return p.resolve()

    start_dir = Path(start).expanduser().resolve() if start else Path.cwd()
    if start_dir.is_file():
        start_dir = start_dir.parent

    for directory in (start_dir, *start_dir.parents):
        candidate = directory / DEFAULT_CONFIG_FILENAME
        if candidate.is_file():
            return candidate.resolve()

    raise ConfigError(
        f"No {DEFAULT_CONFIG_FILENAME} found in {start_dir} or any parent "
        f"directory.\nCopy lazarus.config.example.toml to "
        f"{DEFAULT_CONFIG_FILENAME}, set corpus.path and corpus.globs, and try "
        f"again. Or pass --config / set ${CONFIG_PATH_ENV_VAR}."
    )


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    start: str | os.PathLike[str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Config:
    """Load, validate, and return a :class:`Config`.

    ``path`` may be an explicit config file; if omitted, the file is located via
    :func:`find_config_path` (``$LAZARUS_CONFIG`` then a walk-up search). All
    relative paths in the file are resolved against the config file's own
    directory, so a config checked into a repo works regardless of the cwd a hook
    runs from. ``overrides`` (typically parsed CLI flags) are applied last.

    Raises :class:`ConfigError` on any missing/malformed/failing value. Never
    falls back to scanning home or cwd for a corpus.
    """
    config_path = find_config_path(path, start=start)

    try:
        with open(config_path, "rb") as fp:
            raw = _toml.load(fp)
    except OSError as exc:
        raise ConfigError(f"Could not read config file {config_path}: {exc}") from exc
    except _toml.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        # tomllib always returns a dict at the top level; guard anyway.
        raise ConfigError(f"Config root must be a table, got {type(raw).__name__} in {config_path}.")

    config = _build_config(raw, config_path)

    if overrides:
        config = _apply_overrides(config, overrides)

    return config


# --------------------------------------------------------------------------- #
# Internal: build + validate
# --------------------------------------------------------------------------- #


def _build_config(raw: Mapping[str, Any], config_path: Path) -> Config:
    base_dir = config_path.parent

    corpus = _require_table(raw, "corpus", config_path)

    # --- corpus.path: required, no fallback -------------------------------- #
    corpus_path_raw = corpus.get("path")
    if corpus_path_raw is None:
        raise ConfigError(
            f"Missing required key 'corpus.path' in {config_path}.\n"
            f"Point it at the directory holding your rules/primitives/memory "
            f"files. There is no default; this tool will not guess your "
            f"corpus location."
        )
    if not isinstance(corpus_path_raw, str) or not corpus_path_raw.strip():
        raise ConfigError(
            f"'corpus.path' must be a non-empty string in {config_path}, got "
            f"{corpus_path_raw!r}."
        )

    corpus_path = _resolve_path(corpus_path_raw, base_dir)
    if not corpus_path.exists():
        raise ConfigError(
            f"corpus.path does not exist: {corpus_path}\n"
            f"(resolved from {corpus_path_raw!r} relative to {base_dir}). "
            f"Create the directory or fix the path in {config_path}."
        )
    if not corpus_path.is_dir():
        raise ConfigError(
            f"corpus.path must be a directory, not a file: {corpus_path}\n"
            f"(in {config_path})."
        )

    # --- corpus.globs: required, non-empty, no fallback -------------------- #
    corpus_globs = _require_globs(corpus.get("globs"), config_path)

    # --- corpus.exclude: optional ------------------------------------------ #
    corpus_exclude = _string_tuple(corpus.get("exclude", ()), "corpus.exclude", config_path)

    # corpus.max_file_kb is accepted-and-ignored in v1 (documented but
    # unimplemented). Touch the getter so a wrong TYPE could be surfaced later,
    # but do not error on presence: it is a forward-compat knob.
    _ = corpus.get("max_file_kb")

    # --- [sonar] ----------------------------------------------------------- #
    scoring = _build_scoring(_optional_table(raw, "sonar", config_path), config_path)

    # --- [judge] ----------------------------------------------------------- #
    judge = _build_judge(_optional_table(raw, "judge", config_path), config_path)

    # --- [ledger] ---------------------------------------------------------- #
    ledger = _build_ledger(
        _optional_table(raw, "ledger", config_path), base_dir, config_path
    )

    return Config(
        corpus_path=corpus_path,
        corpus_globs=corpus_globs,
        corpus_exclude=corpus_exclude,
        scoring=scoring,
        judge=judge,
        ledger=ledger,
        source_path=config_path,
    )


def _build_scoring(table: Mapping[str, Any], config_path: Path) -> ScoringConfig:
    """Build the one SONAR-knob object (sonar.ScoringConfig) from [sonar].

    Every field defaults to ScoringConfig's own canonical default; the table only
    overrides what it sets. ``idf_damping`` is a bool and ``extra_stopwords`` is a
    string list. ``overlap_weight`` and ``idf_damping`` are optional knobs most
    users leave alone.
    """
    return ScoringConfig(
        title_boost=_number(
            table.get("title_boost", ScoringConfig.title_boost),
            "sonar.title_boost", config_path, minimum=0.0,
        ),
        path_boost=_number(
            table.get("path_boost", ScoringConfig.path_boost),
            "sonar.path_boost", config_path, minimum=0.0,
        ),
        overlap_weight=_number(
            table.get("overlap_weight", ScoringConfig.overlap_weight),
            "sonar.overlap_weight", config_path, minimum=0.0,
        ),
        idf_damping=_bool(
            table.get("idf_damping", ScoringConfig.idf_damping),
            "sonar.idf_damping", config_path,
        ),
        min_score=_number(
            table.get("min_score", ScoringConfig.min_score),
            "sonar.min_score", config_path, minimum=0.0,
        ),
        top_n=_positive_int(
            table.get("top_n", ScoringConfig.top_n), "sonar.top_n", config_path
        ),
        extra_stopwords=_string_tuple(
            table.get("extra_stopwords", ()), "sonar.extra_stopwords", config_path
        ),
    )


def _build_judge(table: Mapping[str, Any], config_path: Path) -> JudgeConfig:
    model = table.get("model", DEFAULT_JUDGE_MODEL)
    if not isinstance(model, str) or not model.strip():
        raise ConfigError(
            f"'judge.model' must be a non-empty string in {config_path}, got "
            f"{model!r}."
        )

    min_confidence = _number(
        table.get("min_confidence", 0.6), "judge.min_confidence", config_path,
        minimum=0.0, maximum=1.0,
    )
    max_candidates = _positive_int(
        table.get("max_candidates", 15), "judge.max_candidates", config_path
    )
    max_tokens = _positive_int(
        table.get("max_tokens", JudgeConfig.max_tokens), "judge.max_tokens", config_path
    )

    api_key = _optional_string(table.get("api_key"), "judge.api_key", config_path)

    # request_timeout_s / work_unit_char_limit are accepted-and-ignored in v1.
    for ignored in _ACCEPTED_IGNORED_JUDGE:
        _ = table.get(ignored)

    return JudgeConfig(
        model=model.strip(),
        min_confidence=min_confidence,
        max_candidates=max_candidates,
        max_tokens=max_tokens,
        api_key=api_key,
    )


def _build_ledger(
    table: Mapping[str, Any], base_dir: Path, config_path: Path
) -> LedgerConfig:
    path_raw = table.get("path", DEFAULT_LEDGER_PATH)
    if not isinstance(path_raw, str) or not path_raw.strip():
        raise ConfigError(
            f"'ledger.path' must be a non-empty string in {config_path}, got "
            f"{path_raw!r}."
        )
    suppress_declined = _bool(
        table.get("suppress_declined", True), "ledger.suppress_declined", config_path
    )
    return LedgerConfig(
        path=_resolve_path(path_raw, base_dir),
        suppress_declined=suppress_declined,
    )


# --------------------------------------------------------------------------- #
# Internal: overrides
# --------------------------------------------------------------------------- #

# Maps override keys (CLI flag names, underscored) to how they apply. Kept
# explicit so a typo'd flag is rejected rather than silently ignored.
_OVERRIDE_KEYS = frozenset(
    {
        "corpus_path",
        "globs",
        "judge_model",
        "min_confidence",
        "min_score",
        "top_n",
        "max_candidates",
        "ledger_path",
    }
)


def _apply_overrides(config: Config, overrides: Mapping[str, Any]) -> Config:
    unknown = set(overrides) - _OVERRIDE_KEYS
    if unknown:
        raise ConfigError(
            "Unknown config override(s): "
            + ", ".join(sorted(unknown))
            + f". Recognized override keys: {', '.join(sorted(_OVERRIDE_KEYS))}."
        )

    updated = config

    corpus_path = overrides.get("corpus_path")
    if corpus_path is not None:
        p = Path(corpus_path).expanduser().resolve()
        if not p.is_dir():
            raise ConfigError(
                f"--corpus-path override does not point at a directory: {p}"
            )
        updated = replace(updated, corpus_path=p)

    globs = overrides.get("globs")
    if globs is not None:
        g = _string_tuple(globs, "--glob", None)
        if not g:
            raise ConfigError("--glob override produced no non-empty patterns.")
        updated = replace(updated, corpus_globs=g)

    judge_model = overrides.get("judge_model")
    min_confidence = overrides.get("min_confidence")
    max_candidates = overrides.get("max_candidates")
    if judge_model is not None or min_confidence is not None or max_candidates is not None:
        judge = updated.judge
        if judge_model is not None:
            if not isinstance(judge_model, str) or not judge_model.strip():
                raise ConfigError("--judge-model override must be a non-empty string.")
            judge = replace(judge, model=judge_model.strip())
        if min_confidence is not None:
            judge = replace(
                judge,
                min_confidence=_number(
                    min_confidence, "--min-confidence", None, minimum=0.0, maximum=1.0
                ),
            )
        if max_candidates is not None:
            judge = replace(
                judge,
                max_candidates=_positive_int(max_candidates, "--max-candidates", None),
            )
        updated = replace(updated, judge=judge)

    min_score = overrides.get("min_score")
    top_n = overrides.get("top_n")
    if min_score is not None or top_n is not None:
        scoring = updated.scoring
        if min_score is not None:
            scoring = replace(
                scoring, min_score=_number(min_score, "--min-score", None, minimum=0.0)
            )
        if top_n is not None:
            scoring = replace(scoring, top_n=_positive_int(top_n, "--top-n", None))
        updated = replace(updated, scoring=scoring)

    ledger_path = overrides.get("ledger_path")
    if ledger_path is not None:
        p = Path(ledger_path).expanduser().resolve()
        updated = replace(updated, ledger=replace(updated.ledger, path=p))

    return updated


# --------------------------------------------------------------------------- #
# Internal: small validation helpers
# --------------------------------------------------------------------------- #


def _resolve_path(value: str, base_dir: Path) -> Path:
    """Expand ~ and resolve ``value`` against ``base_dir`` if relative.

    Absolute paths (and ~-expanded paths) are used as-is; relative paths are
    anchored to the config file's directory, not the cwd. This keeps a
    repo-checked-in config working regardless of where a hook is invoked from.
    """
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = base_dir / p
    return p.resolve()


def _require_table(raw: Mapping[str, Any], key: str, config_path: Path) -> Mapping[str, Any]:
    value = raw.get(key)
    if value is None:
        raise ConfigError(
            f"Missing required '[{key}]' table in {config_path}. See "
            f"lazarus.config.example.toml for the expected shape."
        )
    if not isinstance(value, dict):
        raise ConfigError(
            f"'[{key}]' must be a table in {config_path}, got {type(value).__name__}."
        )
    return value


def _optional_table(raw: Mapping[str, Any], key: str, config_path: Path) -> Mapping[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(
            f"'[{key}]' must be a table in {config_path}, got {type(value).__name__}."
        )
    return value


def _require_globs(value: Any, config_path: Path) -> tuple[str, ...]:
    """Validate corpus.globs: must be present, a list, and non-empty.

    This is the second half of the fail-loud contract. An empty or missing glob
    list would make Sonar sweep nothing (or, if we fell back to a default,
    silently scan something the user did not ask for). Both are refused.
    """
    if value is None:
        raise ConfigError(
            f"Missing required key 'corpus.globs' in {config_path}.\n"
            f"Set it to a non-empty list of glob patterns matched under "
            f"corpus.path, e.g. globs = {list(_EXAMPLE_GLOBS)}. There is no "
            f"default glob; this tool will not guess which files are your rules."
        )
    if not isinstance(value, list):
        raise ConfigError(
            f"'corpus.globs' must be a list of strings in {config_path}, got "
            f"{type(value).__name__}."
        )
    if not value:
        raise ConfigError(
            f"'corpus.globs' is empty in {config_path}.\n"
            f"An empty glob list would match no files. Add at least one pattern, "
            f"e.g. globs = {list(_EXAMPLE_GLOBS)}."
        )
    globs = _string_tuple(value, "corpus.globs", config_path)
    if not globs:  # all entries were blank
        raise ConfigError(
            f"'corpus.globs' contains only empty patterns in {config_path}. "
            f"Add at least one real glob, e.g. globs = {list(_EXAMPLE_GLOBS)}."
        )
    return globs


def _string_tuple(value: Any, key: str, config_path: Path | None) -> tuple[str, ...]:
    where = f" in {config_path}" if config_path is not None else ""
    if isinstance(value, str):
        # A bare string is a common mistake for a list-valued key. Accept it as
        # a single-element list rather than iterating its characters.
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if not isinstance(value, (list, tuple)):
        raise ConfigError(
            f"'{key}' must be a list of strings{where}, got {type(value).__name__}."
        )
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ConfigError(
                f"'{key}[{i}]' must be a string{where}, got {type(item).__name__}."
            )
        stripped = item.strip()
        if stripped:
            out.append(stripped)
    return tuple(out)


def _optional_string(value: Any, key: str, config_path: Path | None) -> str | None:
    """Validate an optional string knob. Absent/blank -> None; else the stripped
    string. A non-string, non-None value is a hard error.
    """
    if value is None:
        return None
    where = f" in {config_path}" if config_path is not None else ""
    if not isinstance(value, str):
        raise ConfigError(
            f"'{key}' must be a string{where}, got {type(value).__name__} "
            f"({value!r})."
        )
    stripped = value.strip()
    return stripped or None


def _number(
    value: Any,
    key: str,
    config_path: Path | None,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    where = f" in {config_path}" if config_path is not None else ""
    # bool is an int subclass; reject it explicitly so `true` isn't read as 1.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"'{key}' must be a number{where}, got {type(value).__name__} "
            f"({value!r})."
        )
    num = float(value)
    if minimum is not None and num < minimum:
        raise ConfigError(f"'{key}' must be >= {minimum}{where}, got {num}.")
    if maximum is not None and num > maximum:
        raise ConfigError(f"'{key}' must be <= {maximum}{where}, got {num}.")
    return num


def _positive_int(value: Any, key: str, config_path: Path | None) -> int:
    where = f" in {config_path}" if config_path is not None else ""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"'{key}' must be an integer{where}, got {type(value).__name__} "
            f"({value!r})."
        )
    if value < 1:
        raise ConfigError(f"'{key}' must be >= 1{where}, got {value}.")
    return value


def _bool(value: Any, key: str, config_path: Path | None) -> bool:
    """Validate a boolean knob. TOML booleans are real bools; reject anything
    else rather than coercing (a stray 0/1 or "true" string should fail loud).
    """
    where = f" in {config_path}" if config_path is not None else ""
    if not isinstance(value, bool):
        raise ConfigError(
            f"'{key}' must be a boolean (true/false){where}, got "
            f"{type(value).__name__} ({value!r})."
        )
    return value
