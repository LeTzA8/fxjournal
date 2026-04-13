import logging
import os
from datetime import datetime, timedelta, timezone

from celery_app import celery
from celery_workers.cache import (
    CacheUnavailableError,
    get_queue_depth,
    get_queue_monitor_state,
    list_worker_states,
    set_queue_monitor_state,
)

logger = logging.getLogger(__name__)


def _env_int(name, default):
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        return max(int(raw_value), 1)
    except (TypeError, ValueError):
        return default


def _alert_threshold_minutes():
    return _env_int("FXJ_MT5_SYNC_ALERT_THRESHOLD_MINUTES", 5)


def _sync_stale_threshold_minutes():
    return _env_int("FXJ_MT5_STALE_THRESHOLD_MINUTES", 5)


def _setup_stale_threshold_minutes():
    return _env_int("FXJ_MT5_SETUP_STALE_THRESHOLD_MINUTES", 15)


def _utcnow():
    return datetime.now(timezone.utc)


def _to_aware_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_iso_datetime(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value):
    aware = _to_aware_utc(value)
    if aware is None:
        return "never"
    return aware.isoformat(timespec="seconds")


def _format_delay(delta):
    total_seconds = max(int(delta.total_seconds()), 0)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{total_seconds}s"


def _bool_from_cache_value(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _latest_timestamp(*values):
    parsed_values = [value for value in values if value is not None]
    if not parsed_values:
        return None
    return max(parsed_values)


def _normalize_vm_id(value):
    text_value = str(value or "").strip()
    return text_value[:64] or "unknown"


def _send_mt5_sync_delay_email(vm_id, stale_accounts, *, now_utc):
    from auth_account import send_email_placeholder

    to_email = os.getenv("ERROR_LOG_TO_EMAIL", "").strip().lower()
    if not to_email:
        logger.warning(
            "MT5 sync alert email skipped because ERROR_LOG_TO_EMAIL is not configured. vm_id=%s",
            vm_id,
        )
        return False

    lines = []
    for item in stale_accounts:
        account = item["account"]
        lines.append(
            (
                f"- MT5 account id: {account.id} | "
                f"Last sync time: {_format_timestamp(account.last_synced_at)} | "
                f"Delay duration: {_format_delay(item['delay'])}"
            )
        )

    subject = f"[FX Journal Alert] MT5 sync delayed on VM {vm_id}"
    body = (
        "MT5 sync delay detected\n\n"
        f"VM ID: {vm_id}\n"
        f"Current time: {_format_timestamp(now_utc)}\n"
        f"Stale accounts: {len(stale_accounts)}\n\n"
        + "\n".join(lines)
    )
    result = send_email_placeholder(
        to_email,
        subject,
        body,
    )
    if not (result or {}).get("sent"):
        logger.warning(
            "MT5 sync alert email was not sent: vm_id=%s to=%s mode=%s",
            vm_id,
            to_email,
            (result or {}).get("mode", "unknown"),
        )
        return False
    return True


def _build_queue_health_snapshot(
    queue_name,
    *,
    worker_kind,
    stale_threshold_minutes,
    activity_basis,
    now=None,
):
    current_time = now or _utcnow()
    try:
        queue_depth = get_queue_depth(queue_name)
        queue_state = get_queue_monitor_state(queue_name)
        worker_states = list_worker_states(worker_kind)
    except CacheUnavailableError as exc:
        return {
            "queue_name": queue_name,
            "current_time": current_time.isoformat(timespec="seconds"),
            "stale": False,
            "error": str(exc),
        }

    last_processed_at = _parse_iso_datetime(queue_state.get("last_task_processed_at"))
    last_started_at = _parse_iso_datetime(queue_state.get("last_task_started_at"))
    if activity_basis == "started_or_processed":
        activity_reference_at = _latest_timestamp(last_started_at, last_processed_at)
    else:
        activity_reference_at = last_processed_at
    threshold = timedelta(minutes=stale_threshold_minutes)
    stale = bool(
        queue_depth > 0
        and (
            activity_reference_at is None
            or current_time - activity_reference_at > threshold
        )
    )
    return {
        "queue_name": queue_name,
        "queue_depth": queue_depth,
        "current_time": current_time.isoformat(timespec="seconds"),
        "stale": stale,
        "stale_threshold_minutes": stale_threshold_minutes,
        "stale_alert_active": _bool_from_cache_value(queue_state.get("stale_alert_active")),
        "last_task_received_at": queue_state.get("last_task_received_at"),
        "last_task_started_at": queue_state.get("last_task_started_at"),
        "last_task_processed_at": queue_state.get("last_task_processed_at"),
        "activity_basis": activity_basis,
        "activity_reference_at": (
            activity_reference_at.isoformat(timespec="seconds")
            if activity_reference_at is not None
            else None
        ),
        "last_finished_state": queue_state.get("last_finished_state"),
        "last_processed_vm_id": queue_state.get("last_processed_vm_id"),
        "last_processed_worker_hostname": queue_state.get("last_processed_worker_hostname"),
        "last_celery_heartbeat_at": queue_state.get("last_celery_heartbeat_at"),
        "last_broker_connect_attempt_at": queue_state.get("last_broker_connect_attempt_at"),
        "last_broker_connected_at": queue_state.get("last_broker_connected_at"),
        "last_broker_retry_at": queue_state.get("last_broker_retry_at"),
        "last_broker_disconnect_at": queue_state.get("last_broker_disconnect_at"),
        "last_broker_error": queue_state.get("last_broker_error"),
        "worker_states": worker_states,
        "seconds_since_last_processed": (
            max(int((current_time - last_processed_at).total_seconds()), 0)
            if last_processed_at is not None
            else None
        ),
        "seconds_since_activity": (
            max(int((current_time - activity_reference_at).total_seconds()), 0)
            if activity_reference_at is not None
            else None
        ),
    }


def build_mt5_sync_health_snapshot(*, now=None):
    return _build_queue_health_snapshot(
        "mt5_sync",
        worker_kind="mt5_sync",
        stale_threshold_minutes=_sync_stale_threshold_minutes(),
        activity_basis="processed",
        now=now,
    )


def build_mt5_setup_health_snapshot(*, now=None):
    return _build_queue_health_snapshot(
        "mt5_setup",
        worker_kind="mt5_setup",
        stale_threshold_minutes=_setup_stale_threshold_minutes(),
        activity_basis="started_or_processed",
        now=now,
    )


@celery.task
def check_mt5_sync_health():
    from flask import current_app

    from models import MT5Account, MT5SyncVMState, db

    now_utc = _utcnow()
    now_naive = now_utc.replace(tzinfo=None)
    threshold = timedelta(minutes=_alert_threshold_minutes())

    accounts = (
        MT5Account.query.filter(
            MT5Account.is_active.is_(True),
            MT5Account.user_id.isnot(None),
            MT5Account.trade_account_id.isnot(None),
            MT5Account.archived_at.is_(None),
        ).all()
    )

    active_vm_ids = set()
    stale_by_vm = {}
    checked = 0
    for account in accounts:
        checked += 1
        vm_id = _normalize_vm_id(account.vm_id)
        active_vm_ids.add(vm_id)
        last_success_marker = account.last_synced_at or account.created_at
        if last_success_marker is None:
            continue

        delay = max(now_naive - last_success_marker, timedelta())
        if delay > threshold:
            stale_by_vm.setdefault(vm_id, []).append(
                {
                    "account": account,
                    "delay": delay,
                }
            )

    vm_states = {
        state.vm_id: state
        for state in MT5SyncVMState.query.all()
    }

    alerts_sent = 0
    recovered = 0
    inactive_vm_alerts_cleared = 0

    for vm_id in sorted(active_vm_ids):
        vm_state = vm_states.get(vm_id)
        if vm_state is None:
            vm_state = MT5SyncVMState(vm_id=vm_id, vm_alert_sent=False)
            db.session.add(vm_state)
            vm_states[vm_id] = vm_state

        stale_accounts = stale_by_vm.get(vm_id) or []
        if stale_accounts:
            if not vm_state.vm_alert_sent:
                email_sent = _send_mt5_sync_delay_email(
                    vm_id,
                    stale_accounts,
                    now_utc=now_utc,
                )
                if email_sent:
                    vm_state.vm_alert_sent = True
                    alerts_sent += 1
                current_app.logger.warning(
                    "MT5 sync VM alert triggered vm_id=%s stale_accounts=%s current_time=%s email_sent=%s",
                    vm_id,
                    [
                        {
                            "mt5_account_id": item["account"].id,
                            "last_synced_at": _format_timestamp(item["account"].last_synced_at),
                            "delay": _format_delay(item["delay"]),
                        }
                        for item in stale_accounts
                    ],
                    _format_timestamp(now_utc),
                    email_sent,
                )
        elif vm_state.vm_alert_sent:
            vm_state.vm_alert_sent = False
            recovered += 1
            current_app.logger.info(
                "MT5 sync VM recovered vm_id=%s current_time=%s",
                vm_id,
                _format_timestamp(now_utc),
            )

    for vm_id, vm_state in vm_states.items():
        if vm_id in active_vm_ids:
            continue
        if vm_state.vm_alert_sent:
            vm_state.vm_alert_sent = False
            inactive_vm_alerts_cleared += 1
            current_app.logger.info(
                "MT5 sync VM alert state cleared for inactive VM vm_id=%s current_time=%s",
                vm_id,
                _format_timestamp(now_utc),
            )

    db.session.commit()
    return {
        "checked_accounts": checked,
        "stale_vms": len(stale_by_vm),
        "vm_alerts_sent": alerts_sent,
        "vm_recovered": recovered,
        "inactive_vm_alerts_cleared": inactive_vm_alerts_cleared,
    }


def _check_queue_worker_staleness(*, queue_name, snapshot, current_app, log_prefix):
    if snapshot["stale"] and not snapshot["stale_alert_active"]:
        set_queue_monitor_state(
            queue_name,
            {
                "stale_alert_active": True,
                "stale_detected_at": snapshot["current_time"],
            },
        )
        current_app.logger.error(
            "WORKER STALE DETECTED kind=%s queue_length=%s last_processed_time=%s last_started_time=%s "
            "activity_reference_time=%s current_time=%s last_received_time=%s "
            "last_broker_connected_at=%s last_broker_disconnect_at=%s last_broker_retry_at=%s "
            "last_celery_heartbeat_at=%s vm_id=%s worker_hostname=%s",
            log_prefix,
            snapshot.get("queue_depth"),
            snapshot.get("last_task_processed_at"),
            snapshot.get("last_task_started_at"),
            snapshot.get("activity_reference_at"),
            snapshot.get("current_time"),
            snapshot.get("last_task_received_at"),
            snapshot.get("last_broker_connected_at"),
            snapshot.get("last_broker_disconnect_at"),
            snapshot.get("last_broker_retry_at"),
            snapshot.get("last_celery_heartbeat_at"),
            snapshot.get("last_processed_vm_id") or "unknown",
            snapshot.get("last_processed_worker_hostname") or "unknown",
        )
    elif not snapshot["stale"] and snapshot["stale_alert_active"]:
        set_queue_monitor_state(
            queue_name,
            {
                "stale_alert_active": False,
                "stale_recovered_at": snapshot["current_time"],
            },
        )
        current_app.logger.info(
            "WORKER STALE RECOVERED kind=%s queue_length=%s current_time=%s "
            "last_processed_time=%s last_started_time=%s activity_reference_time=%s "
            "vm_id=%s worker_hostname=%s",
            log_prefix,
            snapshot.get("queue_depth"),
            snapshot.get("current_time"),
            snapshot.get("last_task_processed_at"),
            snapshot.get("last_task_started_at"),
            snapshot.get("activity_reference_at"),
            snapshot.get("last_processed_vm_id") or "unknown",
            snapshot.get("last_processed_worker_hostname") or "unknown",
        )

    return snapshot


@celery.task
def check_mt5_worker_staleness():
    from flask import current_app

    snapshot = build_mt5_sync_health_snapshot()
    if snapshot.get("error"):
        current_app.logger.warning(
            "MT5 worker health snapshot unavailable queue=%s error=%s",
            snapshot.get("queue_name"),
            snapshot.get("error"),
        )
        return snapshot

    return _check_queue_worker_staleness(
        queue_name="mt5_sync",
        snapshot=snapshot,
        current_app=current_app,
        log_prefix="MT5 sync worker",
    )


@celery.task
def check_mt5_setup_worker_staleness():
    from flask import current_app

    snapshot = build_mt5_setup_health_snapshot()
    if snapshot.get("error"):
        current_app.logger.info(
            "MT5 setup worker health snapshot unavailable queue=%s error=%s",
            snapshot.get("queue_name"),
            snapshot.get("error"),
        )
        return snapshot

    return _check_queue_worker_staleness(
        queue_name="mt5_setup",
        snapshot=snapshot,
        current_app=current_app,
        log_prefix="MT5 setup worker",
    )
