import logging

import pytest
from kombu.utils.url import maybe_sanitize_url

from helpers.celery_dispatch import describe_celery_broker, dispatch_celery_task


class _DummyConf:
    broker_url = "rediss://user:secret@example.test:6379/0"


class _DummyApp:
    conf = _DummyConf()


class _DummyAsyncResult:
    def __init__(self, task_id):
        self.id = task_id


class _DummyTask:
    app = _DummyApp()
    name = "tests.dummy_task"

    def __init__(self):
        self.calls = []

    def apply_async(self, **kwargs):
        self.calls.append(kwargs)
        return _DummyAsyncResult("task-123")


class _FailingTask(_DummyTask):
    def apply_async(self, **kwargs):
        raise RuntimeError("publish failed")


def test_describe_celery_broker_sanitizes_url():
    task = _DummyTask()

    assert describe_celery_broker(task) == maybe_sanitize_url(task.app.conf.broker_url)


def test_dispatch_celery_task_logs_attempt_and_success(caplog):
    task = _DummyTask()

    with caplog.at_level(logging.INFO):
        result = dispatch_celery_task(
            task,
            args=[1, 2],
            kwargs={"force": True},
            queue="mt5_sync",
            label="admin_mt5_trigger_sync",
            extra={"mt5_account_id": 29},
        )

    assert result.id == "task-123"
    assert task.calls == [{"args": [1, 2], "kwargs": {"force": True}, "queue": "mt5_sync"}]
    assert "Celery publish attempt" in caplog.text
    assert "Celery publish success" in caplog.text
    assert "task-123" in caplog.text
    assert maybe_sanitize_url(task.app.conf.broker_url) in caplog.text


def test_dispatch_celery_task_logs_failure_and_reraises(caplog):
    task = _FailingTask()

    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError, match="publish failed"):
        dispatch_celery_task(
            task,
            args=[29],
            queue="mt5_sync",
            label="admin_mt5_trigger_sync",
            extra={"mt5_account_id": 29},
        )

    assert "Celery publish attempt" in caplog.text
    assert "Celery publish failed" in caplog.text
    assert "Celery publish success" not in caplog.text
