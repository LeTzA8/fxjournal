import json
import os
import re
from datetime import datetime, timedelta, timezone

from celery_workers.cache import (
    CacheUnavailableError,
    get_admin_mt5_monitor_cache,
    get_queue_depths,
    list_mt5_worker_states,
    set_admin_mt5_monitor_cache,
)
from helpers.mt5_dispatch import (
    configured_monitor_vm_ids,
    is_mt5_multi_vm_enabled,
    listen_legacy_mt5_queues,
    mt5_priority_queue,
    mt5_setup_queue,
    mt5_sync_queue,
    parse_setup_vm_ids_env,
)


_CELERY_VM_ID_PREFIX = re.compile(r"^mt5-(?:sync|setup)@(.+)$", re.IGNORECASE)


def _normalize_admin_vm_id(value):
    text_value = str(value or "").strip()
    match = _CELERY_VM_ID_PREFIX.match(text_value)
    if match:
        text_value = match.group(1).strip()
    return text_value[:64] or "unknown"


def _worker_state_freshness(row):
    best = 0.0
    for field in (
        "last_celery_heartbeat_at",
        "last_worker_ready_at",
        "last_task_processed_at",
        "last_task_started_at",
    ):
        stamp = _parse_monitor_timestamp((row or {}).get(field))
        if stamp is not None:
            best = max(best, stamp.timestamp())
    return best


def _merge_worker_state(existing, incoming):
    if not existing:
        return incoming
    if not incoming:
        return existing
    return incoming if _worker_state_freshness(incoming) >= _worker_state_freshness(existing) else existing


def _parse_vm_profiles_env():
    raw = os.getenv("FXJ_MT5_VM_PROFILES", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    profiles = {}
    for key, value in parsed.items():
        vm_id = str(key or "").strip()
        if not vm_id or not isinstance(value, dict):
            continue
        profiles[vm_id] = value
    return profiles


def _parse_monitor_timestamp(raw_value):
    if raw_value is None:
        return None
    if isinstance(raw_value, datetime):
        if raw_value.tzinfo is None:
            return raw_value.replace(tzinfo=timezone.utc)
        return raw_value.astimezone(timezone.utc)
    text_value = str(raw_value).strip()
    if not text_value:
        return None
    if text_value.endswith("Z"):
        text_value = f"{text_value[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _worker_is_online(worker_state, *, now=None, threshold_minutes=3):
    if not worker_state:
        return False
    now = now or datetime.now(timezone.utc)
    threshold = timedelta(minutes=max(int(threshold_minutes or 0), 1))
    for field in (
        "last_celery_heartbeat_at",
        "last_worker_ready_at",
        "last_task_processed_at",
        "last_task_started_at",
    ):
        stamp = _parse_monitor_timestamp(worker_state.get(field))
        if stamp is not None and now - stamp <= threshold:
            return True
    return False


def _format_monitor_timestamp(raw_value):
    stamp = _parse_monitor_timestamp(raw_value)
    if stamp is None:
        return "-"
    return stamp.strftime("%Y-%m-%d %H:%M UTC")


def _summarize_account(account, *, status_meta):
    user = getattr(account, "user", None)
    trade_account = getattr(account, "trade_account", None)
    return {
        "id": account.id,
        "account_number": account.account_number,
        "server": account.server,
        "user_label": user.username if user else "Unknown user",
        "user_email": user.email if user else "-",
        "trade_account_name": trade_account.name if trade_account else "Unknown account",
        "trade_account_id": account.trade_account_id,
        "last_synced_at": account.last_synced_at,
        "last_synced_label": (
            account.last_synced_at.strftime("%Y-%m-%d %H:%M UTC")
            if account.last_synced_at
            else "Never"
        ),
        "is_active": bool(getattr(account, "is_active", False)),
        "is_archived": bool(getattr(account, "is_archived", False)),
        "status_label": (status_meta or {}).get("label") or "Unknown",
        "status_chip_class": (status_meta or {}).get("chip_class") or "default",
        "vm_id": _normalize_admin_vm_id(account.vm_id),
    }


def collect_admin_selectable_vm_ids(*, mt5_accounts=(), vm_ids=(), worker_states_by_vm=None):
    """VM ids admins may target for setup/cleanup (configured, account, worker)."""
    selectable = []
    seen = set()

    def _append_vm_id(raw_value):
        normalized = _normalize_admin_vm_id(raw_value)
        if not normalized or normalized == "unknown":
            return
        key = normalized.casefold()
        if key in seen:
            return
        seen.add(key)
        selectable.append(normalized)

    for vm_id in configured_monitor_vm_ids(mt5_accounts=mt5_accounts):
        _append_vm_id(vm_id)
    for vm_id in vm_ids or ():
        _append_vm_id(vm_id)

    if worker_states_by_vm is not None:
        for vm_id in worker_states_by_vm:
            _append_vm_id(vm_id)
    else:
        try:
            grouped = list_mt5_worker_states()
            for rows in grouped.values():
                for row in rows:
                    _append_vm_id(row.get("vm_id") or row.get("worker_id"))
        except CacheUnavailableError:
            pass
    return selectable


def _worker_states_by_vm_from_rows(grouped_worker_states):
    worker_states_by_vm = {}
    for worker_kind, rows in (grouped_worker_states or {}).items():
        for row in rows or []:
            vm_id = _normalize_admin_vm_id(row.get("vm_id") or row.get("worker_id"))
            workers = worker_states_by_vm.setdefault(vm_id, {})
            workers[worker_kind] = _merge_worker_state(workers.get(worker_kind), row)
    return worker_states_by_vm


def _monitor_vm_ids_for_queue_depths(*, mt5_accounts):
    vm_ids = configured_monitor_vm_ids(mt5_accounts=mt5_accounts)
    seen = {vm_id.casefold() for vm_id in vm_ids}
    for account in mt5_accounts or ():
        vm_id = _normalize_admin_vm_id(getattr(account, "vm_id", None))
        if not vm_id or vm_id == "unknown":
            continue
        key = vm_id.casefold()
        if key in seen:
            continue
        seen.add(key)
        vm_ids.append(vm_id)
    return vm_ids


def _fetch_admin_mt5_monitor_snapshot(*, mt5_accounts):
    monitor_available = True
    worker_states_by_vm = {}
    queue_depths = {}
    scoped_queue_depths = {}

    try:
        grouped_worker_states = list_mt5_worker_states()
        worker_states_by_vm = _worker_states_by_vm_from_rows(grouped_worker_states)
    except CacheUnavailableError:
        monitor_available = False
        grouped_worker_states = {}

    queue_names = ["mt5_sync", "mt5_priority", "mt5_setup"]
    if is_mt5_multi_vm_enabled():
        for vm_id in _monitor_vm_ids_for_queue_depths(mt5_accounts=mt5_accounts):
            queue_names.extend(
                [
                    mt5_sync_queue(vm_id),
                    mt5_priority_queue(vm_id),
                    mt5_setup_queue(vm_id),
                ]
            )

    try:
        depth_values = get_queue_depths(queue_names)
        queue_depths = {
            queue_name: int(depth_values.get(queue_name) or 0)
            for queue_name in ("mt5_sync", "mt5_priority", "mt5_setup")
        }
        if is_mt5_multi_vm_enabled():
            for vm_id in _monitor_vm_ids_for_queue_depths(mt5_accounts=mt5_accounts):
                scoped_queue_depths[vm_id] = {
                    "mt5_sync": int(depth_values.get(mt5_sync_queue(vm_id)) or 0),
                    "mt5_priority": int(depth_values.get(mt5_priority_queue(vm_id)) or 0),
                    "mt5_setup": int(depth_values.get(mt5_setup_queue(vm_id)) or 0),
                }
    except CacheUnavailableError:
        monitor_available = False
        queue_depths = {}
        scoped_queue_depths = {}

    return {
        "worker_states_by_vm": worker_states_by_vm,
        "queue_depths": queue_depths,
        "scoped_queue_depths": scoped_queue_depths,
        "monitor_available": monitor_available,
    }


def load_admin_mt5_monitor_snapshot(*, mt5_accounts):
    try:
        cached = get_admin_mt5_monitor_cache()
        if cached is not None:
            return cached
    except CacheUnavailableError:
        pass

    snapshot = _fetch_admin_mt5_monitor_snapshot(mt5_accounts=mt5_accounts)
    try:
        set_admin_mt5_monitor_cache(snapshot)
    except CacheUnavailableError:
        pass
    return snapshot


def resolve_admin_target_vm_id(raw_value, *, selectable_vm_ids=()):
    """
    Normalize an admin-submitted VM id and optionally restrict it to known VMs.

    Returns ``(vm_id, error_message)``. Empty input yields ``(None, None)``.
    """
    vm_id = _normalize_admin_vm_id(raw_value) if str(raw_value or "").strip() else ""
    if not vm_id or vm_id == "unknown":
        if str(raw_value or "").strip():
            return None, "Choose a valid target VM."
        return None, None

    selectable = []
    seen = set()
    for value in selectable_vm_ids or ():
        normalized = _normalize_admin_vm_id(value)
        if not normalized or normalized == "unknown":
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        selectable.append(normalized)
    if not selectable:
        return vm_id, None

    allowed = {value.casefold(): value for value in selectable}
    if vm_id.casefold() not in allowed:
        return None, f"Unknown target VM {vm_id}. Choose one of: {', '.join(selectable)}."
    return allowed[vm_id.casefold()], None


def build_admin_mt5_vm_overview(*, mt5_accounts, mt5_statuses_by_account_id):
    now = datetime.now(timezone.utc)
    vm_profiles = _parse_vm_profiles_env()
    accounts_by_vm = {}
    orphaned_accounts = []

    for account in mt5_accounts:
        if getattr(account, "is_orphaned", False):
            orphaned_accounts.append(
                _summarize_account(
                    account,
                    status_meta=mt5_statuses_by_account_id.get(account.id),
                )
            )
            continue
        vm_key = _normalize_admin_vm_id(account.vm_id)
        accounts_by_vm.setdefault(vm_key, []).append(account)

    vm_ids = set(accounts_by_vm.keys())
    configured_vm_ids = {
        _normalize_admin_vm_id(vm_id)
        for vm_id in parse_setup_vm_ids_env()
        if _normalize_admin_vm_id(vm_id) not in {"", "unknown"}
    }
    vm_ids.update(configured_vm_ids)
    monitor_snapshot = load_admin_mt5_monitor_snapshot(mt5_accounts=mt5_accounts)
    worker_states_by_vm = monitor_snapshot.get("worker_states_by_vm") or {}
    monitor_available = bool(monitor_snapshot.get("monitor_available", True))
    queue_depths = monitor_snapshot.get("queue_depths") or {}
    scoped_queue_depths = monitor_snapshot.get("scoped_queue_depths") or {}

    for vm_id in worker_states_by_vm:
        vm_ids.add(vm_id)

    vm_rows = []
    for vm_id in sorted(vm_ids):
        profile = vm_profiles.get(vm_id, {})
        workers = worker_states_by_vm.get(vm_id, {})
        sync_worker = workers.get("mt5_sync") or {}
        setup_worker = workers.get("mt5_setup") or {}
        vm_accounts = accounts_by_vm.get(vm_id, [])
        is_unknown_bucket = vm_id == "unknown"
        if (
            not is_unknown_bucket
            and vm_id not in configured_vm_ids
            and not vm_accounts
            and not _worker_is_online(sync_worker, now=now)
            and not _worker_is_online(setup_worker, now=now)
        ):
            # Drop monitor-only ghost buckets with no linked accounts.
            continue
        account_summaries = [
            _summarize_account(
                account,
                status_meta=mt5_statuses_by_account_id.get(account.id),
            )
            for account in sorted(
                vm_accounts,
                key=lambda row: (
                    row.last_synced_at is None,
                    -(row.last_synced_at.timestamp() if row.last_synced_at else 0),
                    row.id,
                ),
            )
        ]
        active_count = sum(1 for row in vm_accounts if getattr(row, "is_active", False))
        vm_scoped_depths = scoped_queue_depths.get(vm_id, {})
        worker_missing = (
            not is_unknown_bucket
            and active_count > 0
            and not _worker_is_online(sync_worker, now=now)
            and not _worker_is_online(setup_worker, now=now)
        )
        vm_rows.append(
            {
                "vm_id": vm_id,
                "label": (
                    str(profile.get("label") or "").strip()
                    or str(sync_worker.get("vm_label") or setup_worker.get("vm_label") or "").strip()
                    or None
                ),
                "region": (
                    str(profile.get("region") or "").strip()
                    or str(sync_worker.get("vm_region") or setup_worker.get("vm_region") or "").strip()
                    or None
                ),
                "provider": (
                    str(profile.get("provider") or "").strip()
                    or str(sync_worker.get("vm_provider") or setup_worker.get("vm_provider") or "").strip()
                    or None
                ),
                "notes": str(profile.get("notes") or "").strip() or None,
                "account_count": len(vm_accounts),
                "active_account_count": active_count,
                "accounts": account_summaries,
                "sync_worker_online": _worker_is_online(sync_worker, now=now),
                "setup_worker_online": _worker_is_online(setup_worker, now=now),
                "sync_worker_hostname": sync_worker.get("worker_hostname"),
                "setup_worker_hostname": setup_worker.get("worker_hostname"),
                "sync_last_heartbeat_label": _format_monitor_timestamp(
                    sync_worker.get("last_celery_heartbeat_at")
                    or sync_worker.get("last_worker_ready_at")
                ),
                "setup_last_heartbeat_label": _format_monitor_timestamp(
                    setup_worker.get("last_celery_heartbeat_at")
                    or setup_worker.get("last_worker_ready_at")
                ),
                "sync_last_processed_label": _format_monitor_timestamp(
                    sync_worker.get("last_task_processed_at")
                ),
                "setup_last_processed_label": _format_monitor_timestamp(
                    setup_worker.get("last_task_processed_at")
                ),
                "scoped_queue_depths": vm_scoped_depths,
                "worker_missing_warning": worker_missing,
                "is_unknown_bucket": is_unknown_bucket,
            }
        )

    vm_rows.sort(
        key=lambda row: (
            row["is_unknown_bucket"],
            -(row["account_count"] or 0),
            row["vm_id"],
        )
    )

    selectable_vm_ids = collect_admin_selectable_vm_ids(
        mt5_accounts=mt5_accounts,
        worker_states_by_vm=worker_states_by_vm,
    )

    return {
        "vms": vm_rows,
        "vm_count": len(vm_rows),
        "orphaned_accounts": orphaned_accounts,
        "orphaned_count": len(orphaned_accounts),
        "queue_depths": queue_depths,
        "scoped_queue_depths": scoped_queue_depths,
        "multi_vm_enabled": is_mt5_multi_vm_enabled(),
        "listen_legacy_queues": listen_legacy_mt5_queues(),
        "setup_vm_ids": parse_setup_vm_ids_env(),
        "selectable_vm_ids": selectable_vm_ids,
        "show_vm_target_selector": bool(selectable_vm_ids),
        "monitor_available": monitor_available,
    }
