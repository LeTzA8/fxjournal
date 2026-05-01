from datetime import datetime, timedelta, timezone


TRADE_CHART_M5_SECONDS = 5 * 60
TRADE_CHART_PRE_ENTRY_M5_BARS = 432
TRADE_CHART_POST_EXIT_M5_BARS = 144
TRADE_CHART_COVERAGE_TOLERANCE_SECONDS = 15 * 60


def _ensure_utc_aware(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def expected_trade_chart_m5_window(opened_at, closed_at, *, now=None):
    opened_at_utc = _ensure_utc_aware(opened_at)
    closed_at_utc = _ensure_utc_aware(closed_at)
    if opened_at_utc is None or closed_at_utc is None:
        return None, None

    start_dt = opened_at_utc - timedelta(
        seconds=TRADE_CHART_PRE_ENTRY_M5_BARS * TRADE_CHART_M5_SECONDS
    )
    full_end_dt = closed_at_utc + timedelta(
        seconds=TRADE_CHART_POST_EXIT_M5_BARS * TRADE_CHART_M5_SECONDS
    )

    now_utc = _ensure_utc_aware(now) if now is not None else datetime.now(timezone.utc)
    available_end_dt = min(full_end_dt, now_utc)
    target_end_dt = max(closed_at_utc, available_end_dt)
    return start_dt, target_end_dt


def has_complete_m5_chart_coverage(
    *,
    opened_at,
    closed_at,
    bar_count,
    min_bar_time,
    max_bar_time,
    now=None,
):
    if int(bar_count or 0) <= 0 or min_bar_time is None or max_bar_time is None:
        return False

    opened_at_utc = _ensure_utc_aware(opened_at)
    _, target_end_dt = expected_trade_chart_m5_window(opened_at, closed_at, now=now)
    if opened_at_utc is None or target_end_dt is None:
        return False

    opened_at_epoch = int(opened_at_utc.timestamp())
    target_end_epoch = int(target_end_dt.timestamp())
    if int(min_bar_time) > opened_at_epoch + TRADE_CHART_COVERAGE_TOLERANCE_SECONDS:
        return False
    if int(max_bar_time) < target_end_epoch - TRADE_CHART_COVERAGE_TOLERANCE_SECONDS:
        return False
    return True
