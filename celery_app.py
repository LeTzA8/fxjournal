import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import importlib
import importlib.util
import logging
from pathlib import Path

from celery import Celery
from celery.app.task import Task as CeleryTask
from celery.schedules import crontab
from celery.app import trace as celery_trace
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
