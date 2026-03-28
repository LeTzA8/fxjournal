from datetime import timedelta
import hashlib

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from ai_service import get_weekly_dashboard_period
from celery_workers.cache import CacheUnavailableError, invalidate
from helpers.core import (
    build_unique_trade_pubkey,
    get_active_trade_account_for_user,
    is_weekly_checkin_complete,
)
from helpers.trade_analysis import detect_outliers
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


def _load_closed_trades_for_period(*, user_id, trade_account_id, period):
    return (
        Trade.query.filter_by(
            user_id=user_id,
            trade_account_id=trade_account_id,
        )
        .filter(Trade.closed_at.isnot(None))
        .filter(Trade.closed_at >= period["period_start_utc"])
        .filter(Trade.closed_at < period["period_end_utc"])
        .order_by(Trade.closed_at.desc(), Trade.id.desc())
        .all()
    )


def _pubkey_group_hash(pubkeys):
    normalized_pubkeys = sorted(
        {
            str(pubkey or "").strip()
            for pubkey in pubkeys
            if str(pubkey or "").strip()
        }
    )
    if not normalized_pubkeys:
        return ""
    return hashlib.md5(",".join(normalized_pubkeys).encode("utf-8")).hexdigest()[:8]


def _build_bundle_group_value(pubkeys):
    normalized_pubkeys = sorted(
        {
            str(pubkey or "").strip()
            for pubkey in pubkeys
            if str(pubkey or "").strip()
        }
    )
    return ",".join(normalized_pubkeys)


def _build_outlier_selection_state(outliers, form_data):
    form_data = form_data or {}
    selected_bundle_groups = {
        value
        for value in form_data.get("bundle_groups", [])
        if value
    }
    selected_bundle_types = {
        key: value
        for key, value in (form_data.get("bundle_types", {}) or {}).items()
        if key
    }
    selected_reactive_pubkeys = {
        value
        for value in form_data.get("confirm_reactive", [])
        if value
    }
    selected_corrective_pubkeys = {
        value
        for value in form_data.get("confirm_corrective", [])
        if value
    }

    bundle_rows = []
    for candidate in (outliers or {}).get("bundle_candidates", []):
        trades = candidate.get("trades") or []
        trade_pubkeys = [
            str(getattr(trade, "pubkey", "") or "").strip()
            for trade in trades
            if str(getattr(trade, "pubkey", "") or "").strip()
        ]
        group_value = _build_bundle_group_value(trade_pubkeys)
        group_hash = _pubkey_group_hash(trade_pubkeys)
        detected_type = candidate.get("sub_type") or "neutral"
        bundle_rows.append(
            {
                **candidate,
                "group_value": group_value,
                "group_hash": group_hash,
                "selected": group_value in selected_bundle_groups,
                "selected_type": selected_bundle_types.get(group_hash, detected_type),
            }
        )

    standalone_rows = []
    for candidate in (outliers or {}).get("standalone_candidates", []):
        trade = candidate.get("trade")
        trade_pubkey = str(getattr(trade, "pubkey", "") or "").strip()
        sub_type = candidate.get("sub_type")
        standalone_rows.append(
            {
                **candidate,
                "trade_pubkey": trade_pubkey,
                "selected": (
                    trade_pubkey in selected_reactive_pubkeys
                    if sub_type == "reactive"
                    else trade_pubkey in selected_corrective_pubkeys
                )
                if form_data
                else sub_type in {"reactive", "corrective"},
            }
        )

    return {
        "bundle_candidates": bundle_rows,
        "standalone_candidates": standalone_rows,
    }


def _render_checkin_page(
    *,
    error=None,
    form_data=None,
    active_trade_account=None,
    period=None,
    outliers=None,
):
    form_data = form_data or {}
    period = period or get_weekly_dashboard_period()
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
        outliers=_build_outlier_selection_state(outliers, form_data) if outliers else None,
    )


@bp.route("/checkin", methods=["GET", "POST"])
@login_required
def checkin():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    if active_trade_account is None:
        return redirect(url_for("dashboard.home"))

    period = get_weekly_dashboard_period(now_utc=utcnow_naive())
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
    week_trades = _load_closed_trades_for_period(
        user_id=user_id,
        trade_account_id=active_trade_account.id,
        period=period,
    )
    outliers = detect_outliers(week_trades)
    has_outliers = bool(outliers["bundle_candidates"] or outliers["standalone_candidates"])
    if closed_trade_count <= 0 or (
        is_weekly_checkin_complete(existing_checkin) and request.method == "GET"
    ):
        return redirect(url_for("dashboard.home"))

    if request.method == "POST":
        emotional_state = (request.form.get("emotional_state") or "").strip().lower()
        plan_adherence = (request.form.get("plan_adherence") or "").strip().lower()
        execution_quality = (request.form.get("execution_quality") or "").strip().lower()
        additional_context = (request.form.get("additional_context") or "").strip()
        selected_bundle_groups = [
            value.strip()
            for value in request.form.getlist("bundle_group")
            if value and value.strip()
        ]
        selected_bundle_types = {}
        for candidate in outliers["bundle_candidates"]:
            trade_pubkeys = [
                str(getattr(trade, "pubkey", "") or "").strip()
                for trade in (candidate.get("trades") or [])
                if str(getattr(trade, "pubkey", "") or "").strip()
            ]
            group_hash = _pubkey_group_hash(trade_pubkeys)
            selected_bundle_types[group_hash] = (
                request.form.get(f"bundle_type_{group_hash}", candidate.get("sub_type") or "neutral")
                .strip()
                .lower()
            )
        selected_reactive_pubkeys = [
            value.strip()
            for value in request.form.getlist("confirm_reactive")
            if value and value.strip()
        ]
        selected_corrective_pubkeys = [
            value.strip()
            for value in request.form.getlist("confirm_corrective")
            if value and value.strip()
        ]
        form_data = {
            "emotional_state": emotional_state,
            "plan_adherence": plan_adherence,
            "execution_quality": execution_quality,
            "additional_context": additional_context,
            "bundle_groups": selected_bundle_groups,
            "bundle_types": selected_bundle_types,
            "confirm_reactive": selected_reactive_pubkeys,
            "confirm_corrective": selected_corrective_pubkeys,
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
                outliers=outliers if has_outliers else None,
            )

        for candidate in outliers["bundle_candidates"]:
            trades = candidate.get("trades") or []
            trade_pubkeys = [
                str(getattr(trade, "pubkey", "") or "").strip()
                for trade in trades
                if str(getattr(trade, "pubkey", "") or "").strip()
            ]
            group_value = _build_bundle_group_value(trade_pubkeys)
            if group_value not in selected_bundle_groups:
                continue
            selected_trades = (
                Trade.query.filter(
                    Trade.user_id == user_id,
                    Trade.trade_account_id == active_trade_account.id,
                    Trade.pubkey.in_(trade_pubkeys),
                    Trade.closed_at >= period["period_start_utc"],
                    Trade.closed_at < period["period_end_utc"],
                )
                .all()
            )
            if len(selected_trades) < 2:
                continue
            bundle_pubkey = build_unique_trade_pubkey()
            group_hash = _pubkey_group_hash(trade_pubkeys)
            bundle_type = selected_bundle_types.get(group_hash, "neutral")
            for trade in selected_trades:
                if not trade.bundle_pubkey:
                    trade.bundle_pubkey = bundle_pubkey
                if bundle_type == "reactive" and not trade.is_reactive:
                    trade.is_reactive = True
                elif bundle_type == "corrective" and not trade.is_corrective:
                    trade.is_corrective = True

        for trade_pubkey in selected_reactive_pubkeys:
            trade = (
                Trade.query.filter(
                    Trade.user_id == user_id,
                    Trade.trade_account_id == active_trade_account.id,
                    Trade.pubkey == trade_pubkey,
                    Trade.closed_at >= period["period_start_utc"],
                    Trade.closed_at < period["period_end_utc"],
                )
                .first()
            )
            if trade is not None and not trade.is_reactive:
                trade.is_reactive = True

        for trade_pubkey in selected_corrective_pubkeys:
            trade = (
                Trade.query.filter(
                    Trade.user_id == user_id,
                    Trade.trade_account_id == active_trade_account.id,
                    Trade.pubkey == trade_pubkey,
                    Trade.closed_at >= period["period_start_utc"],
                    Trade.closed_at < period["period_end_utc"],
                )
                .first()
            )
            if trade is not None and not trade.is_corrective:
                trade.is_corrective = True

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
        try:
            invalidate(user_id=user_id, trade_account_id=active_trade_account.id)
        except CacheUnavailableError:
            pass
        flash("Weekly check-in saved.", "success")
        return redirect(url_for("dashboard.home"))

    return _render_checkin_page(
        active_trade_account=active_trade_account,
        period=period,
        outliers=outliers if has_outliers else None,
    )


@bp.route("/checkin/skip", methods=["POST"])
@login_required
def skip_checkin():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    if active_trade_account is None:
        return redirect(url_for("dashboard.home"))

    period = get_weekly_dashboard_period(now_utc=utcnow_naive())
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
