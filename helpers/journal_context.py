"""Structured context for the admin-only conversational journal surface."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, time

from sqlalchemy.orm import selectinload

from helpers.ai_market_context import build_trade_market_context
from helpers.trade_interpretation import interpretation_snapshot
from helpers.trade_state import trade_is_closed
from models import JournalSession, Trade, TradeBars
from trading import classify_trading_session


MAX_FREEFORM_TRADES = 20
MAX_RELATED_TRADES = 5
MAX_DAY_SCOPE_TRADES = 50


def _safe_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value):
    return value.isoformat() if value is not None else None


def _duration_minutes(trade):
    opened_at = getattr(trade, "opened_at", None)
    closed_at = getattr(trade, "closed_at", None)
    if opened_at is None or closed_at is None:
        return None
    try:
        return round((closed_at - opened_at).total_seconds() / 60.0, 1)
    except (TypeError, ValueError):
        return None


def _closed_trade_query(user_id, trade_account_id=None):
    query = (
        Trade.query.filter(
            Trade.user_id == user_id,
            Trade.closed_at.isnot(None),
        )
        .options(
            selectinload(Trade.interpretation),
            selectinload(Trade.trade_profile),
            selectinload(Trade.trade_profile_version),
            selectinload(Trade.trade_account),
        )
    )
    if trade_account_id is not None:
        query = query.filter(Trade.trade_account_id == trade_account_id)
    return query


def _load_m5_bars(trade):
    trade_id = getattr(trade, "id", None)
    if trade_id is None:
        return []
    return (
        TradeBars.query.filter_by(trade_id=trade_id, timeframe="M5")
        .order_by(TradeBars.bar_time.asc())
        .all()
    )


def _minimal_market_context(trade, market_context=None):
    context = market_context if isinstance(market_context, dict) else build_trade_market_context(trade, _load_m5_bars(trade))
    return {
        "entry_session": classify_trading_session(getattr(trade, "opened_at", None)),
        "entry_active_sessions": context.get("entry_active_sessions"),
        "mfe_price_move": context.get("mfe_price_move"),
        "mae_price_move": context.get("mae_price_move"),
        "mfe_r": context.get("mfe_r"),
        "mae_r": context.get("mae_r"),
    }


def _trade_strategy_name(trade):
    profile = getattr(trade, "trade_profile", None)
    version = getattr(trade, "trade_profile_version", None)
    return (
        str(getattr(version, "name", "") or "").strip()
        or str(getattr(profile, "name", "") or "").strip()
        or None
    )


def _trade_dict(trade, ref, *, market_depth="none"):
    market_context = None
    if market_depth == "deep":
        market_context = build_trade_market_context(trade, _load_m5_bars(trade))
    elif market_depth == "minimal":
        market_context = _minimal_market_context(trade)

    payload = {
        "ref": ref,
        "trade_pubkey": getattr(trade, "pubkey", None),
        "symbol": getattr(trade, "symbol", None),
        "side": getattr(trade, "side", None),
        "entry_price": _safe_float(getattr(trade, "entry_price", None)),
        "exit_price": _safe_float(getattr(trade, "exit_price", None)),
        "lot_size": _safe_float(getattr(trade, "lot_size", None)),
        "pnl": _safe_float(getattr(trade, "pnl", None)),
        "stop_loss": _safe_float(getattr(trade, "stop_loss", None)),
        "take_profit": _safe_float(getattr(trade, "take_profit", None)),
        "opened_at": _iso(getattr(trade, "opened_at", None)),
        "closed_at": _iso(getattr(trade, "closed_at", None)),
        "duration_minutes": _duration_minutes(trade),
        "session": classify_trading_session(getattr(trade, "opened_at", None)),
        "strategy": _trade_strategy_name(trade),
        "behavior_flags": interpretation_snapshot(trade),
    }
    if market_depth == "deep":
        payload["market_context"] = market_context
    elif market_depth == "minimal":
        payload["market_context"] = market_context
    return payload


def _summary_for_trades(trades):
    trade_count = len(trades)
    net_pnl = sum(_safe_float(getattr(trade, "pnl", None)) or 0.0 for trade in trades)
    wins = sum(1 for trade in trades if (_safe_float(getattr(trade, "pnl", None)) or 0.0) > 0)
    win_rate = round((wins / trade_count) * 100.0, 1) if trade_count else 0.0
    symbols = Counter(str(getattr(trade, "symbol", "") or "").strip() for trade in trades)
    flag_counts = Counter()
    for trade in trades:
        flags = interpretation_snapshot(trade)
        for key in ("is_revenge", "is_reactive", "is_corrective"):
            if flags.get(key):
                flag_counts[key] += 1
    return {
        "trade_count": trade_count,
        "net_pnl": round(net_pnl, 2),
        "win_rate_pct": win_rate,
        "dominant_symbol": symbols.most_common(1)[0][0] if symbols else None,
        "behavior_flag_counts": dict(flag_counts),
    }


def build_journal_payload(user, session) -> dict:
    user_id = getattr(user, "id", None)
    scope_type = str(getattr(session, "scope_type", "") or "").strip().lower()
    trade_account_id = getattr(session, "trade_account_id", None)

    payload = {
        "scope_type": scope_type,
        "session_id": getattr(session, "id", None),
        "trade_account_id": trade_account_id,
        "scope_trade_pubkey": getattr(session, "scope_trade_pubkey", None),
        "scope_date": _iso(getattr(session, "scope_date", None)),
        "summary": {},
        "trades": [],
    }

    if scope_type == JournalSession.SCOPE_TRADE:
        focal_trade = (
            _closed_trade_query(user_id, trade_account_id)
            .filter(Trade.pubkey == getattr(session, "scope_trade_pubkey", None))
            .first()
        )
        if focal_trade is None:
            payload["summary"] = {"error": "focal_trade_not_found"}
            return payload
        related = (
            _closed_trade_query(user_id, trade_account_id)
            .filter(
                Trade.symbol == focal_trade.symbol,
                Trade.id != focal_trade.id,
                Trade.closed_at <= focal_trade.closed_at,
            )
            .order_by(Trade.closed_at.desc(), Trade.id.desc())
            .limit(MAX_RELATED_TRADES)
            .all()
        )
        trades = [focal_trade] + list(reversed(related))
        payload["summary"] = {
            "focal_ref": "T1",
            "focal_symbol": focal_trade.symbol,
            "related_closed_trades_same_symbol": len(related),
        }
        payload["trades"] = [
            _trade_dict(focal_trade, "T1", market_depth="deep"),
            *[
                _trade_dict(trade, f"T{index}", market_depth="none")
                for index, trade in enumerate(trades[1:], start=2)
            ],
        ]
        return payload

    if scope_type == JournalSession.SCOPE_DAY:
        scope_date = getattr(session, "scope_date", None)
        if scope_date is None:
            payload["summary"] = {"error": "scope_date_required"}
            return payload
        day_start = datetime.combine(scope_date, time.min)
        day_end = datetime.combine(scope_date, time.max)
        trades = (
            _closed_trade_query(user_id, trade_account_id)
            .filter(Trade.closed_at >= day_start, Trade.closed_at <= day_end)
            .order_by(Trade.closed_at.asc(), Trade.id.asc())
            .all()
        )
        day_closed_trade_total = len(trades)
        truncated = day_closed_trade_total > MAX_DAY_SCOPE_TRADES
        if truncated:
            trades = trades[:MAX_DAY_SCOPE_TRADES]
        payload["summary"] = {"date_utc": scope_date.isoformat(), **_summary_for_trades(trades)}
        if truncated:
            shown = len(trades)
            payload["summary"]["day_closed_trade_total"] = day_closed_trade_total
            payload["summary"]["day_trades_in_payload"] = shown
            payload["summary"]["day_scope_truncation"] = (
                f"showing {shown} of {day_closed_trade_total} trades (UTC day, chronological)"
            )
        payload["trades"] = [
            _trade_dict(trade, f"T{index}", market_depth="minimal")
            for index, trade in enumerate(trades, start=1)
        ]
        return payload

    trades = (
        _closed_trade_query(user_id, trade_account_id)
        .order_by(Trade.closed_at.desc(), Trade.id.desc())
        .limit(MAX_FREEFORM_TRADES)
        .all()
    )
    trades = list(reversed(trades))
    payload["summary"] = {"lookback": f"last_{MAX_FREEFORM_TRADES}_closed_trades", **_summary_for_trades(trades)}
    payload["trades"] = [
        _trade_dict(trade, f"T{index}", market_depth="none")
        for index, trade in enumerate(trades, start=1)
    ]
    return payload


def _format_value(value):
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None or value == "":
        return "-"
    return str(value)


def _trade_prompt_line(trade):
    ordered_keys = [
        "ref",
        "trade_pubkey",
        "symbol",
        "side",
        "pnl",
        "entry_price",
        "exit_price",
        "lot_size",
        "opened_at",
        "closed_at",
        "duration_minutes",
        "session",
        "strategy",
        "behavior_flags",
        "market_context",
    ]
    parts = [f"{key}={_format_value(trade.get(key))}" for key in ordered_keys if key in trade]
    return "; ".join(parts)


def format_journal_payload_for_prompt(payload) -> str:
    payload = payload if isinstance(payload, dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    trades = payload.get("trades") if isinstance(payload.get("trades"), list) else []
    scope_lines = [
        f"scope_type: {_format_value(payload.get('scope_type'))}",
        f"session_id: {_format_value(payload.get('session_id'))}",
        f"trade_account_id: {_format_value(payload.get('trade_account_id'))}",
        f"scope_trade_pubkey: {_format_value(payload.get('scope_trade_pubkey'))}",
        f"scope_date: {_format_value(payload.get('scope_date'))}",
    ]
    summary_lines = [f"{key}: {_format_value(value)}" for key, value in summary.items()]
    trade_lines = [_trade_prompt_line(trade) for trade in trades if isinstance(trade, dict)]
    return "\n".join(
        [
            "SCOPE",
            *scope_lines,
            "",
            "SUMMARY",
            *(summary_lines or ["-"]),
            "",
            "TRADES",
            *(trade_lines or ["-"]),
        ]
    )
