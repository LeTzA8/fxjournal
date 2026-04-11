import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import importlib
import importlib.util
import logging
import threading
from pathlib import Path

from celery import Celery
from celery.app.task import Task as CeleryTask
from celery.schedules import crontab
from celery.app import trace as celery_trace
from celery.signals import task_postrun, task_prerun, worker_ready, worker_shutdown
from dotenv import load_dotenv


def _load_runtime_env():
    configured_env_file = os.getenv("FXJ_ENV_FILE", "").strip()
    env_candidates = []
    if configured_env_file:
        env_candidates.append(configured_env_file)
    env_candidates.extend([
        ".env",
        "FXJournal Main.env",
        "fxjournal.env",
    ])

    seen = set()
    for candidate in env_candidates:
        candidate = str(candidate or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        load_dotenv(candidate, override=False)


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


def _resolve_redis_url():
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        return redis_url, redis_url
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
            "celery_workers.tasks",
            "celery_workers.mt5_sync",
            "celery_workers.mt5_setup",
        ],
        task_cls=FlaskTask,
    )
    celery_config = {
        "task_serializer": "json",
        "result_serializer": "json",
        "accept_content": ["json"],
        "broker_connection_retry_on_startup": True,
        "worker_prefetch_multiplier": 1,
        "beat_schedule": {
            "cleanup-weekly-checkins": {
                "task": "celery_workers.tasks.cleanup_weekly_checkins_task",
                "schedule": crontab(hour=3, minute=0, day_of_week=1),
            },
            "sync-all-mt5-accounts": {
                "task": "celery_workers.mt5_sync.sync_all_active_mt5_accounts",
                "schedule": 300,
            },
        },
        "task_routes": {
            "celery_workers.mt5_setup.*": {"queue": "mt5_setup"},
            "celery_workers.mt5_sync.sync_mt5_account": {"queue": "mt5_sync"},
            "celery_workers.mt5_sync.fetch_trade_bars": {"queue": "mt5_sync"},
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
            "title_prefix": "MT5 Sync Window",
            "task_prefix": "celery_workers.mt5_sync.",
            "primary_label": "Accounts Active",
            "primary_stat_key": "active_accounts",
        }
    if hostname_text.startswith("mt5-setup@"):
        return {
            "worker_kind": "mt5_setup",
            "queue_name": "mt5_setup",
            "title_prefix": "MT5 Setup Window",
            "task_prefix": "celery_workers.mt5_setup.",
            "primary_label": "Pending Setup",
            "primary_stat_key": "pending_accounts",
        }
    return None


def _format_mt5_worker_title_count(value, label):
    if value is None:
        return f"{label}: ?"
    return f"{int(value)} {label}"


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
    with flask_app.app_context():
        from models import MT5Account, db

        linked_filters = (
            MT5Account.user_id.isnot(None),
            MT5Account.trade_account_id.isnot(None),
        )
        active_accounts = (
            db.session.query(MT5Account.id)
            .filter(*linked_filters, MT5Account.is_active.is_(True))
            .count()
        )
        pending_accounts = (
            db.session.query(MT5Account.id)
            .filter(*linked_filters, MT5Account.is_active.is_(False))
            .count()
        )

    queue_depth = None
    try:
        from celery_workers.cache import CacheUnavailableError, get_queue_depth

        queue_depth = get_queue_depth(config["queue_name"])
    except CacheUnavailableError:
        queue_depth = None
    except Exception as exc:
        logging.getLogger(__name__).debug("MT5 worker title queue-depth lookup failed: %s", exc)

    return {
        "active_accounts": active_accounts,
        "pending_accounts": pending_accounts,
        "queue_depth": queue_depth,
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
