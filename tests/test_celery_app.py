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
