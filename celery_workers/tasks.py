import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_service import maybe_generate_weekly_dashboard_advice
from celery_app import celery
from celery_workers.cache import (
    AI_STATUS_FAILED_TTL,
    AI_STATUS_RUNNING_TTL,
    CacheUnavailableError,
    clear_ai_status,
    set_ai_status,
)


def _send_weekly_review_email(user_id, result):
    try:
        import logging

        from auth_account import get_public_base_url, send_email_placeholder
        from flask import render_template
        from models import User

        record = result.get("record")
        if record is None:
            return

        user = User.query.get(user_id)
        if user is None:
            return

        payload = result.get("payload", {}) or {}
        summary = payload.get("summary", {}) or {}
        period = result.get("period", {}) or {}

        period_start = period.get("period_start_utc")
        if isinstance(period_start, str):
            try:
                period_start = datetime.fromisoformat(period_start)
            except ValueError:
                period_start = None

        net_pnl = float(summary.get("net_pnl") or 0.0)
        if net_pnl > 0:
            pnl_color = "#1fc66a"
        elif net_pnl < 0:
            pnl_color = "#f15a60"
        else:
            pnl_color = "#8f9bb0"

        ai_text = (getattr(record, "response_text", "") or "").strip()
        if not ai_text:
            ai_text = "Your weekly AI review is ready on your dashboard."
        ai_preview = ai_text[:150] + ("..." if len(ai_text) > 150 else "")
        win_rate = summary.get("win_rate")
        base_url = get_public_base_url()
        logo_url = f"{base_url}/static/site-logo.png"
        dashboard_url = f"{base_url}/dashboard"
        week_label = period_start.strftime("%d %B %Y") if period_start else ""

        html_body = render_template(
            "emails/weekly-review.html",
            name=user.username,
            week_label=week_label,
            ai_preview=ai_preview,
            total_trades=summary.get("closed_trades", 0),
            win_rate=f"{win_rate:.1f}" if win_rate is not None else "-",
            net_pnl=f"{net_pnl:+.2f}",
            pnl_color=pnl_color,
            unsubscribe_url="",
            logo_url=logo_url,
            dashboard_url=dashboard_url,
        )
        send_email_placeholder(
            user.email,
            "Your weekly trading review is ready",
            (
                f"Hi {user.username}, your weekly AI trading review is ready. "
                "Visit your dashboard to read it."
            ),
            html_body=html_body,
        )
    except Exception as exc:
        logging.getLogger(__name__).warning("Weekly review email failed: %s", exc)


def _set_task_status(user_id, trade_account_id, period_start_utc, status, ttl):
    try:
        set_ai_status(
            user_id,
            trade_account_id=trade_account_id,
            period_start_utc=period_start_utc,
            status=status,
            ttl=ttl,
        )
    except CacheUnavailableError:
        return


def _clear_task_status(user_id, trade_account_id, period_start_utc):
    try:
        clear_ai_status(
            user_id,
            trade_account_id=trade_account_id,
            period_start_utc=period_start_utc,
        )
    except CacheUnavailableError:
        return


@celery.task(bind=True, max_retries=2, default_retry_delay=30)
def generate_weekly_ai_task(
    self,
    user_id,
    trade_account_id,
    prompt_filename=None,
    period_start_utc=None,
    force_regenerate=False,
    send_weekly_email=False,
):
    _set_task_status(
        user_id,
        trade_account_id,
        period_start_utc,
        "running",
        AI_STATUS_RUNNING_TTL,
    )
    try:
        result = maybe_generate_weekly_dashboard_advice(
            user_id=user_id,
            trade_account_id=trade_account_id,
            prompt_filename=prompt_filename,
            force_regenerate=force_regenerate,
        )
        if send_weekly_email and result.get("generated") and result.get("record") is not None:
            _send_weekly_review_email(user_id, result)
        _clear_task_status(user_id, trade_account_id, period_start_utc)
    except Exception as exc:
        max_retries = self.max_retries if self.max_retries is not None else 0
        if self.request.retries >= max_retries:
            _set_task_status(
                user_id,
                trade_account_id,
                period_start_utc,
                "failed",
                AI_STATUS_FAILED_TTL,
            )
            raise
        raise self.retry(exc=exc)
