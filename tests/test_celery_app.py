import logging

import os

import celery_app as celery_app_module
import app as flask_app_module
from celery_workers.mt5_sync import sync_mt5_account


def test_load_runtime_env_reads_dotenv_when_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("FXJ_ENV_FILE", raising=False)
    (tmp_path / ".env").write_text("REDIS_URL=redis://example.test:6379/0\n", encoding="utf-8")

    celery_app_module._load_runtime_env()

    assert os.getenv("REDIS_URL") == "redis://example.test:6379/0"


def test_celery_routes_and_mt5_task_reliability_flags():
    assert celery_app_module.celery.conf.worker_prefetch_multiplier == 1
    assert celery_app_module.celery.conf.task_routes["celery_workers.mt5_sync.sync_mt5_account"]["queue"] == "mt5_sync"
    assert sync_mt5_account.acks_late is True
    assert sync_mt5_account.reject_on_worker_lost is True


def test_app_enables_pool_pre_ping():
    assert flask_app_module.app.config["SQLALCHEMY_ENGINE_OPTIONS"]["pool_pre_ping"] is True


def test_celery_trace_filter_suppresses_plain_task_lifecycle_logs():
    trace_filter = celery_app_module.SuppressCeleryTraceTaskLogs()
    success_record = logging.LogRecord(
        name="celery.app.trace",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=celery_app_module.celery_trace.LOG_SUCCESS,
        args={"name": "tasks.example", "id": "abc", "runtime": "0.1", "return_value": "{}"},
        exc_info=None,
    )
    custom_record = logging.LogRecord(
        name="celery.app.trace",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Process cleanup failed: %r",
        args=("boom",),
        exc_info=None,
    )

    assert trace_filter.filter(success_record) is False
    assert trace_filter.filter(custom_record) is True


def test_get_mt5_worker_window_config_detects_mt5_workers():
    sync_config = celery_app_module._get_mt5_worker_window_config("mt5-sync@FXJOURNAL-SG")
    setup_config = celery_app_module._get_mt5_worker_window_config("mt5-setup@FXJOURNAL-SG")

    assert sync_config["worker_kind"] == "mt5_sync"
    assert sync_config["queue_name"] == "mt5_sync"
    assert sync_config["title_prefix"] == "MT5 Sync Window"
    assert setup_config["worker_kind"] == "mt5_setup"
    assert setup_config["queue_name"] == "mt5_setup"
    assert setup_config["title_prefix"] == "MT5 Setup Window"


def test_format_mt5_worker_window_title_for_sync_worker():
    config = celery_app_module._get_mt5_worker_window_config("mt5-sync@test-host")

    title = celery_app_module._format_mt5_worker_window_title(
        config,
        stats={"active_accounts": 5},
        queue_depth=4,
        active_tasks=1,
    )

    assert title == "MT5 Sync Window | 5 Accounts Active | 4 In Queue | 1 Running"


def test_format_mt5_worker_window_title_for_setup_worker():
    config = celery_app_module._get_mt5_worker_window_config("mt5-setup@test-host")

    title = celery_app_module._format_mt5_worker_window_title(
        config,
        stats={"pending_accounts": 3},
        queue_depth=2,
        active_tasks=0,
    )

    assert title == "MT5 Setup Window | 3 Pending Setup | 2 In Queue | 0 Running"
