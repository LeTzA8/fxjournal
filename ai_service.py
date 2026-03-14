import hashlib
import json
import logging
import os
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func

from models import AIGeneratedResponse, AIPromptHistory, Trade, User, db
from trading import (
    build_trade_analytics,
    classify_trading_session,
    ensure_utc_aware,
    format_trade_symbol,
    resolve_pnl,
)
from helpers.utils import utcnow_naive


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_PROMPT_FILE = "dashboard_advice.txt"
DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_MAX_OUTPUT_TOKENS = 1500
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_HISTORICAL_CONTEXT_DAYS = 90
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
WEEKLY_DASHBOARD_KIND = "weekly_dashboard_advice"
WEEKLY_MARKET_TIMEZONE = ZoneInfo("America/New_York")
WEEKLY_CUTOFF_WEEKDAY = 4
WEEKLY_CUTOFF_HOUR = 17
WEEKLY_CUTOFF_MINUTE = 30
WEEKLY_ACTIVITY_LOOKBACK_DAYS = 3
MIN_CLOSED_TRADES_FOR_ADVICE = 3
logger = logging.getLogger(__name__)


class AIConfigError(RuntimeError):
    pass


class AIRequestError(RuntimeError):
    pass


def get_ai_timezone_name():
    return os.getenv("APP_TIMEZONE", "Asia/Singapore").strip() or "Asia/Singapore"


def get_openai_api_key():
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise AIConfigError("OPENAI_API_KEY is not configured.")
    return api_key


def get_ai_model():
    return os.getenv("AI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def get_ai_timeout_seconds():
    raw_value = os.getenv("AI_REQUEST_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)).strip()
    try:
        timeout_seconds = int(raw_value)
    except ValueError:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    return max(timeout_seconds, 5)


def get_ai_max_output_tokens():
    raw_value = os.getenv("AI_MAX_OUTPUT_TOKENS", str(DEFAULT_MAX_OUTPUT_TOKENS)).strip()
    try:
        max_output_tokens = int(raw_value)
    except ValueError:
        max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS
    return max(max_output_tokens, 64)


def get_prompt_source_path(prompt_filename=None):
    filename = str(prompt_filename or DEFAULT_PROMPT_FILE).strip().replace("\\", "/")
    if not filename:
        raise AIConfigError("Prompt filename is required.")
    if not filename.endswith(".txt"):
        raise AIConfigError("Prompt filename must end with .txt.")
    path = (PROMPTS_DIR / filename).resolve()
    if PROMPTS_DIR.resolve() not in path.parents and path != PROMPTS_DIR.resolve():
        raise AIConfigError("Prompt path must stay within the prompts directory.")
    return path


def load_prompt_text(prompt_filename=None):
    path = get_prompt_source_path(prompt_filename)
    try:
        prompt_text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise AIConfigError(f"Prompt file not found: {path}") from exc
    if not prompt_text:
        raise AIConfigError(f"Prompt file is empty: {path}")
    return {
        "prompt_id": Path(path).stem,
        "prompt_text": prompt_text,
        "source_path": str(path.relative_to(Path(__file__).resolve().parent)),
    }


def hash_text(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def get_or_create_prompt_history(prompt_filename=None):
    prompt_data = load_prompt_text(prompt_filename)
    prompt_sha256 = hash_text(prompt_data["prompt_text"])
    prompt_history = AIPromptHistory.query.filter_by(prompt_sha256=prompt_sha256).first()
    if prompt_history:
        return prompt_history

    prompt_history = AIPromptHistory(
        prompt_id=prompt_data["prompt_id"],
        prompt_sha256=prompt_sha256,
        prompt_text=prompt_data["prompt_text"],
        source_path=prompt_data["source_path"],
    )
    db.session.add(prompt_history)
    db.session.flush()
    return prompt_history


def format_utc_timestamp(value):
    if value is None:
        return None
    if value.tzinfo is not None:
        normalized = value.astimezone(timezone.utc)
    else:
        normalized = value.replace(tzinfo=timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def _to_utc_naive(value):
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(tzinfo=timezone.utc)


def get_weekly_dashboard_period(now_utc=None):
    current_utc = now_utc or datetime.now(timezone.utc)
    if current_utc.tzinfo is None:
        current_utc = current_utc.replace(tzinfo=timezone.utc)
    else:
        current_utc = current_utc.astimezone(timezone.utc)

    market_now = current_utc.astimezone(WEEKLY_MARKET_TIMEZONE)
    current_week_start_market = (market_now - timedelta(days=market_now.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    current_cutoff_market = current_week_start_market + timedelta(
        days=WEEKLY_CUTOFF_WEEKDAY,
        hours=WEEKLY_CUTOFF_HOUR,
        minutes=WEEKLY_CUTOFF_MINUTE,
    )

    if market_now >= current_cutoff_market:
        period_start_market = current_week_start_market
        eligible_at_market = current_cutoff_market
    else:
        period_start_market = current_week_start_market - timedelta(days=7)
        eligible_at_market = period_start_market + timedelta(
            days=WEEKLY_CUTOFF_WEEKDAY,
            hours=WEEKLY_CUTOFF_HOUR,
            minutes=WEEKLY_CUTOFF_MINUTE,
        )

    period_end_market = period_start_market + timedelta(days=7)
    next_eligible_at_market = eligible_at_market + timedelta(days=7)

    return {
        "period_start_utc": _to_utc_naive(period_start_market.astimezone(timezone.utc)),
        "period_end_utc": _to_utc_naive(period_end_market.astimezone(timezone.utc)),
        "eligible_at_utc": _to_utc_naive(eligible_at_market.astimezone(timezone.utc)),
        "next_eligible_at_utc": _to_utc_naive(next_eligible_at_market.astimezone(timezone.utc)),
        "market_cutoff_label": "Friday 5:30 PM New York time",
    }


def get_latest_trade_week_period(*, user_id, trade_account_id=None, now_utc=None):
    current_period = get_weekly_dashboard_period(now_utc=now_utc)
    latest_trade_query = Trade.query.filter_by(user_id=user_id)
    if trade_account_id is not None:
        latest_trade_query = latest_trade_query.filter_by(trade_account_id=trade_account_id)
    latest_trade = (
        latest_trade_query
        .filter(Trade.closed_at.isnot(None))
        .filter(Trade.closed_at < current_period["period_end_utc"])
        .order_by(Trade.closed_at.desc(), Trade.id.desc())
        .first()
    )
    if latest_trade is None or latest_trade.closed_at is None:
        return None

    latest_trade_utc = latest_trade.closed_at
    if latest_trade_utc.tzinfo is None:
        latest_trade_utc = latest_trade_utc.replace(tzinfo=timezone.utc)
    else:
        latest_trade_utc = latest_trade_utc.astimezone(timezone.utc)
    latest_trade_market = latest_trade_utc.astimezone(WEEKLY_MARKET_TIMEZONE)
    period_start_market = (latest_trade_market - timedelta(days=latest_trade_market.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    period_end_market = period_start_market + timedelta(days=7)
    eligible_at_market = period_start_market + timedelta(
        days=WEEKLY_CUTOFF_WEEKDAY,
        hours=WEEKLY_CUTOFF_HOUR,
        minutes=WEEKLY_CUTOFF_MINUTE,
    )

    return {
        "period_start_utc": _to_utc_naive(period_start_market.astimezone(timezone.utc)),
        "period_end_utc": _to_utc_naive(period_end_market.astimezone(timezone.utc)),
        "eligible_at_utc": _to_utc_naive(eligible_at_market.astimezone(timezone.utc)),
        "next_eligible_at_utc": None,
        "market_cutoff_label": "Latest eligible trade week",
    }


def _query_trades_for_payload(
    *,
    user_id,
    trade_account_id=None,
    period_start_utc=None,
    period_end_utc=None,
    closed_trades_only=False,
):
    trade_query = Trade.query.filter_by(user_id=user_id)
    if trade_account_id is not None:
        trade_query = trade_query.filter_by(trade_account_id=trade_account_id)
    if closed_trades_only:
        trade_query = trade_query.filter(Trade.closed_at.isnot(None))
        period_filters = []
        if period_start_utc is not None:
            period_filters.append(Trade.closed_at >= period_start_utc)
        if period_end_utc is not None:
            period_filters.append(Trade.closed_at < period_end_utc)
        if period_filters:
            trade_query = trade_query.filter(and_(*period_filters))
        return trade_query.order_by(Trade.closed_at.desc(), Trade.id.desc()).all()

    if period_start_utc is not None:
        trade_query = trade_query.filter(Trade.opened_at >= period_start_utc)
    if period_end_utc is not None:
        trade_query = trade_query.filter(Trade.opened_at < period_end_utc)
    return trade_query.order_by(Trade.opened_at.desc(), Trade.id.desc()).all()


def _round_metric(value, digits=2):
    if value is None:
        return None
    return round(float(value), digits)


def _build_historical_context(
    *,
    user_id,
    trade_account_id=None,
    period_end_utc=None,
    lookback_days=DEFAULT_HISTORICAL_CONTEXT_DAYS,
    closed_trades_only=False,
):
    if period_end_utc is None:
        return None

    historical_start_utc = period_end_utc - timedelta(days=max(int(lookback_days), 1))
    historical_trades = _query_trades_for_payload(
        user_id=user_id,
        trade_account_id=trade_account_id,
        period_start_utc=historical_start_utc,
        period_end_utc=period_end_utc,
        closed_trades_only=closed_trades_only,
    )
    if not historical_trades:
        return None

    analytics = build_trade_analytics(
        historical_trades,
        display_timezone_name=get_ai_timezone_name(),
    )

    pair_stats = []
    for item in analytics.get("pair_stats", [])[:3]:
        pair_stats.append(
            {
                "symbol": item.get("symbol"),
                "count": item.get("count", 0),
                "win_rate": _round_metric(item.get("win_rate")),
                "net_pnl": _round_metric(item.get("net_pnl")),
            }
        )

    session_stats = []
    for item in analytics.get("session_stats", [])[:3]:
        session_stats.append(
            {
                "name": item.get("name"),
                "count": item.get("count", 0),
                "win_rate": _round_metric(item.get("win_rate")),
                "net_pnl": _round_metric(item.get("net_pnl")),
            }
        )

    weekday_stats = []
    for item in analytics.get("weekday_stats", []):
        if item.get("count", 0) <= 0:
            continue
        weekday_stats.append(
            {
                "name": item.get("name"),
                "count": item.get("count", 0),
                "win_rate": _round_metric(item.get("win_rate")),
                "net_pnl": _round_metric(item.get("net_pnl")),
            }
        )
    weekday_stats = sorted(
        weekday_stats,
        key=lambda item: (-item["count"], -(item["net_pnl"] or 0.0), item["name"]),
    )[:3]

    summary = analytics.get("summary", {})
    return {
        "window_start_utc": format_utc_timestamp(historical_start_utc),
        "window_end_utc": format_utc_timestamp(period_end_utc),
        "window_days": max(int(lookback_days), 1),
        "summary": {
            "total_trades": summary.get("total_trades", 0),
            "closed_trades": summary.get("closed_trades", 0),
            "win_rate": _round_metric(summary.get("win_rate")),
            "net_pnl": _round_metric(summary.get("net_pnl")),
            "max_drawdown": _round_metric(summary.get("max_drawdown")),
        },
        "top_pairs": pair_stats,
        "top_sessions": session_stats,
        "top_weekdays": weekday_stats,
    }


def _get_trade_duration_minutes(trade):
    if trade.opened_at is None or trade.closed_at is None:
        return None
    if trade.closed_at < trade.opened_at:
        return None
    return _round_metric((trade.closed_at - trade.opened_at).total_seconds() / 60.0)


def _classify_trade_session(timestamp):
    timestamp_utc = ensure_utc_aware(timestamp)
    if timestamp_utc is None:
        return None
    return classify_trading_session(timestamp_utc)


def _get_trade_session(trade):
    return _classify_trade_session(trade.opened_at)


def _floor_timestamp_to_five_minutes(timestamp):
    timestamp_utc = ensure_utc_aware(timestamp)
    if timestamp_utc is None:
        return None
    return timestamp_utc.replace(minute=(timestamp_utc.minute // 5) * 5, second=0, microsecond=0)


def _format_bool(value):
    return "true" if bool(value) else "false"


def build_trade_payload(
    *,
    user_id,
    trade_account_id=None,
    max_trades=None,
    period_start_utc=None,
    period_end_utc=None,
    closed_trades_only=False,
):
    trades = _query_trades_for_payload(
        user_id=user_id,
        trade_account_id=trade_account_id,
        period_start_utc=period_start_utc,
        period_end_utc=period_end_utc,
        closed_trades_only=closed_trades_only,
    )
    if max_trades is not None and max_trades > 0:
        trades = trades[:max_trades]

    analytics = build_trade_analytics(
        trades,
        display_timezone_name=get_ai_timezone_name(),
    )

    notes_with_content = 0
    lot_sizes = []
    split_order_groups = {}
    durations = []
    for trade in trades:
        note = (trade.trade_note or "").strip()
        if note:
            notes_with_content += 1
        if trade.lot_size not in {None, ""}:
            try:
                lot_sizes.append(float(trade.lot_size))
            except (TypeError, ValueError):
                pass
        duration_minutes = _get_trade_duration_minutes(trade)
        if duration_minutes is not None:
            durations.append(duration_minutes)
        split_bucket = _floor_timestamp_to_five_minutes(trade.opened_at)
        if split_bucket is None:
            continue
        group_key = (
            (format_trade_symbol(trade) or "").strip().upper(),
            (trade.side or "").strip().upper(),
            split_bucket,
        )
        split_order_groups[group_key] = split_order_groups.get(group_key, 0) + 1

    median_lot_size = statistics.median(lot_sizes) if lot_sizes else None
    median_duration_minutes = statistics.median(durations) if durations else None
    first_trade_query = db.session.query(func.min(Trade.opened_at)).filter(Trade.user_id == user_id)
    if trade_account_id is not None:
        first_trade_query = first_trade_query.filter(Trade.trade_account_id == trade_account_id)
    first_trade_opened_at = first_trade_query.scalar()
    account_age_days = None
    if first_trade_opened_at is not None:
        account_age_days = max((utcnow_naive() - first_trade_opened_at).days, 0)

    serialized_trades = []
    for trade in trades:
        duration_minutes = _get_trade_duration_minutes(trade)
        split_bucket = _floor_timestamp_to_five_minutes(trade.opened_at)
        group_key = (
            (format_trade_symbol(trade) or "").strip().upper(),
            (trade.side or "").strip().upper(),
            split_bucket,
        ) if split_bucket is not None else None
        trade_lot_size = None
        if trade.lot_size not in {None, ""}:
            try:
                trade_lot_size = float(trade.lot_size)
            except (TypeError, ValueError):
                trade_lot_size = None
        serialized_trades.append(
            {
                "symbol": format_trade_symbol(trade),
                "contract_code": (trade.contract_code or "").strip() or None,
                "side": trade.side,
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "stop_loss": trade.stop_loss,
                "take_profit": trade.take_profit,
                "lot_size": trade.lot_size,
                "pnl": resolve_pnl(trade),
                "entry_session": _classify_trade_session(trade.opened_at),
                "exit_session": _classify_trade_session(trade.closed_at),
                "session": _get_trade_session(trade),
                "duration_minutes": duration_minutes,
                "opened_at": format_utc_timestamp(trade.opened_at),
                "closed_at": format_utc_timestamp(trade.closed_at),
                "trade_note": (trade.trade_note or "").strip() or None,
                "outlier_size": bool(
                    median_lot_size
                    and median_lot_size > 0
                    and trade_lot_size is not None
                    and trade_lot_size > median_lot_size * 3
                ),
                "possible_split_order": bool(
                    group_key is not None and split_order_groups.get(group_key, 0) >= 2
                ),
                "is_likely_corrective": bool(
                    median_duration_minutes
                    and median_duration_minutes > 0
                    and duration_minutes is not None
                    and duration_minutes < median_duration_minutes * 0.2
                ),
            }
        )

    payload = {
        "generated_at": format_utc_timestamp(utcnow_naive()),
        "period_start_utc": format_utc_timestamp(period_start_utc),
        "period_end_utc": format_utc_timestamp(period_end_utc),
        "notes_coverage": round(notes_with_content / len(trades), 2) if trades else 0.0,
        "account_age_days": account_age_days,
        "historical_context": _build_historical_context(
            user_id=user_id,
            trade_account_id=trade_account_id,
            period_end_utc=period_end_utc,
            closed_trades_only=closed_trades_only,
        ),
        "summary": {
            "total_trades": analytics["summary"]["total_trades"],
            "closed_trades": analytics["summary"]["closed_trades"],
            "open_trades": analytics["summary"]["open_trades"],
            "win_rate": analytics["summary"]["win_rate"],
            "net_pnl": analytics["summary"]["net_pnl"],
            "weekly_pnl": analytics["summary"]["weekly_pnl"],
            "monthly_pnl": analytics["summary"]["monthly_pnl"],
            "best_trade_pnl": (
                analytics["summary"]["best_trade"]["pnl"]
                if analytics["summary"]["best_trade"]
                else None
            ),
            "worst_trade_pnl": (
                analytics["summary"]["worst_trade"]["pnl"]
                if analytics["summary"]["worst_trade"]
                else None
            ),
            "max_drawdown": analytics["summary"]["max_drawdown"],
        },
        "trades": serialized_trades,
    }
    return payload


def has_trade_data_for_period(*, user_id, trade_account_id=None, period_start_utc=None, period_end_utc=None):
    trade_query = db.session.query(Trade.id).filter_by(user_id=user_id)
    if trade_account_id is not None:
        trade_query = trade_query.filter_by(trade_account_id=trade_account_id)
    trade_query = trade_query.filter(Trade.closed_at.isnot(None))
    if period_start_utc is not None:
        trade_query = trade_query.filter(Trade.closed_at >= period_start_utc)
    if period_end_utc is not None:
        trade_query = trade_query.filter(Trade.closed_at < period_end_utc)
    return trade_query.first() is not None


def should_generate_weekly_dashboard_advice(
    *,
    user_id,
    trade_account_id=None,
    period_start_utc=None,
    period_end_utc=None,
    require_recent_login=False,
):
    if not user_id or trade_account_id is None:
        return False
    if require_recent_login:
        current_utc = _to_utc_naive(datetime.now(timezone.utc))
        active_cutoff = current_utc - timedelta(days=WEEKLY_ACTIVITY_LOOKBACK_DAYS)
        user_last_login_at = (
            db.session.query(User.last_login_at)
            .filter(User.id == user_id)
            .scalar()
        )
        if user_last_login_at is None or user_last_login_at < active_cutoff:
            return False
    return True


def serialize_payload(payload):
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _format_number(value, digits=2):
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"


def _format_signed_currency(value):
    if value is None:
        return "-"
    amount = float(value)
    return f"{amount:+.2f}"


def _format_percent(value):
    if value is None:
        return "-"
    return f"{float(value):.2f}%"


def format_payload_for_prompt(payload):
    summary = payload.get("summary", {})
    historical_context = payload.get("historical_context") or {}
    trades = payload.get("trades", [])

    lines = [
        "CONTEXT",
        f"- generated_at: {payload.get('generated_at') or '-'}",
        f"- period_start_utc: {payload.get('period_start_utc') or '-'}",
        f"- period_end_utc: {payload.get('period_end_utc') or '-'}",
        f"- notes_coverage: {_format_number(payload.get('notes_coverage'))}",
        f"- account_age_days: {payload.get('account_age_days') if payload.get('account_age_days') is not None else '-'}",
        "",
        "SUMMARY",
        f"- total_trades: {summary.get('total_trades', 0)}",
        f"- closed_trades: {summary.get('closed_trades', 0)}",
        f"- open_trades: {summary.get('open_trades', 0)}",
        f"- win_rate: {_format_percent(summary.get('win_rate'))}",
        f"- net_pnl: {_format_signed_currency(summary.get('net_pnl'))}",
        f"- weekly_pnl: {_format_signed_currency(summary.get('weekly_pnl'))}",
        f"- monthly_pnl: {_format_signed_currency(summary.get('monthly_pnl'))}",
        f"- best_trade_pnl: {_format_signed_currency(summary.get('best_trade_pnl'))}",
        f"- worst_trade_pnl: {_format_signed_currency(summary.get('worst_trade_pnl'))}",
        f"- max_drawdown: {_format_signed_currency(summary.get('max_drawdown'))}",
        "",
        "HISTORICAL_CONTEXT",
        f"- window_start_utc: {historical_context.get('window_start_utc') or '-'}",
        f"- window_end_utc: {historical_context.get('window_end_utc') or '-'}",
        f"- window_days: {historical_context.get('window_days') or '-'}",
        f"- historical_total_trades: {(historical_context.get('summary') or {}).get('total_trades', 0)}",
        f"- historical_closed_trades: {(historical_context.get('summary') or {}).get('closed_trades', 0)}",
        f"- historical_win_rate: {_format_percent((historical_context.get('summary') or {}).get('win_rate'))}",
        f"- historical_net_pnl: {_format_signed_currency((historical_context.get('summary') or {}).get('net_pnl'))}",
        f"- historical_max_drawdown: {_format_signed_currency((historical_context.get('summary') or {}).get('max_drawdown'))}",
        "",
        "HISTORICAL_TOP_PAIRS",
    ]

    historical_pairs = historical_context.get("top_pairs") or []
    if historical_pairs:
        for index, item in enumerate(historical_pairs, start=1):
            lines.append(
                f"- {index}. {(item.get('symbol') or '-')}: count={item.get('count', 0)}, "
                f"win_rate={_format_percent(item.get('win_rate'))}, net_pnl={_format_signed_currency(item.get('net_pnl'))}"
            )
    else:
        lines.append("- no pair history available")

    lines.extend(
        [
            "",
            "HISTORICAL_TOP_SESSIONS",
        ]
    )
    historical_sessions = historical_context.get("top_sessions") or []
    if historical_sessions:
        for index, item in enumerate(historical_sessions, start=1):
            lines.append(
                f"- {index}. {(item.get('name') or '-')}: count={item.get('count', 0)}, "
                f"win_rate={_format_percent(item.get('win_rate'))}, net_pnl={_format_signed_currency(item.get('net_pnl'))}"
            )
    else:
        lines.append("- no session history available")

    lines.extend(
        [
            "",
            "HISTORICAL_TOP_WEEKDAYS",
        ]
    )
    historical_weekdays = historical_context.get("top_weekdays") or []
    if historical_weekdays:
        for index, item in enumerate(historical_weekdays, start=1):
            lines.append(
                f"- {index}. {(item.get('name') or '-')}: count={item.get('count', 0)}, "
                f"win_rate={_format_percent(item.get('win_rate'))}, net_pnl={_format_signed_currency(item.get('net_pnl'))}"
            )
    else:
        lines.append("- no weekday history available")

    lines.extend(
        [
            "",
        f"TRADES ({len(trades)})",
        ]
    )

    if not trades:
        lines.append("- no trades available")
        return "\n".join(lines)

    for index, trade in enumerate(trades, start=1):
        note = trade.get("trade_note") or "-"
        lines.extend(
            [
                f"{index}. symbol: {trade.get('symbol') or '-'}",
                f"   contract_code: {trade.get('contract_code') or '-'}",
                f"   side: {trade.get('side') or '-'}",
                f"   entry_price: {_format_number(trade.get('entry_price'), digits=5)}",
                f"   exit_price: {_format_number(trade.get('exit_price'), digits=5)}",
                f"   stop_loss: {_format_number(trade.get('stop_loss'), digits=5)}",
                f"   take_profit: {_format_number(trade.get('take_profit'), digits=5)}",
                f"   lot_size: {_format_number(trade.get('lot_size'))}",
                f"   pnl: {_format_signed_currency(trade.get('pnl'))}",
                f"   entry_session: {trade.get('entry_session') or '-'}",
                f"   exit_session: {trade.get('exit_session') or '-'}",
                f"   session: {trade.get('session') or '-'}",
                f"   duration_minutes: {_format_number(trade.get('duration_minutes'))}",
                f"   opened_at: {trade.get('opened_at') or '-'}",
                f"   closed_at: {trade.get('closed_at') or '-'}",
                f"   outlier_size: {_format_bool(trade.get('outlier_size'))}",
                f"   possible_split_order: {_format_bool(trade.get('possible_split_order'))}",
                f"   is_likely_corrective: {_format_bool(trade.get('is_likely_corrective'))}",
                f"   trade_note: {note}",
            ]
        )
    return "\n".join(lines)


def build_dashboard_advice_messages(payload, prompt_filename=None):
    prompt_history = get_or_create_prompt_history(prompt_filename)
    payload_json = serialize_payload(payload)
    prompt_input = format_payload_for_prompt(payload)
    return prompt_history, [
        {
            "role": "system",
            "content": [
                {
                    "type": "input_text",
                    "text": prompt_history.prompt_text,
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": prompt_input,
                }
            ],
        },
    ], payload_json


def extract_response_text(response_payload):
    output_text = str(response_payload.get("output_text") or "").strip()
    if output_text:
        return output_text

    text_chunks = []
    for item in response_payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                text_chunks.append(str(content["text"]).strip())
    return "\n\n".join(chunk for chunk in text_chunks if chunk).strip()


def describe_empty_response(response_payload):
    status = str(response_payload.get("status") or "").strip() or "unknown"
    incomplete_details = response_payload.get("incomplete_details") or {}
    reason = str(incomplete_details.get("reason") or "").strip()
    if reason:
        return f"OpenAI response did not include any text output (status={status}, reason={reason})."
    return f"OpenAI response did not include any text output (status={status})."


def summarize_response_payload(response_payload):
    usage = response_payload.get("usage") or {}
    incomplete_details = response_payload.get("incomplete_details") or {}
    output_items = response_payload.get("output") or []
    content_types = []
    for item in output_items:
        for content in item.get("content", []):
            content_type = str(content.get("type") or "").strip()
            if content_type:
                content_types.append(content_type)
    summary = {
        "id": response_payload.get("id"),
        "model": response_payload.get("model"),
        "status": response_payload.get("status"),
        "incomplete_reason": incomplete_details.get("reason"),
        "output_item_count": len(output_items),
        "content_types": content_types[:10],
        "has_output_text": bool(str(response_payload.get("output_text") or "").strip()),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cached_input_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens"),
    }
    return summary


def request_openai_response(messages, *, model=None, timeout_seconds=None):
    resolved_model = model or get_ai_model()
    resolved_max_output_tokens = get_ai_max_output_tokens()
    resolved_timeout_seconds = timeout_seconds or get_ai_timeout_seconds()
    api_key = get_openai_api_key()
    request_body = {
        "model": resolved_model,
        "input": messages,
        "max_output_tokens": resolved_max_output_tokens,
        "reasoning": {"effort": "medium"},
        "text": {"verbosity": "low"},
    }
    encoded_body = json.dumps(request_body).encode("utf-8")
    request = Request(
        OPENAI_RESPONSES_URL,
        data=encoded_body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=resolved_timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
            usage = response_payload.get("usage") or {}
            logger.info(
                "OpenAI response received. model=%s max_output_tokens=%s timeout_seconds=%s input_tokens=%s output_tokens=%s cached_input_tokens=%s summary=%s",
                resolved_model,
                resolved_max_output_tokens,
                resolved_timeout_seconds,
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                (usage.get("input_tokens_details") or {}).get("cached_tokens"),
                summarize_response_payload(response_payload),
            )
            return response_payload
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise AIRequestError(f"OpenAI request failed with HTTP {exc.code}: {details}") from exc
    except URLError as exc:
        raise AIRequestError(f"OpenAI request failed: {exc.reason}") from exc


def save_ai_response(
    *,
    user_id,
    trade_account_id,
    prompt_history,
    model,
    response_text,
    payload_json,
    trade_count_used,
    source_last_trade_id,
    kind,
    period_start_utc=None,
    period_end_utc=None,
):
    ai_response = AIGeneratedResponse(
        user_id=user_id,
        trade_account_id=trade_account_id,
        prompt_history_id=prompt_history.id,
        kind=kind,
        model=model,
        response_text=response_text,
        payload_hash=hash_text(payload_json),
        trade_count_used=trade_count_used,
        source_last_trade_id=source_last_trade_id,
        period_start_utc=period_start_utc,
        period_end_utc=period_end_utc,
    )
    db.session.add(ai_response)
    db.session.flush()
    return ai_response


def generate_dashboard_advice(*, user_id, trade_account_id=None, prompt_filename=None, max_trades=None):
    try:
        payload = build_trade_payload(
            user_id=user_id,
            trade_account_id=trade_account_id,
            max_trades=max_trades,
        )
        prompt_history, messages, payload_json = build_dashboard_advice_messages(
            payload,
            prompt_filename=prompt_filename,
        )
        response_payload = request_openai_response(messages, model=get_ai_model())
        response_text = extract_response_text(response_payload)
        if not response_text:
            logger.warning(
                "AI dashboard advice returned no text. user_id=%s trade_account_id=%s kind=%s summary=%s",
                user_id,
                trade_account_id,
                prompt_history.prompt_id,
                summarize_response_payload(response_payload),
            )
            raise AIRequestError(describe_empty_response(response_payload))

        latest_trade = (
            Trade.query.filter_by(user_id=user_id, trade_account_id=trade_account_id)
            .order_by(Trade.id.desc())
            .first()
            if trade_account_id is not None
            else Trade.query.filter_by(user_id=user_id).order_by(Trade.id.desc()).first()
        )

        ai_response = save_ai_response(
            user_id=user_id,
            trade_account_id=trade_account_id,
            prompt_history=prompt_history,
            model=str(response_payload.get("model") or get_ai_model()),
            response_text=response_text,
            payload_json=payload_json,
            trade_count_used=len(payload["trades"]),
            source_last_trade_id=latest_trade.id if latest_trade else None,
            kind=prompt_history.prompt_id,
        )
        db.session.commit()
        return {
            "record": ai_response,
            "payload": payload,
            "response_payload": response_payload,
            "response_text": response_text,
        }
    except Exception:
        db.session.rollback()
        raise


def get_latest_ai_response(*, user_id, trade_account_id=None, kind=None):
    query = AIGeneratedResponse.query.filter_by(user_id=user_id)
    if trade_account_id is not None:
        query = query.filter_by(trade_account_id=trade_account_id)
    if kind:
        query = query.filter_by(kind=kind)
    return query.order_by(AIGeneratedResponse.generated_at.desc(), AIGeneratedResponse.id.desc()).first()


def get_latest_weekly_dashboard_advice(*, user_id, trade_account_id=None, period_start_utc=None):
    query = AIGeneratedResponse.query.filter_by(
        user_id=user_id,
        trade_account_id=trade_account_id,
        kind=WEEKLY_DASHBOARD_KIND,
    )
    if period_start_utc is not None:
        query = query.filter_by(period_start_utc=period_start_utc)
    return query.order_by(AIGeneratedResponse.generated_at.desc(), AIGeneratedResponse.id.desc()).first()


def maybe_generate_weekly_dashboard_advice(
    *,
    user_id,
    trade_account_id=None,
    prompt_filename=None,
    max_trades=None,
    now_utc=None,
    require_recent_login=False,
    force_regenerate=False,
):
    period = get_latest_trade_week_period(
        user_id=user_id,
        trade_account_id=trade_account_id,
        now_utc=now_utc,
    )
    if period is None:
        return {
            "record": None,
            "generated": False,
            "period": get_weekly_dashboard_period(now_utc=now_utc),
            "skip_reason": "no_trades",
        }
    try:
        existing = get_latest_weekly_dashboard_advice(
            user_id=user_id,
            trade_account_id=trade_account_id,
            period_start_utc=period["period_start_utc"],
        )
        if existing is not None and not force_regenerate:
            return {"record": existing, "generated": False, "period": period}

        if not should_generate_weekly_dashboard_advice(
            user_id=user_id,
            trade_account_id=trade_account_id,
            period_start_utc=period["period_start_utc"],
            period_end_utc=period["period_end_utc"],
            require_recent_login=require_recent_login,
        ):
            return {"record": None, "generated": False, "period": period}

        payload = build_trade_payload(
            user_id=user_id,
            trade_account_id=trade_account_id,
            max_trades=max_trades,
            period_start_utc=period["period_start_utc"],
            period_end_utc=period["period_end_utc"],
            closed_trades_only=True,
        )
        if not payload["trades"]:
            return {"record": None, "generated": False, "period": period, "skip_reason": "no_trades"}
        if payload["summary"].get("closed_trades", 0) < MIN_CLOSED_TRADES_FOR_ADVICE:
            return {"record": None, "generated": False, "period": period, "skip_reason": "too_few_trades"}

        payload_json = serialize_payload(payload)
        payload_hash = hash_text(payload_json)
        if existing is not None and not force_regenerate and existing.payload_hash == payload_hash:
            return {"record": existing, "generated": False, "period": period}

        prompt_history, messages, payload_json = build_dashboard_advice_messages(
            payload,
            prompt_filename=prompt_filename,
        )
        response_payload = request_openai_response(messages, model=get_ai_model())
        response_text = extract_response_text(response_payload)
        if not response_text:
            logger.warning(
                "Weekly AI dashboard advice returned no text. user_id=%s trade_account_id=%s kind=%s "
                "period_start_utc=%s period_end_utc=%s summary=%s",
                user_id,
                trade_account_id,
                WEEKLY_DASHBOARD_KIND,
                period["period_start_utc"],
                period["period_end_utc"],
                summarize_response_payload(response_payload),
            )
            raise AIRequestError(describe_empty_response(response_payload))

        latest_trade = (
            Trade.query.filter_by(user_id=user_id, trade_account_id=trade_account_id)
            .order_by(Trade.id.desc())
            .first()
            if trade_account_id is not None
            else Trade.query.filter_by(user_id=user_id).order_by(Trade.id.desc()).first()
        )

        ai_response = save_ai_response(
            user_id=user_id,
            trade_account_id=trade_account_id,
            prompt_history=prompt_history,
            model=str(response_payload.get("model") or get_ai_model()),
            response_text=response_text,
            payload_json=payload_json,
            trade_count_used=len(payload["trades"]),
            source_last_trade_id=latest_trade.id if latest_trade else None,
            kind=WEEKLY_DASHBOARD_KIND,
            period_start_utc=period["period_start_utc"],
            period_end_utc=period["period_end_utc"],
        )
        db.session.commit()
        return {
            "record": ai_response,
            "generated": True,
            "period": period,
            "payload": payload,
            "response_payload": response_payload,
            "response_text": response_text,
        }
    except Exception:
        db.session.rollback()
        raise
