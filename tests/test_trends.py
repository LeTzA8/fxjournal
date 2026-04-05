from helpers.trends import (
    trend_direction_ei_scores,
    trend_direction_expectancy_weeks,
    trend_direction_win_rate_weeks,
)


def test_trend_direction_win_rate_weeks_detects_improving():
    assert trend_direction_win_rate_weeks([80.0, 70.0, 65.0, 60.0]) == "improving"


def test_trend_direction_expectancy_weeks_uses_scaled_flat_band():
    # Large magnitude: 5% of scale (~103) > delta 2.5 → flat
    assert trend_direction_expectancy_weeks([103.0, 102.0, 100.0, 100.0]) == "flat"
    assert trend_direction_expectancy_weeks([120.0, 100.0, 100.0, 100.0]) == "improving"


def test_trend_direction_ei_scores_lower_is_better():
    assert trend_direction_ei_scores([3.0, 5.0, 6.0, 7.0]) == "improving"
    assert trend_direction_ei_scores([8.0, 7.0, 6.0, 5.0]) == "declining"
