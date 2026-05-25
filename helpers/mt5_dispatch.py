import logging
import os
import re

from helpers.celery_dispatch import dispatch_celery_task

logger = logging.getLogger(__name__)

MT5_SYNC_QUEUE = "mt5_sync"
MT5_PRIORITY_QUEUE = "mt5_priority"
MT5_SETUP_QUEUE = "mt5_setup"

MT5_QUEUE_KINDS = ("sync", "priority", "setup")

MT5_DISPATCH_SKIPPED_MISSING_VM_MSG = (
    "MT5 task could not be queued: this account has no VM affinity (vm_id). "
    "Run setup or a successful sync on a worker VM first."
)

_QUEUE_SLUG_MAX_LEN = 48
_QUEUE_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def is_mt5_multi_vm_enabled() -> bool:
    return str(os.getenv("FXJ_MT5_MULTI_VM", "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def mt5_dispatch_was_skipped(result) -> bool:
    """True when multi-VM dispatch intentionally skipped (e.g. missing vm_id)."""
    return result is None


def listen_legacy_mt5_queues() -> bool:
    if not is_mt5_multi_vm_enabled():
        return True
    return str(os.getenv("FXJ_MT5_LISTEN_LEGACY_QUEUES", "1") or "").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def vm_id_to_queue_slug(vm_id) -> str:
    text = str(vm_id or "").strip().lower()
    if not text:
        return "unknown"
    slug = _QUEUE_SLUG_PATTERN.sub("-", text)
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        return "unknown"
    return slug[:_QUEUE_SLUG_MAX_LEN]


_CELERY_VM_ID_PREFIX = re.compile(r"^mt5-(?:sync|setup)@(.+)$", re.IGNORECASE)


def normalize_vm_id(value) -> str:
    text = str(value or "").strip()
    return text[:64] if text else ""


def canonical_monitor_vm_id(value) -> str:
    """Normalize admin/worker VM ids for comparison and routing (strips Celery host prefix)."""
    text = str(value or "").strip()
    match = _CELERY_VM_ID_PREFIX.match(text)
    if match:
        text = match.group(1).strip()
    text = text[:64] if text else ""
    if text.casefold() == "unknown":
        return ""
    return text


def current_vm_id(default_hostname=None) -> str:
    """Prefer Windows COMPUTERNAME for MT5 worker identity."""
    from celery_workers.worker_monitor import get_vm_id

    return get_vm_id(default_hostname=default_hostname)


def current_vm_queue_slug(default_hostname=None) -> str:
    return vm_id_to_queue_slug(current_vm_id(default_hostname=default_hostname))


def mt5_queue_name(kind: str, vm_id=None) -> str:
    base = {
        "sync": MT5_SYNC_QUEUE,
        "priority": MT5_PRIORITY_QUEUE,
        "setup": MT5_SETUP_QUEUE,
    }.get(str(kind or "").strip().lower())
    if base is None:
        raise ValueError(f"Unknown MT5 queue kind: {kind!r}")
    if not is_mt5_multi_vm_enabled():
        return base
    slug = vm_id_to_queue_slug(vm_id)
    if slug == "unknown":
        return base
    return f"{base}.{slug}"


def mt5_sync_queue(vm_id) -> str:
    return mt5_queue_name("sync", vm_id)


def mt5_priority_queue(vm_id) -> str:
    return mt5_queue_name("priority", vm_id)


def mt5_setup_queue(vm_id) -> str:
    return mt5_queue_name("setup", vm_id)


def scoped_mt5_queue_names(*, vm_ids=None, include_legacy=None) -> tuple[str, ...]:
    names = []
    seen = set()
    if include_legacy is None:
        include_legacy = listen_legacy_mt5_queues() or not is_mt5_multi_vm_enabled()
    if include_legacy or not is_mt5_multi_vm_enabled():
        for queue_name in (MT5_PRIORITY_QUEUE, MT5_SYNC_QUEUE, MT5_SETUP_QUEUE):
            if queue_name not in seen:
                seen.add(queue_name)
                names.append(queue_name)
    if is_mt5_multi_vm_enabled():
        for vm_id in vm_ids or ():
            normalized = normalize_vm_id(vm_id)
            if not normalized:
                continue
            for kind in MT5_QUEUE_KINDS:
                queue_name = mt5_queue_name(kind, normalized)
                if queue_name not in seen:
                    seen.add(queue_name)
                    names.append(queue_name)
    return tuple(names)


def parse_setup_vm_ids_env() -> list[str]:
    raw = str(os.getenv("FXJ_MT5_SETUP_VM_IDS", "") or "").strip()
    if not raw:
        default_vm = get_default_setup_vm_id()
        return [default_vm] if default_vm else []
    values = []
    seen = set()
    for part in raw.split(","):
        vm_id = normalize_vm_id(part)
        if not vm_id:
            continue
        key = vm_id.casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(vm_id)
    return values


def get_default_setup_vm_id() -> str:
    return normalize_vm_id(os.getenv("FXJ_MT5_SETUP_DEFAULT_VM_ID", "MYFXJOURNAL-SG")) or "MYFXJOURNAL-SG"


def setup_failover_max_vms() -> int:
    raw = str(os.getenv("FXJ_MT5_SETUP_FAILOVER_MAX_VMS", "2") or "").strip()
    try:
        return max(int(raw), 1)
    except (TypeError, ValueError):
        return 2


def resolve_setup_target_vm_id(target_vm_id=None) -> str:
    explicit = normalize_vm_id(target_vm_id)
    if explicit:
        return explicit
    configured = parse_setup_vm_ids_env()
    if configured:
        return configured[0]
    return get_default_setup_vm_id()


def next_setup_failover_vm_id(*, setup_attempt_index=0) -> str | None:
    vm_ids = parse_setup_vm_ids_env()
    if not vm_ids:
        return None
    next_attempt = max(int(setup_attempt_index or 0), 0) + 1
    if next_attempt >= setup_failover_max_vms():
        return None
    if next_attempt >= len(vm_ids):
        return None
    return vm_ids[next_attempt]


def is_setup_failover_eligible_error(exc) -> bool:
    if exc is None:
        return False
    from celery_workers.mt5_setup_tasks import PermanentSetupError, TradingPasswordDetectedError

    if isinstance(exc, TradingPasswordDetectedError):
        return False
    message = str(exc).lower()
    if isinstance(exc, PermanentSetupError):
        if any(
            token in message
            for token in (
                "wrong account",
                "trading/master password",
                "trading password",
                "trade_allowed",
                "invalid server",
                "invalid password",
                "invalid account",
                "auth failed",
                "authorization",
            )
        ):
            return False
        return True
    non_failover_tokens = (
        "wrong account",
        "expected",
        " got ",
        "trading/master password",
        "trading password",
        "trade_allowed",
        "auth_failed",
        "auth failed",
        "authorization",
        "invalid password",
        "invalid account",
        "invalid server",
        "unsupported",
        "res_x",
    )
    if any(token in message for token in non_failover_tokens):
        return False
    failover_tokens = (
        "no_ipc",
        "no ipc",
        "ipc connection",
        "timeout",
        "timed out",
        "connection refused",
        "network",
        "mt5_base_path not found",
        "could not find base mt5 appdata",
        "appdata folder not created",
        "terminal64.exe not found after copy",
        "requires windows",
        "metatrader5 not installed",
    )
    return any(token in message for token in failover_tokens)


def build_setup_task_kwargs(*, target_vm_id=None, allow_failover=True, setup_attempt_index=0):
    return {
        "target_vm_id": resolve_setup_target_vm_id(target_vm_id),
        "allow_failover": bool(allow_failover),
        "setup_attempt_index": max(int(setup_attempt_index or 0), 0),
    }


def _account_vm_id(account) -> str:
    return normalize_vm_id(getattr(account, "vm_id", None))


def _log_dispatch_skip(label, reason, *, extra=None):
    logger.warning(
        "MT5 dispatch skipped label=%s reason=%s extra=%s",
        label,
        reason,
        extra or {},
    )


def _resolve_account_vm_queue(account, *, kind, label, extra=None):
    vm_id = _account_vm_id(account)
    if is_mt5_multi_vm_enabled() and not vm_id:
        _log_dispatch_skip(label, "missing_vm_id", extra={**(extra or {}), "mt5_account_id": getattr(account, "id", None)})
        return None, None
    queue = mt5_queue_name(kind, vm_id or None)
    return queue, vm_id


def dispatch_mt5_sync(
    task,
    mt5_account_id,
    *,
    account_vm_id=None,
    kwargs=None,
    expires=None,
    label=None,
    extra=None,
    log=None,
):
    vm_id = normalize_vm_id(account_vm_id)
    if is_mt5_multi_vm_enabled() and not vm_id:
        _log_dispatch_skip(label or "mt5_sync", "missing_vm_id", extra={**(extra or {}), "mt5_account_id": mt5_account_id})
        return None
    queue = mt5_sync_queue(vm_id)
    publish_kwargs = dict(kwargs or {})
    if is_mt5_multi_vm_enabled() and vm_id:
        publish_kwargs.setdefault("target_vm_id", vm_id)
    dispatch_kwargs = {
        "task": task,
        "args": [mt5_account_id],
        "kwargs": publish_kwargs,
        "queue": queue,
        "label": label,
        "extra": extra,
        "log": log,
    }
    if expires is not None:
        return task.apply_async(
            args=[mt5_account_id],
            kwargs=publish_kwargs,
            queue=queue,
            expires=expires,
        )
    return dispatch_celery_task(**dispatch_kwargs)


def dispatch_mt5_priority(
    task,
    *args,
    account_vm_id=None,
    kwargs=None,
    label=None,
    extra=None,
    log=None,
):
    vm_id = normalize_vm_id(account_vm_id)
    if is_mt5_multi_vm_enabled() and not vm_id:
        _log_dispatch_skip(label or "mt5_priority", "missing_vm_id", extra=extra)
        return None
    queue = mt5_priority_queue(vm_id)
    publish_kwargs = dict(kwargs or {})
    if is_mt5_multi_vm_enabled() and vm_id:
        publish_kwargs.setdefault("target_vm_id", vm_id)
    return dispatch_celery_task(
        task,
        args=list(args),
        kwargs=publish_kwargs,
        queue=queue,
        label=label,
        extra=extra,
        log=log,
    )


def dispatch_mt5_setup(
    task,
    mt5_account_id,
    *,
    target_vm_id=None,
    allow_failover=True,
    setup_attempt_index=0,
    label=None,
    extra=None,
    log=None,
):
    vm_id = resolve_setup_target_vm_id(target_vm_id)
    queue = mt5_setup_queue(vm_id)
    task_kwargs = build_setup_task_kwargs(
        target_vm_id=vm_id,
        allow_failover=allow_failover,
        setup_attempt_index=setup_attempt_index,
    )
    return dispatch_celery_task(
        task,
        args=[mt5_account_id],
        kwargs=task_kwargs,
        queue=queue,
        label=label,
        extra={**(extra or {}), **task_kwargs},
        log=log,
    )


def dispatch_mt5_cleanup(
    task,
    terminal_path,
    appdata_hash,
    *,
    account_vm_id=None,
    kwargs=None,
    label=None,
    extra=None,
    log=None,
):
    vm_id = canonical_monitor_vm_id(account_vm_id)
    if is_mt5_multi_vm_enabled() and not vm_id:
        _log_dispatch_skip(label or "mt5_cleanup", "missing_vm_id", extra=extra)
        return None
    queue = mt5_setup_queue(vm_id)
    publish_kwargs = dict(kwargs or {})
    if vm_id:
        publish_kwargs.setdefault("target_vm_id", vm_id)
    return dispatch_celery_task(
        task,
        args=[terminal_path, appdata_hash],
        kwargs=publish_kwargs,
        queue=queue,
        label=label,
        extra=extra,
        log=log,
    )


def dispatch_mt5_pause(task, mt5_account_id, *, account_vm_id=None, label=None, extra=None, log=None):
    vm_id = normalize_vm_id(account_vm_id)
    if is_mt5_multi_vm_enabled() and not vm_id:
        _log_dispatch_skip(label or "mt5_pause", "missing_vm_id", extra=extra)
        return None
    queue = mt5_setup_queue(vm_id)
    return dispatch_celery_task(
        task,
        args=[mt5_account_id],
        kwargs={"target_vm_id": vm_id or None},
        queue=queue,
        label=label or "mt5_pause",
        extra=extra,
        log=log,
    )


def resolve_queue_kind_from_request(task) -> str:
    delivery_info = getattr(getattr(task, "request", None), "delivery_info", None) or {}
    routing_key = str(delivery_info.get("routing_key") or "").strip().lower()
    if routing_key.startswith(MT5_PRIORITY_QUEUE):
        return "priority"
    if routing_key.startswith(MT5_SETUP_QUEUE):
        return "setup"
    return "sync"


def guard_wrong_vm_task(
    task,
    *,
    target_vm_id,
    queue_kind=None,
    redispatch,
    max_redispatch=1,
):
    """
    If this worker is not the target VM, re-dispatch to the scoped queue and exit.

    Returns a result dict when re-queued, otherwise None.
    """
    if not is_mt5_multi_vm_enabled():
        return None

    target = canonical_monitor_vm_id(target_vm_id)
    if not target:
        return None

    effective_queue_kind = queue_kind or resolve_queue_kind_from_request(task)

    target_slug = vm_id_to_queue_slug(target)
    current_slug = current_vm_queue_slug(
        default_hostname=getattr(getattr(task, "request", None), "hostname", None),
    )
    if target_slug == "unknown" or current_slug == "unknown":
        return None
    if target_slug == current_slug:
        return None

    request = getattr(task, "request", None)
    kwargs = dict(getattr(request, "kwargs", None) or {})
    redispatch_count = int(kwargs.get("_wrong_vm_redispatch_count") or 0)
    if redispatch_count >= max_redispatch:
        logger.error(
            "MT5 wrong-VM guard exceeded max redispatch task=%s target_vm_id=%s current_slug=%s count=%s",
            getattr(request, "id", None),
            target,
            current_slug,
            redispatch_count,
        )
        return {
            "error": "wrong_vm_max_redispatch",
            "target_vm_id": target,
            "current_vm_slug": current_slug,
        }

    kwargs["_wrong_vm_redispatch_count"] = redispatch_count + 1
    kwargs["target_vm_id"] = target
    queue = mt5_queue_name(effective_queue_kind, target)
    logger.warning(
        "MT5 wrong-VM redispatch task=%s target_vm_id=%s target_slug=%s current_slug=%s queue=%s count=%s",
        getattr(request, "id", None),
        target,
        target_slug,
        current_slug,
        queue,
        redispatch_count + 1,
    )
    redispatch(queue=queue, kwargs=kwargs)
    return {"requeued": True, "target_vm_id": target, "queue": queue}


def account_sync_queue_depths(vm_id) -> tuple[int, int]:
    from celery_workers.cache import get_queue_depth

    if not is_mt5_multi_vm_enabled():
        return (
            int(get_queue_depth(MT5_SYNC_QUEUE) or 0),
            int(get_queue_depth(MT5_PRIORITY_QUEUE) or 0),
        )
    return (
        int(get_queue_depth(mt5_sync_queue(vm_id)) or 0),
        int(get_queue_depth(mt5_priority_queue(vm_id)) or 0),
    )


def configured_monitor_vm_ids(*, mt5_accounts=None) -> list[str]:
    vm_ids = []
    seen = set()
    for vm_id in parse_setup_vm_ids_env():
        key = vm_id.casefold()
        if key not in seen:
            seen.add(key)
            vm_ids.append(vm_id)
    for account in mt5_accounts or ():
        vm_id = normalize_vm_id(getattr(account, "vm_id", None))
        if not vm_id:
            continue
        key = vm_id.casefold()
        if key not in seen:
            seen.add(key)
            vm_ids.append(vm_id)
    return vm_ids
