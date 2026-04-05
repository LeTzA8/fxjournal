"""Shared trend direction helpers for dashboard rolling windows and similar series."""


def _non_null_values(series):
    return [v for v in series if v is not None]


def trend_direction_half_split(values, *, flat_threshold, lower_is_better=False):
    """
    Compare average of the more recent half of ``values`` vs the older half.
    ``values`` must contain no None entries. Index 0 = most recent when the
    caller builds the series in newest-first order.
    """
    if len(values) < 2:
        return None
    mid = len(values) // 2
    recent_avg = sum(values[:mid]) / mid if mid else values[0]
    older_avg = sum(values[mid:]) / (len(values) - mid)
    delta = recent_avg - older_avg
    if abs(delta) < flat_threshold:
        return "flat"
    if lower_is_better:
        return "improving" if delta < 0 else "declining"
    return "improving" if delta > 0 else "declining"


def trend_direction_win_rate_weeks(series):
    """Win-rate % series (optional entries); flat band is one percentage point."""
    values = _non_null_values(series)
    return trend_direction_half_split(values, flat_threshold=1.0, lower_is_better=False)


def trend_direction_expectancy_weeks(series):
    """Expectancy (currency per trade) series; flat band scales with typical magnitude."""
    values = _non_null_values(series)
    if len(values) < 2:
        return None
    mid = len(values) // 2
    recent_avg = sum(values[:mid]) / mid if mid else values[0]
    older_avg = sum(values[mid:]) / (len(values) - mid)
    delta = recent_avg - older_avg
    scale = max(abs(v) for v in values)
    if scale < 1e-12:
        scale = 1.0
    if abs(delta) < 0.05 * scale:
        return "flat"
    return "improving" if delta > 0 else "declining"


def trend_direction_ei_scores(scores):
    """Emotional index scores; lower is better. Flat band matches weekly AI payload logic."""
    values = _non_null_values(scores)
    return trend_direction_half_split(values, flat_threshold=0.3, lower_is_better=True)
