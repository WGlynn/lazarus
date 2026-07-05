"""Tests for the v2 trigger policy: the SONAR-signal gate + adaptive controller."""

import pytest

from lazarus_sonar.async_.trigger import (
    ControllerConfig,
    RiskProfile,
    ThresholdController,
    TriggerPolicy,
    accept_stats,
    load_threshold,
    save_threshold,
)


class Cand:
    """Minimal duck-typed SONAR candidate (only .score is read by the gate)."""

    def __init__(self, score: float) -> None:
        self.score = score


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


def test_no_candidates_skips():
    d = TriggerPolicy(base_threshold=2.0).decide([], "some work unit")
    assert d.should_judge is False
    assert d.n_considered == 0
    assert d.top_score == 0.0


def test_below_bar_skips():
    d = TriggerPolicy(base_threshold=2.0).decide([Cand(1.0), Cand(0.5)], "cosmetic text change")
    assert d.should_judge is False
    assert d.top_score == 1.0


def test_above_bar_judges():
    d = TriggerPolicy(base_threshold=2.0).decide([Cand(3.0)], "harmless text")
    assert d.should_judge is True


def test_high_risk_lowers_bar():
    pol = TriggerPolicy(base_threshold=2.0, risk=RiskProfile(high_risk_multiplier=0.4))
    # Same SONAR score, two work-units. Routine -> below bar -> skip.
    normal = pol.decide([Cand(1.0)], "just reformatting whitespace")
    assert normal.should_judge is False
    # Mentions api_key -> high risk -> bar drops to 0.8 -> judged on the same signal.
    risky = pol.decide([Cand(1.0)], "log the api_key while debugging")
    assert risky.should_judge is True
    assert risky.risk_multiplier == 0.4
    assert risky.effective_threshold == pytest.approx(0.8)


def test_dict_candidates_supported():
    d = TriggerPolicy(base_threshold=1.0).decide([{"score": 2.0}], "x")
    assert d.should_judge is True


def test_select_for_judge_caps():
    cands = [Cand(9), Cand(8), Cand(7), Cand(6), Cand(5)]
    sel = TriggerPolicy(base_threshold=0.0, max_judge_candidates=3).select_for_judge(cands)
    assert [c.score for c in sel] == [9, 8, 7]


# --------------------------------------------------------------------------- #
# The controller
# --------------------------------------------------------------------------- #


def test_controller_holds_on_thin_data():
    ctl = ThresholdController(ControllerConfig(min_samples=10))
    new, why = ctl.next_threshold(2.0, surfaced=1, declined=2)
    assert new == 2.0
    assert "insufficient" in why


def test_controller_raises_on_low_accept():
    ctl = ThresholdController(ControllerConfig(min_samples=10, target_low=0.5, step_up=1.15))
    new, why = ctl.next_threshold(2.0, surfaced=2, declined=18)  # accept 0.10
    assert new == pytest.approx(2.3)
    assert "raise" in why


def test_controller_lowers_on_high_accept():
    ctl = ThresholdController(ControllerConfig(min_samples=10, target_high=0.9, step_down=0.9))
    new, why = ctl.next_threshold(2.0, surfaced=19, declined=1)  # accept 0.95
    assert new == pytest.approx(1.8)
    assert "lower" in why


def test_controller_holds_in_band():
    ctl = ThresholdController(ControllerConfig(min_samples=10))
    new, why = ctl.next_threshold(2.0, surfaced=14, declined=6)  # accept 0.70
    assert new == 2.0
    assert "hold" in why


def test_controller_clamps_to_min():
    ctl = ThresholdController(
        ControllerConfig(min_samples=1, target_high=0.9, step_down=0.9, min_threshold=1.0)
    )
    new, _ = ctl.next_threshold(1.0, surfaced=10, declined=0)  # would drop below min
    assert new == 1.0


# --------------------------------------------------------------------------- #
# accept_stats + persistence
# --------------------------------------------------------------------------- #


def test_accept_stats_counts_actioned_as_surfaced():
    recs = [
        {"verdict": "SURFACED"},
        {"verdict": "ACTIONED"},
        {"verdict": "DECLINED"},
        {"verdict": "DECLINED"},
    ]
    assert accept_stats(recs) == (2, 2)


def test_accept_stats_window():
    recs = [{"verdict": "DECLINED"}] * 20 + [{"verdict": "SURFACED"}] * 5
    assert accept_stats(recs, window=5) == (5, 0)


def test_update_from_records_raises_on_noisy_history():
    recs = [{"verdict": "DECLINED"}] * 18 + [{"verdict": "SURFACED"}] * 2
    ctl = ThresholdController(ControllerConfig(min_samples=10, step_up=1.15))
    new, _ = ctl.update_from_records(2.0, recs)
    assert new == pytest.approx(2.3)


def test_threshold_persistence_roundtrip(tmp_path):
    p = tmp_path / "sub" / "threshold.json"
    assert load_threshold(p, default=1.5) == 1.5  # missing -> default
    save_threshold(p, 2.75, reason="test", surfaced=3, declined=1)
    assert load_threshold(p, default=1.5) == 2.75


def test_threshold_load_corrupt_returns_default(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json{{", encoding="utf-8")
    assert load_threshold(p, default=0.9) == 0.9
