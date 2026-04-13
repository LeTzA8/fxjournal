"""Running P&L event builder.

Builds a chronologically ordered sequence of account-level events (trade closes,
deposits, withdrawals, adjustments) with cumulative running totals for:
  - realized trading P&L  (trade_close events only)
  - cash flow             (deposit / withdrawal / adjustment events only)
  - net result            (sum of both)

The module is intentionally free of Flask/SQLAlchemy imports so it can be tested
with plain dicts and SimpleNamespace objects.
"""

from __future__ import annotations

from datetime import datetime


def _safe_float(value, default=0.0):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _event_sort_key(event):
    """(timestamp, type_priority, secondary_id) — deterministic even for identical timestamps."""
    type_priority = {"deposit": 0, "withdrawal": 1, "adjustment": 2, "trade_close": 3}
    return (
        event.get("timestamp") or datetime.min,
        type_priority.get(event.get("event_type"), 9),
        event.get("_sort_id", 0),
    )


def build_running_pnl_events(
    closed_trades,
    cash_flows,
    *,
    resolve_trade_pnl=None,
    date_from=None,
    date_to=None,
):
    """Return a list of running-P&L event dicts, oldest first.

    Parameters
    ----------
    closed_trades : iterable
        Trade-like objects that are closed (``closed_at`` is not None).
        Each must expose ``closed_at``, ``pnl`` (raw), ``commission``, ``swap``,
        and ``id`` (used as a tiebreaker).
    cash_flows : iterable
        AccountCashFlow-like objects.  Each must expose ``occurred_at``,
        ``flow_type`` (deposit / withdrawal / adjustment), ``amount``, and ``id``.
    resolve_trade_pnl : callable, optional
        ``fn(trade) -> float | None``.  If given, used instead of the built-in
        pnl + commission + swap logic.  Trades that return ``None`` are skipped.
    date_from : datetime, optional
        Inclusive lower bound on event timestamps.
    date_to : datetime, optional
        Exclusive upper bound on event timestamps.

    Returns
    -------
    list[dict]
        Each dict:
          timestamp, event_type, amount, description,
          running_realized_pnl, running_cash_flow, running_net_result
    """
    raw_events = []

    for trade in (closed_trades or []):
        closed_at = getattr(trade, "closed_at", None)
        if closed_at is None:
            continue

        if resolve_trade_pnl is not None:
            pnl_value = resolve_trade_pnl(trade)
        else:
            raw_pnl = _safe_float(getattr(trade, "pnl", None), default=None)
            if raw_pnl is None:
                continue
            commission = _safe_float(getattr(trade, "commission", None), default=0.0)
            swap = _safe_float(getattr(trade, "swap", None), default=0.0)
            pnl_value = raw_pnl - abs(commission) + swap

        if pnl_value is None:
            continue

        symbol = str(getattr(trade, "symbol", "") or "").strip().upper() or "?"
        side = str(getattr(trade, "side", "") or "").strip().upper() or "?"

        raw_events.append({
            "timestamp": closed_at,
            "event_type": "trade_close",
            "amount": round(pnl_value, 2),
            "description": f"{side} {symbol}",
            "_sort_id": getattr(trade, "id", 0) or 0,
        })

    for cf in (cash_flows or []):
        occurred_at = getattr(cf, "occurred_at", None)
        if occurred_at is None:
            continue
        flow_type = str(getattr(cf, "flow_type", "") or "").strip().lower()
        if flow_type not in ("deposit", "withdrawal", "adjustment"):
            continue
        raw_amount = _safe_float(getattr(cf, "amount", None), default=None)
        if raw_amount is None:
            continue

        signed_amount = raw_amount
        if flow_type == "withdrawal":
            signed_amount = -abs(raw_amount)
        elif flow_type == "deposit":
            signed_amount = abs(raw_amount)

        note = str(getattr(cf, "note", "") or "").strip()
        description = note if note else flow_type.capitalize()

        raw_events.append({
            "timestamp": occurred_at,
            "event_type": flow_type,
            "amount": round(signed_amount, 2),
            "description": description,
            "_sort_id": getattr(cf, "id", 0) or 0,
        })

    raw_events.sort(key=_event_sort_key)

    if date_from is not None:
        raw_events = [e for e in raw_events if e["timestamp"] >= date_from]
    if date_to is not None:
        raw_events = [e for e in raw_events if e["timestamp"] < date_to]

    running_realized_pnl = 0.0
    running_cash_flow = 0.0
    result = []

    for event in raw_events:
        amount = event["amount"]
        if event["event_type"] == "trade_close":
            running_realized_pnl += amount
        else:
            running_cash_flow += amount

        result.append({
            "timestamp": event["timestamp"],
            "event_type": event["event_type"],
            "amount": amount,
            "description": event["description"],
            "running_realized_pnl": round(running_realized_pnl, 2),
            "running_cash_flow": round(running_cash_flow, 2),
            "running_net_result": round(running_realized_pnl + running_cash_flow, 2),
        })

    return result


def summarize_running_pnl(events):
    """Return a summary dict from a list of running-P&L events."""
    if not events:
        return {
            "total_realized_pnl": 0.0,
            "total_cash_flow": 0.0,
            "total_net_result": 0.0,
            "event_count": 0,
            "trade_close_count": 0,
            "deposit_count": 0,
            "withdrawal_count": 0,
            "adjustment_count": 0,
        }

    last = events[-1]
    counts = {"trade_close": 0, "deposit": 0, "withdrawal": 0, "adjustment": 0}
    for event in events:
        event_type = event.get("event_type", "")
        if event_type in counts:
            counts[event_type] += 1

    return {
        "total_realized_pnl": last["running_realized_pnl"],
        "total_cash_flow": last["running_cash_flow"],
        "total_net_result": last["running_net_result"],
        "event_count": len(events),
        "trade_close_count": counts["trade_close"],
        "deposit_count": counts["deposit"],
        "withdrawal_count": counts["withdrawal"],
        "adjustment_count": counts["adjustment"],
    }
