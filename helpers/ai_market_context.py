import re
import statistics
from datetime import timezone

from trading import get_active_sessions


BAR_TIMEFRAME_SECONDS = 5 * 60
ENTRY_CONTEXT_BAR_COUNT = 12
LARGE_CANDLE_MULTIPLIER = 2.0
LARGE_CANDLE_LOOKBACK = 3
POST_EXIT_DIRECTION_BARS = 12

_BREAKEVEN_PATTERNS = (
    re.compile(r"\bbreakeven\b", re.IGNORECASE),
    re.compile(r"\bbreak[\s-]?even\b", re.IGNORECASE),
    re.compile(r"\bsl\s*(?:to|@)\s*be\b", re.IGNORECASE),
    re.compile(r"\bstop(?:\s*loss)?\s*(?:to|@)\s*be\b", re.IGNORECASE),
    re.compile(r"\b(?:sl|stop(?:\s*loss)?)\b.{0,24}\bbe\b", re.IGNORECASE),
)
_TRAILING_PATTERNS = (
    re.compile(r"\btrail(?:ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\btrailing\s+(?:sl|stop|stop\s*loss)\b", re.IGNORECASE),
    re.compile(r"\b(?:sl|stop|stop\s*loss)\s+(?:moved|modified|adjusted)\b", re.IGNORECASE),
    re.compile(r"\bmoved\s+(?:sl|stop|stop\s*loss)\b", re.IGNORECASE),
)


def _safe_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value, digits=2):
    if value is None:
        return None
    return round(float(value), digits)


def _ensure_utc_aware(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _attr(record, name, default=None):
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _bar_dict(bar):
    return {
        "time": int(_attr(bar, "bar_time", _attr(bar, "time", 0)) or 0),
        "open": _safe_float(_attr(bar, "open")),
        "high": _safe_float(_attr(bar, "high")),
        "low": _safe_float(_attr(bar, "low")),
        "close": _safe_float(_attr(bar, "close")),
        "tick_volume": _attr(bar, "tick_volume"),
    }


def _trade_side(trade):
    return str(_attr(trade, "side", "") or "").strip().upper()


def _price_tolerance(entry_price):
    if entry_price is None:
        return 0.0
    return max(abs(float(entry_price)) * 0.000001, 0.0000001)


def _stop_on_initial_risk_side(*, side, entry_price, stop_loss):
    if entry_price is None or stop_loss is None:
        return False
    tolerance = _price_tolerance(entry_price)
    if side == "SELL":
        return stop_loss > entry_price + tolerance
    return stop_loss < entry_price - tolerance


def _crossed_price_level(bars, *, side, level, target_kind):
    if level is None:
        return None
    for bar in bars:
        high = bar.get("high")
        low = bar.get("low")
        if high is None or low is None:
            continue
        if target_kind == "tp":
            crossed = high >= level if side != "SELL" else low <= level
        else:
            crossed = low <= level if side != "SELL" else high >= level
        if crossed:
            return bar.get("time")
    return None


def _count_consecutive_in_direction(bars, side):
    """Count trailing consecutive bars (from most recent) that close in the trade direction."""
    count = 0
    for bar in reversed(bars):
        open_ = bar.get("open")
        close = bar.get("close")
        if open_ is None or close is None:
            break
        in_direction = (close > open_) if side == "BUY" else (close < open_)
        if in_direction:
            count += 1
        else:
            break
    return count


def _matched_note_hints(system_trade_note):
    text = str(system_trade_note or "").strip()
    if not text:
        return []
    hints = []
    if any(pattern.search(text) for pattern in _BREAKEVEN_PATTERNS):
        hints.append("system_note_mentions_breakeven_stop")
    if any(pattern.search(text) for pattern in _TRAILING_PATTERNS):
        hints.append("system_note_mentions_trailing_or_moved_stop")
    return hints


def build_stop_management_context(trade):
    side = _trade_side(trade)
    entry_price = _safe_float(_attr(trade, "entry_price"))
    stop_loss = _safe_float(_attr(trade, "stop_loss"))
    note_hints = _matched_note_hints(_attr(trade, "system_trade_note"))
    evidence = list(note_hints)
    breakeven_or_better = False
    protects_profit = False

    if side in {"BUY", "SELL"} and entry_price is not None and stop_loss is not None:
        tolerance = _price_tolerance(entry_price)
        if side == "SELL":
            breakeven_or_better = stop_loss <= entry_price + tolerance
            protects_profit = stop_loss < entry_price - tolerance
        else:
            breakeven_or_better = stop_loss >= entry_price - tolerance
            protects_profit = stop_loss > entry_price + tolerance
        if protects_profit:
            evidence.append("stored_stop_loss_protects_profit")
        elif breakeven_or_better:
            evidence.append("stored_stop_loss_at_or_beyond_breakeven")

    if protects_profit and note_hints:
        confidence = "high"
    elif breakeven_or_better and note_hints:
        confidence = "high"
    elif protects_profit or breakeven_or_better or note_hints:
        confidence = "medium"
    else:
        confidence = "none"

    return {
        "stop_loss_breakeven_or_better": bool(breakeven_or_better),
        "stop_loss_protects_profit": bool(protects_profit),
        "possible_trailing_or_breakeven_stop": bool(evidence),
        "confidence": confidence,
        "evidence": evidence,
        "basis": "stored_stop_loss_and_system_trade_note",
    }


def build_trade_market_context(trade, bars):
    side = _trade_side(trade)
    entry_price = _safe_float(_attr(trade, "entry_price"))
    exit_price = _safe_float(_attr(trade, "exit_price"))
    stop_loss = _safe_float(_attr(trade, "stop_loss"))
    take_profit = _safe_float(_attr(trade, "take_profit"))
    opened_at = _ensure_utc_aware(_attr(trade, "opened_at"))
    closed_at = _ensure_utc_aware(_attr(trade, "closed_at"))
    stop_management = build_stop_management_context(trade)

    context = {
        "bars_status": "unavailable",
        "timeframe": "M5",
        "bars_used": 0,
        "in_trade_bars": 0,
        "post_exit_bars": 0,
        "mfe_price_move": None,
        "mae_price_move": None,
        "mfe_r": None,
        "mae_r": None,
        "entry_range_vs_prior_median": None,
        "entry_location_in_prior_range_pct": None,
        "entry_near_prior_high": None,
        "entry_near_prior_low": None,
        "entry_active_sessions": None,
        "entry_in_session_overlap": None,
        "large_candle_before_entry": None,
        "entry_bar_body_ratio": None,
        "entry_bar_closes_in_trade_direction": None,
        "pre_entry_bars_in_trade_direction": None,
        "entry_tick_volume_vs_median": None,
        "post_exit_tp_reached": None,
        "minutes_after_exit_to_tp": None,
        "post_exit_sl_reached": None,
        "minutes_after_exit_to_sl": None,
        "post_exit_price_move": None,
        "post_exit_direction": None,
        "stop_management": stop_management,
    }
    if entry_price is None or opened_at is None or closed_at is None or side not in {"BUY", "SELL"}:
        return context

    # Session overlap — DST-aware via pytz timezones in get_active_sessions
    active_sessions = get_active_sessions(opened_at)
    context["entry_active_sessions"] = active_sessions
    context["entry_in_session_overlap"] = len(active_sessions) >= 2

    opened_epoch = int(opened_at.timestamp())
    closed_epoch = int(closed_at.timestamp())
    bar_dicts = sorted(
        [bar for bar in (_bar_dict(row) for row in (bars or [])) if bar["time"]],
        key=lambda item: item["time"],
    )
    if not bar_dicts:
        return context

    in_trade = [
        bar
        for bar in bar_dicts
        if opened_epoch <= int(bar["time"]) <= closed_epoch
    ]
    post_exit = [bar for bar in bar_dicts if int(bar["time"]) > closed_epoch]
    prior = [bar for bar in bar_dicts if int(bar["time"]) < opened_epoch]
    context["bars_status"] = "ready" if in_trade else "partial"
    context["bars_used"] = len(bar_dicts)
    context["in_trade_bars"] = len(in_trade)
    context["post_exit_bars"] = len(post_exit)

    if in_trade:
        max_high = max(bar["high"] for bar in in_trade if bar.get("high") is not None)
        min_low = min(bar["low"] for bar in in_trade if bar.get("low") is not None)
        if side == "SELL":
            mfe = entry_price - min_low
            mae = max_high - entry_price
        else:
            mfe = max_high - entry_price
            mae = entry_price - min_low
        context["mfe_price_move"] = _round(max(mfe, 0.0), 5)
        context["mae_price_move"] = _round(max(mae, 0.0), 5)

        if _stop_on_initial_risk_side(side=side, entry_price=entry_price, stop_loss=stop_loss):
            risk_price = abs(entry_price - stop_loss)
            if risk_price > 0:
                context["mfe_r"] = _round(max(mfe, 0.0) / risk_price)
                context["mae_r"] = _round(max(mae, 0.0) / risk_price)

    entry_bar = None
    for bar in reversed(bar_dicts):
        if int(bar["time"]) <= opened_epoch:
            entry_bar = bar
            break

    prior_sample = prior[-ENTRY_CONTEXT_BAR_COUNT:]

    # Compute range stats for prior_sample — used by multiple sections below
    ranges = []
    median_range = None
    if prior_sample:
        ranges = [
            bar["high"] - bar["low"]
            for bar in prior_sample
            if bar.get("high") is not None and bar.get("low") is not None and bar["high"] >= bar["low"]
        ]
        if ranges:
            median_range = statistics.median(ranges)

    if entry_bar and prior_sample:
        if median_range is not None:
            entry_range = entry_bar["high"] - entry_bar["low"]
            if median_range > 0:
                context["entry_range_vs_prior_median"] = _round(entry_range / median_range)
        prior_high = max(bar["high"] for bar in prior_sample if bar.get("high") is not None)
        prior_low = min(bar["low"] for bar in prior_sample if bar.get("low") is not None)
        span = prior_high - prior_low
        if span > 0:
            location = ((entry_price - prior_low) / span) * 100.0
            context["entry_location_in_prior_range_pct"] = _round(location, 1)
            context["entry_near_prior_high"] = location >= 85.0
            context["entry_near_prior_low"] = location <= 15.0

    # Large candle in the bars immediately before entry
    if median_range is not None and median_range > 0 and prior:
        lookback = prior[-LARGE_CANDLE_LOOKBACK:]
        context["large_candle_before_entry"] = any(
            bar.get("high") is not None
            and bar.get("low") is not None
            and (bar["high"] - bar["low"]) > LARGE_CANDLE_MULTIPLIER * median_range
            for bar in lookback
        )

    # Entry bar quality
    if entry_bar:
        o = entry_bar.get("open")
        c = entry_bar.get("close")
        h = entry_bar.get("high")
        lo = entry_bar.get("low")
        if all(v is not None for v in [o, c, h, lo]):
            bar_range = h - lo
            if bar_range > 0:
                context["entry_bar_body_ratio"] = _round(abs(c - o) / bar_range)
            context["entry_bar_closes_in_trade_direction"] = (c > o) if side == "BUY" else (c < o)

    # Pre-entry momentum: consecutive bars in trade direction up to entry
    if prior_sample:
        context["pre_entry_bars_in_trade_direction"] = _count_consecutive_in_direction(prior_sample, side)

    # Tick volume at entry vs median of prior sample
    if entry_bar and prior_sample:
        entry_vol = entry_bar.get("tick_volume")
        if entry_vol is not None:
            prior_vols = [
                b["tick_volume"] for b in prior_sample
                if b.get("tick_volume") is not None
            ]
            if prior_vols:
                med_vol = statistics.median(prior_vols)
                if med_vol > 0:
                    context["entry_tick_volume_vs_median"] = _round(entry_vol / med_vol)

    tp_time = _crossed_price_level(post_exit, side=side, level=take_profit, target_kind="tp")
    if take_profit is not None:
        context["post_exit_tp_reached"] = tp_time is not None
        if tp_time is not None:
            context["minutes_after_exit_to_tp"] = _round((tp_time - closed_epoch) / 60.0)

    sl_time = _crossed_price_level(post_exit, side=side, level=stop_loss, target_kind="sl")
    if stop_loss is not None:
        context["post_exit_sl_reached"] = sl_time is not None
        if sl_time is not None:
            context["minutes_after_exit_to_sl"] = _round((sl_time - closed_epoch) / 60.0)

    # Post-exit price direction relative to exit price
    if post_exit and exit_price is not None:
        direction_window = post_exit[:POST_EXIT_DIRECTION_BARS]
        if direction_window:
            last_close = direction_window[-1].get("close")
            if last_close is not None:
                raw_move = last_close - exit_price
                # Signed so positive = favorable for the trade direction
                signed_move = raw_move if side == "BUY" else -raw_move
                context["post_exit_price_move"] = _round(raw_move, 5)
                # Use 30% of median pre-entry range as flat/move threshold
                threshold = (median_range * 0.3) if (median_range is not None and median_range > 0) else 0.0
                if threshold > 0:
                    if signed_move > threshold:
                        context["post_exit_direction"] = "continued"
                    elif signed_move < -threshold:
                        context["post_exit_direction"] = "reversed"
                    else:
                        context["post_exit_direction"] = "flat"

    return context
