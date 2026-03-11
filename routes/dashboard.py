from datetime import timedelta

from flask import Blueprint, current_app, render_template, session
from sqlalchemy.exc import OperationalError

from models import Trade, db
from trading import (
    build_trade_analytics,
    classify_trading_session,
    format_trade_symbol,
    resolve_pnl,
    to_display_timezone,
)
from ai_service import (
    AIConfigError,
    AIRequestError,
    get_latest_trade_week_period,
    get_weekly_dashboard_period,
    maybe_generate_weekly_dashboard_advice,
)
from helpers import get_active_trade_account_for_user, get_display_timezone_name
from utils import login_required, utcnow_naive

bp = Blueprint("dashboard", __name__)


@bp.route("/dashboard")
@login_required
def home():
    username = session.get("username", "User")
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)

    try:
        user_trades = (
            Trade.query.filter_by(
                user_id=user_id,
                trade_account_id=active_trade_account.id,
            )
            .order_by(Trade.opened_at.desc())
            .all()
        )
    except OperationalError:
        db.session.rollback()
        user_trades = []

    timezone_name = get_display_timezone_name()
    analytics = build_trade_analytics(
        user_trades,
        display_timezone_name=timezone_name,
        account_size=active_trade_account.account_size if active_trade_account else None,
    )
    summary = analytics["summary"]
    closed_records = analytics["closed_records"]
    session_stats = analytics["session_stats"]
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
        pnl_value = resolve_pnl(trade)
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

    chart_points = analytics["daily_equity_curve"]
    current_week_start = now_local.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ) - timedelta(days=now_local.weekday())
    previous_week_start = current_week_start - timedelta(days=7)
    previous_week_end = current_week_start

    def summarize_week(records, start_local, end_local=None):
        weekly_records = []
        for record in records:
            opened_local = record.get("opened_at_local")
            if opened_local is None or opened_local < start_local:
                continue
            if end_local is not None and opened_local >= end_local:
                continue
            weekly_records.append(record)

        trade_count = len(weekly_records)
        wins = sum(1 for record in weekly_records if record["pnl"] > 0)
        net_pnl = sum(record["pnl"] for record in weekly_records)
        return {
            "trade_count": trade_count,
            "wins": wins,
            "win_rate": (wins / trade_count * 100.0) if trade_count else None,
            "net_pnl": net_pnl,
        }

    current_week_stats = summarize_week(closed_records, current_week_start)
    previous_week_stats = summarize_week(
        closed_records,
        previous_week_start,
        end_local=previous_week_end,
    )
    week_on_week_insight = "No prior completed trade week to compare yet."
    if current_week_stats["trade_count"] and previous_week_stats["trade_count"]:
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
        week_on_week_insight = (
            f"Net PnL is {pnl_direction} by ${abs(pnl_delta):.2f} versus last week."
            f"{win_rate_text}{volume_text}"
        )
    elif current_week_stats["trade_count"]:
        week_on_week_insight = "This is your first completed trade week with comparable dashboard data."
    weekly_ai_review = None
    weekly_ai_generated_at_label = ""
    weekly_ai_period_label = ""
    weekly_ai_empty_message = (
        "Your weekly AI review will appear once this account has an eligible trade week. It uses the most recent completed week with trades on the active account."
    )

    try:
        weekly_ai_result = maybe_generate_weekly_dashboard_advice(
            user_id=user_id,
            trade_account_id=active_trade_account.id if active_trade_account else None,
            prompt_filename="dashboard_advice.txt",
        )
        weekly_ai_review = weekly_ai_result.get("record")
        weekly_period = weekly_ai_result.get("period") or get_latest_trade_week_period(
            user_id=user_id,
            trade_account_id=active_trade_account.id if active_trade_account else None,
        )
    except (AIConfigError, AIRequestError, OSError, ValueError) as exc:
        db.session.rollback()
        current_app.logger.warning("Weekly AI review unavailable: %s", exc)
        weekly_period = get_latest_trade_week_period(
            user_id=user_id,
            trade_account_id=active_trade_account.id if active_trade_account else None,
        ) or get_weekly_dashboard_period()
        weekly_ai_empty_message = "Weekly AI review is temporarily unavailable. Please try again in a little while."

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

    return render_template(
        "index.html",
        title="FX Journal",
        username=username,
        win_rate=summary["win_rate"],
        net_pnl_week=summary["weekly_pnl"],
        account_pnl_total=summary["net_pnl"],
        trades_this_month=trades_this_month,
        avg_win=summary["avg_win"],
        avg_loss_abs=summary["avg_loss_abs"],
        recent_trades=recent_trades,
        session_stats=session_stats[:4],
        current_week_stats=current_week_stats,
        previous_week_stats=previous_week_stats,
        week_on_week_insight=week_on_week_insight,
        chart_points=chart_points,
        weekly_ai_review=weekly_ai_review,
        weekly_ai_generated_at_label=weekly_ai_generated_at_label,
        weekly_ai_period_label=weekly_ai_period_label,
        weekly_ai_empty_message=weekly_ai_empty_message,
    )


@bp.route("/dashboard/analytics")
@login_required
def analytics():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    timezone_name = get_display_timezone_name()

    try:
        user_trades = (
            Trade.query.filter_by(
                user_id=user_id,
                trade_account_id=active_trade_account.id,
            )
            .order_by(Trade.opened_at.desc())
            .all()
        )
    except OperationalError:
        db.session.rollback()
        user_trades = []

    analytics_payload = build_trade_analytics(
        user_trades,
        display_timezone_name=timezone_name,
        account_size=active_trade_account.account_size if active_trade_account else None,
    )

    return render_template(
        "analytics.html",
        title="Analytics | FX Journal",
        username=session.get("username", "User"),
        analytics=analytics_payload,
        analytics_timezone=timezone_name,
        active_trade_account=active_trade_account,
    )
