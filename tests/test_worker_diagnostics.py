from celery_workers.worker_diagnostics import (
    build_worker_diagnostic_text,
    worker_diagnostic_enabled,
    _mask_url_credentials,
)


def test_mask_url_credentials_redacts_password():
    u = "redis://:s3cr3t@redis.example.com:6379/0"
    assert _mask_url_credentials(u) == "redis://:***@redis.example.com:6379/0"


def test_build_worker_diagnostic_text_no_raw_sync_secret(monkeypatch):
    monkeypatch.setenv("MT5_SYNC_SECRET", "super-secret-value")
    monkeypatch.delenv("FLASK_API_URL", raising=False)
    text = build_worker_diagnostic_text(
        phase="task_failure_final",
        task_name="celery_workers.mt5_sync_tasks.sync_mt5_account",
        task_id="abc-123",
        args=(7,),
        kwargs={"trigger_source": "beat"},
        exception=RuntimeError("boom"),
        retries=None,
    )
    assert "FXJ_WORKER_DIAGNOSTIC_BEGIN" in text
    assert "FXJ_WORKER_DIAGNOSTIC_END" in text
    assert "super-secret-value" not in text
    assert "MT5_SYNC_SECRET=set (length=18)" in text
    assert "boom" in text
    assert "RuntimeError" in text
    assert "sync_mt5_account" in text


def test_worker_diagnostic_disabled(monkeypatch):
    monkeypatch.setenv("FXJ_WORKER_DIAGNOSTIC", "0")
    assert worker_diagnostic_enabled() is False


def test_worker_diagnostic_default_enabled(monkeypatch):
    monkeypatch.delenv("FXJ_WORKER_DIAGNOSTIC", raising=False)
    assert worker_diagnostic_enabled() is True
