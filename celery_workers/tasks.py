import sys
import os

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
):
    _set_task_status(
        user_id,
        trade_account_id,
        period_start_utc,
        "running",
        AI_STATUS_RUNNING_TTL,
    )
    try:
        maybe_generate_weekly_dashboard_advice(
            user_id=user_id,
            trade_account_id=trade_account_id,
            prompt_filename=prompt_filename,
            force_regenerate=force_regenerate,
        )
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
