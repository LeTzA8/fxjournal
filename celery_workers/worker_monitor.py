import json
import logging
import os
import socket
import threading
from datetime import datetime, timezone

from celery.worker.consumer.consumer import Consumer
from kombu.utils.url import maybe_sanitize_url

from celery_workers.cache import (
    CacheUnavailableError,
    set_queue_monitor_state,
    set_worker_state,
)

logger = logging.getLogger("fxj.worker.monitor")

_PATCH_LOCK = threading.Lock()
_CONNECTION_PATCHED = False


def _utcnow():
    return datetime.now(timezone.utc)


def _isoformat(value=None):
    value = value or _utcnow()
    return value.isoformat(timespec="seconds")


def get_vm_id(default_hostname=None):
    """Return the machine id used for account vm_id stamping and worker monitor keys."""
    for candidate in (
        os.getenv("COMPUTERNAME", "").strip(),
        os.getenv("VM_ID", "").strip(),
        socket.gethostname().strip(),
        str(default_hostname or "").strip(),
    ):
        if candidate:
            return candidate
    return "unknown"


def _vm_profile_fields():
    fields = {}
    for env_key, field_key in (
        ("FXJ_MT5_VM_REGION", "vm_region"),
        ("FXJ_MT5_VM_PROVIDER", "vm_provider"),
        ("FXJ_MT5_VM_LABEL", "vm_label"),
    ):
        value = os.getenv(env_key, "").strip()
        if value:
            fields[field_key] = value
    return fields


def _worker_kind_from_hostname(hostname):
    hostname_text = str(hostname or "").strip().lower()
    if hostname_text.startswith("mt5-sync@"):
        return "mt5_sync"
    if hostname_text.startswith("mt5-setup@"):
        return "mt5_setup"
    return "celery"


def _queue_name_for_worker_kind(worker_kind):
    if worker_kind == "mt5_sync":
        return "mt5_sync"
    if worker_kind == "mt5_setup":
        return "mt5_setup"
    return "celery"


def _queue_name_from_task_name(task_name):
    task_name = str(task_name or "").strip()
    if task_name.startswith("celery_workers.mt5_sync_tasks."):
        return "mt5_sync"
    if task_name.startswith("celery_workers.mt5_setup_tasks."):
        return "mt5_setup"
    return "celery"


def _queue_name_from_request(request, task_name=None):
    delivery_info = getattr(request, "delivery_info", None) or {}
    routing_key = str(delivery_info.get("routing_key") or "").strip()
    if routing_key:
        return routing_key
    return _queue_name_from_task_name(task_name)


def _serialize_fields(fields):
    payload = {}
    for key, value in (fields or {}).items():
        if value is None:
            continue
        if isinstance(value, datetime):
            payload[key] = value.astimezone(timezone.utc).isoformat(timespec="seconds")
        else:
            payload[key] = value
    return payload


def log_worker_event(event, *, level=logging.INFO, **fields):
    payload = {"event": event, "occurred_at": _isoformat()}
    payload.update(_serialize_fields(fields))
    logger.log(
        level,
        "FXJ_WORKER_EVENT %s",
        json.dumps(payload, default=str, ensure_ascii=True, sort_keys=True),
    )


def _update_monitor_state(
    *,
    worker_kind,
    queue_name,
    worker_hostname,
    vm_id,
    worker_fields=None,
    queue_fields=None,
):
    worker_fields = worker_fields or {}
    queue_fields = queue_fields or {}
    profile_fields = _vm_profile_fields()
    worker_fields = {**profile_fields, **worker_fields}
    try:
        if worker_kind:
            set_worker_state(
                worker_kind,
                vm_id,
                {
                    "worker_kind": worker_kind,
                    "worker_hostname": worker_hostname,
                    "queue_name": queue_name,
                    "vm_id": vm_id,
                    **worker_fields,
                },
            )
        if queue_name:
            set_queue_monitor_state(
                queue_name,
                {
                    "queue_name": queue_name,
                    **queue_fields,
                },
            )
    except CacheUnavailableError as exc:
        log_worker_event(
            "worker_monitor_cache_unavailable",
            level=logging.WARNING,
            worker_kind=worker_kind,
            queue_name=queue_name,
            worker_hostname=worker_hostname,
            vm_id=vm_id,
            error=str(exc),
        )


def record_task_received(request):
    task_name = getattr(request, "name", None)
    task_id = getattr(request, "id", None)
    worker_hostname = getattr(request, "hostname", None) or socket.gethostname()
    vm_id = get_vm_id(worker_hostname)
    queue_name = _queue_name_from_request(request, task_name=task_name)
    worker_kind = _worker_kind_from_hostname(worker_hostname)
    delivery_info = getattr(request, "delivery_info", None) or {}
    occurred_at = _utcnow()

    log_worker_event(
        "task_received",
        task_name=task_name,
        task_id=task_id,
        queue_name=queue_name,
        worker_hostname=worker_hostname,
        vm_id=vm_id,
        retries=getattr(request, "retries", None),
        eta=getattr(request, "eta", None),
        routing_key=delivery_info.get("routing_key"),
        exchange=delivery_info.get("exchange"),
    )
    _update_monitor_state(
        worker_kind=worker_kind,
        queue_name=queue_name,
        worker_hostname=worker_hostname,
        vm_id=vm_id,
        worker_fields={
            "last_event": "task_received",
            "last_task_received_at": occurred_at,
            "last_received_task_id": task_id,
            "last_received_task_name": task_name,
        },
        queue_fields={
            "last_task_received_at": occurred_at,
            "last_received_task_id": task_id,
            "last_received_task_name": task_name,
            "last_received_vm_id": vm_id,
            "last_received_worker_hostname": worker_hostname,
        },
    )


def record_task_started(task, task_id):
    task_name = getattr(task, "name", None)
    request = getattr(task, "request", None)
    worker_hostname = getattr(request, "hostname", None) or socket.gethostname()
    vm_id = get_vm_id(worker_hostname)
    queue_name = _queue_name_from_request(request, task_name=task_name)
    worker_kind = _worker_kind_from_hostname(worker_hostname)
    occurred_at = _utcnow()

    log_worker_event(
        "task_started",
        task_name=task_name,
        task_id=task_id,
        queue_name=queue_name,
        worker_hostname=worker_hostname,
        vm_id=vm_id,
        retries=getattr(request, "retries", None),
    )
    _update_monitor_state(
        worker_kind=worker_kind,
        queue_name=queue_name,
        worker_hostname=worker_hostname,
        vm_id=vm_id,
        worker_fields={
            "last_event": "task_started",
            "last_task_started_at": occurred_at,
            "last_started_task_id": task_id,
            "last_started_task_name": task_name,
        },
        queue_fields={
            "last_task_started_at": occurred_at,
            "last_started_task_id": task_id,
            "last_started_task_name": task_name,
            "last_started_vm_id": vm_id,
            "last_started_worker_hostname": worker_hostname,
        },
    )


def record_task_finished(task, task_id, *, state=None):
    task_name = getattr(task, "name", None)
    request = getattr(task, "request", None)
    worker_hostname = getattr(request, "hostname", None) or socket.gethostname()
    vm_id = get_vm_id(worker_hostname)
    queue_name = _queue_name_from_request(request, task_name=task_name)
    worker_kind = _worker_kind_from_hostname(worker_hostname)
    occurred_at = _utcnow()

    log_worker_event(
        "task_finished",
        task_name=task_name,
        task_id=task_id,
        queue_name=queue_name,
        worker_hostname=worker_hostname,
        vm_id=vm_id,
        state=state,
        retries=getattr(request, "retries", None),
    )
    _update_monitor_state(
        worker_kind=worker_kind,
        queue_name=queue_name,
        worker_hostname=worker_hostname,
        vm_id=vm_id,
        worker_fields={
            "last_event": "task_finished",
            "last_task_finished_at": occurred_at,
            "last_task_processed_at": occurred_at,
            "last_finished_task_id": task_id,
            "last_finished_task_name": task_name,
            "last_finished_state": state or "unknown",
        },
        queue_fields={
            "last_task_finished_at": occurred_at,
            "last_task_processed_at": occurred_at,
            "last_finished_task_id": task_id,
            "last_finished_task_name": task_name,
            "last_finished_state": state or "unknown",
            "last_processed_vm_id": vm_id,
            "last_processed_worker_hostname": worker_hostname,
        },
    )


def record_worker_ready(sender):
    worker_hostname = getattr(sender, "hostname", None) or socket.gethostname()
    worker_kind = _worker_kind_from_hostname(worker_hostname)
    queue_name = _queue_name_for_worker_kind(worker_kind)
    vm_id = get_vm_id(worker_hostname)
    occurred_at = _utcnow()

    log_worker_event(
        "worker_ready",
        worker_hostname=worker_hostname,
        worker_kind=worker_kind,
        queue_name=queue_name,
        vm_id=vm_id,
    )
    _update_monitor_state(
        worker_kind=worker_kind,
        queue_name=queue_name,
        worker_hostname=worker_hostname,
        vm_id=vm_id,
        worker_fields={
            "last_event": "worker_ready",
            "last_worker_ready_at": occurred_at,
        },
        queue_fields={
            "last_worker_ready_at": occurred_at,
            "last_ready_vm_id": vm_id,
            "last_ready_worker_hostname": worker_hostname,
        },
    )


def record_worker_shutdown(sender):
    worker_hostname = getattr(sender, "hostname", None) or socket.gethostname()
    worker_kind = _worker_kind_from_hostname(worker_hostname)
    queue_name = _queue_name_for_worker_kind(worker_kind)
    vm_id = get_vm_id(worker_hostname)
    occurred_at = _utcnow()

    log_worker_event(
        "worker_shutdown",
        level=logging.WARNING,
        worker_hostname=worker_hostname,
        worker_kind=worker_kind,
        queue_name=queue_name,
        vm_id=vm_id,
    )
    _update_monitor_state(
        worker_kind=worker_kind,
        queue_name=queue_name,
        worker_hostname=worker_hostname,
        vm_id=vm_id,
        worker_fields={
            "last_event": "worker_shutdown",
            "last_worker_shutdown_at": occurred_at,
        },
        queue_fields={
            "last_worker_shutdown_at": occurred_at,
            "last_shutdown_vm_id": vm_id,
            "last_shutdown_worker_hostname": worker_hostname,
        },
    )


def record_celery_heartbeat(sender):
    worker_hostname = getattr(sender, "hostname", None) or socket.gethostname()
    worker_kind = _worker_kind_from_hostname(worker_hostname)
    queue_name = _queue_name_for_worker_kind(worker_kind)
    vm_id = get_vm_id(worker_hostname)
    occurred_at = _utcnow()

    _update_monitor_state(
        worker_kind=worker_kind,
        queue_name=queue_name,
        worker_hostname=worker_hostname,
        vm_id=vm_id,
        worker_fields={
            "last_celery_heartbeat_at": occurred_at,
        },
        queue_fields={
            "last_celery_heartbeat_at": occurred_at,
            "last_heartbeat_vm_id": vm_id,
            "last_heartbeat_worker_hostname": worker_hostname,
        },
    )


def _broker_url(sender):
    try:
        conninfo = getattr(sender, "conninfo", None)
        if conninfo is None:
            return None
        return maybe_sanitize_url(conninfo.as_uri())
    except Exception:
        return None


def record_broker_event(event, sender, *, exc=None, level=logging.INFO):
    worker_hostname = getattr(sender, "hostname", None) or socket.gethostname()
    worker_kind = _worker_kind_from_hostname(worker_hostname)
    queue_name = _queue_name_for_worker_kind(worker_kind)
    vm_id = get_vm_id(worker_hostname)
    occurred_at = _utcnow()
    broker_url = _broker_url(sender)
    error_text = str(exc) if exc is not None else None

    log_worker_event(
        event,
        level=level,
        worker_hostname=worker_hostname,
        worker_kind=worker_kind,
        queue_name=queue_name,
        vm_id=vm_id,
        broker_url=broker_url,
        error=error_text,
    )
    state_field = {
        "broker_connect_attempt": "last_broker_connect_attempt_at",
        "broker_connected": "last_broker_connected_at",
        "broker_connect_retry": "last_broker_retry_at",
        "broker_connection_lost": "last_broker_disconnect_at",
        "broker_connect_exception": "last_broker_connect_exception_at",
    }.get(event)
    worker_fields = {
        "last_event": event,
        "last_broker_url": broker_url,
    }
    queue_fields = {
        "last_broker_url": broker_url,
    }
    if state_field:
        worker_fields[state_field] = occurred_at
        queue_fields[state_field] = occurred_at
    if error_text:
        worker_fields["last_broker_error"] = error_text
        queue_fields["last_broker_error"] = error_text

    _update_monitor_state(
        worker_kind=worker_kind,
        queue_name=queue_name,
        worker_hostname=worker_hostname,
        vm_id=vm_id,
        worker_fields=worker_fields,
        queue_fields=queue_fields,
    )


def install_celery_connection_logging():
    global _CONNECTION_PATCHED
    with _PATCH_LOCK:
        if _CONNECTION_PATCHED:
            return

        original_connect = Consumer.connect
        original_before = Consumer.on_connection_error_before_connected
        original_after = Consumer.on_connection_error_after_connected

        def _fxj_connect(self, *args, **kwargs):
            record_broker_event("broker_connect_attempt", self)
            try:
                connection = original_connect(self, *args, **kwargs)
            except Exception as exc:
                record_broker_event(
                    "broker_connect_exception",
                    self,
                    exc=exc,
                    level=logging.WARNING,
                )
                raise
            record_broker_event("broker_connected", self)
            return connection

        def _fxj_before(self, exc):
            record_broker_event(
                "broker_connect_retry",
                self,
                exc=exc,
                level=logging.WARNING,
            )
            return original_before(self, exc)

        def _fxj_after(self, exc):
            record_broker_event(
                "broker_connection_lost",
                self,
                exc=exc,
                level=logging.WARNING,
            )
            return original_after(self, exc)

        Consumer.connect = _fxj_connect
        Consumer.on_connection_error_before_connected = _fxj_before
        Consumer.on_connection_error_after_connected = _fxj_after
        _CONNECTION_PATCHED = True
