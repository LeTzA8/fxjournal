import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone
import logging

from celery_app import celery
from celery_workers.cache import (
    AI_STATUS_FAILED_TTL,
    AI_STATUS_RUNNING_TTL,
    CacheUnavailableError,
    clear_ai_status,
    set_ai_status,
)
from celery_workers.logging_utils import duration_label, log_ascii_table
from helpers.utils import utcnow_naive
from models import WeeklyCheckin, db


logger = logging.getLogger(__name__)


def _retry_with_backoff(task, exc, *, base_delay=30, max_delay=300):
    retry_number = getattr(getattr(task, "request", None), "retries", 0)
    countdown = min(base_delay * (2 ** retry_number), max_delay)
    raise task.retry(exc=exc, countdown=countdown)


def _send_weekly_review_email(user_id, result):
    try:
        from auth_account import get_public_base_url, render_app_template, send_email_placeholder
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

        ai_preview = "Open your dashboard to read the full weekly review."
        win_rate = summary.get("win_rate")
        base_url = get_public_base_url()
        logo_url = f"{base_url}/static/site-logo.png"
        dashboard_url = f"{base_url}/dashboard"
        week_label = period_start.strftime("%d %B %Y") if period_start else ""

        html_body = render_app_template(
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
        log_ascii_table(
            logger,
            "Weekly AI Email Failed",
            [
                ("User ID", user_id),
                ("Record ID", getattr(result.get("record"), "id", None) if isinstance(result, dict) else None),
                ("Error", exc),
            ],
            level=logging.WARNING,
        )


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
    task_id = getattr(getattr(self, "request", None), "id", None)
    started_at = datetime.now(timezone.utc)
    log_ascii_table(
        logger,
        "Weekly AI Task Context",
        [
            ("Started", started_at),
            ("Task ID", task_id),
            ("User ID", user_id),
            ("Trade Account ID", trade_account_id),
            ("Period Start", period_start_utc),
            ("Prompt", prompt_filename),
            ("Force Regenerate", force_regenerate),
            ("Send Weekly Email", send_weekly_email),
        ],
    )
    _set_task_status(
        user_id,
        trade_account_id,
        period_start_utc,
        "running",
        AI_STATUS_RUNNING_TTL,
    )
    try:
        from ai_service import maybe_generate_weekly_dashboard_advice

        result = maybe_generate_weekly_dashboard_advice(
            user_id=user_id,
            trade_account_id=trade_account_id,
            prompt_filename=prompt_filename,
            force_regenerate=force_regenerate,
        )
        email_sent = False
        if send_weekly_email and result.get("generated") and result.get("record") is not None:
            _send_weekly_review_email(user_id, result)
            email_sent = True
        _clear_task_status(user_id, trade_account_id, period_start_utc)
        finished_at = datetime.now(timezone.utc)
        result_period = (result or {}).get("period", {}) or {}
        log_ascii_table(
            logger,
            "Weekly AI Task Result",
            [
                ("Finished", finished_at),
                ("Duration", duration_label(started_at, finished_at)),
                ("Task ID", task_id),
                ("User ID", user_id),
                ("Trade Account ID", trade_account_id),
                ("Period Start", result_period.get("period_start_utc") or period_start_utc),
                ("Generated", (result or {}).get("generated")),
                ("Skip Reason", (result or {}).get("skip_reason")),
                ("Record ID", getattr((result or {}).get("record"), "id", None)),
                ("Weekly Email Attempted", email_sent),
                ("Status", "complete"),
            ],
        )
    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        max_retries = self.max_retries if self.max_retries is not None else 0
        retries_exhausted = self.request.retries >= max_retries
        if retries_exhausted:
            _set_task_status(
                user_id,
                trade_account_id,
                period_start_utc,
                "failed",
                AI_STATUS_FAILED_TTL,
            )
        log_ascii_table(
            logger,
            "Weekly AI Task Failed",
            [
                ("Finished", finished_at),
                ("Duration", duration_label(started_at, finished_at)),
                ("Task ID", task_id),
                ("User ID", user_id),
                ("Trade Account ID", trade_account_id),
                ("Period Start", period_start_utc),
                ("Prompt", prompt_filename),
                ("Will Retry", not retries_exhausted),
                ("Error", exc),
            ],
            level=logging.ERROR,
        )
        logger.exception(
            "Weekly AI task failed. task_id=%s user_id=%s trade_account_id=%s",
            task_id,
            user_id,
            trade_account_id,
        )
        if retries_exhausted:
            raise
        _retry_with_backoff(self, exc, base_delay=30, max_delay=300)


@celery.task
def cleanup_weekly_checkins_task():
    cutoff = utcnow_naive() - timedelta(weeks=12)
    started_at = datetime.now(timezone.utc)
    try:
        deleted_count = (
            WeeklyCheckin.query
            .filter(WeeklyCheckin.week_end_utc < cutoff)
            .delete(synchronize_session=False)
        )
        db.session.commit()
        finished_at = datetime.now(timezone.utc)
        log_ascii_table(
            logger,
            "Weekly Checkin Cleanup Result",
            [
                ("Finished", finished_at),
                ("Duration", duration_label(started_at, finished_at)),
                ("Cutoff", cutoff),
                ("Deleted Rows", deleted_count),
                ("Status", "cleanup complete"),
            ],
        )
        return deleted_count
    except Exception as exc:
        db.session.rollback()
        finished_at = datetime.now(timezone.utc)
        log_ascii_table(
            logger,
            "Weekly Checkin Cleanup Failed",
            [
                ("Finished", finished_at),
                ("Duration", duration_label(started_at, finished_at)),
                ("Cutoff", cutoff),
                ("Error", exc),
            ],
            level=logging.ERROR,
        )
        logger.exception("Weekly checkin cleanup failed.")
        raise
