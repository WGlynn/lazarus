"""SONAR: portable, stdlib-only searcher for a file-based rules/memory corpus.

SONAR is the perception organ of Lazarus. It is deliberately wide, cheap, and
high-recall: given a work-unit (a diff, a finished response, a decision), it
globs a corpus of rule/primitive/memory files, tokenizes both sides, and scores
each file by keyword overlap plus two structural boosts (title/filename match
and path-keyword match). It returns a ranked shortlist of candidates.

This is the firehose stage. Its raw output is not meant to be shown to a human;
it is fed to LAZARUS (the precision organ), which applies the
"would applying this rule have changed the output?" filter. Keeping the two
organs separate is the whole point: recall here, precision there.

Design constraints honored by this module:

- stdlib only. No embeddings, no third-party deps. The scorer is a single
  function (`score_file`) so an embedding-based recall stage can be dropped in
  later behind the same interface without touching callers.
- Parameterized. Corpus path, globs, exclude patterns, scoring weights, and
  min_score/top_n all come from configuration (see config.py). Nothing here is
  hardcoded to any one project or user directory.
- Reusable. A "corpus" is just a directory tree of text files. The same code
  sweeps primitives, source code, docs, or exported chat threads. The work-unit
  is likewise just text plus a `kind` label.
- Fail-loud. If the corpus directory does not exist, or matches zero files,
  SONAR raises rather than silently returning an empty shortlist. A silent
  empty result would look identical to "nothing relevant", which is the exact
  failure mode this tool exists to prevent.

SONAR does not call any model and does not propose fixes. It only ranks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from .config import Config

__all__ = [
    "Candidate",
    "ScoringConfig",
    "SonarError",
    "CorpusEmptyError",
    "STOPWORDS",
    "tokenize",
    "load_corpus_file",
    "iter_corpus_files",
    "score_file",
    "run_sonar",
    "run_sonar_for_config",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SonarError(RuntimeError):
    """Base class for SONAR failures. Raised loudly, never swallowed."""


class CorpusEmptyError(SonarError):
    """The corpus path exists but no files matched the configured globs.

    Treated as an error, not an empty result: a silent empty shortlist is
    indistinguishable from "nothing relevant" and would defeat the tool.
    """


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

# A small, general English stopword set plus a handful of tokens that show up
# constantly in code diffs and markdown rules and carry almost no signal. Kept
# intentionally short: over-pruning hurts recall, and recall is SONAR's job.
STOPWORDS: frozenset[str] = frozenset(
    {
        # articles / conjunctions / prepositions
        "a", "an", "the", "and", "or", "but", "if", "then", "else", "for",
        "of", "to", "in", "on", "at", "by", "as", "is", "are", "was", "were",
        "be", "been", "being", "with", "without", "from", "into", "onto",
        "over", "under", "out", "up", "down", "off", "than", "that", "this",
        "these", "those", "it", "its", "so", "no", "not", "do", "does", "did",
        "done", "can", "will", "would", "should", "could", "may", "might",
        "must", "shall", "we", "you", "he", "she", "they", "them", "his",
        "her", "their", "our", "your", "my", "me", "us", "i", "am", "any",
        "all", "each", "some", "such", "only", "own", "same", "just", "very",
        "more", "most", "much", "many", "few", "other", "which", "who", "whom",
        "what", "when", "where", "why", "how", "there", "here", "about",
        "also", "because", "while", "after", "before", "between", "both",
        "through", "during", "above", "below", "again", "once", "per",
        # diff / markdown / code noise that carries little topical signal
        "def", "self", "return", "import", "from", "class", "true", "false",
        "none", "null", "let", "const", "var", "function", "diff", "index",
        "git", "line", "lines", "file", "files", "http", "https", "www", "com",
    }
)

# Split on anything that is not a word character. This keeps ascii-ident-style
# tokens and numbers, and folds snake_case boundaries when combined with the
# underscore split below. Unicode word characters are preserved so non-English
# corpora still tokenize into something.
_WORD_RE = re.compile(r"[^\w]+", flags=re.UNICODE)
# Break snake_case and camelCase so `no_secrets_in_logs` and `noSecretsInLogs`
# both contribute {no, secrets, in, logs}. This materially improves overlap on
# code corpora, where identifiers are the topical signal.
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _split_identifier(raw: str) -> Iterable[str]:
    """Yield sub-tokens of a single raw token (snake_case + camelCase aware)."""
    # camelCase -> camel Case
    for part in _CAMEL_RE.sub(" ", raw).split():
        # snake_case / kebab already handled by the non-word split upstream,
        # but underscores survive `\w`, so split them here.
        for sub in part.split("_"):
            if sub:
                yield sub


def tokenize(
    text: str,
    *,
    min_len: int = 2,
    keep_stopwords: bool = False,
    extra_stopwords: frozenset[str] | tuple[str, ...] = (),
) -> list[str]:
    """Lowercase, split into word tokens, drop stopwords and very short tokens.

    Returns a list (not a set) so callers that want term frequency can keep it;
    `score_file` reduces to sets internally. Pure text in, tokens out, no I/O.

    Args:
        text: arbitrary text (a rule file, a diff, a response).
        min_len: drop tokens shorter than this after case-folding.
        keep_stopwords: if True, do not prune the stopword set. Useful when a
            caller wants the raw token stream for diagnostics.
        extra_stopwords: additional stopwords to prune on top of the built-in
            STOPWORDS set (corpus-specific noise). Ignored when keep_stopwords
            is True. `run_sonar` passes the config's extra_stopwords here so the
            work-unit and corpus files are tokenized against the same set.
    """
    if not text:
        return []
    if keep_stopwords:
        stop: frozenset[str] = frozenset()
    elif extra_stopwords:
        stop = STOPWORDS | frozenset(extra_stopwords)
    else:
        stop = STOPWORDS
    tokens: list[str] = []
    for raw in _WORD_RE.split(text):
        if not raw:
            continue
        for sub in _split_identifier(raw):
            lowered = sub.lower()
            if len(lowered) < min_len:
                continue
            if lowered in stop:
                continue
            tokens.append(lowered)
    return tokens


# ---------------------------------------------------------------------------
# Corpus discovery + file loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """A scored corpus file. The unit SONAR returns and LAZARUS consumes.

    Attributes:
        rule_id: a stable identifier for the file, relative to the corpus root
            (POSIX-style, so ledger keys are portable across OSes).
        path: absolute path to the file on disk.
        title: the file's human title (first markdown heading, else the stem).
        score: total relevance score (overlap + structural boosts).
        overlap: number of distinct shared tokens between work-unit and file.
        matched_terms: the shared tokens, sorted, for explainability.
        title_boost: structural boost contributed by title/filename matches.
        path_boost: structural boost contributed by path-keyword matches.
        excerpt: a short leading slice of the file for the judge's context.
    """

    rule_id: str
    path: Path
    title: str
    score: float
    overlap: int
    matched_terms: tuple[str, ...] = ()
    title_boost: float = 0.0
    path_boost: float = 0.0
    excerpt: str = ""

    def as_dict(self) -> dict:
        """JSON-serializable view (paths as strings). Used by the CLI/hooks."""
        return {
            "rule_id": self.rule_id,
            "path": str(self.path),
            "title": self.title,
            "score": round(self.score, 4),
            "overlap": self.overlap,
            "matched_terms": list(self.matched_terms),
            "title_boost": round(self.title_boost, 4),
            "path_boost": round(self.path_boost, 4),
            "excerpt": self.excerpt,
        }


@dataclass
class CorpusFile:
    """A loaded corpus file plus its precomputed token sets. Internal to SONAR."""

    rule_id: str
    path: Path
    title: str
    text: str
    body_tokens: frozenset[str] = field(default_factory=frozenset)
    title_tokens: frozenset[str] = field(default_factory=frozenset)
    path_tokens: frozenset[str] = field(default_factory=frozenset)


_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", flags=re.MULTILINE)


def _derive_title(text: str, path: Path) -> str:
    """First markdown heading if present, else a title-cased file stem."""
    m = _HEADING_RE.search(text)
    if m:
        return m.group(1).strip()
    stem = path.stem.replace("_", " ").replace("-", " ").strip()
    return stem or path.name


def iter_corpus_files(
    root: Path,
    globs: Sequence[str],
    exclude: Sequence[str] = (),
) -> list[Path]:
    """Return the sorted, de-duplicated set of files matching `globs` under `root`.

    Fail-loud: raises if `root` does not exist or is not a directory, and raises
    `CorpusEmptyError` if the globs match nothing after applying `exclude`.

    Args:
        root: corpus directory. Must exist.
        globs: glob patterns relative to `root` (e.g. ["**/*.md", "**/*.py"]).
            An empty globs list is a configuration error and raises.
        exclude: glob patterns (relative to `root`) to drop from the result,
            e.g. ["**/node_modules/**", "**/.git/**"].
    """
    if not globs:
        raise SonarError(
            "SONAR: no corpus globs configured. Set corpus.globs in your config "
            "(for example [\"**/*.md\"]). Refusing to guess."
        )
    if not root.exists():
        raise SonarError(
            f"SONAR: corpus path does not exist: {root}. "
            "Set corpus.path in your config to a real directory. "
            "There is no silent fallback to the home or working directory."
        )
    if not root.is_dir():
        raise SonarError(f"SONAR: corpus path is not a directory: {root}")

    matched: set[Path] = set()
    for pattern in globs:
        for p in root.glob(pattern):
            if p.is_file():
                matched.add(p.resolve())

    if exclude:
        excluded: set[Path] = set()
        for pattern in exclude:
            for p in root.glob(pattern):
                excluded.add(p.resolve())
        matched -= excluded

    if not matched:
        raise CorpusEmptyError(
            f"SONAR: corpus at {root} matched zero files for globs "
            f"{list(globs)} (exclude={list(exclude)}). An empty corpus is "
            "treated as an error, not an empty result, so a misconfigured path "
            "cannot masquerade as 'nothing relevant'."
        )

    return sorted(matched)


def load_corpus_file(
    path: Path,
    root: Path,
    *,
    encoding: str = "utf-8",
    extra_stopwords: frozenset[str] | tuple[str, ...] = (),
) -> CorpusFile:
    """Read a corpus file and precompute its title/body/path token sets.

    `rule_id` is the POSIX-style path relative to `root`, so ledger keys are
    stable and portable regardless of OS path separators. Unreadable files are
    skipped by the caller via the returned exception, not silently ignored.

    `extra_stopwords` is threaded into every tokenize call here so that the
    corpus side and the work-unit side (tokenized in `run_sonar`) prune the
    exact same set. Passing mismatched stopword sets between the two would break
    overlap symmetry, so `run_sonar` always supplies the same value to both.
    """
    text = path.read_text(encoding=encoding, errors="replace")
    title = _derive_title(text, path)

    try:
        rel = path.resolve().relative_to(root.resolve())
        rule_id = rel.as_posix()
    except ValueError:
        # Path outside root (should not happen given how we glob), fall back to
        # the absolute posix path so the id is still unique and stable.
        rule_id = path.resolve().as_posix()

    body_tokens = frozenset(tokenize(text, extra_stopwords=extra_stopwords))
    title_tokens = frozenset(tokenize(title, extra_stopwords=extra_stopwords))
    # Path tokens: every path component minus the extension, tokenized. This is
    # what lets `path_boost` reward corpus files whose folder names echo the
    # work-unit's vocabulary (e.g. work touches "logging", rule lives in
    # security/logging/...).
    path_text = " ".join(path.with_suffix("").parts)
    path_tokens = frozenset(tokenize(path_text, extra_stopwords=extra_stopwords))

    return CorpusFile(
        rule_id=rule_id,
        path=path.resolve(),
        title=title,
        text=text,
        body_tokens=body_tokens,
        title_tokens=title_tokens,
        path_tokens=path_tokens,
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoringConfig:
    """Tunable weights for `score_file`. All defaults are conservative.

    These map directly onto the [sonar] table in the config file. Every field
    is a knob the user can turn; none is hardcoded into the scoring math. This
    is the ONE SONAR-knob type in the codebase: config.py imports it and stores
    an instance, and Config.scoring is an instance of it. There is no separate
    config.SonarConfig.

    Attributes:
        title_boost: weight per work-unit token that also appears in the rule's
            title/filename. Title matches are strong topical signal.
        path_boost: weight per work-unit token that appears in the rule's path
            components. Weaker signal than title, higher than body-only.
        overlap_weight: weight per distinct shared body token (the TF-lite
            intersection). This is the base recall signal.
        idf_damping: if True, down-weight tokens that are common across the
            corpus so ubiquitous words do not dominate. Computed locally from
            document frequency, no external corpus needed. This is a damping
            (ranking) factor, not a precision cut: with min_score at its default
            of 0.0 it changes candidate ORDER, not membership, so recall is
            preserved.
        min_score: candidates below this total score are dropped. The default is
            0.0 so a single shared body token OR any structural boost admits a
            candidate. SONAR is deliberately high-recall; the LAZARUS judge is
            the precision gate. The example config documents 0.05 as a hair above
            zero to drop pure-noise zero-overlap files while staying permissive.
        top_n: keep at most this many candidates after sorting by score.
        excerpt_chars: how many leading characters of each file to carry as an
            excerpt for the judge stage.
        extra_stopwords: additional stopwords to prune during tokenization, on
            top of the built-in STOPWORDS set. Useful for corpus-specific noise
            (e.g. a project name that appears in every file and carries no
            signal). Threaded into both work-unit and corpus tokenization so the
            two sides always use the same stopword set.
    """

    title_boost: float = 2.0
    path_boost: float = 1.5
    overlap_weight: float = 1.0
    idf_damping: bool = True
    min_score: float = 0.0
    top_n: int = 20
    excerpt_chars: int = 800
    extra_stopwords: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: dict | None) -> "ScoringConfig":
        """Build from a plain dict (the [sonar] config table), ignoring extras.

        Filters to the dataclass's own fields, so unknown keys in the table are
        ignored rather than raising. `extra_stopwords` is a normal field and is
        picked up here automatically.
        """
        if not data:
            return cls()
        known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
        return cls(**known)


def _compute_idf(files: Sequence[CorpusFile]) -> dict[str, float]:
    """Local inverse document frequency over the corpus body tokens.

    idf(t) = ln(1 + N / (1 + df(t))). No smoothing beyond the +1 terms; this is
    a damping factor, not a full TF-IDF ranker. Returns a dict token -> weight.
    """
    from math import log

    n = len(files)
    df: dict[str, int] = {}
    for f in files:
        for tok in f.body_tokens:
            df[tok] = df.get(tok, 0) + 1
    return {tok: log(1.0 + n / (1.0 + d)) for tok, d in df.items()}


def score_file(
    work_tokens: frozenset[str],
    cf: CorpusFile,
    cfg: ScoringConfig,
    idf: dict[str, float] | None = None,
) -> tuple[float, int, tuple[str, ...], float, float]:
    """Score one corpus file against a work-unit's token set.

    This is the single scoring seam. An embedding-based recall stage can replace
    the body of this function without changing any caller: the contract is
    (work_tokens, corpus_file, config) -> (score, overlap, matched, tboost, pboost).

    Returns:
        (score, overlap, matched_terms, title_boost, path_boost)
    """
    shared = work_tokens & cf.body_tokens
    overlap = len(shared)

    if idf and cfg.idf_damping:
        overlap_score = sum(idf.get(tok, 1.0) for tok in shared) * cfg.overlap_weight
    else:
        overlap_score = overlap * cfg.overlap_weight

    title_hits = work_tokens & cf.title_tokens
    path_hits = work_tokens & cf.path_tokens
    title_boost = len(title_hits) * cfg.title_boost
    path_boost = len(path_hits) * cfg.path_boost

    score = overlap_score + title_boost + path_boost
    matched = tuple(sorted(shared))
    return score, overlap, matched, title_boost, path_boost


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_sonar(
    work_unit: str,
    *,
    corpus_path: Path | str,
    globs: Sequence[str],
    exclude: Sequence[str] = (),
    scoring: ScoringConfig | dict | None = None,
    encoding: str = "utf-8",
) -> list[Candidate]:
    """Run a SONAR sweep and return the ranked candidate shortlist.

    This is the whole perception organ in one call. It is intentionally the only
    function most callers (CLI, hooks, LAZARUS) need to import.

    Args:
        work_unit: the text to search against (a diff, a finished response, a
            decision). Empty/whitespace-only input is a caller error and raises,
            because a silent empty shortlist would hide the mistake.
        corpus_path: root directory of the rules/memory corpus.
        globs: glob patterns relative to corpus_path (config-driven).
        exclude: glob patterns to drop (config-driven).
        scoring: a ScoringConfig, a plain dict (the [sonar] table), or None for
            defaults.
        encoding: text encoding for corpus files.

    Returns:
        A list of Candidate, sorted by score descending then rule_id ascending
        for deterministic ties, truncated to scoring.top_n, with scores at or
        above scoring.min_score.

    Raises:
        SonarError / CorpusEmptyError: on empty work-unit, missing corpus, or a
            corpus that matches no files. Fail-loud by design.
    """
    if not work_unit or not work_unit.strip():
        raise SonarError(
            "SONAR: empty work-unit. Provide the diff/response/decision to "
            "audit. Refusing to return an empty shortlist for empty input."
        )

    cfg = (
        scoring
        if isinstance(scoring, ScoringConfig)
        else ScoringConfig.from_mapping(scoring)
    )

    # One stopword set shared by both sides. The work-unit and every corpus file
    # must prune the same tokens or the overlap intersection is asymmetric, so
    # the config's extra_stopwords is threaded into every tokenize call below.
    extra = frozenset(cfg.extra_stopwords)

    root = Path(corpus_path).expanduser().resolve()
    paths = iter_corpus_files(root, globs, exclude)

    files: list[CorpusFile] = []
    read_errors: list[str] = []
    for p in paths:
        try:
            files.append(
                load_corpus_file(p, root, encoding=encoding, extra_stopwords=extra)
            )
        except OSError as exc:
            # Record and surface later; do not silently drop a file that might
            # be the relevant rule.
            read_errors.append(f"{p}: {exc}")

    if not files:
        detail = "; ".join(read_errors) if read_errors else "no readable files"
        raise CorpusEmptyError(
            f"SONAR: corpus at {root} produced no readable files ({detail})."
        )

    idf = _compute_idf(files) if cfg.idf_damping else None
    work_tokens = frozenset(tokenize(work_unit, extra_stopwords=extra))

    if not work_tokens:
        raise SonarError(
            "SONAR: work-unit tokenized to zero terms (all stopwords or "
            "punctuation). Nothing to search on."
        )

    candidates: list[Candidate] = []
    for cf in files:
        score, overlap, matched, tboost, pboost = score_file(
            work_tokens, cf, cfg, idf
        )
        if score < cfg.min_score:
            continue
        candidates.append(
            Candidate(
                rule_id=cf.rule_id,
                path=cf.path,
                title=cf.title,
                score=score,
                overlap=overlap,
                matched_terms=matched,
                title_boost=tboost,
                path_boost=pboost,
                excerpt=cf.text[: cfg.excerpt_chars].strip(),
            )
        )

    candidates.sort(key=lambda c: (-c.score, c.rule_id))
    return candidates[: cfg.top_n]


def run_sonar_for_config(
    work_unit: str,
    config: "Config",
    *,
    kind: str = "generic",
    top_n: int | None = None,
) -> list[Candidate]:
    """Config adapter over `run_sonar` for callers that hold a `Config`.

    `run_sonar` deliberately takes plain `corpus_path` / `globs` / `exclude` /
    `scoring` arguments so the perception core stays portable and free of any
    Config dependency. This thin adapter is the ONE obvious call for every
    Config-holding caller (the CLI's sonar/audit commands, the retro-audit and
    session-start hooks): it unpacks the corpus fields and the scoring knobs off
    `Config` and delegates.

    Args:
        work_unit: the text to search against (a diff, a finished response, a
            decision). Forwarded to `run_sonar`, which fails loud on empty input.
        config: a fully-resolved `Config`. `config.scoring` is the SONAR-knob
            object (a `ScoringConfig`); `config.corpus_path` / `corpus_globs` /
            `corpus_exclude` supply the corpus.
        kind: an advisory label for the work-unit (e.g. "diff", "generic"). It
            is accepted for forward-compatibility and threaded by higher layers,
            but has NO effect on scoring in v1. SONAR ranks purely on token
            overlap plus structural boosts; the `kind` distinction is the judge's
            concern, not the scorer's.
        top_n: optional per-call override of the shortlist cap. `None` uses
            `config.scoring.top_n`; a value replaces it on a copy of the scoring
            config, leaving the shared config untouched.

    Returns:
        The ranked `Candidate` shortlist, identical to a direct `run_sonar`
        call with the same effective arguments.
    """
    scoring = (
        config.scoring
        if top_n is None
        else replace(config.scoring, top_n=top_n)
    )
    return run_sonar(
        work_unit,
        corpus_path=config.corpus_path,
        globs=config.corpus_globs,
        exclude=config.corpus_exclude,
        scoring=scoring,
    )
