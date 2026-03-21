import sys
import os

if os.environ.get("CELERY_POOL") == "gevent":
    from gevent import monkey

    monkey.patch_all()
    from psycogreen.gevent import patch_psycopg

    patch_psycopg()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import importlib
import importlib.util
from pathlib import Path

from celery import Celery
from celery.app.task import Task as CeleryTask
from celery.schedules import crontab
from dotenv import load_dotenv

load_dotenv("FXJournal Main.env")

_flask_app = None


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
    broker_url, backend_url = _resolve_redis_url()
    celery_app = Celery(
        "fxjournal",
        broker=broker_url,
        backend=backend_url,
        include=["celery_workers.tasks", "celery_workers.mt5_sync"],
        task_cls=FlaskTask,
    )
    celery_config = {
        "task_serializer": "json",
        "result_serializer": "json",
        "accept_content": ["json"],
        "broker_connection_retry_on_startup": True,
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
    }
    configured_pool = os.environ.get("CELERY_POOL", "").strip().lower()
    if configured_pool == "gevent":
        celery_config["worker_pool"] = "gevent"
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
