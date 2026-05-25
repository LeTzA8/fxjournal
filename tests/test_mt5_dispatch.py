import pytest

from helpers.mt5_dispatch import (
    MT5_DISPATCH_SKIPPED_MISSING_VM_MSG,
    build_setup_task_kwargs,
    dispatch_mt5_priority,
    dispatch_mt5_setup,
    dispatch_mt5_sync,
    guard_wrong_vm_task,
    is_mt5_multi_vm_enabled,
    is_setup_failover_eligible_error,
    mt5_dispatch_was_skipped,
    mt5_priority_queue,
    mt5_setup_queue,
    mt5_sync_queue,
    next_setup_failover_vm_id,
    parse_setup_vm_ids_env,
    resolve_setup_target_vm_id,
    vm_id_to_queue_slug,
)


def test_vm_id_to_queue_slug_normalizes():
    assert vm_id_to_queue_slug("MYFXJOURNAL-SG") == "myfxjournal-sg"
    assert vm_id_to_queue_slug("  VM__2!!  ") == "vm-2"
    assert vm_id_to_queue_slug("") == "unknown"
    assert vm_id_to_queue_slug("A" * 80) == "a" * 48


def test_queue_names_legacy_when_flag_off(monkeypatch):
    monkeypatch.delenv("FXJ_MT5_MULTI_VM", raising=False)
    assert mt5_sync_queue("MYFXJOURNAL-SG") == "mt5_sync"
    assert mt5_priority_queue("MYFXJOURNAL-SG") == "mt5_priority"
    assert mt5_setup_queue("MYFXJOURNAL-SG") == "mt5_setup"


def test_queue_names_scoped_when_flag_on(monkeypatch):
    monkeypatch.setenv("FXJ_MT5_MULTI_VM", "1")
    assert mt5_sync_queue("MYFXJOURNAL-SG") == "mt5_sync.myfxjournal-sg"
    assert mt5_priority_queue("MYFXJOURNAL-SG") == "mt5_priority.myfxjournal-sg"
    assert mt5_setup_queue("MYFXJOURNAL-SG") == "mt5_setup.myfxjournal-sg"


def test_parse_setup_vm_ids_env(monkeypatch):
    monkeypatch.setenv("FXJ_MT5_SETUP_VM_IDS", "MYFXJOURNAL-SG, VM2-TEST")
    assert parse_setup_vm_ids_env() == ["MYFXJOURNAL-SG", "VM2-TEST"]


def test_resolve_setup_target_vm_id(monkeypatch):
    monkeypatch.setenv("FXJ_MT5_SETUP_VM_IDS", "MYFXJOURNAL-SG,VM2")
    assert resolve_setup_target_vm_id() == "MYFXJOURNAL-SG"
    assert resolve_setup_target_vm_id("VM2") == "VM2"


def test_next_setup_failover_vm_id_respects_max(monkeypatch):
    monkeypatch.setenv("FXJ_MT5_SETUP_VM_IDS", "MYFXJOURNAL-SG,VM2,VM3")
    monkeypatch.setenv("FXJ_MT5_SETUP_FAILOVER_MAX_VMS", "2")
    assert next_setup_failover_vm_id(setup_attempt_index=0) == "VM2"
    assert next_setup_failover_vm_id(setup_attempt_index=1) is None


def test_is_setup_failover_eligible_error():
    from celery_workers.mt5_setup_tasks import PermanentSetupError, TradingPasswordDetectedError

    assert is_setup_failover_eligible_error(PermanentSetupError("MT5_BASE_PATH not found"))
    assert not is_setup_failover_eligible_error(TradingPasswordDetectedError("trade_allowed"))
    assert not is_setup_failover_eligible_error(RuntimeError("Wrong account logged in: expected 1, got 2"))
    assert is_setup_failover_eligible_error(RuntimeError("IPC connection timeout"))


class _FakeRequest:
    def __init__(self, *, kwargs=None, delivery_info=None, task_id="task-1", hostname="host"):
        self.kwargs = kwargs or {}
        self.delivery_info = delivery_info or {"routing_key": "mt5_sync.myfxjournal-sg"}
        self.id = task_id
        self.hostname = hostname


class _FakeTask:
    def __init__(self, request):
        self.request = request


def test_guard_wrong_vm_task_redispatches(monkeypatch):
    monkeypatch.setenv("FXJ_MT5_MULTI_VM", "1")
    monkeypatch.setenv("COMPUTERNAME", "VM2-TEST")
    calls = []
    task = _FakeTask(_FakeRequest(kwargs={"trigger_source": "beat"}))

    result = guard_wrong_vm_task(
        task,
        target_vm_id="MYFXJOURNAL-SG",
        redispatch=lambda queue, kwargs: calls.append({"queue": queue, "kwargs": kwargs}),
    )

    assert result == {"requeued": True, "target_vm_id": "MYFXJOURNAL-SG", "queue": "mt5_sync.myfxjournal-sg"}
    assert calls[0]["queue"] == "mt5_sync.myfxjournal-sg"
    assert calls[0]["kwargs"]["_wrong_vm_redispatch_count"] == 1


def test_guard_wrong_vm_task_noop_on_matching_vm(monkeypatch):
    monkeypatch.setenv("FXJ_MT5_MULTI_VM", "1")
    monkeypatch.setenv("COMPUTERNAME", "MYFXJOURNAL-SG")
    task = _FakeTask(_FakeRequest())
    assert guard_wrong_vm_task(task, target_vm_id="MYFXJOURNAL-SG", redispatch=lambda **_: pytest.fail("unexpected")) is None


def test_guard_wrong_vm_task_canonicalizes_celery_prefixed_target(monkeypatch):
    monkeypatch.setenv("FXJ_MT5_MULTI_VM", "1")
    monkeypatch.setenv("COMPUTERNAME", "VM2-TEST")
    calls = []
    task = _FakeTask(_FakeRequest())

    result = guard_wrong_vm_task(
        task,
        target_vm_id="mt5-sync@MYFXJOURNAL-SG",
        queue_kind="setup",
        redispatch=lambda queue, kwargs: calls.append({"queue": queue, "kwargs": kwargs}),
    )

    assert result == {
        "requeued": True,
        "target_vm_id": "MYFXJOURNAL-SG",
        "queue": "mt5_setup.myfxjournal-sg",
    }
    assert calls[0]["queue"] == "mt5_setup.myfxjournal-sg"


def test_mt5_dispatch_was_skipped():
    assert mt5_dispatch_was_skipped(None) is True
    assert mt5_dispatch_was_skipped(type("Result", (), {"id": "task-id"})()) is False


def test_resolve_mt5_cleanup_target_vm_rejects_mismatch(app_ctx):
    from helpers.core import resolve_mt5_cleanup_target_vm
    from models import MT5Account, TradeAccount, User, db

    user = User(username="resolve-vm-user", email="resolve-vm@example.com", password="hashed")
    db.session.add(user)
    db.session.flush()
    trade_account = TradeAccount(user_id=user.id, name="Acct", account_type="CFD", is_default=True)
    db.session.add(trade_account)
    db.session.flush()
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="12345",
        server="Test-Live",
        investor_password_encrypted="enc",
        vm_id="VM-A",
    )
    db.session.add(mt5_account)
    db.session.commit()

    vm_id, error = resolve_mt5_cleanup_target_vm(mt5_account=mt5_account, target_vm_id="VM-B")
    assert vm_id is None
    assert "does not match" in error

    vm_id, error = resolve_mt5_cleanup_target_vm(mt5_account=mt5_account, target_vm_id="")
    assert vm_id == "VM-A"
    assert error is None

    mt5_account.vm_id = "mt5-sync@VM-A"
    db.session.commit()
    vm_id, error = resolve_mt5_cleanup_target_vm(mt5_account=mt5_account, target_vm_id="VM-A")
    assert vm_id == "VM-A"
    assert error is None


def test_resolve_mt5_cleanup_target_vm_requires_target_in_multi_vm(app_ctx, monkeypatch):
    from helpers.core import resolve_mt5_cleanup_target_vm
    from models import MT5Account, TradeAccount, User, db

    monkeypatch.setenv("FXJ_MT5_MULTI_VM", "1")
    user = User(username="resolve-vm-multi", email="resolve-vm-multi@example.com", password="hashed")
    db.session.add(user)
    db.session.flush()
    trade_account = TradeAccount(user_id=user.id, name="Acct", account_type="CFD", is_default=True)
    db.session.add(trade_account)
    db.session.flush()
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="12345",
        server="Test-Live",
        investor_password_encrypted="enc",
        vm_id=None,
    )
    db.session.add(mt5_account)
    db.session.commit()

    vm_id, error = resolve_mt5_cleanup_target_vm(mt5_account=mt5_account, target_vm_id="")
    assert vm_id is None
    assert "Choose a target VM" in error


def test_queue_mt5_account_cleanup_reports_missing_vm_skip(app_ctx, monkeypatch):
    from helpers.core import queue_mt5_account_cleanup
    from models import MT5Account, TradeAccount, User, db

    monkeypatch.setenv("FXJ_MT5_MULTI_VM", "1")

    user = User(username="cleanup-skip-user", email="cleanup-skip@example.com", password="hashed")
    db.session.add(user)
    db.session.flush()
    trade_account = TradeAccount(user_id=user.id, name="Acct", account_type="CFD", is_default=True)
    db.session.add(trade_account)
    db.session.flush()
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="12345",
        server="Test-Live",
        investor_password_encrypted="enc",
        terminal_path="C:\\terminals\\12345",
        appdata_hash="abc123",
        vm_id=None,
    )
    db.session.add(mt5_account)
    db.session.commit()

    warning = queue_mt5_account_cleanup(mt5_account=mt5_account, log_context="test")
    assert warning == MT5_DISPATCH_SKIPPED_MISSING_VM_MSG


def test_queue_mt5_account_cleanup_uses_target_vm_override(app_ctx, monkeypatch):
    from helpers.core import queue_mt5_account_cleanup
    from models import MT5Account, TradeAccount, User, db

    monkeypatch.setenv("FXJ_MT5_MULTI_VM", "1")
    captured = {}

    class _Task:
        name = "cleanup"

        @property
        def app(self):
            class _App:
                conf = type("Conf", (), {"broker_url": "memory://"})()

            return _App()

        def apply_async(self, **kwargs):
            captured.update(kwargs)
            return type("Result", (), {"id": "task-id"})()

    monkeypatch.setattr("celery_workers.mt5_setup_tasks.cleanup_mt5_terminal", _Task())

    user = User(username="cleanup-target-user", email="cleanup-target@example.com", password="hashed")
    db.session.add(user)
    db.session.flush()
    trade_account = TradeAccount(user_id=user.id, name="Acct", account_type="CFD", is_default=True)
    db.session.add(trade_account)
    db.session.flush()
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="12345",
        server="Test-Live",
        investor_password_encrypted="enc",
        terminal_path="C:\\terminals\\12345",
        appdata_hash="abc123",
        vm_id=None,
    )
    db.session.add(mt5_account)
    db.session.commit()

    warning = queue_mt5_account_cleanup(
        mt5_account=mt5_account,
        log_context="test",
        target_vm_id="VM-TARGET",
    )
    assert warning is None
    assert captured["queue"] == "mt5_setup.vm-target"
    assert captured["kwargs"]["target_vm_id"] == "VM-TARGET"
    assert captured["kwargs"]["mt5_account_id"] == mt5_account.id
    assert captured["kwargs"]["delete_account_row"] is False
    assert captured["kwargs"]["clear_cleanup_mark"] is False


def test_queue_mt5_accounts_cleanup_for_vm_queues_matching_accounts(app_ctx, monkeypatch):
    from helpers.core import queue_mt5_accounts_cleanup_for_vm
    from models import MT5Account, TradeAccount, User, db

    calls = []

    def _fake_queue(*, mt5_account, log_context, target_vm_id=None, delete_row_on_success=False):
        calls.append((mt5_account.id, target_vm_id, log_context))
        return None

    monkeypatch.setattr("helpers.core.queue_mt5_account_cleanup", _fake_queue)

    user = User(username="bulk-vm-user", email="bulk-vm@example.com", password="hashed")
    db.session.add(user)
    db.session.flush()
    trade_a = TradeAccount(user_id=user.id, name="Acct A", account_type="CFD", is_default=True)
    trade_b = TradeAccount(user_id=user.id, name="Acct B", account_type="CFD", is_default=False)
    trade_c = TradeAccount(user_id=user.id, name="Acct C", account_type="CFD", is_default=False)
    trade_d = TradeAccount(user_id=user.id, name="Acct D", account_type="CFD", is_default=False)
    db.session.add_all([trade_a, trade_b, trade_c, trade_d])
    db.session.flush()
    match = MT5Account(
        user_id=user.id,
        trade_account_id=trade_a.id,
        account_number="111",
        server="Test-Live",
        investor_password_encrypted="enc",
        terminal_path="C:\\terminals\\111",
        appdata_hash="hash111",
        vm_id="VM-A",
        is_active=False,
    )
    skip_vm = MT5Account(
        user_id=user.id,
        trade_account_id=trade_b.id,
        account_number="222",
        server="Test-Live",
        investor_password_encrypted="enc",
        terminal_path="C:\\terminals\\222",
        appdata_hash="hash222",
        vm_id="VM-B",
        is_active=False,
    )
    skip_empty = MT5Account(
        user_id=user.id,
        trade_account_id=trade_c.id,
        account_number="333",
        server="Test-Live",
        investor_password_encrypted="enc",
        vm_id="VM-A",
        is_active=False,
    )
    skip_active = MT5Account(
        user_id=user.id,
        trade_account_id=trade_d.id,
        account_number="444",
        server="Test-Live",
        investor_password_encrypted="enc",
        terminal_path="C:\\terminals\\444",
        appdata_hash="hash444",
        vm_id="VM-A",
        is_active=True,
    )
    db.session.add_all([match, skip_vm, skip_empty, skip_active])
    db.session.commit()

    queued, message = queue_mt5_accounts_cleanup_for_vm(
        vm_id="VM-A",
        mt5_accounts=[match, skip_vm, skip_empty, skip_active],
    )
    assert queued == 1
    assert "VM-A" in message
    assert "cleared their terminal runtime fields" in message
    assert "Skipped 1 active account" in message
    assert calls == [(match.id, "VM-A", "admin vm delete-files")]

    db.session.refresh(match)
    assert match.terminal_path is None
    assert match.appdata_hash is None
    assert match.is_active is False


def test_queue_mt5_accounts_cleanup_for_vm_skips_active_only(app_ctx, monkeypatch):
    from helpers.core import queue_mt5_accounts_cleanup_for_vm
    from models import MT5Account, TradeAccount, User, db

    monkeypatch.setattr(
        "helpers.core.queue_mt5_account_cleanup",
        lambda **kwargs: pytest.fail("should not queue cleanup for active accounts"),
    )

    user = User(username="bulk-active-user", email="bulk-active@example.com", password="hashed")
    db.session.add(user)
    db.session.flush()
    trade_account = TradeAccount(user_id=user.id, name="Active", account_type="CFD", is_default=True)
    db.session.add(trade_account)
    db.session.flush()
    active = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="999",
        server="Test-Live",
        investor_password_encrypted="enc",
        terminal_path="C:\\terminals\\999",
        appdata_hash="hash999",
        vm_id="VM-A",
        is_active=True,
    )
    db.session.add(active)
    db.session.commit()

    queued, message = queue_mt5_accounts_cleanup_for_vm(vm_id="VM-A", mt5_accounts=[active])
    assert queued == 0
    assert "active account" in message.lower()


def test_dispatch_mt5_sync_skips_missing_vm_id_when_multi_vm(monkeypatch, caplog):
    monkeypatch.setenv("FXJ_MT5_MULTI_VM", "1")

    class _Task:
        name = "sync"

        def apply_async(self, **kwargs):
            raise AssertionError("should not publish")

    assert dispatch_mt5_sync(_Task(), 123, account_vm_id=None, label="test_sync") is None


def test_dispatch_mt5_setup_builds_kwargs(monkeypatch):
    monkeypatch.setenv("FXJ_MT5_MULTI_VM", "1")
    captured = {}

    class _Task:
        name = "setup"

        @property
        def app(self):
            class _App:
                conf = type("Conf", (), {"broker_url": "memory://"})()

            return _App()

        def apply_async(self, **kwargs):
            captured.update(kwargs)
            return type("Result", (), {"id": "task-id"})()

    dispatch_mt5_setup(
        _Task(),
        99,
        target_vm_id="MYFXJOURNAL-SG",
        allow_failover=False,
        label="test_setup",
    )
    assert captured["queue"] == "mt5_setup.myfxjournal-sg"
    assert captured["kwargs"]["target_vm_id"] == "MYFXJOURNAL-SG"
    assert captured["kwargs"]["allow_failover"] is False


def test_dispatch_mt5_priority_scoped_queue(monkeypatch):
    monkeypatch.setenv("FXJ_MT5_MULTI_VM", "1")
    captured = {}

    class _Task:
        name = "bars"

        @property
        def app(self):
            class _App:
                conf = type("Conf", (), {"broker_url": "memory://"})()

            return _App()

        def apply_async(self, **kwargs):
            captured.update(kwargs)
            return type("Result", (), {"id": "task-id"})()

    dispatch_mt5_priority(
        _Task(),
        1,
        2,
        account_vm_id="MYFXJOURNAL-SG",
        label="test_priority",
    )
    assert captured["queue"] == "mt5_priority.myfxjournal-sg"
    assert captured["args"] == [1, 2]
