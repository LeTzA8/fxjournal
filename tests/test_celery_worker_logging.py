from datetime import datetime
import logging
import types
import sys

import celery_workers.mt5_setup_tasks as mt5_setup_module
import celery_workers.weekly_tasks as task_module


def test_setup_mt5_terminal_logs_missing_account_as_table(app_ctx, monkeypatch, caplog, tmp_path):
    base_dir = tmp_path / "mt5-base"
    base_dir.mkdir()

    monkeypatch.setattr(mt5_setup_module.os, "name", "nt")
    monkeypatch.setattr(mt5_setup_module, "MT5_BASE_PATH", str(base_dir))

    caplog.set_level(logging.INFO, logger="celery_workers.mt5_setup_tasks")

    result = mt5_setup_module.setup_mt5_terminal.run(999999)

    assert result == {"error": "MT5Account not found"}
    assert "MT5 Setup Result" in caplog.text
    assert "account missing" in caplog.text


def test_generate_weekly_ai_task_logs_context_and_result(monkeypatch, caplog):
    fake_ai_service = types.ModuleType("ai_service")
    fake_ai_service.maybe_generate_weekly_dashboard_advice = lambda **kwargs: {
        "generated": True,
        "record": types.SimpleNamespace(id=42),
        "period": {"period_start_utc": datetime(2026, 3, 17, 0, 0, 0)},
    }
    monkeypatch.setitem(sys.modules, "ai_service", fake_ai_service)
    monkeypatch.setattr(task_module, "_set_task_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(task_module, "_clear_task_status", lambda *args, **kwargs: None)

    caplog.set_level(logging.INFO, logger="celery_workers.weekly_tasks")

    task_module.generate_weekly_ai_task.run(
        7,
        11,
        prompt_filename="dashboard_advice.txt",
        period_start_utc=datetime(2026, 3, 17, 0, 0, 0),
        force_regenerate=False,
        send_weekly_email=False,
    )

    assert "Weekly AI Task Context" in caplog.text
    assert "Weekly AI Task Result" in caplog.text
    assert "dashboard_advice.txt" in caplog.text


def test_cleanup_weekly_checkins_task_logs_result(app_ctx, monkeypatch, caplog):
    class DummyQuery:
        def filter(self, *args, **kwargs):
            return self

        def delete(self, synchronize_session=False):
            return 5

    monkeypatch.setattr(task_module.WeeklyCheckin, "query", DummyQuery())
    monkeypatch.setattr(task_module.db.session, "commit", lambda: None)

    caplog.set_level(logging.INFO, logger="celery_workers.weekly_tasks")

    result = task_module.cleanup_weekly_checkins_task.run()

    assert result == 5
    assert "Weekly Checkin Cleanup Result" in caplog.text
    assert "Deleted Rows" in caplog.text
