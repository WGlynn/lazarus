#!/usr/bin/env python3
"""LAZARUS v2 trigger policy -- gate the expensive judge on the cheap SONAR signal.

WHY THIS EXISTS
---------------
The v2 launcher/runner run SONAR (keyword overlap + structural boosts, stdlib,
offline, no API) on the finished work-unit, then hand candidates to the JUDGE
(the LLM call). SONAR is ~free; the judge is the only expensive step. Running the
judge on every work-unit -- even off the critical path -- burns compute and money
on units that carry no buried-rule risk. Running it on a fixed clock (every N
minutes) is worse: the clock is uncorrelated with when risk actually arises, so it
both wastes idle ticks and misses risky bursts.

The equilibrium is not a cadence. It is a GATE. SONAR runs on every unit (total
recall coverage stays cheap); the judge fires only when SONAR's signal clears a
RISK-WEIGHTED, ADAPTIVE bar worth paying for. This is the same router pattern as
the on-chain ShapleyAttributionHook: a cheap structural signal escalates to the
expensive exact computation only where it fires. Cost then tracks RISK DENSITY,
not wall-clock or token count.

TWO PARTS
---------
1. TriggerPolicy -- the per-work-unit gate. Given SONAR candidates (already scored)
   and the work-unit text, decide whether the top signal clears the bar. The bar is
   scaled DOWN for high-risk work-units (trust-boundary / secret / destructive /
   money), so the audit is eager exactly where a miss is expensive, and lazy where
   it is cheap. This mirrors the ponytail "not-lazy at trust boundaries" gradient.

2. ThresholdController -- tunes the base bar from ledger outcomes so the number is
   FITTED, not guessed. Among judged units, accept-rate = SURFACED / (SURFACED +
   DECLINED). Too many DECLINED means the bar is too low (paying the judge for
   noise) -> raise it. Almost everything surfacing means the bar is probably too
   high (borderline real catches never reach the judge) -> lower it. The state is a
   single float persisted next to the ledger.

Stdlib only, no third-party deps, deterministic -- same discipline as sonar.py.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Tuple

__all__ = [
    "RiskProfile",
    "TriggerDecision",
    "TriggerPolicy",
    "ControllerConfig",
    "ThresholdController",
    "accept_stats",
    "load_threshold",
    "save_threshold",
    "load_shadow_stats",
    "record_shadow",
    "DEFAULT_HIGH_RISK_MARKERS",
    "SURFACED_VERDICTS",
    "DECLINED_VERDICT",
]

# Verdict strings mirror ledger.py (kept as literals so this module does not import
# the ledger -- it operates on already-read records, duck-typed by `.verdict`).
SURFACED_VERDICTS = ("SURFACED", "ACTIONED")  # ACTIONED = surfaced then applied
DECLINED_VERDICT = "DECLINED"

# Substrings whose presence in a work-unit marks it high-risk: a buried rule missed
# here is expensive (security, credentials, destructive ops, money movement). Lower
# case, matched case-insensitively. Configurable via RiskProfile.
DEFAULT_HIGH_RISK_MARKERS: Tuple[str, ...] = (
    "secret",
    "api_key",
    "apikey",
    "api key",
    "token",
    "password",
    "passwd",
    "private_key",
    "privatekey",
    "credential",
    "authorization",
    "authenticate",
    "delete from",
    "drop table",
    "truncate",
    "migration",
    "payment",
    "transfer",
    "withdraw",
    "approve(",
    "chmod",
    "chown",
    "rm -rf",
    "eval(",
    "exec(",
    "os.system",
    "subprocess",
    "pickle.loads",
    "0.0.0.0",
    "disable_",
    "bypass",
    "allow_all",
    "verify=false",
)


# --------------------------------------------------------------------------- #
# Risk weighting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RiskProfile:
    """Maps a work-unit to a threshold multiplier in (0, 1].

    A multiplier < 1 LOWERS the effective bar, so a high-risk unit escalates to the
    judge on a weaker SONAR signal than a routine one. The default keeps this a
    binary high/normal split, but `markers` and both multipliers are tunable.
    """

    markers: Tuple[str, ...] = DEFAULT_HIGH_RISK_MARKERS
    high_risk_multiplier: float = 0.4
    normal_multiplier: float = 1.0

    def classify(self, work_unit: str) -> float:
        low = work_unit.lower()
        for m in self.markers:
            if m in low:
                return self.high_risk_multiplier
        return self.normal_multiplier


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TriggerDecision:
    should_judge: bool
    top_score: float
    effective_threshold: float
    risk_multiplier: float
    n_considered: int
    reason: str
    shadow: bool = False


@dataclass
class TriggerPolicy:
    """Per-work-unit gate: does the cheap SONAR signal justify the expensive judge?

    `base_threshold` is the SONAR-score bar at normal risk. The effective bar for a
    given unit is `base_threshold * risk.classify(work_unit)`. The judge fires iff
    the top candidate's score meets the effective bar. `max_judge_candidates` caps
    how many candidates are handed to the judge, bounding cost per fire.
    """

    base_threshold: float
    risk: RiskProfile = field(default_factory=RiskProfile)
    max_judge_candidates: int = 3
    shadow_epsilon: float = 0.0

    def decide(self, candidates: Sequence[Any], work_unit: str) -> TriggerDecision:
        risk_mult = self.risk.classify(work_unit)
        effective = self.base_threshold * risk_mult

        if not candidates:
            return TriggerDecision(
                should_judge=False,
                top_score=0.0,
                effective_threshold=effective,
                risk_multiplier=risk_mult,
                n_considered=0,
                reason="no SONAR candidates; nothing to judge",
            )

        top_score = float(_score_of(candidates[0]))
        over_bar = top_score >= effective
        should = over_bar
        shadow = False
        if not over_bar and self._should_shadow(work_unit):
            # Force-judge a below-bar unit occasionally so the controller can measure
            # the recall it is otherwise blind to (its own false negatives).
            should = True
            shadow = True
        band = "high-risk" if risk_mult < self.risk.normal_multiplier else "normal"
        reason = (
            f"top SONAR score {top_score:.4f} "
            f"{'>=' if over_bar else '<'} bar {effective:.4f} "
            f"({band}, x{risk_mult:g})" + (" [shadow sample]" if shadow else "")
        )
        return TriggerDecision(
            should_judge=should,
            top_score=top_score,
            effective_threshold=effective,
            risk_multiplier=risk_mult,
            n_considered=min(len(candidates), self.max_judge_candidates),
            reason=reason,
            shadow=shadow,
        )

    def _should_shadow(self, work_unit: str) -> bool:
        """Deterministic ~epsilon sampler over the work-unit text.

        Reproducible with no PYTHONHASHSEED dependence: a given unit is always or
        never a shadow sample, so behaviour is testable and stable across runs.
        """
        if self.shadow_epsilon <= 0.0:
            return False
        if self.shadow_epsilon >= 1.0:
            return True
        denom = max(1, round(1.0 / self.shadow_epsilon))
        digest = hashlib.sha256(work_unit.encode("utf-8")).hexdigest()
        return int(digest[:8], 16) % denom == 0

    def select_for_judge(self, candidates: Sequence[Any]) -> list:
        """The (already score-sorted) candidates to actually hand the judge, capped."""
        return list(candidates[: self.max_judge_candidates])


def _score_of(candidate: Any) -> float:
    """Duck-typed score access: works with sonar.Candidate, dicts, or test stubs."""
    if isinstance(candidate, dict):
        return float(candidate.get("score", 0.0))
    return float(getattr(candidate, "score", 0.0))


# --------------------------------------------------------------------------- #
# Ledger-driven adaptive control of the base threshold
# --------------------------------------------------------------------------- #


def accept_stats(records: Iterable[Any], window: Optional[int] = None) -> Tuple[int, int]:
    """(surfaced, declined) over ledger records, optionally the last `window` only.

    Records are duck-typed by `.verdict` (or a "verdict" dict key). ACTIONED counts
    as surfaced: it was surfaced and then applied, i.e. the judge was right to fire.
    """
    recs = list(records)
    if window is not None and window > 0:
        recs = recs[-window:]
    surfaced = 0
    declined = 0
    for r in recs:
        v = r.get("verdict") if isinstance(r, dict) else getattr(r, "verdict", None)
        if v in SURFACED_VERDICTS:
            surfaced += 1
        elif v == DECLINED_VERDICT:
            declined += 1
    return surfaced, declined


@dataclass(frozen=True)
class ControllerConfig:
    """Bounds and step sizes for the threshold control loop.

    accept-rate = surfaced / (surfaced + declined) over judged units:
      * below `target_low`  -> judging too much noise  -> RAISE the bar
      * above `target_high` -> too conservative, likely missing borderline catches
                               -> LOWER the bar
      * in band             -> hold
    Adjustments are multiplicative and clamped to [min_threshold, max_threshold].
    Thin data (< `min_samples`) holds -- never chase a handful of verdicts.
    """

    target_low: float = 0.5
    target_high: float = 0.9
    step_up: float = 1.15
    step_down: float = 0.9
    min_threshold: float = 0.0
    max_threshold: float = 1e9
    min_samples: int = 10
    # Shadow-sampling recall guard: if at least `shadow_min_samples` below-bar units
    # were force-judged and at least this fraction surfaced a real catch, the bar is
    # too high and is lowered regardless of accept-rate.
    shadow_recall_floor: float = 0.15
    shadow_min_samples: int = 5


@dataclass
class ThresholdController:
    config: ControllerConfig = field(default_factory=ControllerConfig)

    def next_threshold(
        self,
        current: float,
        surfaced: int,
        declined: int,
        shadow_surfaced: int = 0,
        shadow_total: int = 0,
    ) -> Tuple[float, str]:
        cfg = self.config

        # Recall guard first: shadow sampling is the ONLY signal the gate has about its
        # own false negatives. If force-judged below-bar units are surfacing real
        # catches, the bar is too high no matter how clean the accept-rate looks.
        if shadow_total >= cfg.shadow_min_samples:
            shadow_recall = shadow_surfaced / shadow_total
            if shadow_recall >= cfg.shadow_recall_floor:
                new = max(current * cfg.step_down, cfg.min_threshold)
                return new, (
                    f"shadow recall {shadow_recall:.2f} >= {cfg.shadow_recall_floor}; "
                    f"lower bar {current:.4f} -> {new:.4f} (real catches below the bar)"
                )

        n = surfaced + declined
        if n < cfg.min_samples:
            return current, f"insufficient samples ({n} < {cfg.min_samples}); hold"

        accept = surfaced / n
        if accept < cfg.target_low:
            new = min(current * cfg.step_up, cfg.max_threshold)
            return new, (
                f"accept {accept:.2f} < {cfg.target_low}; raise bar "
                f"{current:.4f} -> {new:.4f} (judging noise)"
            )
        if accept > cfg.target_high:
            new = max(current * cfg.step_down, cfg.min_threshold)
            return new, (
                f"accept {accept:.2f} > {cfg.target_high}; lower bar "
                f"{current:.4f} -> {new:.4f} (too conservative)"
            )
        return current, f"accept {accept:.2f} in [{cfg.target_low}, {cfg.target_high}]; hold"

    def update_from_records(
        self,
        current: float,
        records: Iterable[Any],
        window: Optional[int] = None,
        shadow_surfaced: int = 0,
        shadow_total: int = 0,
    ) -> Tuple[float, str]:
        surfaced, declined = accept_stats(records, window=window)
        return self.next_threshold(
            current, surfaced, declined, shadow_surfaced, shadow_total
        )


# --------------------------------------------------------------------------- #
# Threshold state persistence (a single float + provenance, next to the ledger)
# --------------------------------------------------------------------------- #


def load_threshold(path: "Path | str", default: float) -> float:
    p = Path(path)
    if not p.exists():
        return default
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return float(data["threshold"])
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        # Corrupt state is not fatal: fall back to the default bar rather than wedge.
        return default


def save_threshold(
    path: "Path | str",
    value: float,
    *,
    reason: str = "",
    surfaced: int = 0,
    declined: int = 0,
) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "threshold": value,
                "reason": reason,
                "surfaced": surfaced,
                "declined": declined,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def load_shadow_stats(path: "Path | str") -> Tuple[float, float]:
    """(shadow_surfaced, shadow_total) for below-bar units that were force-judged.

    Floats, because record_shadow can DECAY history into a moving estimate so the
    recall signal reflects recent behaviour rather than a lifetime average that goes
    unresponsive as the total grows.
    """
    p = Path(path)
    if not p.exists():
        return 0.0, 0.0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return float(data.get("shadow_surfaced", 0.0)), float(data.get("shadow_total", 0.0))
    except (json.JSONDecodeError, ValueError, OSError):
        return 0.0, 0.0


def record_shadow(
    path: "Path | str", surfaced: bool, decay: float = 1.0
) -> Tuple[float, float]:
    """Update the shadow-sample counters, returning the new (surfaced, total).

    ``decay`` in (0, 1] downweights the prior counts before adding this sample, so
    the recall estimate tracks RECENT behaviour instead of a lifetime average that
    stops responding once the total is large. decay=1.0 is a plain lifetime count.
    """
    shadow_surfaced, shadow_total = load_shadow_stats(path)
    shadow_surfaced = shadow_surfaced * decay + (1.0 if surfaced else 0.0)
    shadow_total = shadow_total * decay + 1.0
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {"shadow_surfaced": shadow_surfaced, "shadow_total": shadow_total},
            indent=2,
        ),
        encoding="utf-8",
    )
    return shadow_surfaced, shadow_total
