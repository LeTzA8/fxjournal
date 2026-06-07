import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from helpers.app_settings import MT5_BROKER_DISCOVERY_REFRESH_ENABLED_KEY, set_bool_app_setting
from helpers.mt5_broker_discovery_refresh import (
    COMPANY_SEARCH_PLACEHOLDER,
    DEFAULT_BROKER_SEARCH_TERM,
    RefreshResult,
    _company_search_label_score,
    _fill_company_search_input,
    _find_company_search_input,
    diff_terminal_snapshots,
    refresh_broker_server_cache,
    snapshot_terminal_data_dir,
)
from helpers.mt5_dispatch import dispatch_mt5_broker_discovery_refresh, guard_wrong_vm_task, mt5_setup_queue
from models import MT5Account, TradeAccount, User, db


def _create_user_with_account(*, username, email):
    user = User(
        username=username,
        email=email,
        password="hashed",
        email_verified=True,
        signup_status="approved",
    )
    db.session.add(user)
    db.session.flush()
    trade_account = TradeAccount(
        user_id=user.id,
        name=f"{username} CFD",
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.commit()
    return user, trade_account


def _log_in_root_admin(client, *, email, username):
    os.environ["ADMIN_USER_EMAILS"] = email
    user, trade_account = _create_user_with_account(username=username, email=email)
    user.is_admin = True
    db.session.commit()
    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username
        session_state["display_timezone"] = "UTC"
        session_state["active_trade_account_id"] = trade_account.id
    return user, trade_account


class _FakeRequest:
    def __init__(self, *, kwargs=None, delivery_info=None, task_id="task-1", hostname="host"):
        self.kwargs = kwargs or {}
        self.delivery_info = delivery_info or {"routing_key": "mt5_setup.myfxjournal-sg"}
        self.id = task_id
        self.hostname = hostname


class _FakeTask:
    def __init__(self, request):
        self.request = request


def test_snapshot_detects_added_modified_removed(tmp_path):
    data_dir = tmp_path / "terminal_data"
    data_dir.mkdir()
    servers = data_dir / "servers.dat"
    servers.write_text("v1", encoding="utf-8")
    old = data_dir / "old.dat"
    old.write_text("gone", encoding="utf-8")
    before = snapshot_terminal_data_dir(str(data_dir))

    servers.write_text("v2", encoding="utf-8")
    added = data_dir / "config" / "common.ini"
    added.parent.mkdir()
    added.write_text("new", encoding="utf-8")
    old.unlink()

    after = snapshot_terminal_data_dir(str(data_dir))
    changes = diff_terminal_snapshots(before, after)

    change_types = {change.change_type for change in changes}
    assert "modified" in change_types
    assert "added" in change_types
    assert "removed" in change_types


def test_snapshot_ignores_logs_and_history(tmp_path):
    data_dir = tmp_path / "terminal_data"
    (data_dir / "logs").mkdir(parents=True)
    (data_dir / "history").mkdir(parents=True)
    (data_dir / "logs" / "app.log").write_text("noise", encoding="utf-8")
    (data_dir / "history" / "h.dat").write_text("noise", encoding="utf-8")
    (data_dir / "servers.dat").write_text("keep", encoding="utf-8")

    snapshot = snapshot_terminal_data_dir(str(data_dir))
    assert "servers.dat" in snapshot
    assert not any("logs/" in key for key in snapshot)
    assert not any("history/" in key for key in snapshot)


def test_company_search_label_score_prefers_placeholder():
    assert _company_search_label_score(COMPANY_SEARCH_PLACEHOLDER) == 100
    assert _company_search_label_score("add new company like 'Foo'") == 85
    assert _company_search_label_score("random field") == 0
    assert _company_search_label_score("") == 5


def test_find_company_search_input_prefers_placeholder_edit():
    class _Edit:
        def __init__(self, label):
            self.label = label

        def exists(self, timeout=0):
            return True

        def window_text(self):
            return self.label

    class _Dialog:
        def child_window(self, **kwargs):
            raise RuntimeError("no direct child match")

        def descendants(self, control_type="Edit"):
            if control_type != "Edit":
                return []
            return [
                _Edit("Account name"),
                _Edit(COMPANY_SEARCH_PLACEHOLDER),
            ]

    found = _find_company_search_input(_Dialog())
    assert found is not None
    assert found.label == COMPANY_SEARCH_PLACEHOLDER


def test_fill_company_search_input_clicks_before_set_edit_text():
    calls = []

    class _FakeInput:
        def wait(self, *args, **kwargs):
            calls.append("wait")

        def click_input(self):
            calls.append("click")

        def set_focus(self):
            calls.append("focus")

        def set_edit_text(self, value):
            calls.append(("set_edit_text", value))

        def type_keys(self, *args, **kwargs):
            calls.append("type_keys")

    _fill_company_search_input(_FakeInput(), "Exness")
    assert calls[0] == "wait"
    assert calls[1] == "click"
    assert ("set_edit_text", "Exness") in calls
    assert "type_keys" not in calls


def test_refresh_dry_run_does_not_launch_terminal():
    launched = {"called": False}

    def _unexpected_launch(_path):
        launched["called"] = True
        return 999

    result = refresh_broker_server_cache(
        terminal_path=r"C:\MT5Terminals\mt5_1_1\terminal64.exe",
        terminal_data_dir=None,
        broker_search_term="Exness",
        dry_run=True,
        launch_process=_unexpected_launch,
    )
    assert result.success is True
    assert result.dry_run is True
    assert result.attempted is False
    assert launched["called"] is False


def test_refresh_automation_mocked_pid_scoped(monkeypatch):
    calls = {"titles": 0, "terminate": 0}

    class _FakeDialog:
        def exists(self, timeout=0):
            return True

        def child_window(self, **kwargs):
            return self

        def wait(self, *args, **kwargs):
            return None

        def set_focus(self):
            return None

        def set_edit_text(self, value):
            return None

        def type_keys(self, *args, **kwargs):
            return None

        def click_input(self):
            return None

    class _FakeApp:
        def window(self, **kwargs):
            return _FakeDialog()

    def _launch(_path):
        return 4242

    def _connect(_pid):
        assert _pid == 4242
        return _FakeApp()

    def _titles(pid):
        calls["titles"] += 1
        assert pid == 4242
        if calls["titles"] == 1:
            return ["MetaTrader 5"]
        return ["MetaTrader 5", "Open an Account"]

    def _terminate(pid, terminal_path):
        calls["terminate"] += 1
        assert pid == 4242

    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh._wait_for_pid_window",
        lambda pid, timeout_seconds=0: None,
    )
    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh._open_account_dialog_from_main",
        lambda app, pid, timeout_seconds=0: _FakeDialog(),
    )
    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh.time.sleep",
        lambda *_args, **_kwargs: None,
    )

    result = refresh_broker_server_cache(
        terminal_path=r"C:\MT5Terminals\mt5_1_1\terminal64.exe",
        terminal_data_dir=None,
        broker_search_term="Exness",
        dry_run=False,
        launch_process=_launch,
        connect_application=_connect,
        collect_window_titles=_titles,
        terminate_process=_terminate,
    )
    assert result.success is True
    assert result.pid == 4242
    assert "Open an Account" in result.window_titles_seen
    assert calls["terminate"] == 1


def test_refresh_records_failed_step_and_window_titles(monkeypatch):
    class _BrokenDialog:
        def exists(self, timeout=0):
            return True

        def child_window(self, **kwargs):
            raise RuntimeError("company search input not found in Open an Account dialog")

    def _launch(_path):
        return 5151

    def _connect(_pid):
        return type("App", (), {"window": lambda self, **kwargs: _BrokenDialog()})()

    def _titles(pid):
        return ["MetaTrader 5", "Open an Account"]

    terminated = []

    def _terminate(pid, terminal_path):
        terminated.append(pid)

    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh._wait_for_pid_window",
        lambda pid, timeout_seconds=0: None,
    )
    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh._open_account_dialog_from_main",
        lambda app, pid, timeout_seconds=0: _BrokenDialog(),
    )

    result = refresh_broker_server_cache(
        terminal_path=r"C:\MT5Terminals\mt5_1_1\terminal64.exe",
        terminal_data_dir=None,
        broker_search_term="Exness",
        dry_run=False,
        launch_process=_launch,
        connect_application=_connect,
        collect_window_titles=_titles,
        terminate_process=_terminate,
    )
    assert result.success is False
    assert result.failed_step == "company_search_input"
    assert result.window_titles_seen
    assert terminated == [5151]


def test_dispatch_mt5_broker_discovery_refresh_uses_scoped_queue(monkeypatch):
    captured = {}

    class _Task:
        name = "run_mt5_broker_discovery_refresh"

        @property
        def app(self):
            class _App:
                conf = type("Conf", (), {"broker_url": "memory://"})()

            return _App()

        def apply_async(self, **kwargs):
            captured.update(kwargs)
            return type("Result", (), {"id": "job-123"})()

    result = dispatch_mt5_broker_discovery_refresh(
        _Task(),
        target_vm_id="MYFXJOURNAL-SG",
        kwargs={"dry_run": True, "broker_search_term": "Exness"},
        label="test_broker_refresh",
    )
    assert captured["queue"] == "mt5_setup.myfxjournal-sg"
    assert captured["kwargs"]["target_vm_id"] == "MYFXJOURNAL-SG"
    assert result.id == "job-123"


def test_dispatch_rejects_missing_vm_id():
    class _Task:
        name = "run_mt5_broker_discovery_refresh"

        def apply_async(self, **kwargs):
            raise AssertionError("should not publish")

    assert dispatch_mt5_broker_discovery_refresh(_Task(), target_vm_id=None) is None


def test_guard_wrong_vm_redispatches_setup_queue(monkeypatch):
    monkeypatch.setenv("COMPUTERNAME", "VM2-TEST")
    calls = []
    task = _FakeTask(_FakeRequest(delivery_info={"routing_key": "mt5_setup.vm2-test"}))

    result = guard_wrong_vm_task(
        task,
        target_vm_id="MYFXJOURNAL-SG",
        queue_kind="setup",
        redispatch=lambda queue, kwargs: calls.append({"queue": queue, "kwargs": kwargs}),
    )

    assert result == {
        "requeued": True,
        "target_vm_id": "MYFXJOURNAL-SG",
        "queue": "mt5_setup.myfxjournal-sg",
    }
    assert calls[0]["queue"] == "mt5_setup.myfxjournal-sg"


def test_run_task_dry_run_stores_result(monkeypatch, app_ctx):
    from helpers.mt5_broker_discovery_refresh import execute_broker_discovery_refresh_job

    stored = {}

    monkeypatch.setattr(
        "celery_workers.cache.claim_lock",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr("celery_workers.cache.release_lock", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "celery_workers.cache.set_mt5_broker_refresh_result",
        lambda job_id, payload: stored.update({"job_id": job_id, "payload": payload}),
    )
    monkeypatch.setenv("COMPUTERNAME", "MYFXJOURNAL-SG")

    result = execute_broker_discovery_refresh_job(
        task_id="dry-job-1",
        worker_hostname="mt5-setup@MYFXJOURNAL-SG",
        target_vm_id="MYFXJOURNAL-SG",
        broker_search_term="Exness",
        dry_run=True,
        admin_user_id=99,
    )
    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["queue_name"] == "mt5_setup.myfxjournal-sg"
    assert stored["job_id"] == "dry-job-1"
    assert stored["payload"]["broker_search_term"] == "Exness"


def test_run_task_feature_disabled_blocks_real_run(app_ctx, monkeypatch):
    from helpers.mt5_broker_discovery_refresh import execute_broker_discovery_refresh_job

    stored = {}
    monkeypatch.setattr(
        "celery_workers.cache.set_mt5_broker_refresh_result",
        lambda job_id, payload: stored.update(payload),
    )

    result = execute_broker_discovery_refresh_job(
        task_id="real-job-1",
        target_vm_id="MYFXJOURNAL-SG",
        broker_search_term="Exness",
        dry_run=False,
        admin_user_id=1,
    )
    assert result["success"] is False
    assert result["failed_step"] == "feature_disabled"


def test_run_task_lock_unavailable_skips(monkeypatch, app_ctx):
    from helpers.mt5_broker_discovery_refresh import execute_broker_discovery_refresh_job

    monkeypatch.setattr("celery_workers.cache.claim_lock", lambda *args, **kwargs: False)
    monkeypatch.setattr("celery_workers.cache.release_lock", lambda *args, **kwargs: None)
    monkeypatch.setattr("celery_workers.cache.set_mt5_broker_refresh_result", lambda *args, **kwargs: None)

    result = execute_broker_discovery_refresh_job(
        task_id="lock-job-1",
        target_vm_id="MYFXJOURNAL-SG",
        dry_run=True,
        broker_search_term="Exness",
    )
    assert result["success"] is False
    assert result["failed_step"] == "lock_unavailable"


def test_run_task_releases_lock_after_completion(monkeypatch, app_ctx):
    from helpers.mt5_broker_discovery_refresh import execute_broker_discovery_refresh_job

    released = {"called": False}

    monkeypatch.setattr("celery_workers.cache.claim_lock", lambda *args, **kwargs: True)

    def _release(*args, **kwargs):
        released["called"] = True

    monkeypatch.setattr("celery_workers.cache.release_lock", _release)
    monkeypatch.setattr("celery_workers.cache.set_mt5_broker_refresh_result", lambda *args, **kwargs: None)

    execute_broker_discovery_refresh_job(
        task_id="release-job-1",
        target_vm_id="MYFXJOURNAL-SG",
        dry_run=True,
        broker_search_term="Exness",
    )
    assert released["called"] is True


def test_admin_route_requires_root_admin(app_ctx, client):
    user, trade_account = _create_user_with_account(username="plain-user", email="plain-user@example.com")
    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username
        session_state["active_trade_account_id"] = trade_account.id

    response = client.post(
        "/dashboard/admin/access/mt5/broker-discovery-refresh",
        data={"target_vm_id": "VM-A", "dry_run": "1"},
    )
    assert response.status_code == 404


def test_admin_route_rejects_invalid_vm(app_ctx, client, monkeypatch):
    _log_in_root_admin(client, email="root-broker@test.com", username="root-broker")
    monkeypatch.setenv("FXJ_MT5_SETUP_VM_IDS", "VM-A,VM-B")
    monkeypatch.setattr(
        "auth_account.collect_admin_selectable_vm_ids",
        lambda **kwargs: ["VM-A", "VM-B"],
    )

    response = client.post(
        "/dashboard/admin/access/mt5/broker-discovery-refresh",
        data={"target_vm_id": "VM-Z", "dry_run": "1", "broker_search_term": "Exness"},
    )
    assert response.status_code == 400
    payload = response.get_json()
    assert "Unknown target VM" in payload["error"]


def test_admin_route_dispatches_to_scoped_queue(app_ctx, client, monkeypatch):
    admin_user, _ = _log_in_root_admin(client, email="dispatch-broker@test.com", username="dispatch-broker")
    monkeypatch.setenv("FXJ_MT5_SETUP_VM_IDS", "MYFXJOURNAL-SG")
    monkeypatch.setattr(
        "auth_account.collect_admin_selectable_vm_ids",
        lambda **kwargs: ["MYFXJOURNAL-SG"],
    )

    captured = {}

    class _Task:
        name = "run_mt5_broker_discovery_refresh"

        @property
        def app(self):
            class _App:
                conf = type("Conf", (), {"broker_url": "memory://"})()

            return _App()

        def apply_async(self, **kwargs):
            captured.update(kwargs)
            return type("Result", (), {"id": "admin-job-1"})()

    monkeypatch.setattr("celery_workers.mt5_setup_tasks.run_mt5_broker_discovery_refresh", _Task())

    response = client.post(
        "/dashboard/admin/access/mt5/broker-discovery-refresh",
        data={
            "target_vm_id": "MYFXJOURNAL-SG",
            "dry_run": "1",
            "broker_search_term": "Exness",
        },
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["job_id"] == "admin-job-1"
    assert payload["queue_name"] == mt5_setup_queue("MYFXJOURNAL-SG")
    assert captured["queue"] == "mt5_setup.myfxjournal-sg"
    assert captured["kwargs"]["admin_user_id"] == admin_user.id
    assert captured["kwargs"]["dry_run"] is True
    assert "investor_password" not in json.dumps(captured)


def test_admin_route_blocks_real_run_when_feature_disabled(app_ctx, client, monkeypatch):
    _log_in_root_admin(client, email="disabled-broker@test.com", username="disabled-broker")
    monkeypatch.setenv("FXJ_MT5_SETUP_VM_IDS", "VM-A")
    monkeypatch.setattr("auth_account.collect_admin_selectable_vm_ids", lambda **kwargs: ["VM-A"])

    response = client.post(
        "/dashboard/admin/access/mt5/broker-discovery-refresh",
        data={"target_vm_id": "VM-A", "broker_search_term": "Exness"},
    )
    assert response.status_code == 403
    assert "disabled" in response.get_json()["error"].lower()


def test_admin_route_allows_dry_run_when_feature_disabled(app_ctx, client, monkeypatch):
    _log_in_root_admin(client, email="dryonly-broker@test.com", username="dryonly-broker")
    monkeypatch.setenv("FXJ_MT5_SETUP_VM_IDS", "VM-A")
    monkeypatch.setattr("auth_account.collect_admin_selectable_vm_ids", lambda **kwargs: ["VM-A"])

    class _Task:
        name = "run_mt5_broker_discovery_refresh"

        @property
        def app(self):
            class _App:
                conf = type("Conf", (), {"broker_url": "memory://"})()

            return _App()

        def apply_async(self, **kwargs):
            return type("Result", (), {"id": "dry-only-job"})()

    monkeypatch.setattr("celery_workers.mt5_setup_tasks.run_mt5_broker_discovery_refresh", _Task())

    response = client.post(
        "/dashboard/admin/access/mt5/broker-discovery-refresh",
        data={"target_vm_id": "VM-A", "dry_run": "1", "broker_search_term": "Exness"},
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_admin_poll_endpoint_returns_result(app_ctx, client, monkeypatch):
    _log_in_root_admin(client, email="poll-broker@test.com", username="poll-broker")

    monkeypatch.setattr(
        "celery_workers.cache.get_mt5_broker_refresh_result",
        lambda job_id: {
            "success": True,
            "dry_run": True,
            "queue_name": "mt5_setup.vm-a",
            "broker_search_term": DEFAULT_BROKER_SEARCH_TERM,
        },
    )

    response = client.get("/dashboard/admin/access/mt5/broker-discovery-refresh/job-1")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ready"] is True
    assert payload["result"]["success"] is True


def test_admin_poll_endpoint_pending_when_missing(app_ctx, client, monkeypatch):
    _log_in_root_admin(client, email="pending-broker@test.com", username="pending-broker")
    monkeypatch.setattr("celery_workers.cache.get_mt5_broker_refresh_result", lambda job_id: None)

    response = client.get("/dashboard/admin/access/mt5/broker-discovery-refresh/job-2")
    assert response.status_code == 202
    assert response.get_json()["ready"] is False


def test_set_and_get_mt5_broker_refresh_result_round_trip(monkeypatch):
    from celery_workers.cache import get_mt5_broker_refresh_result, set_mt5_broker_refresh_result

    fake_store = {}

    class _FakeRedis:
        def setex(self, key, ttl, value):
            fake_store[key] = value

        def get(self, key):
            return fake_store.get(key)

    monkeypatch.setattr("celery_workers.cache._client", lambda: _FakeRedis())
    set_mt5_broker_refresh_result(
        "job-xyz",
        RefreshResult(success=True, dry_run=True, broker_search_term="Exness").to_dict(),
    )
    loaded = get_mt5_broker_refresh_result("job-xyz")
    assert loaded["success"] is True
    assert loaded["broker_search_term"] == "Exness"
