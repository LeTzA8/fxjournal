from datetime import timedelta

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from ai_service import get_current_market_week_period
from helpers.core import get_active_trade_account_for_user
from helpers.utils import login_required, utcnow_naive
from models import Trade, WeeklyCheckin, db

bp = Blueprint("checkin", __name__)

EMOTIONAL_STATE_OPTIONS = (
    {
        "value": "calm",
        "label": "Calm and focused",
    },
    {
        "value": "slightly_off",
        "label": "Slightly off",
    },
    {
        "value": "stressed",
        "label": "Stressed or frustrated",
    },
)
PLAN_ADHERENCE_OPTIONS = (
    {
        "value": "consistent",
        "label": "Followed it consistently",
    },
    {
        "value": "some_deviations",
        "label": "Some deviations",
    },
    {
        "value": "impulsive",
        "label": "Traded mostly on impulse",
    },
)
EXECUTION_QUALITY_OPTIONS = (
    {
        "value": "sharp",
        "label": "Sharp",
    },
    {
        "value": "average",
        "label": "Average",
    },
    {
        "value": "poor",
        "label": "Poor",
    },
)
VALID_EMOTIONAL_STATES = {option["value"] for option in EMOTIONAL_STATE_OPTIONS}
VALID_PLAN_ADHERENCE = {option["value"] for option in PLAN_ADHERENCE_OPTIONS}
VALID_EXECUTION_QUALITY = {option["value"] for option in EXECUTION_QUALITY_OPTIONS}


def _count_closed_trades_for_period(*, user_id, trade_account_id, period):
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


def _get_current_weekly_checkin(*, user_id, trade_account_id, period):
    return WeeklyCheckin.query.filter_by(
        user_id=user_id,
        trade_account_id=trade_account_id,
        week_start_utc=period["period_start_utc"],
    ).first()


def _render_checkin_page(*, error=None, form_data=None, active_trade_account=None, period=None):
    form_data = form_data or {}
    period = period or get_current_market_week_period()
    week_start_utc = period.get("period_start_utc")
    week_end_utc = period.get("period_end_utc")
    week_label = ""
    if week_start_utc is not None and week_end_utc is not None:
        week_label = (
            f"{week_start_utc.strftime('%d %b %Y')} to "
            f"{(week_end_utc - timedelta(days=1)).strftime('%d %b %Y')}"
        )
    return render_template(
        "checkin.html",
        title="Weekly Check-In | FX Journal",
        body_class="auth-layout",
        username=session.get("username", "User"),
        error=error,
        active_trade_account=active_trade_account,
        week_label=week_label,
        emotional_state_options=EMOTIONAL_STATE_OPTIONS,
        plan_adherence_options=PLAN_ADHERENCE_OPTIONS,
        execution_quality_options=EXECUTION_QUALITY_OPTIONS,
        emotional_state_value=form_data.get("emotional_state", ""),
        plan_adherence_value=form_data.get("plan_adherence", ""),
        execution_quality_value=form_data.get("execution_quality", ""),
        additional_context_value=form_data.get("additional_context", ""),
    )


@bp.route("/checkin", methods=["GET", "POST"])
@login_required
def checkin():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    if active_trade_account is None:
        return redirect(url_for("dashboard.home"))

    period = get_current_market_week_period(now_utc=utcnow_naive())
    closed_trade_count = _count_closed_trades_for_period(
        user_id=user_id,
        trade_account_id=active_trade_account.id,
        period=period,
    )
    existing_checkin = _get_current_weekly_checkin(
        user_id=user_id,
        trade_account_id=active_trade_account.id,
        period=period,
    )
    if closed_trade_count <= 0 or (existing_checkin is not None and request.method == "GET"):
        return redirect(url_for("dashboard.home"))

    if request.method == "POST":
        emotional_state = (request.form.get("emotional_state") or "").strip().lower()
        plan_adherence = (request.form.get("plan_adherence") or "").strip().lower()
        execution_quality = (request.form.get("execution_quality") or "").strip().lower()
        additional_context = (request.form.get("additional_context") or "").strip()
        form_data = {
            "emotional_state": emotional_state,
            "plan_adherence": plan_adherence,
            "execution_quality": execution_quality,
            "additional_context": additional_context,
        }
        if (
            emotional_state not in VALID_EMOTIONAL_STATES
            or plan_adherence not in VALID_PLAN_ADHERENCE
            or execution_quality not in VALID_EXECUTION_QUALITY
        ):
            return _render_checkin_page(
                error="Please answer the three multiple-choice questions before saving.",
                form_data=form_data,
                active_trade_account=active_trade_account,
                period=period,
            )

        weekly_checkin = existing_checkin or WeeklyCheckin(
            user_id=user_id,
            trade_account_id=active_trade_account.id,
            week_start_utc=period["period_start_utc"],
            week_end_utc=period["period_end_utc"],
        )
        if existing_checkin is None:
            db.session.add(weekly_checkin)
        weekly_checkin.emotional_state = emotional_state
        weekly_checkin.plan_adherence = plan_adherence
        weekly_checkin.execution_quality = execution_quality
        weekly_checkin.additional_context = additional_context or None
        db.session.commit()
        flash("Weekly check-in saved.", "success")
        return redirect(url_for("dashboard.home"))

    return _render_checkin_page(
        active_trade_account=active_trade_account,
        period=period,
    )


@bp.route("/checkin/skip", methods=["POST"])
@login_required
def skip_checkin():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    if active_trade_account is None:
        return redirect(url_for("dashboard.home"))

    period = get_current_market_week_period(now_utc=utcnow_naive())
    closed_trade_count = _count_closed_trades_for_period(
        user_id=user_id,
        trade_account_id=active_trade_account.id,
        period=period,
    )
    if closed_trade_count <= 0:
        return redirect(url_for("dashboard.home"))

    weekly_checkin = _get_current_weekly_checkin(
        user_id=user_id,
        trade_account_id=active_trade_account.id,
        period=period,
    )
    if weekly_checkin is None:
        weekly_checkin = WeeklyCheckin(
            user_id=user_id,
            trade_account_id=active_trade_account.id,
            week_start_utc=period["period_start_utc"],
            week_end_utc=period["period_end_utc"],
        )
        db.session.add(weekly_checkin)
        db.session.commit()

    flash("Weekly check-in skipped for now.", "info")
    return redirect(url_for("dashboard.home"))
