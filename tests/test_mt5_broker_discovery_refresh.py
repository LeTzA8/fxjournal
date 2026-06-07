import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from helpers.app_settings import MT5_BROKER_DISCOVERY_REFRESH_ENABLED_KEY, set_bool_app_setting
from helpers.mt5_broker_discovery_refresh import (
    COMPANY_SEARCH_PLACEHOLDER,
    DEFAULT_BROKER_SEARCH_TERM,
    FIND_COMPANY_BUTTON_TEXT,
    RefreshResult,
    _company_search_label_score,
    _escape_send_keys_text,
    _fill_company_search_input,
    _find_company_search_input,
    _find_open_account_dialog,
    _terminate_pid,
    _window_looks_like_open_account_dialog,
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


# ---------------------------------------------------------------------------
# Fix 1: terminate helper
# ---------------------------------------------------------------------------

def test_terminate_pid_keyword_arg_does_not_raise():
    # _terminate_pid uses keyword-only terminal_path; the call site passes it as
    # a keyword so this must not raise TypeError.
    _terminate_pid(None)
    _terminate_pid(None, terminal_path=None)
    _terminate_pid(None, terminal_path=r"C:\some\path\terminal64.exe")


def test_refresh_terminate_called_with_keyword_terminal_path(monkeypatch):
    """terminate_process receives terminal_path as a keyword argument."""
    terminated = {}

    def _terminate(pid, *, terminal_path=None):
        terminated["pid"] = pid
        terminated["terminal_path"] = terminal_path

    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh._wait_for_pid_window",
        lambda pid, timeout_seconds=0: None,
    )
    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh._open_account_dialog_from_main",
        lambda app, pid, timeout_seconds=0: (_FakeFullDialog(), False),
    )
    monkeypatch.setattr("helpers.mt5_broker_discovery_refresh.time.sleep", lambda *a, **k: None)

    refresh_broker_server_cache(
        terminal_path=r"C:\MT5Terminals\mt5_1_1\terminal64.exe",
        terminal_data_dir=None,
        broker_search_term="Exness",
        dry_run=False,
        launch_process=lambda _path: 1111,
        connect_application=lambda _pid: _FakeApp(),
        collect_window_titles=lambda pid: ["MetaTrader 5"],
        terminate_process=_terminate,
    )
    assert terminated["pid"] == 1111
    assert terminated["terminal_path"] == r"C:\MT5Terminals\mt5_1_1\terminal64.exe"


# ---------------------------------------------------------------------------
# Shared fake helpers
# ---------------------------------------------------------------------------

class _FakeFullDialog:
    """A dialog stub that satisfies all calls made during a successful run."""

    def exists(self, timeout=0):
        return True

    def child_window(self, **kwargs):
        return self

    def descendants(self, control_type=None):
        return []

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

    def get_value(self):
        return "Exness"

    def window_text(self):
        return ""

    def friendly_class_name(self):
        return "Edit"

    def is_enabled(self):
        return True

    def is_visible(self):
        return True


class _FakeApp:
    def window(self, **kwargs):
        return _FakeFullDialog()


# ---------------------------------------------------------------------------
# Fix 2: dialog detection
# ---------------------------------------------------------------------------

def test_window_looks_like_dialog_true_when_find_button_present():
    class _Win:
        def child_window(self, **kwargs):
            # Only match the "Find your company" button
            if kwargs.get("title") == FIND_COMPANY_BUTTON_TEXT:
                return _Exists()
            return _Missing()

    class _Exists:
        def exists(self, timeout=0):
            return True

    class _Missing:
        def exists(self, timeout=0):
            return False

    assert _window_looks_like_open_account_dialog(_Win()) is True


def test_window_looks_like_dialog_false_when_no_indicators():
    class _Win:
        def child_window(self, **kwargs):
            class _Missing:
                def exists(self, timeout=0):
                    return False
            return _Missing()

    assert _window_looks_like_open_account_dialog(_Win()) is False


def test_find_open_account_dialog_matches_by_title(monkeypatch):
    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh._collect_window_titles_for_pid",
        lambda pid: ["Open an Account"],
    )

    class _Win:
        def exists(self, timeout=0):
            return True

    class _App:
        def window(self, **kwargs):
            return _Win()

    found = _find_open_account_dialog(_App(), pid=42)
    assert found is not None


def test_find_open_account_dialog_matches_by_content(monkeypatch):
    """Title does not match 'Open an Account' but content has the Find button."""
    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh._collect_window_titles_for_pid",
        lambda pid: ["MetaTrader 5 - [ChartWindow]"],
    )

    class _Win:
        def exists(self, timeout=0):
            return True

        def child_window(self, **kwargs):
            if kwargs.get("title") == FIND_COMPANY_BUTTON_TEXT:
                class _Yes:
                    def exists(self, timeout=0):
                        return True
                return _Yes()
            class _No:
                def exists(self, timeout=0):
                    return False
            return _No()

    class _App:
        def window(self, **kwargs):
            return _Win()

    found = _find_open_account_dialog(_App(), pid=42)
    assert found is not None


def test_find_open_account_dialog_returns_none_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh._collect_window_titles_for_pid",
        lambda pid: ["MetaTrader 5"],
    )

    class _Win:
        def exists(self, timeout=0):
            return False

        def child_window(self, **kwargs):
            class _No:
                def exists(self, timeout=0):
                    return False
            return _No()

    class _App:
        def window(self, **kwargs):
            return _Win()

    assert _find_open_account_dialog(_App(), pid=42) is None


def test_dialog_reused_when_already_open(monkeypatch):
    """If the dialog is already open, _open_account_dialog_from_main returns it
    immediately with already_open=True and does not attempt to open it again."""
    from helpers.mt5_broker_discovery_refresh import _open_account_dialog_from_main

    open_attempts = {"count": 0}

    class _AlreadyOpenDialog:
        def exists(self, timeout=0):
            return True

        def child_window(self, **kwargs):
            open_attempts["count"] += 1
            raise RuntimeError("should not try to click Open an Account")

    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh._find_open_account_dialog",
        lambda app, pid: _AlreadyOpenDialog(),
    )

    dialog, already_open = _open_account_dialog_from_main(
        _FakeApp(), pid=99, timeout_seconds=5
    )
    assert already_open is True
    assert open_attempts["count"] == 0


# ---------------------------------------------------------------------------
# Fix 2 + 3: search input selection and focus
# ---------------------------------------------------------------------------

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
            if control_type not in ("Edit", "ComboBox"):
                return []
            return [
                _Edit("Account name"),
                _Edit(COMPANY_SEARCH_PLACEHOLDER),
            ]

    found = _find_company_search_input(_Dialog())
    assert found is not None
    assert found.label == COMPANY_SEARCH_PLACEHOLDER


def test_find_company_search_input_falls_back_to_first_visible_edit():
    """When no label matches the placeholder, the first existing Edit is returned."""
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
            if control_type not in ("Edit", "ComboBox"):
                return []
            # Unnamed edit — gets score 5 (unnamed fallback)
            return [_Edit("")]

    found = _find_company_search_input(_Dialog())
    assert found is not None


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

        def get_value(self):
            return "Exness"

        def window_text(self):
            return ""

    _fill_company_search_input(_FakeInput(), "Exness")
    assert calls[0] == "wait"
    assert calls[1] == "click"
    assert ("set_edit_text", "Exness") in calls
    assert "type_keys" not in calls


def test_fill_company_search_input_falls_back_to_type_keys_when_set_edit_text_fails():
    calls = []

    class _FakeInput:
        def wait(self, *args, **kwargs):
            pass

        def click_input(self):
            calls.append("click")

        def set_focus(self):
            pass

        def set_edit_text(self, value):
            raise RuntimeError("UIA value pattern unavailable")

        def type_keys(self, keys, *args, **kwargs):
            calls.append(("type_keys", keys))

        def get_value(self):
            return ""

        def window_text(self):
            return ""

    _fill_company_search_input(_FakeInput(), "Exness")
    assert any(k[0] == "type_keys" for k in calls if isinstance(k, tuple))


# ---------------------------------------------------------------------------
# Fix 4: send_keys escaping
# ---------------------------------------------------------------------------

def test_escape_send_keys_text_wraps_special_chars():
    assert _escape_send_keys_text("Exness") == "Exness"
    assert _escape_send_keys_text("IC{Markets}") == "IC{{}Markets{}}"
    assert _escape_send_keys_text("A+B^C%D~E(F)") == "A{+}B{^}C{%}D{~}E{(}F{)}"
    assert _escape_send_keys_text("safe text 123") == "safe text 123"


# ---------------------------------------------------------------------------
# Fix 5 + integration: full automation flow
# ---------------------------------------------------------------------------

def test_refresh_automation_mocked_pid_scoped(monkeypatch):
    calls = {"titles": 0, "terminate": 0}

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

    def _terminate(pid, *, terminal_path=None):
        calls["terminate"] += 1
        assert pid == 4242

    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh._wait_for_pid_window",
        lambda pid, timeout_seconds=0: None,
    )
    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh._open_account_dialog_from_main",
        lambda app, pid, timeout_seconds=0: (_FakeFullDialog(), False),
    )
    monkeypatch.setattr("helpers.mt5_broker_discovery_refresh.time.sleep", lambda *a, **k: None)

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


def test_refresh_result_includes_dialog_already_open_flag(monkeypatch):
    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh._wait_for_pid_window",
        lambda pid, timeout_seconds=0: None,
    )
    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh._open_account_dialog_from_main",
        lambda app, pid, timeout_seconds=0: (_FakeFullDialog(), True),
    )
    monkeypatch.setattr("helpers.mt5_broker_discovery_refresh.time.sleep", lambda *a, **k: None)

    result = refresh_broker_server_cache(
        terminal_path=r"C:\MT5Terminals\mt5_1_1\terminal64.exe",
        terminal_data_dir=None,
        broker_search_term="Exness",
        dry_run=False,
        launch_process=lambda _: 7777,
        connect_application=lambda _: _FakeApp(),
        collect_window_titles=lambda pid: ["Open an Account"],
        terminate_process=lambda pid, *, terminal_path=None: None,
    )
    assert result.success is True
    assert result.dialog_already_open is True


def test_refresh_records_failed_step_and_window_titles(monkeypatch):
    class _BrokenDialog:
        def exists(self, timeout=0):
            return True

        def child_window(self, **kwargs):
            raise RuntimeError("company search input not found in Open an Account dialog")

        def descendants(self, control_type=None):
            return []

    def _launch(_path):
        return 5151

    def _connect(_pid):
        return type("App", (), {"window": lambda self, **kwargs: _BrokenDialog()})()

    def _titles(pid):
        return ["MetaTrader 5", "Open an Account"]

    terminated = []

    def _terminate(pid, *, terminal_path=None):
        terminated.append(pid)

    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh._wait_for_pid_window",
        lambda pid, timeout_seconds=0: None,
    )
    monkeypatch.setattr(
        "helpers.mt5_broker_discovery_refresh._open_account_dialog_from_main",
        lambda app, pid, timeout_seconds=0: (_BrokenDialog(), False),
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
