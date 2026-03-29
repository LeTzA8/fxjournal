import hashlib
import json
import logging
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func

from helpers.scoring import compute_emotional_index
from helpers.trade_analysis import (
    build_trade_annotations as _build_trade_annotations,
    coerce_float as _coerce_float,
    get_trade_identity as _get_trade_identity,
    get_trade_session as _get_trade_session,
)
from models import (
    AIGeneratedResponse,
    AIPromptHistory,
    Trade,
    User,
    UserProfile,
    WeeklyCheckin,
    db,
)
from trading import (
    build_trade_analytics,
    classify_trading_session,
    did_trade_reach_level,
    ensure_utc_aware,
    format_trade_symbol,
    get_trade_account_type,
    get_trade_level_validation_issues,
    is_extremely_long_duration_minutes,
    merge_bundled_trades,
    resolve_net_pnl,
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

_DASHBOARD_ADVICE_RULE_PREFIX_REPLACEMENTS = (
    ("\u2192 Rule:", "Rule:"),
    ("\u00e2\u2020\u2019 Rule:", "Rule:"),
    ("\u00e2\u2020' Rule:", "Rule:"),
    ("\u00c3\u00a2\u00e2\u20ac\u00a0\u00e2\u20ac\u2122 Rule:", "Rule:"),
)


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


def normalize_dashboard_advice_text(value):
    text = str(value or "").strip()
    if not text:
        return ""

    normalized = text
    for broken_prefix, replacement in _DASHBOARD_ADVICE_RULE_PREFIX_REPLACEMENTS:
        normalized = normalized.replace(broken_prefix, replacement)

    normalized = re.sub(r"(?im)^[ \t]*[-*]\s*Rule:\s*", "Rule: ", normalized)
    normalized = re.sub(r"(?im)^[ \t]*Rule:\s*", "Rule: ", normalized)
    return normalized.strip()


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


def _normalize_optional_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _get_record_value(record, field_name):
    if record is None:
        return None
    if isinstance(record, dict):
        return record.get(field_name)
    return getattr(record, field_name, None)


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


def get_current_market_week_period(now_utc=None):
    current_utc = now_utc or datetime.now(timezone.utc)
    if current_utc.tzinfo is None:
        current_utc = current_utc.replace(tzinfo=timezone.utc)
    else:
        current_utc = current_utc.astimezone(timezone.utc)

    market_now = current_utc.astimezone(WEEKLY_MARKET_TIMEZONE)
    period_start_market = (market_now - timedelta(days=market_now.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    period_end_market = period_start_market + timedelta(days=7)
    return {
        "period_start_utc": _to_utc_naive(period_start_market.astimezone(timezone.utc)),
        "period_end_utc": _to_utc_naive(period_end_market.astimezone(timezone.utc)),
        "market_cutoff_label": "Current market week",
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


def _serialize_user_profile(user_profile):
    return {
        "trading_style": _normalize_optional_text(_get_record_value(user_profile, "trading_style")),
        "instruments": _normalize_optional_text(_get_record_value(user_profile, "instruments")),
        "experience_level": _normalize_optional_text(_get_record_value(user_profile, "experience_level")),
    }


def _serialize_weekly_checkin(weekly_checkin):
    return {
        "emotional_state": _normalize_optional_text(_get_record_value(weekly_checkin, "emotional_state")),
        "plan_adherence": _normalize_optional_text(_get_record_value(weekly_checkin, "plan_adherence")),
        "execution_quality": _normalize_optional_text(_get_record_value(weekly_checkin, "execution_quality")),
        "additional_context": _normalize_optional_text(_get_record_value(weekly_checkin, "additional_context")),
    }


def _get_user_profile_for_prompt(user_id):
    if not user_id:
        return None
    return UserProfile.query.filter_by(user_id=user_id).first()


def _get_weekly_checkin_for_prompt(*, user_id, trade_account_id=None, period_start_utc=None):
    if not user_id:
        return None
    checkin_query = WeeklyCheckin.query.filter_by(user_id=user_id)
    if trade_account_id is not None:
        checkin_query = checkin_query.filter_by(trade_account_id=trade_account_id)
    if period_start_utc is not None:
        checkin_query = checkin_query.filter_by(week_start_utc=period_start_utc)
    return checkin_query.order_by(WeeklyCheckin.week_start_utc.desc(), WeeklyCheckin.id.desc()).first()


def build_profile_instructions(
    trading_style,
    experience_level,
    instruments,
    emotional_state,
    plan_adherence,
    execution_quality,
    *,
    emotional_index_label=None,
    emotional_index_mismatch=False,
):
    trading_style = _normalize_optional_text(trading_style)
    experience_level = _normalize_optional_text(experience_level)
    instruments = _normalize_optional_text(instruments)
    emotional_state = _normalize_optional_text(emotional_state)
    plan_adherence = _normalize_optional_text(plan_adherence)
    execution_quality = _normalize_optional_text(execution_quality)
    instructions = []

    if experience_level == "beginner":
        instructions.append("Use plain language. Explain any jargon.")
        instructions.append("Focus on maximum 2-3 observations only.")
        instructions.append("Rule must be simple and immediately actionable.")
        instructions.append("Never reference ICT, SMC, or orderflow terms.")
        instructions.append("Encouraging tone always, even on bad weeks.")
    elif experience_level == "intermediate":
        instructions.append("Assume basic trading knowledge, minimal explanations.")
        instructions.append("Balance foundational feedback with advanced patterns.")
        instructions.append("Reference RR and session patterns without over-explaining.")
    elif experience_level == "experienced":
        instructions.append("Assume full trading knowledge, no explanations needed.")
        instructions.append("Focus on subtle patterns, not obvious mistakes.")
        instructions.append("Reference expectancy and RR trends where relevant.")

    if trading_style == "scalper":
        instructions.append("Duration analysis in minutes not hours.")
        instructions.append("Flag any trade held over 30 mins as outside style.")
        instructions.append("Overtrading threshold is 5 or more trades per session.")
        instructions.append("Session timing is critical - flag off-session entries.")
    elif trading_style in ("swing", "position"):
        instructions.append("Cross-week trades are expected, never flag as unusual.")
        instructions.append("Holding time vs planned duration is primary focus.")
        instructions.append("Weekly PnL less relevant than per-trade RR.")
        instructions.append("Premature exit analysis weighted more heavily.")
        instructions.append("Acknowledge single week is thin sample for swing traders.")
    elif trading_style == "intraday":
        instructions.append("Same-session open and close is expected.")
        instructions.append("Treat overnight holds as style exceptions, not automatic mistakes.")
        instructions.append("Only flag overnight holding when it is repeated, clearly unplanned, or concentrated the week's risk.")

    if instruments == "indices":
        instructions.append("US market hours dominate - flag trades outside 13:30-20:00 UTC.")
        instructions.append("Session analysis scoped to US session primarily.")
    elif instruments == "forex":
        instructions.append("London/NY overlap is prime session - weight it accordingly.")
    elif instruments == "gold":
        instructions.append("Gold trades across all sessions - session analysis less critical.")
    elif instruments == "mixed":
        instructions.append("Scope session relevance per instrument, not globally.")

    if emotional_state == "stressed":
        instructions.append("Emotional week detected - prioritise BEHAVIOUR section.")
        instructions.append("Acknowledge emotional context in tone, not explicitly.")
    if plan_adherence == "impulsive":
        instructions.append("Flag impulsive trading in BEHAVIOUR section.")
        instructions.append("Reference plan adherence directly in the Rule.")
    if execution_quality == "poor":
        instructions.append("Cross-reference poor execution with actual trade data.")
        instructions.append("Find specific examples of poor execution in the trades.")
    if emotional_index_label in {"high", "very_high"}:
        instructions.append("Objective emotional index is elevated - prioritise BEHAVIOUR section.")
        instructions.append("Treat revenge sequences as the strongest behaviour signal, and use reactive/corrective signals as supporting context with less weight.")
        instructions.append("Describe the week as emotionally pressured or less composed only when the trade evidence supports it, and never mention internal scores or labels.")
    if emotional_index_label == "very_high":
        instructions.append("Lead with behavioural observations before performance metrics.")
    if emotional_index_mismatch:
        instructions.append("Self-report sounded calm or controlled, but observed behaviour signals were elevated - note that mismatch gently and ground it in trade evidence.")
    if emotional_index_label == "moderate" and emotional_state not in {"stressed"}:
        instructions.append("Mild behavioural signals detected - note briefly, don't over-weight.")
    if emotional_index_label == "low" and not emotional_index_mismatch:
        instructions.append("If the trade evidence looks orderly, explicitly acknowledge that emotions looked in check this week without mentioning any internal score.")

    if not instructions:
        return ""

    lines = "\n".join(f"- {instruction}" for instruction in instructions)
    return f"\nTRADER PROFILE ADJUSTMENTS\nApply all of the following:\n{lines}"


def _build_profile_adjustments_for_prompt(user_profile, weekly_checkin, emotional_index=None):
    return build_profile_instructions(
        _get_record_value(user_profile, "trading_style"),
        _get_record_value(user_profile, "experience_level"),
        _get_record_value(user_profile, "instruments"),
        _get_record_value(weekly_checkin, "emotional_state"),
        _get_record_value(weekly_checkin, "plan_adherence"),
        _get_record_value(weekly_checkin, "execution_quality"),
        emotional_index_label=_get_record_value(emotional_index, "label"),
        emotional_index_mismatch=bool(_get_record_value(emotional_index, "self_report_mismatch")),
    )


def _round_metric(value, digits=2):
    if value is None:
        return None
    return round(float(value), digits)


def _build_historical_context(
    *,
    user_id,
    trade_account_id=None,
    period_start_utc=None,
    period_end_utc=None,
    lookback_days=DEFAULT_HISTORICAL_CONTEXT_DAYS,
    closed_trades_only=False,
):
    historical_end_utc = period_start_utc or period_end_utc
    if historical_end_utc is None:
        return None

    historical_start_utc = historical_end_utc - timedelta(days=max(int(lookback_days), 1))
    historical_trades = _query_trades_for_payload(
        user_id=user_id,
        trade_account_id=trade_account_id,
        period_start_utc=historical_start_utc,
        period_end_utc=historical_end_utc,
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
        "comparison_scope": (
            "history_before_review_period_only"
            if period_start_utc is not None
            else "history_up_to_period_end"
        ),
        "window_start_utc": format_utc_timestamp(historical_start_utc),
        "window_end_utc": format_utc_timestamp(historical_end_utc),
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


def _format_bool(value):
    return "true" if bool(value) else "false"


def _format_optional_bool(value):
    if value is None:
        return "-"
    return _format_bool(value)


def _calculate_trade_rr(target_price, entry_price, stop_loss, side=None, *, signed=False):
    if target_price is None or entry_price is None or stop_loss is None:
        return None
    normalized_side = str(side or "BUY").strip().upper()
    if normalized_side == "SELL":
        if stop_loss <= entry_price:
            return None
        move_amount = entry_price - target_price
    else:
        if stop_loss >= entry_price:
            return None
        move_amount = target_price - entry_price
    risk_amount = abs(entry_price - stop_loss)
    if risk_amount == 0:
        return None
    if signed:
        return _round_metric(move_amount / risk_amount)
    if move_amount <= 0:
        return None
    return _round_metric(move_amount / risk_amount)


def _build_trade_exit_quality(trade, trade_pnl):
    result = {
        "planned_rr": None,
        "realized_rr": None,
        "tp_capture_pct": None,
        "closed_before_tp": None,
        "closed_before_sl": None,
    }
    instrument_type = get_trade_account_type(trade)
    validation_issues = get_trade_level_validation_issues(
        getattr(trade, "entry_price", None),
        getattr(trade, "stop_loss", None),
        getattr(trade, "take_profit", None),
        getattr(trade, "side", None),
        getattr(trade, "symbol", None),
        instrument_type=instrument_type,
        contract_code=getattr(trade, "contract_code", None),
    )
    stop_loss_is_valid = not (
        validation_issues["stop_loss_too_close"] or validation_issues["invalid_stop_loss_side"]
    )
    take_profit_is_valid = not (
        validation_issues["take_profit_too_close"] or validation_issues["invalid_take_profit_side"]
    )

    if stop_loss_is_valid and take_profit_is_valid:
        result["planned_rr"] = _calculate_trade_rr(
            getattr(trade, "take_profit", None),
            getattr(trade, "entry_price", None),
            getattr(trade, "stop_loss", None),
            getattr(trade, "side", None),
        )

    if stop_loss_is_valid:
        result["realized_rr"] = _calculate_trade_rr(
            getattr(trade, "exit_price", None),
            getattr(trade, "entry_price", None),
            getattr(trade, "stop_loss", None),
            getattr(trade, "side", None),
            signed=True,
        )

    if result["planned_rr"] is not None and result["realized_rr"] is not None and result["realized_rr"] > 0:
        result["tp_capture_pct"] = _round_metric((result["realized_rr"] / result["planned_rr"]) * 100.0)

    if (
        trade_pnl is not None
        and trade_pnl > 0
        and getattr(trade, "exit_price", None) is not None
        and getattr(trade, "take_profit", None) is not None
        and take_profit_is_valid
    ):
        result["closed_before_tp"] = not did_trade_reach_level(
            getattr(trade, "exit_price", None),
            getattr(trade, "take_profit", None),
            getattr(trade, "side", None),
            "tp",
            getattr(trade, "symbol", None),
            instrument_type=instrument_type,
            contract_code=getattr(trade, "contract_code", None),
        )
    elif (
        trade_pnl is not None
        and trade_pnl < 0
        and getattr(trade, "exit_price", None) is not None
        and getattr(trade, "stop_loss", None) is not None
        and stop_loss_is_valid
    ):
        result["closed_before_sl"] = not did_trade_reach_level(
            getattr(trade, "exit_price", None),
            getattr(trade, "stop_loss", None),
            getattr(trade, "side", None),
            "sl",
            getattr(trade, "symbol", None),
            instrument_type=instrument_type,
            contract_code=getattr(trade, "contract_code", None),
        )

    return result


def build_trade_payload(
    *,
    user_id,
    trade_account_id=None,
    max_trades=None,
    period_start_utc=None,
    period_end_utc=None,
    closed_trades_only=False,
    user_profile=None,
    weekly_checkin=None,
):
    raw_trades = _query_trades_for_payload(
        user_id=user_id,
        trade_account_id=trade_account_id,
        period_start_utc=period_start_utc,
        period_end_utc=period_end_utc,
        closed_trades_only=closed_trades_only,
    )
    trades = merge_bundled_trades(raw_trades)
    if max_trades is not None and max_trades > 0:
        trades = trades[:max_trades]

    analytics = build_trade_analytics(
        trades,
        display_timezone_name=get_ai_timezone_name(),
    )
    trade_annotations = _build_trade_annotations(trades)
    emotional_index = compute_emotional_index(trades=raw_trades, weekly_checkin=weekly_checkin)
    signals = (emotional_index or {}).get("signals", {})

    notes_with_content = 0
    lot_sizes = []
    durations = []
    closed_trade_count = 0
    total_absolute_trade_pnl = 0.0
    largest_trade_symbol = None
    largest_trade_abs_pnl = 0.0
    symbol_trade_counts = {}
    symbol_net_pnl = {}
    bundle_count = 0
    for trade in trades:
        note = (trade.trade_note or "").strip()
        if note:
            notes_with_content += 1
        if bool(getattr(trade, "_is_bundle", False)):
            bundle_count += 1
        trade_lot_size = _coerce_float(trade.lot_size)
        if trade_lot_size is not None:
            lot_sizes.append(trade_lot_size)
        duration_minutes = _get_trade_duration_minutes(trade)
        if duration_minutes is not None and not is_extremely_long_duration_minutes(duration_minutes):
            durations.append(duration_minutes)
        trade_pnl = resolve_net_pnl(trade)
        if getattr(trade, "closed_at", None) is None or trade_pnl is None:
            continue
        closed_trade_count += 1
        absolute_trade_pnl = abs(float(trade_pnl))
        total_absolute_trade_pnl += absolute_trade_pnl
        trade_symbol = format_trade_symbol(trade) or "-"
        symbol_trade_counts[trade_symbol] = symbol_trade_counts.get(trade_symbol, 0) + 1
        symbol_net_pnl[trade_symbol] = symbol_net_pnl.get(trade_symbol, 0.0) + float(trade_pnl)
        if absolute_trade_pnl > largest_trade_abs_pnl:
            largest_trade_abs_pnl = absolute_trade_pnl
            largest_trade_symbol = trade_symbol

    median_lot_size = statistics.median(lot_sizes) if lot_sizes else None
    median_duration_minutes = statistics.median(durations) if durations else None
    notes_coverage = round(notes_with_content / len(trades), 2) if trades else 0.0
    notes_missing = max(len(trades) - notes_with_content, 0)
    notes_confidence = _get_notes_confidence_label(notes_coverage, len(trades))
    first_trade_query = db.session.query(func.min(Trade.opened_at)).filter(Trade.user_id == user_id)
    if trade_account_id is not None:
        first_trade_query = first_trade_query.filter(Trade.trade_account_id == trade_account_id)
    first_trade_opened_at = first_trade_query.scalar()
    account_age_days = None
    if first_trade_opened_at is not None:
        account_age_days = max((utcnow_naive() - first_trade_opened_at).days, 0)

    top_symbol_by_trade_count = None
    top_symbol_trade_count = 0
    top_symbol_trade_share_pct = None
    if symbol_trade_counts and closed_trade_count > 0:
        top_symbol_by_trade_count, top_symbol_trade_count = max(
            symbol_trade_counts.items(),
            key=lambda item: (item[1], abs(symbol_net_pnl.get(item[0], 0.0)), item[0]),
        )
        top_symbol_trade_share_pct = _round_metric((top_symbol_trade_count / closed_trade_count) * 100.0)

    top_symbol_by_abs_pnl = None
    top_symbol_abs_pnl_share_pct = None
    total_absolute_symbol_pnl = sum(abs(value) for value in symbol_net_pnl.values())
    if symbol_net_pnl and total_absolute_symbol_pnl > 0:
        top_symbol_by_abs_pnl, dominant_symbol_pnl = max(
            symbol_net_pnl.items(),
            key=lambda item: (abs(item[1]), symbol_trade_counts.get(item[0], 0), item[0]),
        )
        top_symbol_abs_pnl_share_pct = _round_metric(
            (abs(dominant_symbol_pnl) / total_absolute_symbol_pnl) * 100.0
        )

    largest_trade_abs_pnl_share_pct = None
    if total_absolute_trade_pnl > 0 and largest_trade_abs_pnl > 0:
        largest_trade_abs_pnl_share_pct = _round_metric(
            (largest_trade_abs_pnl / total_absolute_trade_pnl) * 100.0
        )

    serialized_trades = []
    for trade in trades:
        identity = _get_trade_identity(trade)
        annotation = trade_annotations.get(identity, {})
        trade_pnl = resolve_net_pnl(trade)
        duration_minutes = _get_trade_duration_minutes(trade)
        trade_lot_size = _coerce_float(trade.lot_size)
        exit_quality = _build_trade_exit_quality(trade, trade_pnl)
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
                "pnl": trade_pnl,
                "entry_session": _classify_trade_session(trade.opened_at),
                "exit_session": _classify_trade_session(trade.closed_at),
                "session": _get_trade_session(trade),
                "duration_minutes": duration_minutes,
                "opened_at": format_utc_timestamp(trade.opened_at),
                "closed_at": format_utc_timestamp(trade.closed_at),
                "trade_sequence_number": annotation.get("trade_sequence_number"),
                "trade_number_in_session": annotation.get("trade_number_in_session"),
                "prev_trade_pnl": annotation.get("prev_trade_pnl"),
                "minutes_since_prev_close": annotation.get("minutes_since_prev_close"),
                "size_vs_prev_trade": annotation.get("size_vs_prev_trade"),
                "prev_symbol_trade_pnl": annotation.get("prev_symbol_trade_pnl"),
                "minutes_since_prev_symbol_close": annotation.get("minutes_since_prev_symbol_close"),
                "size_vs_prev_symbol_trade": annotation.get("size_vs_prev_symbol_trade"),
                "loss_streak_before_trade": annotation.get("loss_streak_before_trade"),
                "is_post_loss_trade": bool(annotation.get("is_post_loss_trade")),
                "same_symbol_reentry": bool(annotation.get("same_symbol_reentry")),
                "is_post_loss_same_symbol_trade": bool(annotation.get("is_post_loss_same_symbol_trade")),
                "same_trade_idea_reentry": bool(annotation.get("same_trade_idea_reentry")),
                "is_potential_revenge": bool(annotation.get("is_potential_revenge")),
                "is_potential_reactive": bool(annotation.get("is_potential_reactive")),
                "trade_note": (trade.trade_note or "").strip() or None,
                "is_revenge": bool(getattr(trade, "is_revenge", False)),
                "is_reactive": bool(getattr(trade, "is_reactive", False)),
                "is_corrective": bool(getattr(trade, "is_corrective", False)),
                "bundle_pubkey": (getattr(trade, "bundle_pubkey", None) or None),
                "is_bundle": bool(getattr(trade, "_is_bundle", False)),
                "bundle_trade_count": int(getattr(trade, "_bundle_trade_count", 1) or 1),
                "planned_rr": exit_quality["planned_rr"],
                "realized_rr": exit_quality["realized_rr"],
                "tp_capture_pct": exit_quality["tp_capture_pct"],
                "closed_before_tp": exit_quality["closed_before_tp"],
                "closed_before_sl": exit_quality["closed_before_sl"],
                "outlier_size": bool(
                    median_lot_size
                    and median_lot_size > 0
                    and trade_lot_size is not None
                    and trade_lot_size > median_lot_size * 3
                ),
                "possible_split_order": bool(annotation.get("possible_split_order")),
                "split_group_size": annotation.get("split_group_size", 1),
                "split_group_index": annotation.get("split_group_index", 1),
                "split_group_role": annotation.get("split_group_role") or "solo",
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
        "notes_coverage": notes_coverage,
        "notes_with_content": notes_with_content,
        "notes_missing": notes_missing,
        "notes_confidence": notes_confidence,
        "notes_basis": "Per weekly trade idea after bundle merging; counts non-empty user-authored trade_note text only.",
        "account_age_days": account_age_days,
        "user_profile": _serialize_user_profile(user_profile),
        "weekly_checkin": _serialize_weekly_checkin(weekly_checkin),
        "emotional_index": emotional_index,
        "historical_context": _build_historical_context(
            user_id=user_id,
            trade_account_id=trade_account_id,
            period_start_utc=period_start_utc,
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
            "closed_before_tp_count": analytics["summary"].get("closed_before_tp_count", 0),
            "closed_before_sl_count": analytics["summary"].get("closed_before_sl_count", 0),
            "top_symbol_by_trade_count": top_symbol_by_trade_count,
            "top_symbol_trade_count": top_symbol_trade_count,
            "top_symbol_trade_share_pct": top_symbol_trade_share_pct,
            "top_symbol_by_abs_pnl": top_symbol_by_abs_pnl,
            "top_symbol_abs_pnl_share_pct": top_symbol_abs_pnl_share_pct,
            "largest_trade_symbol": largest_trade_symbol,
            "largest_trade_abs_pnl_share_pct": largest_trade_abs_pnl_share_pct,
            "pair_sample_is_diverse": analytics["summary"].get("pair_sample_is_diverse", False),
            "equity_has_outlier_dominance": analytics["summary"].get("equity_has_outlier_dominance", False),
            "bundle_count": bundle_count,
            "confirmed_revenge_trade_count": signals.get("confirmed_revenge_trade_count", 0),
            "heuristic_revenge_trade_count": signals.get("heuristic_revenge_trade_count", 0),
            "confirmed_reactive_trade_count": signals.get("confirmed_reactive_trade_count", 0),
            "heuristic_reactive_trade_count": signals.get("heuristic_reactive_trade_count", 0),
            "reactive_trade_count": signals.get("reactive_trade_count", 0),
            "reactive_signal_trade_count": signals.get("reactive_signal_trade_count", 0),
            "confirmed_corrective_trade_count": signals.get("confirmed_corrective_trade_count", 0),
            "heuristic_corrective_trade_count": signals.get("heuristic_corrective_trade_count", 0),
            "corrective_trade_count": signals.get("corrective_trade_count", 0),
            "corrective_signal_trade_count": signals.get("corrective_signal_trade_count", 0),
            "revenge_trade_count": signals.get("revenge_trade_count", 0),
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


def _format_currency_magnitude(value):
    if value is None:
        return "-"
    return f"{abs(float(value)):.2f}"


def _format_percent(value):
    if value is None:
        return "-"
    return f"{float(value):.2f}%"


def _payload_section_has_values(section):
    return any(value is not None for value in (section or {}).values())


def _get_notes_confidence_label(notes_coverage, trade_count):
    if trade_count <= 0:
        return None
    if notes_coverage < 0.5:
        return "low"
    if notes_coverage < 0.8:
        return "medium"
    return "high"


def _format_notes_coverage(value, notes_with_content=None, notes_missing=None):
    ratio_text = _format_number(value)
    if notes_with_content is None or notes_missing is None:
        return ratio_text
    total = notes_with_content + notes_missing
    if total <= 0:
        return ratio_text
    ticket_label = "trade ticket" if total == 1 else "trade tickets"
    verb = "has" if notes_with_content == 1 else "have"
    return (
        f"{ratio_text} "
        f"({notes_with_content} of {total} weekly {ticket_label} {verb} non-empty notes)"
    )


def format_payload_for_prompt(payload):
    summary = payload.get("summary", {})
    user_profile = payload.get("user_profile") or {}
    weekly_checkin = payload.get("weekly_checkin") or {}
    emotional_index = payload.get("emotional_index") or {}
    historical_context = payload.get("historical_context") or {}
    trades = payload.get("trades", [])

    lines = [
        "CONTEXT",
        f"- generated_at: {payload.get('generated_at') or '-'}",
        f"- period_start_utc: {payload.get('period_start_utc') or '-'}",
        f"- period_end_utc: {payload.get('period_end_utc') or '-'}",
        "- payload_scope: one completed review period for one trade account",
        "- historical_scope: historical sections are prior account context, not this week's pair/session/weekday breakdown",
        f"- notes_coverage: {_format_notes_coverage(payload.get('notes_coverage'), payload.get('notes_with_content'), payload.get('notes_missing'))}",
        f"- notes_confidence: {payload.get('notes_confidence') or '-'}",
        f"- notes_basis: {payload.get('notes_basis') or '-'}",
        f"- account_age_days: {payload.get('account_age_days') if payload.get('account_age_days') is not None else '-'}",
    ]

    if _payload_section_has_values(user_profile):
        lines.extend(
            [
                "",
                "USER PROFILE",
                f"- trading_style: {user_profile.get('trading_style') or '-'}",
                f"- instruments: {user_profile.get('instruments') or '-'}",
                f"- experience_level: {user_profile.get('experience_level') or '-'}",
            ]
        )

    if _payload_section_has_values(weekly_checkin):
        lines.extend(
            [
                "",
                "WEEKLY CHECKIN",
                f"- emotional_state: {weekly_checkin.get('emotional_state') or '-'}",
                f"- plan_adherence: {weekly_checkin.get('plan_adherence') or '-'}",
                f"- execution_quality: {weekly_checkin.get('execution_quality') or '-'}",
                f"- additional_context: {weekly_checkin.get('additional_context') or '-'}",
            ]
        )

    if _payload_section_has_values(emotional_index):
        signals = emotional_index.get("signals") or {}
        components = emotional_index.get("components") or {}
        lines.extend(
            [
                "",
                "EMOTIONAL INDEX",
                "- score_range: 0.00 to 10.00 (higher = stronger objective behavioural pressure)",
                f"- score: {_format_number(emotional_index.get('score'))}",
                f"- label: {emotional_index.get('label') or '-'}",
                f"- self_report_mismatch: {_format_bool(emotional_index.get('self_report_mismatch'))}",
                f"- subjective_points: {_format_number(components.get('subjective_points'))}",
                f"- confirmed_revenge_points: {_format_number(components.get('confirmed_revenge_points'))}",
                f"- heuristic_revenge_points: {_format_number(components.get('heuristic_revenge_points'))}",
                f"- revenge_points: {_format_number(components.get('revenge_points'))}",
                f"- confirmed_reactive_points: {_format_number(components.get('confirmed_reactive_points'))}",
                f"- heuristic_reactive_points: {_format_number(components.get('heuristic_reactive_points'))}",
                f"- reactive_points: {_format_number(components.get('reactive_points'))}",
                f"- confirmed_corrective_points: {_format_number(components.get('confirmed_corrective_points'))}",
                f"- heuristic_corrective_points: {_format_number(components.get('heuristic_corrective_points'))}",
                f"- corrective_points: {_format_number(components.get('corrective_points'))}",
                f"- bundle_count: {signals.get('bundle_count', 0)}",
                f"- total_closed_trades: {signals.get('total_closed_trades', 0)}",
                f"- confirmed_behavior_trade_count: {signals.get('confirmed_behavior_trade_count', 0)}",
                f"- confirmed_revenge_trade_count: {signals.get('confirmed_revenge_trade_count', 0)}",
                f"- heuristic_revenge_trade_count: {signals.get('heuristic_revenge_trade_count', 0)}",
                f"- confirmed_reactive_trade_count: {signals.get('confirmed_reactive_trade_count', 0)}",
                f"- heuristic_reactive_trade_count: {signals.get('heuristic_reactive_trade_count', 0)}",
                f"- reactive_trade_count: {signals.get('reactive_trade_count', 0)}",
                f"- reactive_signal_trade_count: {signals.get('reactive_signal_trade_count', 0)}",
                f"- confirmed_corrective_trade_count: {signals.get('confirmed_corrective_trade_count', 0)}",
                f"- heuristic_corrective_trade_count: {signals.get('heuristic_corrective_trade_count', 0)}",
                f"- corrective_trade_count: {signals.get('corrective_trade_count', 0)}",
                f"- corrective_signal_trade_count: {signals.get('corrective_signal_trade_count', 0)}",
                f"- revenge_trade_count: {signals.get('revenge_trade_count', 0)}",
            ]
        )

    lines.extend(
        [
            "",
            "SUMMARY",
            f"- total_trades: {summary.get('total_trades', 0)}",
            f"- closed_trades: {summary.get('closed_trades', 0)}",
            f"- open_trades: {summary.get('open_trades', 0)}",
            f"- win_rate: {_format_percent(summary.get('win_rate'))}",
            f"- net_pnl: {_format_signed_currency(summary.get('net_pnl'))}",
            f"- weekly_pnl: {_format_signed_currency(summary.get('weekly_pnl'))}",
            f"- monthly_pnl: {_format_signed_currency(summary.get('monthly_pnl'))}",
            f"- closed_before_tp_count: {summary.get('closed_before_tp_count', 0)}",
            f"- closed_before_sl_count: {summary.get('closed_before_sl_count', 0)}",
            f"- top_symbol_by_trade_count: {summary.get('top_symbol_by_trade_count') or '-'}",
            f"- top_symbol_trade_share_pct: {_format_percent(summary.get('top_symbol_trade_share_pct'))}",
            f"- top_symbol_by_abs_pnl: {summary.get('top_symbol_by_abs_pnl') or '-'}",
            f"- top_symbol_abs_pnl_share_pct: {_format_percent(summary.get('top_symbol_abs_pnl_share_pct'))}",
            f"- largest_trade_symbol: {summary.get('largest_trade_symbol') or '-'}",
            f"- largest_trade_abs_pnl_share_pct: {_format_percent(summary.get('largest_trade_abs_pnl_share_pct'))}",
            f"- bundle_count: {summary.get('bundle_count', 0)}",
            f"- confirmed_revenge_trade_count: {summary.get('confirmed_revenge_trade_count', 0)}",
            f"- heuristic_revenge_trade_count: {summary.get('heuristic_revenge_trade_count', 0)}",
            f"- confirmed_reactive_trade_count: {summary.get('confirmed_reactive_trade_count', 0)}",
            f"- heuristic_reactive_trade_count: {summary.get('heuristic_reactive_trade_count', 0)}",
            f"- reactive_trade_count: {summary.get('reactive_trade_count', 0)}",
            f"- reactive_signal_trade_count: {summary.get('reactive_signal_trade_count', 0)}",
            f"- confirmed_corrective_trade_count: {summary.get('confirmed_corrective_trade_count', 0)}",
            f"- heuristic_corrective_trade_count: {summary.get('heuristic_corrective_trade_count', 0)}",
            f"- corrective_trade_count: {summary.get('corrective_trade_count', 0)}",
            f"- corrective_signal_trade_count: {summary.get('corrective_signal_trade_count', 0)}",
            f"- revenge_trade_count: {summary.get('revenge_trade_count', 0)}",
            f"- best_trade_pnl: {_format_signed_currency(summary.get('best_trade_pnl'))}",
            f"- worst_trade_pnl: {_format_signed_currency(summary.get('worst_trade_pnl'))}",
            f"- max_drawdown_amount: {_format_currency_magnitude(summary.get('max_drawdown'))}",
            "",
            "HISTORICAL_CONTEXT",
            f"- comparison_scope: {historical_context.get('comparison_scope') or '-'}",
            "- comparison_scope_note: history sections are comparison-only context and may exclude the current review period",
            f"- window_start_utc: {historical_context.get('window_start_utc') or '-'}",
            f"- window_end_utc: {historical_context.get('window_end_utc') or '-'}",
            f"- window_days: {historical_context.get('window_days') or '-'}",
            f"- historical_total_trades: {(historical_context.get('summary') or {}).get('total_trades', 0)}",
            f"- historical_closed_trades: {(historical_context.get('summary') or {}).get('closed_trades', 0)}",
            f"- historical_win_rate: {_format_percent((historical_context.get('summary') or {}).get('win_rate'))}",
            f"- historical_net_pnl: {_format_signed_currency((historical_context.get('summary') or {}).get('net_pnl'))}",
            f"- historical_max_drawdown_amount: {_format_currency_magnitude((historical_context.get('summary') or {}).get('max_drawdown'))}",
            "",
            "HISTORICAL_TOP_PAIRS",
        ]
    )

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
                f"   trade_sequence_number: {trade.get('trade_sequence_number') if trade.get('trade_sequence_number') is not None else '-'}",
                f"   trade_number_in_session: {trade.get('trade_number_in_session') if trade.get('trade_number_in_session') is not None else '-'}",
                f"   prev_trade_pnl: {_format_signed_currency(trade.get('prev_trade_pnl'))}",
                f"   minutes_since_prev_close: {_format_number(trade.get('minutes_since_prev_close'))}",
                f"   size_vs_prev_trade: {trade.get('size_vs_prev_trade') or '-'}",
                f"   prev_symbol_trade_pnl: {_format_signed_currency(trade.get('prev_symbol_trade_pnl'))}",
                f"   minutes_since_prev_symbol_close: {_format_number(trade.get('minutes_since_prev_symbol_close'))}",
                f"   size_vs_prev_symbol_trade: {trade.get('size_vs_prev_symbol_trade') or '-'}",
                f"   loss_streak_before_trade: {trade.get('loss_streak_before_trade') if trade.get('loss_streak_before_trade') is not None else '-'}",
                f"   is_post_loss_trade: {_format_bool(trade.get('is_post_loss_trade'))}",
                f"   same_symbol_reentry: {_format_bool(trade.get('same_symbol_reentry'))}",
                f"   is_post_loss_same_symbol_trade: {_format_bool(trade.get('is_post_loss_same_symbol_trade'))}",
                f"   same_trade_idea_reentry: {_format_bool(trade.get('same_trade_idea_reentry'))}",
                f"   is_potential_revenge: {_format_bool(trade.get('is_potential_revenge'))}",
                f"   is_potential_reactive: {_format_bool(trade.get('is_potential_reactive'))}",
                f"   is_revenge: {_format_bool(trade.get('is_revenge'))}",
                f"   is_reactive: {_format_bool(trade.get('is_reactive'))}",
                f"   is_corrective: {_format_bool(trade.get('is_corrective'))}",
                f"   is_bundle: {_format_bool(trade.get('is_bundle'))}",
                f"   bundle_trade_count: {trade.get('bundle_trade_count') if trade.get('bundle_trade_count') is not None else '-'}",
                f"   planned_rr: {_format_number(trade.get('planned_rr'))}",
                f"   realized_rr: {_format_number(trade.get('realized_rr'))}",
                f"   tp_capture_pct: {_format_percent(trade.get('tp_capture_pct'))}",
                f"   closed_before_tp: {_format_optional_bool(trade.get('closed_before_tp'))}",
                f"   closed_before_sl: {_format_optional_bool(trade.get('closed_before_sl'))}",
                f"   outlier_size: {_format_bool(trade.get('outlier_size'))}",
                f"   possible_split_order: {_format_bool(trade.get('possible_split_order'))}",
                f"   split_group_size: {trade.get('split_group_size') if trade.get('split_group_size') is not None else '-'}",
                f"   split_group_index: {trade.get('split_group_index') if trade.get('split_group_index') is not None else '-'}",
                f"   split_group_role: {trade.get('split_group_role') or '-'}",
                f"   is_likely_corrective: {_format_bool(trade.get('is_likely_corrective'))}",
                f"   trade_note: {note}",
            ]
        )
    return "\n".join(lines)


def build_dashboard_advice_messages(payload, prompt_filename=None, profile_adjustments=""):
    prompt_history = get_or_create_prompt_history(prompt_filename)
    payload_json = serialize_payload(payload)
    prompt_input = format_payload_for_prompt(payload)
    if profile_adjustments:
        prompt_input = f"{prompt_input}{profile_adjustments}"
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
        return normalize_dashboard_advice_text(output_text)

    text_chunks = []
    for item in response_payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                text_chunks.append(str(content["text"]).strip())
    return normalize_dashboard_advice_text("\n\n".join(chunk for chunk in text_chunks if chunk).strip())


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
        payload_json=payload_json,
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
        user_profile = _get_user_profile_for_prompt(user_id)
        weekly_checkin = _get_weekly_checkin_for_prompt(
            user_id=user_id,
            trade_account_id=trade_account_id,
        )
        payload = build_trade_payload(
            user_id=user_id,
            trade_account_id=trade_account_id,
            max_trades=max_trades,
            user_profile=user_profile,
            weekly_checkin=weekly_checkin,
        )
        profile_adjustments = _build_profile_adjustments_for_prompt(
            user_profile,
            weekly_checkin,
            payload.get("emotional_index"),
        )
        prompt_history, messages, payload_json = build_dashboard_advice_messages(
            payload,
            prompt_filename=prompt_filename,
            profile_adjustments=profile_adjustments,
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

        user_profile = _get_user_profile_for_prompt(user_id)
        weekly_checkin = _get_weekly_checkin_for_prompt(
            user_id=user_id,
            trade_account_id=trade_account_id,
            period_start_utc=period["period_start_utc"],
        )
        payload = build_trade_payload(
            user_id=user_id,
            trade_account_id=trade_account_id,
            max_trades=max_trades,
            period_start_utc=period["period_start_utc"],
            period_end_utc=period["period_end_utc"],
            closed_trades_only=True,
            user_profile=user_profile,
            weekly_checkin=weekly_checkin,
        )
        if not payload["trades"]:
            return {"record": None, "generated": False, "period": period, "skip_reason": "no_trades"}
        if payload["summary"].get("closed_trades", 0) < MIN_CLOSED_TRADES_FOR_ADVICE:
            return {"record": None, "generated": False, "period": period, "skip_reason": "too_few_trades"}

        payload_json = serialize_payload(payload)
        payload_hash = hash_text(payload_json)
        if existing is not None and not force_regenerate and existing.payload_hash == payload_hash:
            return {"record": existing, "generated": False, "period": period}

        profile_adjustments = _build_profile_adjustments_for_prompt(
            user_profile,
            weekly_checkin,
            payload.get("emotional_index"),
        )
        prompt_history, messages, payload_json = build_dashboard_advice_messages(
            payload,
            prompt_filename=prompt_filename,
            profile_adjustments=profile_adjustments,
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
