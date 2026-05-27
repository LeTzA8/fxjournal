"""Risk-size comparison helpers for AI payload and weekly signals."""

from __future__ import annotations

RISK_CHANGE_TOLERANCE = 0.15


def _safe_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def classify_risk_change(current, baseline):
    """Return larger, smaller, same, or None."""
    current_value = _safe_float(current)
    baseline_value = _safe_float(baseline)
    if current_value is None or baseline_value in (None, 0):
        return None
    ratio = (current_value - baseline_value) / abs(baseline_value)
    if abs(ratio) <= RISK_CHANGE_TOLERANCE:
        return "same"
    return "larger" if ratio > 0 else "smaller"


def classify_risk_change_between(current_trade, baseline_trade):
    """Compare two serialized trades using matching risk basis (pct, then dollars)."""
    current_pct = _safe_float((current_trade or {}).get("trade_risk_pct"))
    baseline_pct = _safe_float((baseline_trade or {}).get("trade_risk_pct"))
    if current_pct is not None and baseline_pct is not None:
        return classify_risk_change(current_pct, baseline_pct)

    current_dollars = _safe_float((current_trade or {}).get("planned_risk_dollars"))
    baseline_dollars = _safe_float((baseline_trade or {}).get("planned_risk_dollars"))
    if current_dollars is not None and baseline_dollars is not None:
        return classify_risk_change(current_dollars, baseline_dollars)
    return None


def _trade_sort_key(trade):
    return (
        trade.get("trade_sequence_number") or 0,
        trade.get("opened_at") or "",
        trade.get("review_ref") or "",
    )


def enrich_serialized_trades_with_risk_comparison(serialized_trades):
    """Add risk_pct_vs_prev and risk_pct_vs_prev_symbol to each trade dict."""
    if not serialized_trades:
        return serialized_trades

    ordered = sorted(serialized_trades, key=_trade_sort_key)
    previous_trade = None
    last_symbol_trade = {}

    for trade in ordered:
        if previous_trade is not None:
            change = classify_risk_change_between(trade, previous_trade)
            if change is not None:
                trade["risk_pct_vs_prev"] = change

        symbol = (trade.get("symbol") or "").strip().upper()
        if symbol:
            previous_symbol_trade = last_symbol_trade.get(symbol)
            if previous_symbol_trade is not None:
                symbol_change = classify_risk_change_between(trade, previous_symbol_trade)
                if symbol_change is not None:
                    trade["risk_pct_vs_prev_symbol"] = symbol_change
            last_symbol_trade[symbol] = trade

        previous_trade = trade

    return serialized_trades
