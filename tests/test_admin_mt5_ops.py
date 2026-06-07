from datetime import datetime, timezone

from celery_workers.worker_monitor import get_vm_id
from helpers.admin_mt5_ops import (
    build_admin_mt5_vm_overview,
    resolve_admin_target_vm_id,
)
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


def test_get_vm_id_prefers_computername_over_celery_hostname(monkeypatch):
    monkeypatch.delenv("VM_ID", raising=False)
    monkeypatch.setenv("COMPUTERNAME", "MYFXJOURNAL-SG")
    assert get_vm_id("mt5-sync@MYFXJOURNAL-SG") == "MYFXJOURNAL-SG"


def test_build_admin_mt5_vm_overview_groups_accounts_by_vm(app_ctx, monkeypatch):
    user_a, trade_a = _create_user_with_account(username="vm-user-a", email="vm-user-a@example.com")
    user_b, trade_b = _create_user_with_account(username="vm-user-b", email="vm-user-b@example.com")

    account_a = MT5Account(
        user_id=user_a.id,
        trade_account_id=trade_a.id,
        account_number="111111",
        server="Broker-A",
        is_active=True,
        vm_id="vm-east",
    )
    account_b = MT5Account(
        user_id=user_b.id,
        trade_account_id=trade_b.id,
        account_number="222222",
        server="Broker-B",
        is_active=True,
        vm_id=None,
    )
    db.session.add_all([account_a, account_b])
    db.session.commit()

    statuses = {
        account_a.id: {"label": "Active", "chip_class": "success-chip"},
        account_b.id: {"label": "Active", "chip_class": "success-chip"},
    }

    now = datetime.now(timezone.utc)
    monkeypatch.setenv("FXJ_MT5_SETUP_VM_IDS", "vm-east")
    monkeypatch.setattr(
        "helpers.admin_mt5_ops.list_mt5_worker_states",
        lambda: {
            "mt5_sync": [
                {
                    "worker_id": "vm-east",
                    "vm_id": "vm-east",
                    "worker_hostname": "mt5-sync@vm-east",
                    "last_celery_heartbeat_at": now.isoformat(timespec="seconds"),
                    "vm_region": "US East",
                    "vm_provider": "Hyonix",
                }
            ],
            "mt5_setup": [],
        },
    )
    monkeypatch.setattr(
        "helpers.admin_mt5_ops.get_queue_depths",
        lambda queue_names: {
            name: {
                "mt5_sync.vm-east": 2,
                "mt5_priority.vm-east": 1,
            }.get(name, 0)
            for name in queue_names
        },
    )

    overview = build_admin_mt5_vm_overview(
        mt5_accounts=[account_a, account_b],
        mt5_statuses_by_account_id=statuses,
    )

    assert overview["vm_count"] == 1
    assert overview["scoped_queue_depths"]["vm-east"]["mt5_sync"] == 2
    vm_by_id = {row["vm_id"]: row for row in overview["vms"]}
    assert vm_by_id["vm-east"]["account_count"] == 1
    assert vm_by_id["vm-east"]["region"] == "US East"
    assert vm_by_id["vm-east"]["sync_worker_online"] is True
    assert "unknown" not in vm_by_id
    assert "vm-east" in overview["selectable_vm_ids"]
    assert overview["show_vm_target_selector"] is bool(overview["selectable_vm_ids"])


def test_build_admin_mt5_vm_overview_exposes_vm_selector_when_multiple_vms(app_ctx, monkeypatch):
    monkeypatch.setenv("FXJ_MT5_SETUP_VM_IDS", "VM-A,VM-B")
    monkeypatch.setattr(
        "helpers.admin_mt5_ops.list_mt5_worker_states",
        lambda: {"mt5_sync": [], "mt5_setup": []},
    )
    monkeypatch.setattr(
        "helpers.admin_mt5_ops.get_queue_depths",
        lambda queue_names: {name: 0 for name in queue_names},
    )

    overview = build_admin_mt5_vm_overview(
        mt5_accounts=[],
        mt5_statuses_by_account_id={},
    )

    assert overview["selectable_vm_ids"] == ["VM-A", "VM-B"]
    assert overview["show_vm_target_selector"] is True


def test_resolve_admin_target_vm_id_rejects_unknown_vm(app_ctx, monkeypatch):
    monkeypatch.setenv("FXJ_MT5_SETUP_VM_IDS", "VM-A,VM-B")

    vm_id, error = resolve_admin_target_vm_id("VM-Z", selectable_vm_ids=["VM-A", "VM-B"])
    assert vm_id is None
    assert "Unknown target VM" in error

    vm_id, error = resolve_admin_target_vm_id("", selectable_vm_ids=["VM-A", "VM-B"])
    assert vm_id is None
    assert error is None

    vm_id, error = resolve_admin_target_vm_id("VM-B", selectable_vm_ids=["VM-A", "VM-B"])
    assert vm_id == "VM-B"
    assert error is None

    vm_id, error = resolve_admin_target_vm_id(
        "mt5-sync@MYFXJOURNAL-SG",
        selectable_vm_ids=["MYFXJOURNAL-SG"],
    )
    assert vm_id == "MYFXJOURNAL-SG"
    assert error is None


def test_build_admin_mt5_vm_overview_merges_celery_hostname_worker_buckets(app_ctx, monkeypatch):
    user, trade_account = _create_user_with_account(
        username="vm-sg-user",
        email="vm-sg-user@example.com",
    )
    account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="555555",
        server="Broker-SG",
        is_active=True,
        vm_id="MYFXJOURNAL-SG",
    )
    db.session.add(account)
    db.session.commit()

    now = datetime.now(timezone.utc)
    stale = datetime(2026, 5, 10, 18, 35, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "helpers.admin_mt5_ops.list_mt5_worker_states",
        lambda: {
            "mt5_sync": [
                {
                    "worker_id": "MYFXJOURNAL-SG",
                    "vm_id": "MYFXJOURNAL-SG",
                    "worker_hostname": "mt5-sync@MYFXJOURNAL-SG",
                    "last_celery_heartbeat_at": now.isoformat(timespec="seconds"),
                },
                {
                    "worker_id": "mt5-sync@MYFXJOURNAL-SG",
                    "worker_hostname": "mt5-sync@MYFXJOURNAL-SG",
                    "last_celery_heartbeat_at": stale.isoformat(timespec="seconds"),
                },
            ],
            "mt5_setup": [],
        },
    )
    monkeypatch.setattr(
        "helpers.admin_mt5_ops.get_queue_depths",
        lambda queue_names: {name: 0 for name in queue_names},
    )

    overview = build_admin_mt5_vm_overview(
        mt5_accounts=[account],
        mt5_statuses_by_account_id={account.id: {"label": "Active", "chip_class": "success-chip"}},
    )

    vm_ids = [row["vm_id"] for row in overview["vms"]]
    assert vm_ids == ["MYFXJOURNAL-SG"]
    assert overview["selectable_vm_ids"] == ["MYFXJOURNAL-SG"]
    sg_row = overview["vms"][0]
    assert sg_row["account_count"] == 1
    assert sg_row["sync_worker_online"] is True


def test_load_admin_mt5_monitor_snapshot_uses_cache(app_ctx, monkeypatch):
    fetch_calls = {"count": 0}

    def fake_fetch(*, mt5_accounts):
        fetch_calls["count"] += 1
        return {
            "worker_states_by_vm": {"VM-A": {"mt5_sync": {"worker_id": "VM-A"}}},
            "queue_depths": {"mt5_sync": 4, "mt5_priority": 0, "mt5_setup": 0},
            "scoped_queue_depths": {},
            "monitor_available": True,
        }

    monkeypatch.setattr("helpers.admin_mt5_ops._fetch_admin_mt5_monitor_snapshot", fake_fetch)
    monkeypatch.setattr(
        "helpers.admin_mt5_ops.get_admin_mt5_monitor_cache",
        lambda: None,
    )
    captured = {}

    def capture_set(data, ttl=None):
        captured["payload"] = data

    monkeypatch.setattr("helpers.admin_mt5_ops.set_admin_mt5_monitor_cache", capture_set)

    from helpers.admin_mt5_ops import load_admin_mt5_monitor_snapshot

    first = load_admin_mt5_monitor_snapshot(mt5_accounts=[])
    assert fetch_calls["count"] == 1
    assert first["queue_depths"]["mt5_sync"] == 4

    monkeypatch.setattr(
        "helpers.admin_mt5_ops.get_admin_mt5_monitor_cache",
        lambda: captured["payload"],
    )
    second = load_admin_mt5_monitor_snapshot(mt5_accounts=[])
    assert fetch_calls["count"] == 1
    assert second["queue_depths"]["mt5_sync"] == 4


def test_build_admin_mt5_vm_overview_fetches_monitor_once(app_ctx, monkeypatch):
    monitor_calls = {"count": 0}

    def fake_load(*, mt5_accounts):
        monitor_calls["count"] += 1
        return {
            "worker_states_by_vm": {},
            "queue_depths": {"mt5_sync": 0, "mt5_priority": 0, "mt5_setup": 0},
            "scoped_queue_depths": {},
            "monitor_available": True,
        }

    monkeypatch.setattr("helpers.admin_mt5_ops.load_admin_mt5_monitor_snapshot", fake_load)

    build_admin_mt5_vm_overview(mt5_accounts=[], mt5_statuses_by_account_id={})

    assert monitor_calls["count"] == 1


def test_admin_users_default_status_is_approved(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "root@example.com")

    root = User(
        username="rootadmin",
        email="root@example.com",
        password="hashed",
        email_verified=True,
        signup_status="approved",
        is_admin=True,
    )
    approved = User(
        username="approved-user",
        email="approved@example.com",
        password="hashed",
        email_verified=True,
        signup_status="approved",
    )
    pending = User(
        username="pending-user",
        email="pending@example.com",
        password="hashed",
        email_verified=True,
        signup_status="pending",
    )
    db.session.add_all([root, approved, pending])
    db.session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = root.id

    response = client.get("/dashboard/admin/access/users")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'name="status" value="approved"' in body
    assert 'class="admin-filter is-active">Approved</a>' in body
    assert "approved-user" in body
    assert body.count("pending-user") == 1
    assert "Pending Spotlight" in body
