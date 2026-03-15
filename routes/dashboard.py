from datetime import datetime, timedelta

from flask import Blueprint, current_app, jsonify, render_template, session
from sqlalchemy.exc import OperationalError

from ai_service import (
    MIN_CLOSED_TRADES_FOR_ADVICE,
    get_latest_trade_week_period,
    get_latest_weekly_dashboard_advice,
    get_weekly_dashboard_period,
    should_generate_weekly_dashboard_advice,
)
from celery_workers.cache import (
    AI_STATUS_FAILED_TTL,
    AI_STATUS_QUEUED_TTL,
    ANALYTICS_TTL,
    CacheUnavailableError,
    claim_ai_status,
    get_ai_status,
    get_cached,
    set_ai_status,
    set_cached,
)
from helpers.core import get_active_trade_account_for_user, get_display_timezone_name
from helpers.utils import login_required, utcnow_naive
from models import Trade, db
from trading import (
    SMALL_SAMPLE_MIN_TRADES,
    build_rr_summary,
    build_trade_analytics,
    classify_trading_session,
    format_trade_symbol,
    resolve_net_pnl,
    to_display_timezone,
)

bp = Blueprint("dashboard", __name__)

DEFAULT_WEEKLY_AI_EMPTY_MESSAGE = (
    "Your weekly AI review will appear once this account has an eligible trade week. "
    "It uses the most recent completed week with trades on the active account."
)
WEEKLY_AI_GENERATING_MESSAGE = (
    "Your weekly AI review is being generated - check back in a moment."
)
WEEKLY_AI_NO_TRADES_MESSAGE = "No trades this week. Add closed trades to generate your AI review."
WEEKLY_AI_TOO_FEW_TRADES_MESSAGE = (
    "Not enough data for a meaningful review. Add at least 3 closed trades this week."
)
WEEKLY_AI_UNAVAILABLE_MESSAGE = (
    "Weekly AI review is temporarily unavailable. Please try again in a little while."
)
WEEKLY_AI_PROMPT_FILENAME = "dashboard_advice.txt"
DASHBOARD_CACHE_PREFIX = "dashboard_v2"
ANALYTICS_CACHE_PREFIX = "analytics_v2"
RR_SUMMARY_CACHE_PREFIX = "rr_summary_v4"


def _serialize_datetime(value):
    if value is None:
        return None
    return value.isoformat()


def _deserialize_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _load_user_trades(user_id, active_trade_account):
    if active_trade_account is None:
        return []
    try:
        return (
            Trade.query.filter_by(
                user_id=user_id,
                trade_account_id=active_trade_account.id,
            )
            .order_by(Trade.opened_at.desc())
            .all()
        )
    except OperationalError:
        db.session.rollback()
        return []


def _count_closed_trades_for_period(user_id, trade_account_id, period):
    if trade_account_id is None or period is None:
        return 0
    try:
        return (
            Trade.query.filter_by(
                user_id=user_id,
                trade_account_id=trade_account_id,
            )
            .filter(Trade.closed_at.isnot(None))
            .filter(Trade.closed_at >= period["period_start_utc"])
            .filter(Trade.closed_at < period["period_end_utc"])
            .count()
        )
    except OperationalError:
        db.session.rollback()
        return 0


def _serialize_dashboard_cache_payload(analytics):
    summary = analytics.get("summary") or {}
    return {
        "summary": {
            "win_rate": summary.get("win_rate"),
            "weekly_pnl": summary.get("weekly_pnl"),
            "net_pnl": summary.get("net_pnl"),
            "avg_win": summary.get("avg_win"),
            "avg_loss_abs": summary.get("avg_loss_abs"),
        },
        "session_stats": analytics.get("session_stats") or [],
        "chart_points": analytics.get("daily_equity_curve") or [],
        "closed_records": [
            {
                "opened_at_local": _serialize_datetime(record.get("opened_at_local")),
                "pnl": record.get("pnl"),
            }
            for record in (analytics.get("closed_records") or [])
        ],
    }


def _deserialize_dashboard_cache_payload(payload):
    payload = payload or {}
    return {
        "summary": payload.get("summary") or {},
        "session_stats": payload.get("session_stats") or [],
        "chart_points": payload.get("chart_points") or [],
        "closed_records": [
            {
                "opened_at_local": _deserialize_datetime(record.get("opened_at_local")),
                "pnl": record.get("pnl"),
            }
            for record in (payload.get("closed_records") or [])
        ],
    }


def _serialize_summary_trade(record):
    if not record:
        return None
    return {
        "pnl": record.get("pnl"),
        "symbol": record.get("symbol"),
        "opened_label": record.get("opened_label"),
    }


def _serialize_analytics_payload(analytics):
    summary = dict(analytics.get("summary") or {})
    summary["best_trade"] = _serialize_summary_trade(summary.get("best_trade"))
    summary["worst_trade"] = _serialize_summary_trade(summary.get("worst_trade"))
    return {
        "summary": summary,
        "streaks": analytics.get("streaks") or {},
        "equity_curve": analytics.get("equity_curve") or [],
        "daily_equity_curve": analytics.get("daily_equity_curve") or [],
        "weekday_stats": analytics.get("weekday_stats") or [],
        "weekday_has_reliable_pattern": analytics.get("weekday_has_reliable_pattern", False),
        "pair_stats": analytics.get("pair_stats") or [],
        "pair_has_reliable_pattern": analytics.get("pair_has_reliable_pattern", False),
        "session_stats": analytics.get("session_stats") or [],
        "session_has_reliable_pattern": analytics.get("session_has_reliable_pattern", False),
        "week_label": analytics.get("week_label") or "",
    }


def _get_cached_payload(prefix, user_id, trade_account_id):
    try:
        return get_cached(prefix, user_id, trade_account_id=trade_account_id)
    except CacheUnavailableError as exc:
        current_app.logger.warning("%s cache unavailable: %s", prefix, exc)
        return None


def _set_cached_payload(prefix, user_id, trade_account_id, payload):
    try:
        set_cached(
            prefix,
            user_id,
            payload,
            ANALYTICS_TTL,
            trade_account_id=trade_account_id,
        )
    except CacheUnavailableError as exc:
        current_app.logger.warning("%s cache unavailable: %s", prefix, exc)


def _load_dashboard_analytics(user_id, active_trade_account, user_trades, timezone_name):
    account_id = getattr(active_trade_account, "id", None)
    cached_payload = _get_cached_payload(DASHBOARD_CACHE_PREFIX, user_id, account_id)
    if cached_payload is not None:
        return _deserialize_dashboard_cache_payload(cached_payload)

    analytics = build_trade_analytics(
        user_trades,
        display_timezone_name=timezone_name,
        account_size=active_trade_account.account_size if active_trade_account else None,
    )
    payload = _serialize_dashboard_cache_payload(analytics)
    _set_cached_payload(DASHBOARD_CACHE_PREFIX, user_id, account_id, payload)
    return {
        "summary": payload["summary"],
        "session_stats": payload["session_stats"],
        "chart_points": payload["chart_points"],
        "closed_records": [
            {
                "opened_at_local": record.get("opened_at_local"),
                "pnl": record.get("pnl"),
            }
            for record in (analytics.get("closed_records") or [])
        ],
    }


def _build_cached_analytics_payload(user_id, active_trade_account, user_trades, timezone_name):
    analytics = build_trade_analytics(
        user_trades,
        display_timezone_name=timezone_name,
        account_size=active_trade_account.account_size if active_trade_account else None,
    )
    payload = _serialize_analytics_payload(analytics)
    _set_cached_payload(ANALYTICS_CACHE_PREFIX, user_id, getattr(active_trade_account, "id", None), payload)
    return payload


def _build_cached_rr_summary(user_id, active_trade_account, user_trades):
    rr_summary = build_rr_summary(user_trades)
    _set_cached_payload(
        RR_SUMMARY_CACHE_PREFIX,
        user_id,
        getattr(active_trade_account, "id", None),
        rr_summary,
    )
    return rr_summary


def _summarize_week(records, start_local, end_local=None):
    weekly_records = []
    for record in records:
        opened_local = record.get("opened_at_local")
        if opened_local is None or opened_local < start_local:
            continue
        if end_local is not None and opened_local >= end_local:
            continue
        weekly_records.append(record)

    trade_count = len(weekly_records)
    wins = sum(1 for record in weekly_records if (record.get("pnl") or 0) > 0)
    net_pnl = sum((record.get("pnl") or 0) for record in weekly_records)
    return {
        "trade_count": trade_count,
        "wins": wins,
        "win_rate": (wins / trade_count * 100.0) if trade_count else None,
        "net_pnl": net_pnl,
        "week_sample_is_reliable": trade_count >= SMALL_SAMPLE_MIN_TRADES,
    }


def _build_week_on_week_insight(current_week_stats, previous_week_stats):
    if (
        current_week_stats["trade_count"]
        and previous_week_stats["trade_count"]
        and current_week_stats["week_sample_is_reliable"]
        and previous_week_stats["week_sample_is_reliable"]
    ):
        pnl_delta = current_week_stats["net_pnl"] - previous_week_stats["net_pnl"]
        win_rate_delta = (
            current_week_stats["win_rate"] - previous_week_stats["win_rate"]
            if current_week_stats["win_rate"] is not None
            and previous_week_stats["win_rate"] is not None
            else None
        )
        trade_delta = current_week_stats["trade_count"] - previous_week_stats["trade_count"]
        pnl_direction = "up" if pnl_delta > 0 else "down" if pnl_delta < 0 else "flat"
        win_rate_text = (
            f" Win rate {'improved' if win_rate_delta > 0 else 'fell' if win_rate_delta < 0 else 'held flat'} by {abs(win_rate_delta):.1f} pts."
            if win_rate_delta is not None
            else ""
        )
        volume_text = (
            f" Trade count {'increased' if trade_delta > 0 else 'decreased' if trade_delta < 0 else 'matched last week'}"
            + (f" by {abs(trade_delta)}." if trade_delta != 0 else ".")
        )
        return (
            f"Net PnL is {pnl_direction} by ${abs(pnl_delta):.2f} versus last week."
            f"{win_rate_text}{volume_text}"
        )
    if current_week_stats["trade_count"] and previous_week_stats["trade_count"]:
        return (
            f"One or both weeks have fewer than {SMALL_SAMPLE_MIN_TRADES} trades, "
            "so treat this comparison as early context rather than a reliable pattern."
        )
    if current_week_stats["trade_count"]:
        if not current_week_stats["week_sample_is_reliable"]:
            return (
                f"This week has fewer than {SMALL_SAMPLE_MIN_TRADES} trades, "
                "so use it as early context rather than a firm pattern."
            )
        return "This is your first completed trade week with comparable dashboard data."
    return "No prior completed trade week to compare yet."


def _get_weekly_ai_state(user_id, active_trade_account, timezone_name):
    account_id = getattr(active_trade_account, "id", None)
    weekly_ai_review = None
    weekly_ai_generated_at_label = ""
    weekly_ai_period_label = ""
    weekly_ai_empty_message = DEFAULT_WEEKLY_AI_EMPTY_MESSAGE
    weekly_ai_is_generating = False

    latest_trade_period = get_latest_trade_week_period(
        user_id=user_id,
        trade_account_id=account_id,
    )
    weekly_period = latest_trade_period or get_weekly_dashboard_period()

    if latest_trade_period is None:
        weekly_ai_empty_message = WEEKLY_AI_NO_TRADES_MESSAGE
    else:
        weekly_ai_review = get_latest_weekly_dashboard_advice(
            user_id=user_id,
            trade_account_id=account_id,
            period_start_utc=latest_trade_period["period_start_utc"],
        )

        ai_status = None
        try:
            ai_status = get_ai_status(
                user_id,
                trade_account_id=account_id,
                period_start_utc=latest_trade_period["period_start_utc"],
            )
        except CacheUnavailableError as exc:
            current_app.logger.warning("Weekly AI status unavailable: %s", exc)

        if weekly_ai_review is None:
            if ai_status in {"queued", "running"}:
                weekly_ai_is_generating = True
                weekly_ai_empty_message = WEEKLY_AI_GENERATING_MESSAGE
            elif ai_status == "failed":
                weekly_ai_empty_message = WEEKLY_AI_UNAVAILABLE_MESSAGE
            elif not should_generate_weekly_dashboard_advice(
                user_id=user_id,
                trade_account_id=account_id,
                period_start_utc=latest_trade_period["period_start_utc"],
                period_end_utc=latest_trade_period["period_end_utc"],
            ):
                weekly_ai_empty_message = DEFAULT_WEEKLY_AI_EMPTY_MESSAGE
            else:
                closed_trade_count = _count_closed_trades_for_period(
                    user_id,
                    account_id,
                    latest_trade_period,
                )
                if closed_trade_count <= 0:
                    weekly_ai_empty_message = WEEKLY_AI_NO_TRADES_MESSAGE
                elif closed_trade_count < MIN_CLOSED_TRADES_FOR_ADVICE:
                    weekly_ai_empty_message = WEEKLY_AI_TOO_FEW_TRADES_MESSAGE
                else:
                    try:
                        claimed = claim_ai_status(
                            user_id,
                            trade_account_id=account_id,
                            period_start_utc=latest_trade_period["period_start_utc"],
                            status="queued",
                            ttl=AI_STATUS_QUEUED_TTL,
                        )
                    except CacheUnavailableError as exc:
                        current_app.logger.warning("Weekly AI queue unavailable: %s", exc)
                        claimed = False
                        weekly_ai_empty_message = WEEKLY_AI_UNAVAILABLE_MESSAGE

                    if claimed:
                        try:
                            from celery_workers.tasks import generate_weekly_ai_task

                            generate_weekly_ai_task.delay(
                                user_id,
                                account_id,
                                WEEKLY_AI_PROMPT_FILENAME,
                                _serialize_datetime(latest_trade_period["period_start_utc"]),
                                send_weekly_email=True,
                            )
                        except Exception as exc:
                            try:
                                set_ai_status(
                                    user_id,
                                    trade_account_id=account_id,
                                    period_start_utc=latest_trade_period["period_start_utc"],
                                    status="failed",
                                    ttl=AI_STATUS_FAILED_TTL,
                                )
                            except CacheUnavailableError:
                                pass
                            current_app.logger.warning("Weekly AI review dispatch failed: %s", exc)
                            weekly_ai_empty_message = WEEKLY_AI_UNAVAILABLE_MESSAGE
                        else:
                            weekly_ai_is_generating = True
                            weekly_ai_empty_message = WEEKLY_AI_GENERATING_MESSAGE
                    else:
                        try:
                            ai_status = get_ai_status(
                                user_id,
                                trade_account_id=account_id,
                                period_start_utc=latest_trade_period["period_start_utc"],
                            )
                        except CacheUnavailableError:
                            ai_status = None
                        if ai_status in {"queued", "running"}:
                            weekly_ai_is_generating = True
                            weekly_ai_empty_message = WEEKLY_AI_GENERATING_MESSAGE
                        elif ai_status == "failed":
                            weekly_ai_empty_message = WEEKLY_AI_UNAVAILABLE_MESSAGE

    if weekly_ai_review and weekly_ai_review.generated_at:
        generated_local = to_display_timezone(weekly_ai_review.generated_at, timezone_name)
        if generated_local is not None:
            weekly_ai_generated_at_label = generated_local.strftime("%d %b %Y %H:%M")

    period_end_local = to_display_timezone(
        weekly_period.get("period_end_utc") if weekly_period else None,
        timezone_name,
    )
    if period_end_local is not None:
        weekly_ai_period_label = (
            f"{period_end_local.strftime('%a %d %b %Y %H:%M')} {timezone_name}"
        )

    return {
        "weekly_ai_review": weekly_ai_review,
        "weekly_ai_generated_at_label": weekly_ai_generated_at_label,
        "weekly_ai_period_label": weekly_ai_period_label,
        "weekly_ai_empty_message": weekly_ai_empty_message,
        "weekly_ai_is_generating": weekly_ai_is_generating,
    }


@bp.route("/dashboard")
@login_required
def home():
    username = session.get("username", "User")
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    user_trades = _load_user_trades(user_id, active_trade_account)

    timezone_name = get_display_timezone_name()
    dashboard_analytics = _load_dashboard_analytics(
        user_id,
        active_trade_account,
        user_trades,
        timezone_name,
    )
    summary = dashboard_analytics["summary"]
    closed_records = dashboard_analytics["closed_records"]
    closed_trade_count = len(closed_records)
    session_stats = dashboard_analytics["session_stats"]
    chart_points = dashboard_analytics["chart_points"]

    now_local = to_display_timezone(utcnow_naive(), timezone_name)
    trades_this_month = sum(
        1
        for trade in user_trades
        if (opened_local := to_display_timezone(trade.opened_at, timezone_name))
        and opened_local.month == now_local.month
        and opened_local.year == now_local.year
    )

    recent_trades = []
    for trade in user_trades:
        pnl_value = resolve_net_pnl(trade)
        opened_local = to_display_timezone(trade.opened_at, timezone_name)
        trade_date = opened_local.strftime("%d %b %Y") if opened_local else "-"
        trade_date_value = opened_local.strftime("%Y-%m-%d") if opened_local else ""
        trade_profile = getattr(trade, "trade_profile", None)
        trade_profile_version = getattr(trade, "trade_profile_version", None)
        recent_trades.append(
            {
                "date": trade_date,
                "date_value": trade_date_value,
                "symbol": format_trade_symbol(trade),
                "trade_profile_label": (
                    trade_profile_version.name
                    if trade_profile_version is not None
                    else (trade_profile.name if trade_profile is not None else "-")
                ),
                "side": trade.side,
                "pnl": pnl_value,
                "session_label": classify_trading_session(trade.opened_at) if trade.opened_at else "-",
            }
        )

    current_week_start = now_local.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ) - timedelta(days=now_local.weekday())
    previous_week_start = current_week_start - timedelta(days=7)
    previous_week_end = current_week_start

    current_week_stats = _summarize_week(closed_records, current_week_start)
    previous_week_stats = _summarize_week(
        closed_records,
        previous_week_start,
        end_local=previous_week_end,
    )
    week_on_week_insight = _build_week_on_week_insight(
        current_week_stats,
        previous_week_stats,
    )
    weekly_ai_state = _get_weekly_ai_state(user_id, active_trade_account, timezone_name)

    return render_template(
        "index.html",
        title="FX Journal",
        username=username,
        win_rate=summary.get("win_rate"),
        closed_trade_count=closed_trade_count,
        net_pnl_week=summary.get("weekly_pnl"),
        account_pnl_total=summary.get("net_pnl"),
        trades_this_month=trades_this_month,
        avg_win=summary.get("avg_win"),
        avg_loss_abs=summary.get("avg_loss_abs"),
        recent_trades=recent_trades,
        session_stats=session_stats[:4],
        current_week_stats=current_week_stats,
        previous_week_stats=previous_week_stats,
        week_on_week_insight=week_on_week_insight,
        chart_points=chart_points,
        weekly_ai_review=weekly_ai_state["weekly_ai_review"],
        weekly_ai_generated_at_label=weekly_ai_state["weekly_ai_generated_at_label"],
        weekly_ai_period_label=weekly_ai_state["weekly_ai_period_label"],
        weekly_ai_empty_message=weekly_ai_state["weekly_ai_empty_message"],
        weekly_ai_is_generating=weekly_ai_state["weekly_ai_is_generating"],
    )


@bp.route("/api/ai-status")
@login_required
def ai_status():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    account_id = getattr(active_trade_account, "id", None)
    weekly_period = get_latest_trade_week_period(
        user_id=user_id,
        trade_account_id=account_id,
    ) or get_weekly_dashboard_period()
    try:
        status = get_ai_status(
            user_id,
            trade_account_id=account_id,
            period_start_utc=weekly_period.get("period_start_utc") if weekly_period else None,
        )
    except CacheUnavailableError as exc:
        current_app.logger.warning("Weekly AI status poll unavailable: %s", exc)
        status = None
    return jsonify({"ready": status is None})


@bp.route("/dashboard/analytics")
@login_required
def analytics():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    account_id = getattr(active_trade_account, "id", None)
    timezone_name = get_display_timezone_name()

    analytics_payload = _get_cached_payload(ANALYTICS_CACHE_PREFIX, user_id, account_id)
    rr_summary = _get_cached_payload(RR_SUMMARY_CACHE_PREFIX, user_id, account_id)

    user_trades = None
    if analytics_payload is None or rr_summary is None:
        user_trades = _load_user_trades(user_id, active_trade_account)

    if analytics_payload is None:
        analytics_payload = _build_cached_analytics_payload(
            user_id,
            active_trade_account,
            user_trades,
            timezone_name,
        )
    else:
        analytics_payload.setdefault(
            "weekday_has_reliable_pattern",
            any((item.get("count") or 0) >= 5 for item in (analytics_payload.get("weekday_stats") or [])),
        )
        analytics_payload.setdefault(
            "pair_has_reliable_pattern",
            any((item.get("count") or 0) >= 5 for item in (analytics_payload.get("pair_stats") or [])),
        )
        analytics_payload.setdefault(
            "session_has_reliable_pattern",
            any((item.get("count") or 0) >= 5 for item in (analytics_payload.get("session_stats") or [])),
        )
        analytics_payload.setdefault(
            "summary",
            {},
        )
        analytics_payload["summary"].setdefault(
            "pair_sample_is_diverse",
            len(analytics_payload.get("pair_stats") or []) >= 2,
        )
        analytics_payload["summary"].setdefault(
            "equity_has_outlier_dominance",
            False,
        )
        analytics_payload["summary"].setdefault(
            "closed_before_tp_count",
            0,
        )
        analytics_payload["summary"].setdefault(
            "closed_before_sl_count",
            0,
        )

    if rr_summary is None:
        rr_summary = _build_cached_rr_summary(
            user_id,
            active_trade_account,
            user_trades,
        )

    return render_template(
        "analytics.html",
        title="Analytics | FX Journal",
        username=session.get("username", "User"),
        analytics=analytics_payload,
        analytics_timezone=timezone_name,
        active_trade_account=active_trade_account,
        rr_summary=rr_summary,
    )
