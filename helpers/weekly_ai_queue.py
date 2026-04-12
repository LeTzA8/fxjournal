import logging

from ai_service import (
    MIN_CLOSED_TRADES_FOR_ADVICE,
    count_closed_trade_ideas_in_period,
    get_latest_trade_week_period,
    get_latest_weekly_dashboard_advice,
    should_generate_weekly_dashboard_advice,
    weekly_review_generation_past_market_week_cutoff,
)

WEEKLY_DASHBOARD_PROMPT_FILENAME = "dashboard_advice.txt"


def queue_weekly_ai_review_after_ingest(*, user_id, trade_account_id, log=None):
    """
    After bulk trade ingest (file import, MT5 sync), queue weekly dashboard AI when
    the account is eligible. Skips if a review already exists for the period or
    generation is already queued. Does not send the weekly email (ingest path).
    """
    from celery_workers.cache import (
        AI_STATUS_FAILED_TTL,
        AI_STATUS_QUEUED_TTL,
        CacheUnavailableError,
        claim_ai_status,
        set_ai_status,
    )
    from celery_workers.weekly_tasks import generate_weekly_ai_task

    log = log or logging.getLogger(__name__)
    if not user_id or trade_account_id is None:
        return False

    period = get_latest_trade_week_period(user_id=user_id, trade_account_id=trade_account_id)
    if period is None:
        return False

    existing = get_latest_weekly_dashboard_advice(
        user_id=user_id,
        trade_account_id=trade_account_id,
        period_start_utc=period["period_start_utc"],
    )
    if existing is not None:
        return False

    if not should_generate_weekly_dashboard_advice(
        user_id=user_id,
        trade_account_id=trade_account_id,
        period_start_utc=period["period_start_utc"],
        period_end_utc=period["period_end_utc"],
    ):
        return False

    if not weekly_review_generation_past_market_week_cutoff(
        user_id=user_id,
        trade_account_id=trade_account_id,
        period=period,
    ):
        return False

    closed_trade_idea_count = count_closed_trade_ideas_in_period(
        user_id=user_id,
        trade_account_id=trade_account_id,
        period_start_utc=period["period_start_utc"],
        period_end_utc=period["period_end_utc"],
    )
    if closed_trade_idea_count < MIN_CLOSED_TRADES_FOR_ADVICE:
        return False

    try:
        claimed = claim_ai_status(
            user_id,
            trade_account_id=trade_account_id,
            period_start_utc=period["period_start_utc"],
            status="queued",
            ttl=AI_STATUS_QUEUED_TTL,
        )
    except CacheUnavailableError as exc:
        log.warning(
            "Weekly AI ingest queue: cache unavailable user_id=%s trade_account_id=%s: %s",
            user_id,
            trade_account_id,
            exc,
        )
        return False

    if not claimed:
        return False

    try:
        generate_weekly_ai_task.delay(
            user_id,
            trade_account_id,
            WEEKLY_DASHBOARD_PROMPT_FILENAME,
            period["period_start_utc"].isoformat(),
            send_weekly_email=False,
        )
    except Exception as exc:
        try:
            set_ai_status(
                user_id,
                trade_account_id=trade_account_id,
                period_start_utc=period["period_start_utc"],
                status="failed",
                ttl=AI_STATUS_FAILED_TTL,
            )
        except CacheUnavailableError:
            pass
        log.warning(
            "Weekly AI ingest queue dispatch failed user_id=%s trade_account_id=%s: %s",
            user_id,
            trade_account_id,
            exc,
        )
        return False

    return True
