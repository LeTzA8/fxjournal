"""Structured context for the admin-only conversational journal surface."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import selectinload

from helpers.ai_market_context import build_trade_market_context
from helpers.trade_interpretation import interpretation_snapshot
from helpers.trade_state import trade_is_closed
from models import JournalSession, Trade, TradeBars, TradeInterpretation
from trading import classify_trading_session, merge_bundled_trades, resolve_net_pnl


MAX_FREEFORM_TRADES = 20
MAX_DAY_SCOPE_TRADES = 50
WEEKLY_MARKET_TIMEZONE = ZoneInfo("America/New_York")


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


def _trade_strategy(trade):
    profile = getattr(trade, "trade_profile", None)
    version = getattr(trade, "trade_profile_version", None)
    name = (
        str(getattr(version, "name", "") or "").strip()
        or str(getattr(profile, "name", "") or "").strip()
        or None
    )
    description = str(getattr(version, "short_description", "") or "").strip() or None
    version_number = getattr(version, "version_number", None)
    return {
        "name": name,
        "description": description,
        "version": int(version_number) if version_number is not None else None,
    }


def _trade_dict(trade, ref, *, market_depth="none"):
    is_bundle = bool(getattr(trade, "_is_bundle", False))
    market_context = None
    if market_depth == "deep":
        market_context = build_trade_market_context(trade, _load_m5_bars(trade))
    elif market_depth == "minimal":
        market_context = _minimal_market_context(trade)

    strategy = _trade_strategy(trade)
    payload = {
        "ref": ref,
        "trade_pubkey": None if is_bundle else getattr(trade, "pubkey", None),
        "bundle_pubkey": getattr(trade, "bundle_pubkey", None) if is_bundle else None,
        "bundle_member_pubkeys": list(getattr(trade, "_bundle_member_pubkeys", []) or []),
        "is_bundle": is_bundle,
        "bundle_trade_count": int(getattr(trade, "_bundle_trade_count", 1) or 1),
        "symbol": getattr(trade, "symbol", None),
        "side": getattr(trade, "side", None),
        "entry_price": _safe_float(getattr(trade, "entry_price", None)),
        "exit_price": _safe_float(getattr(trade, "exit_price", None)),
        "lot_size": _safe_float(getattr(trade, "lot_size", None)),
        "pnl": _safe_float(resolve_net_pnl(trade)),
        "stop_loss": _safe_float(getattr(trade, "stop_loss", None)),
        "take_profit": _safe_float(getattr(trade, "take_profit", None)),
        "opened_at": _iso(getattr(trade, "opened_at", None)),
        "closed_at": _iso(getattr(trade, "closed_at", None)),
        "duration_minutes": _duration_minutes(trade),
        "session": classify_trading_session(getattr(trade, "opened_at", None)),
        "strategy": strategy["name"],
        "strategy_version": strategy["version"],
        "strategy_description": strategy["description"],
        "behavior_flags": interpretation_snapshot(trade),
    }
    if market_depth == "deep":
        payload["market_context"] = market_context
    elif market_depth == "minimal":
        payload["market_context"] = market_context
    return payload


def _summary_for_trades(trades):
    trades = merge_bundled_trades(trades)
    trade_count = len(trades)
    net_pnl = sum(_safe_float(resolve_net_pnl(trade)) or 0.0 for trade in trades)
    wins = sum(1 for trade in trades if (_safe_float(resolve_net_pnl(trade)) or 0.0) > 0)
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


def _bundle_members_for_trade(trade, user_id, trade_account_id):
    bundle_pubkey = str(getattr(trade, "bundle_pubkey", "") or "").strip()
    if not bundle_pubkey:
        return [trade]
    bundle_query = (
        Trade.query.filter(Trade.user_id == user_id)
        .join(TradeInterpretation, TradeInterpretation.trade_id == Trade.id)
        .filter(TradeInterpretation.bundle_pubkey == bundle_pubkey)
        .options(
            selectinload(Trade.interpretation),
            selectinload(Trade.trade_profile),
            selectinload(Trade.trade_profile_version),
            selectinload(Trade.trade_account),
        )
    )
    if trade_account_id is not None:
        bundle_query = bundle_query.filter(Trade.trade_account_id == trade_account_id)
    bundle_members = bundle_query.order_by(Trade.opened_at.asc(), Trade.id.asc()).all()
    return bundle_members or [trade]


def _expand_trades_with_bundle_members(raw_trades, user_id, trade_account_id):
    raw_trades = list(raw_trades or [])
    bundle_keys = sorted(
        {
            str(getattr(trade, "bundle_pubkey", "") or "").strip()
            for trade in raw_trades
            if str(getattr(trade, "bundle_pubkey", "") or "").strip()
        }
    )
    if not bundle_keys:
        return raw_trades

    by_id = {
        getattr(trade, "id", None): trade
        for trade in raw_trades
        if getattr(trade, "id", None) is not None
    }
    bundle_query = (
        Trade.query.filter(Trade.user_id == user_id)
        .join(TradeInterpretation, TradeInterpretation.trade_id == Trade.id)
        .filter(TradeInterpretation.bundle_pubkey.in_(bundle_keys))
        .options(
            selectinload(Trade.interpretation),
            selectinload(Trade.trade_profile),
            selectinload(Trade.trade_profile_version),
            selectinload(Trade.trade_account),
        )
    )
    if trade_account_id is not None:
        bundle_query = bundle_query.filter(Trade.trade_account_id == trade_account_id)
    bundle_members = bundle_query.all()
    for trade in bundle_members:
        trade_id = getattr(trade, "id", None)
        if trade_id is not None:
            by_id[trade_id] = trade
    return list(by_id.values())


def _closed_trade_ideas_in_period(user_id, trade_account_id, period_start_utc, period_end_utc):
    raw_trades = (
        _closed_trade_query(user_id, trade_account_id)
        .filter(Trade.closed_at >= period_start_utc, Trade.closed_at < period_end_utc)
        .order_by(Trade.closed_at.asc(), Trade.id.asc())
        .all()
    )
    expanded_trades = _expand_trades_with_bundle_members(raw_trades, user_id, trade_account_id)
    trade_ideas = merge_bundled_trades(expanded_trades)
    filtered = []
    for trade in trade_ideas:
        closed_at = getattr(trade, "closed_at", None)
        if closed_at is None:
            continue
        if closed_at < period_start_utc or closed_at >= period_end_utc:
            continue
        filtered.append(trade)
    filtered.sort(
        key=lambda trade: (
            getattr(trade, "closed_at", None) or datetime.min,
            getattr(trade, "id", 0) or 0,
        )
    )
    return filtered


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
        focal_trades = merge_bundled_trades(
            _bundle_members_for_trade(focal_trade, user_id, trade_account_id)
        )
        focal_trade_idea = focal_trades[0] if focal_trades else focal_trade
        focal_ref = "B1" if bool(getattr(focal_trade_idea, "_is_bundle", False)) else "T1"
        payload["summary"] = {
            "focal_ref": focal_ref,
            "focal_symbol": focal_trade_idea.symbol,
            "trade_count": 1,
        }
        payload["trades"] = [_trade_dict(focal_trade_idea, focal_ref, market_depth="deep")]
        return payload

    if scope_type == JournalSession.SCOPE_DAY:
        scope_date = getattr(session, "scope_date", None)
        if scope_date is None:
            payload["summary"] = {"error": "scope_date_required"}
            return payload
        day_start = datetime.combine(scope_date, time.min)
        day_end = day_start + timedelta(days=1)
        trades = _closed_trade_ideas_in_period(user_id, trade_account_id, day_start, day_end)
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
                f"showing {shown} of {day_closed_trade_total} trade ideas (UTC day, chronological)"
            )
        payload["trades"] = [
            _trade_dict(
                trade,
                f"B{index}" if bool(getattr(trade, "_is_bundle", False)) else f"T{index}",
                market_depth="minimal",
            )
            for index, trade in enumerate(trades, start=1)
        ]
        return payload

    if scope_type == JournalSession.SCOPE_WEEK:
        scope_date = getattr(session, "scope_date", None)
        if scope_date is None:
            payload["summary"] = {"error": "scope_date_required"}
            return payload
        local_start = datetime.combine(scope_date, time.min, tzinfo=WEEKLY_MARKET_TIMEZONE)
        local_end = local_start + timedelta(days=7)
        period_start_utc = local_start.astimezone(timezone.utc).replace(tzinfo=None)
        period_end_utc = local_end.astimezone(timezone.utc).replace(tzinfo=None)
        trades = _closed_trade_ideas_in_period(user_id, trade_account_id, period_start_utc, period_end_utc)
        week_closed_trade_total = len(trades)
        truncated = week_closed_trade_total > MAX_DAY_SCOPE_TRADES
        if truncated:
            trades = trades[:MAX_DAY_SCOPE_TRADES]
        payload["summary"] = {
            "market_week_start": scope_date.isoformat(),
            "market_week_timezone": str(WEEKLY_MARKET_TIMEZONE),
            "period_start_utc": period_start_utc.isoformat(),
            "period_end_utc": period_end_utc.isoformat(),
            **_summary_for_trades(trades),
        }
        if truncated:
            shown = len(trades)
            payload["summary"]["week_closed_trade_total"] = week_closed_trade_total
            payload["summary"]["week_trades_in_payload"] = shown
            payload["summary"]["week_scope_truncation"] = (
                f"showing {shown} of {week_closed_trade_total} trade ideas (New York market week, chronological)"
            )
        payload["trades"] = [
            _trade_dict(
                trade,
                f"B{index}" if bool(getattr(trade, "_is_bundle", False)) else f"T{index}",
                market_depth="minimal",
            )
            for index, trade in enumerate(trades, start=1)
        ]
        return payload

    raw_trades = (
        _closed_trade_query(user_id, trade_account_id)
        .order_by(Trade.closed_at.desc(), Trade.id.desc())
        .all()
    )
    trades = list(reversed(merge_bundled_trades(raw_trades)[:MAX_FREEFORM_TRADES]))
    payload["summary"] = {
        "lookback": f"last_{MAX_FREEFORM_TRADES}_closed_trade_ideas",
        **_summary_for_trades(trades),
    }
    payload["trades"] = [
        _trade_dict(
            trade,
            f"B{index}" if bool(getattr(trade, "_is_bundle", False)) else f"T{index}",
            market_depth="none",
        )
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
        "bundle_pubkey",
        "is_bundle",
        "bundle_trade_count",
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
        "strategy_version",
        "strategy_description",
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
