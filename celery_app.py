import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import importlib
import importlib.util
import logging
import logging.handlers
import re
import threading
from pathlib import Path

from celery import Celery
from celery.app.task import Task as CeleryTask
from celery.schedules import crontab
from celery.app import trace as celery_trace
from celery.signals import (
    heartbeat_sent,
    task_failure,
    task_postrun,
    task_prerun,
    task_received,
    task_retry,
    worker_ready,
    worker_shutdown,
)

from celery_workers.worker_monitor import (
    install_celery_connection_logging,
    record_celery_heartbeat,
    record_task_finished,
    record_task_received,
    record_task_started,
    record_worker_ready,
    record_worker_shutdown,
)
from helpers.runtime_env import load_runtime_env as _load_runtime_env


_load_runtime_env()

_flask_app = None
_mt5_worker_window_lock = threading.Lock()
_mt5_worker_window_state = {
    "config": None,
    "active_tasks": 0,
    "stop_event": None,
    "thread": None,
}


class SuppressCeleryTraceTaskLogs(logging.Filter):
    SUPPRESSED_MESSAGES = {
        celery_trace.LOG_RECEIVED,
        celery_trace.LOG_SUCCESS,
        celery_trace.LOG_RETRY,
        celery_trace.LOG_FAILURE,
        celery_trace.LOG_INTERNAL_ERROR,
        celery_trace.LOG_IGNORED,
        celery_trace.LOG_REJECTED,
    }

    def filter(self, record):
        return record.getMessage() not in self.SUPPRESSED_MESSAGES and record.msg not in self.SUPPRESSED_MESSAGES


def _configure_celery_trace_logger():
    trace_logger = logging.getLogger("celery.app.trace")
    filter_name = SuppressCeleryTraceTaskLogs.__name__
    for existing_filter in trace_logger.filters:
        if existing_filter.__class__.__name__ == filter_name:
            return
    trace_logger.addFilter(SuppressCeleryTraceTaskLogs())


def _normalize_redis_url(url):
    import re
    url = re.sub(
        r'(ssl_cert_reqs=)(CERT_NONE|CERT_OPTIONAL|CERT_REQUIRED)',
        lambda m: m.group(1) + m.group(2).replace("CERT_", "").lower(),
        url,
    )
    if url.startswith("rediss://") and "ssl_cert_reqs=" not in url:
        url += ("&" if "?" in url else "?") + "ssl_cert_reqs=none"
    return url


def _resolve_redis_url():
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        normalized = _normalize_redis_url(redis_url)
        return normalized, normalized
    return "memory://", "cache+memory://"


def _load_flask_app_module():
    try:
        return importlib.import_module("app")
    except ModuleNotFoundError as exc:
        if exc.name != "app":
            raise

    module_name = "fxjournal_app_runtime"
    existing_module = sys.modules.get(module_name)
    if existing_module is not None:
        return existing_module

    app_path = Path(__file__).resolve().with_name("app.py")
    spec = importlib.util.spec_from_file_location(module_name, app_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Flask app module from {app_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _resolve_flask_app():
    global _flask_app
    if _flask_app is None:
        app_module = _load_flask_app_module()
        flask_app = getattr(app_module, "app", None)
        if flask_app is None:
            raise RuntimeError("Flask app instance was not found on the app module.")
        _flask_app = flask_app
    return _flask_app


class FlaskTask(CeleryTask):
    abstract = True

    def __call__(self, *args, **kwargs):
        flask_app = _resolve_flask_app()
        with flask_app.app_context():
            return self.run(*args, **kwargs)


def _create_celery():
    _configure_celery_trace_logger()
    broker_url, backend_url = _resolve_redis_url()
    celery_app = Celery(
        "fxjournal",
        broker=broker_url,
        backend=backend_url,
        include=[
            "celery_workers.weekly_tasks",
            "celery_workers.mt5_sync_tasks",
            "celery_workers.mt5_setup_tasks",
            "celery_workers.mt5_monitoring",
        ],
        task_cls=FlaskTask,
    )
    celery_config = {
        "task_serializer": "json",
        "result_serializer": "json",
        "accept_content": ["json"],
        "broker_connection_retry_on_startup": True,
        "broker_connection_retry": True,
        "broker_connection_max_retries": None,
        "worker_prefetch_multiplier": 1,
        "beat_schedule": {
            "cleanup-weekly-checkins": {
                "task": "celery_workers.weekly_tasks.cleanup_weekly_checkins_task",
                "schedule": crontab(hour=3, minute=0, day_of_week=1),
            },
            "sync-all-mt5-accounts": {
                "task": "celery_workers.mt5_sync_tasks.sync_all_active_mt5_accounts",
                "schedule": 30,
            },
            "check-mt5-sync-health": {
                "task": "celery_workers.mt5_monitoring.check_mt5_sync_health",
                "schedule": 60,
            },
            "check-mt5-worker-staleness": {
                "task": "celery_workers.mt5_monitoring.check_mt5_worker_staleness",
                "schedule": 60,
            },
            "check-mt5-setup-worker-staleness": {
                "task": "celery_workers.mt5_monitoring.check_mt5_setup_worker_staleness",
                "schedule": 60,
            },
        },
        "task_routes": {
            "celery_workers.mt5_setup_tasks.*": {"queue": "mt5_setup"},
            "celery_workers.mt5_sync_tasks.sync_mt5_account": {"queue": "mt5_sync"},
            "celery_workers.mt5_sync_tasks.fetch_trade_bars": {"queue": "mt5_sync"},
            "celery_workers.mt5_sync_tasks.fetch_trade_bars_batch": {"queue": "mt5_sync"},
        },
    }
    configured_pool = os.environ.get("CELERY_POOL", "").strip().lower()
    if configured_pool in {"threads", "solo"}:
        celery_config["worker_pool"] = configured_pool
    elif os.name == "nt":
        # Celery's default prefork pool is not reliable on Windows. Force a
        # single-process worker so tasks execute without spawn/prefork tracing.
        celery_config.update(
            worker_pool="solo",
            worker_concurrency=1,
        )
    celery_app.conf.update(
        **celery_config,
    )
    return celery_app


celery = _create_celery()
install_celery_connection_logging()


def init_celery(app):
    global _flask_app
    _flask_app = app
    return celery


def _get_mt5_worker_title_refresh_seconds():
    raw_value = os.getenv("FXJ_MT5_WORKER_TITLE_REFRESH_SECONDS", "").strip()
    if not raw_value:
        return 10
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return 10
    return max(parsed, 1)


def _get_mt5_worker_window_config(hostname):
    hostname_text = str(hostname or "").strip().lower()
    if hostname_text.startswith("mt5-sync@"):
        return {
            "worker_kind": "mt5_sync",
            "queue_name": "mt5_sync",
            "queue_names": ("mt5_priority", "mt5_sync"),
            "title_prefix": "MT5 Sync Window",
            "task_prefix": "celery_workers.mt5_sync_tasks.",
            "primary_label": "Accounts Active",
            "primary_stat_key": "active_accounts",
        }
    if hostname_text.startswith("mt5-setup@"):
        return {
            "worker_kind": "mt5_setup",
            "queue_name": "mt5_setup",
            "title_prefix": "MT5 Setup Window",
            "task_prefix": "celery_workers.mt5_setup_tasks.",
            "primary_label": "Pending Setup",
            "primary_stat_key": "pending_accounts",
        }
    return None


def _format_mt5_worker_title_count(value, label):
    if value is None:
        return f"{label}: ?"
    return f"{int(value)} {label}"


def _parse_worker_stat_int(value):
    text_value = str(value or "").strip()
    if not text_value:
        return None
    try:
        return int(text_value)
    except (TypeError, ValueError):
        return None


def _parse_worker_stat_bool(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _format_mt5_worker_lag_label(minutes):
    parsed = _parse_worker_stat_int(minutes)
    if parsed is None:
        return "?"
    hours, mins = divmod(max(parsed, 0), 60)
    if hours > 0:
        return f"{hours}h{mins:02d}m"
    return f"{mins}m"


def _summarize_mt5_sync_diag_states(diag_states):
    stale_states = []
    for row in diag_states or []:
        if not _parse_worker_stat_bool(row.get("history_stale")):
            continue
        stale_states.append(row)

    if not stale_states:
        return {
            "history_stale_accounts": 0,
            "history_stale_mt5_account_id": None,
            "history_stale_max_lag_minutes": None,
        }

    def _sort_key(row):
        lag_minutes = _parse_worker_stat_int(row.get("latest_deal_lag_minutes"))
        return lag_minutes if lag_minutes is not None else -1

    worst_state = max(stale_states, key=_sort_key)
    return {
        "history_stale_accounts": len(stale_states),
        "history_stale_mt5_account_id": worst_state.get("mt5_account_id"),
        "history_stale_max_lag_minutes": _parse_worker_stat_int(
            worst_state.get("latest_deal_lag_minutes")
        ),
    }


def _format_mt5_worker_window_title(config, *, stats=None, queue_depth=None, active_tasks=0):
    stats = stats or {}
    title_parts = [config["title_prefix"]]
    title_parts.append(
        _format_mt5_worker_title_count(
            stats.get(config["primary_stat_key"]),
            config["primary_label"],
        )
    )
    title_parts.append(_format_mt5_worker_title_count(queue_depth, "In Queue"))
    title_parts.append(_format_mt5_worker_title_count(active_tasks, "Running"))
    if config["worker_kind"] == "mt5_sync":
        history_stale_accounts = _parse_worker_stat_int(stats.get("history_stale_accounts"))
        stale_mt5_account_id = stats.get("history_stale_mt5_account_id")
        if history_stale_accounts:
            title_parts.append(f"{history_stale_accounts} Hist Stale")
            if stale_mt5_account_id:
                title_parts.append(
                    f"MT5 {stale_mt5_account_id} {_format_mt5_worker_lag_label(stats.get('history_stale_max_lag_minutes'))}"
                )
    return " | ".join(title_parts)


def _set_windows_console_title(title_text):
    if os.name != "nt":
        return False
    normalized_title = str(title_text or "").strip()
    if not normalized_title:
        return False
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.SetConsoleTitleW(normalized_title))
    except Exception:
        return False


def _load_mt5_worker_window_stats(config):
    flask_app = _resolve_flask_app()
    active_account_ids = set()
    with flask_app.app_context():
        from models import MT5Account, db

        linked_filters = (
            MT5Account.user_id.isnot(None),
            MT5Account.trade_account_id.isnot(None),
        )
        active_account_rows = (
            db.session.query(MT5Account.id)
            .filter(*linked_filters, MT5Account.is_active.is_(True))
            .all()
        )
        active_accounts = len(active_account_rows)
        if config["worker_kind"] == "mt5_sync":
            active_account_ids = {str(row[0]) for row in active_account_rows}
        pending_accounts = (
            db.session.query(MT5Account.id)
            .filter(*linked_filters, MT5Account.is_active.is_(False))
            .count()
        )

    queue_depth = None
    diag_summary = {}
    try:
        from celery_workers.cache import CacheUnavailableError, get_queue_depth, list_worker_states

        queue_names = config.get("queue_names") or (config["queue_name"],)
        queue_depth = sum(get_queue_depth(queue_name) for queue_name in queue_names)
        if config["worker_kind"] == "mt5_sync":
            diag_summary = _summarize_mt5_sync_diag_states(
                [
                    row
                    for row in list_worker_states("mt5_sync_diag")
                    if str(row.get("mt5_account_id") or row.get("worker_id") or "").strip() in active_account_ids
                ]
            )
    except CacheUnavailableError:
        queue_depth = None
    except Exception as exc:
        logging.getLogger(__name__).debug("MT5 worker title queue-depth lookup failed: %s", exc)

    return {
        "active_accounts": active_accounts,
        "pending_accounts": pending_accounts,
        "queue_depth": queue_depth,
        **diag_summary,
    }


def _current_mt5_worker_window_state():
    with _mt5_worker_window_lock:
        return {
            "config": _mt5_worker_window_state["config"],
            "active_tasks": _mt5_worker_window_state["active_tasks"],
            "stop_event": _mt5_worker_window_state["stop_event"],
            "thread": _mt5_worker_window_state["thread"],
        }


def _mt5_worker_title_matches_task(task_name, config):
    task_name_text = str(task_name or "").strip()
    if not task_name_text or not config:
        return False
    return task_name_text.startswith(config["task_prefix"])


def _refresh_mt5_worker_window_title():
    state = _current_mt5_worker_window_state()
    config = state["config"]
    if not config:
        return
    try:
        stats = _load_mt5_worker_window_stats(config)
        title_text = _format_mt5_worker_window_title(
            config,
            stats=stats,
            queue_depth=stats.get("queue_depth"),
            active_tasks=state["active_tasks"],
        )
    except Exception as exc:
        title_text = f'{config["title_prefix"]} | Title Refresh Failed | {exc}'
    _set_windows_console_title(title_text)


def _run_mt5_worker_window_title_loop(stop_event):
    _refresh_mt5_worker_window_title()
    refresh_seconds = _get_mt5_worker_title_refresh_seconds()
    while not stop_event.wait(refresh_seconds):
        _refresh_mt5_worker_window_title()


def _resolve_worker_hostname(sender):
    return (
        getattr(sender, "hostname", None)
        or getattr(getattr(sender, "consumer", None), "hostname", None)
        or getattr(getattr(sender, "controller", None), "hostname", None)
    )


def _sanitize_worker_log_dir_name(hostname: str) -> str:
    """Filesystem-safe folder name from Celery --hostname (e.g. mt5-sync@PC)."""
    text = (hostname or "celery").strip().replace("@", "_at_")
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._") or "celery"
    return text[:120]


def _default_worker_log_root() -> str:
    return str(Path(__file__).resolve().parent / "logs" / "workers")


def _worker_file_log_explicitly_disabled() -> bool:
    flag = os.getenv("FXJ_WORKER_FILE_LOG", "").strip().lower()
    return flag in {"0", "false", "no", "off"}


def _should_enable_worker_file_logging(sender) -> bool:
    """MT5 VM workers (--hostname mt5-sync@… / mt5-setup@…) get files by default; others need env."""
    if _worker_file_log_explicitly_disabled():
        return False
    hostname = _resolve_worker_hostname(sender)
    if _get_mt5_worker_window_config(hostname):
        return True
    if os.getenv("FXJ_WORKER_LOG_DIR", "").strip():
        return True
    flag = os.getenv("FXJ_WORKER_FILE_LOG", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def _resolve_worker_log_root() -> str:
    configured = os.getenv("FXJ_WORKER_LOG_DIR", "").strip()
    if configured:
        return os.path.abspath(configured)
    return os.path.abspath(_default_worker_log_root())


# Rotating worker file logs: sized for ~1 week on one MT5 sync worker with ~10–20 accounts
# (beat every 5m → ~288 syncs/account/day; rough budget ~5–8 KiB per sync line incl. JSON follow-up).
# Cap ≈ 32 MiB × (1 active + 9 backups) ≈ 320 MiB per celery.log stream.
_WORKER_LOG_FILE_MAX_BYTES = 32 * 1024 * 1024
_WORKER_LOG_FILE_BACKUP_COUNT = 9


def _rotating_file_handler_for_path(log_path: str) -> logging.handlers.RotatingFileHandler:
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=_WORKER_LOG_FILE_MAX_BYTES,
        backupCount=_WORKER_LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    return handler


def _root_has_rotating_handler_for_path(root_logger: logging.Logger, log_path: str) -> bool:
    wanted = os.path.normcase(os.path.abspath(log_path))
    for existing in root_logger.handlers:
        if isinstance(existing, logging.handlers.RotatingFileHandler):
            try:
                if os.path.normcase(os.path.abspath(existing.baseFilename)) == wanted:
                    return True
            except (OSError, ValueError, AttributeError):
                continue
    return False


@worker_ready.connect
def _configure_worker_file_logging(sender=None, **kwargs):
    """Rotating file under logs/workers/<hostname>/celery.log (MT5 workers by hostname; else opt-in via env)."""
    if not _should_enable_worker_file_logging(sender):
        return

    log_root = _resolve_worker_log_root()
    hostname = _resolve_worker_hostname(sender) or "celery"
    safe_name = _sanitize_worker_log_dir_name(str(hostname))
    worker_dir = os.path.join(log_root, safe_name)
    try:
        os.makedirs(worker_dir, exist_ok=True)
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Worker file log dir not created (%s): %s", worker_dir, exc
        )
        return

    log_path = os.path.join(worker_dir, "celery.log")
    root_logger = logging.getLogger()
    if _root_has_rotating_handler_for_path(root_logger, log_path):
        return

    try:
        handler = _rotating_file_handler_for_path(log_path)
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Worker file log handler not attached (%s): %s", log_path, exc
        )
        return

    root_logger.addHandler(handler)
    logging.getLogger(__name__).info(
        "Worker file logging enabled path=%s hostname=%s", log_path, hostname
    )


@worker_ready.connect
def _record_worker_ready_signal(sender=None, **kwargs):
    record_worker_ready(sender)


@worker_ready.connect
def _start_mt5_worker_window_title(sender=None, **kwargs):
    config = _get_mt5_worker_window_config(_resolve_worker_hostname(sender))
    if os.name != "nt" or not config:
        return

    with _mt5_worker_window_lock:
        existing_thread = _mt5_worker_window_state["thread"]
        if existing_thread is not None and existing_thread.is_alive():
            return

        stop_event = threading.Event()
        thread = threading.Thread(
            target=_run_mt5_worker_window_title_loop,
            args=(stop_event,),
            name=f'{config["worker_kind"]}_window_title',
            daemon=True,
        )
        _mt5_worker_window_state["config"] = config
        _mt5_worker_window_state["active_tasks"] = 0
        _mt5_worker_window_state["stop_event"] = stop_event
        _mt5_worker_window_state["thread"] = thread

    _set_windows_console_title(f'{config["title_prefix"]} | Starting...')
    thread.start()


@worker_shutdown.connect
def _stop_mt5_worker_window_title(sender=None, **kwargs):
    with _mt5_worker_window_lock:
        stop_event = _mt5_worker_window_state["stop_event"]
        config = _mt5_worker_window_state["config"]
        _mt5_worker_window_state["config"] = None
        _mt5_worker_window_state["active_tasks"] = 0
        _mt5_worker_window_state["stop_event"] = None
        _mt5_worker_window_state["thread"] = None
    if stop_event is not None:
        stop_event.set()
    if config:
        _set_windows_console_title(f'{config["title_prefix"]} | Stopping...')


@worker_shutdown.connect
def _record_worker_shutdown_signal(sender=None, **kwargs):
    record_worker_shutdown(sender)


@heartbeat_sent.connect
def _record_celery_heartbeat(sender=None, **kwargs):
    record_celery_heartbeat(sender)


@task_received.connect
def _record_task_received_signal(request=None, **kwargs):
    if request is None:
        return
    record_task_received(request)


@task_prerun.connect
def _increment_mt5_worker_active_tasks(task=None, sender=None, **kwargs):
    state = _current_mt5_worker_window_state()
    config = state["config"]
    task_name = getattr(task, "name", None) or getattr(sender, "name", None)
    if not _mt5_worker_title_matches_task(task_name, config):
        return

    with _mt5_worker_window_lock:
        _mt5_worker_window_state["active_tasks"] += 1
    _refresh_mt5_worker_window_title()


@task_prerun.connect
def _record_task_started_signal(task_id=None, task=None, **kwargs):
    if task is None:
        return
    record_task_started(task, task_id)


@task_postrun.connect
def _decrement_mt5_worker_active_tasks(task=None, sender=None, **kwargs):
    state = _current_mt5_worker_window_state()
    config = state["config"]
    task_name = getattr(task, "name", None) or getattr(sender, "name", None)
    if not _mt5_worker_title_matches_task(task_name, config):
        return

    with _mt5_worker_window_lock:
        _mt5_worker_window_state["active_tasks"] = max(
            int(_mt5_worker_window_state["active_tasks"]) - 1,
            0,
        )
    _refresh_mt5_worker_window_title()


@task_postrun.connect
def _record_task_finished_signal(task_id=None, task=None, state=None, **kwargs):
    if task is None:
        return
    record_task_finished(task, task_id, state=state)


@task_failure.connect(weak=False)
def _fxj_log_task_failure_diagnostic(
    sender=None,
    task_id=None,
    exception=None,
    args=None,
    kwargs=None,
    traceback=None,
    einfo=None,
    **extra,
):
    """Emit a single copy-paste block on final task failure (after retries exhausted)."""
    try:
        from celery_workers.worker_diagnostics import log_worker_task_diagnostic

        task_name = getattr(sender, "name", None) or getattr(sender, "__name__", None)
        log_worker_task_diagnostic(
            logging.getLogger("celery.app.task"),
            phase="task_failure_final",
            task_name=str(task_name) if task_name else None,
            task_id=str(task_id) if task_id is not None else None,
            args=args if args is not None else (),
            kwargs=kwargs if kwargs is not None else {},
            exception=exception,
            traceback_obj=traceback,
            einfo=einfo,
        )
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "FXJ task_failure diagnostic hook failed: %s", exc, exc_info=exc
        )


@task_retry.connect(weak=False)
def _fxj_log_task_retry_diagnostic(sender=None, request=None, reason=None, einfo=None, **extra):
    """Emit a diagnostic block each time a task schedules a retry (intermittent errors)."""
    try:
        from celery_workers.worker_diagnostics import log_worker_task_diagnostic

        task_name = getattr(sender, "name", None)
        req = request
        log_worker_task_diagnostic(
            logging.getLogger("celery.app.task"),
            phase="task_retry",
            task_name=str(task_name) if task_name else None,
            task_id=str(getattr(req, "id", None) or "") or None,
            args=getattr(req, "args", ()) or (),
            kwargs=getattr(req, "kwargs", {}) or {},
            exception=reason,
            retries=getattr(req, "retries", None),
            einfo=einfo,
        )
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "FXJ task_retry diagnostic hook failed: %s", exc, exc_info=exc
        )
