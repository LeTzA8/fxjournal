from datetime import datetime, timezone

from helpers.admin_mt5_ops import build_admin_mt5_vm_overview
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
    monkeypatch.setattr(
        "helpers.admin_mt5_ops.list_worker_states",
        lambda worker_kind: [
            {
                "worker_id": "vm-east",
                "vm_id": "vm-east",
                "worker_hostname": "mt5-sync@vm-east",
                "last_celery_heartbeat_at": now.isoformat(timespec="seconds"),
                "vm_region": "US East",
                "vm_provider": "Hyonix",
            }
        ]
        if worker_kind == "mt5_sync"
        else [],
    )
    monkeypatch.setattr(
        "helpers.admin_mt5_ops.get_queue_depth",
        lambda queue_name: {"mt5_sync": 2, "mt5_priority": 1, "mt5_setup": 0}.get(queue_name, 0),
    )

    overview = build_admin_mt5_vm_overview(
        mt5_accounts=[account_a, account_b],
        mt5_statuses_by_account_id=statuses,
    )

    assert overview["vm_count"] == 2
    assert overview["queue_depths"]["mt5_sync"] == 2
    vm_by_id = {row["vm_id"]: row for row in overview["vms"]}
    assert vm_by_id["vm-east"]["account_count"] == 1
    assert vm_by_id["vm-east"]["region"] == "US East"
    assert vm_by_id["vm-east"]["sync_worker_online"] is True
    assert vm_by_id["unknown"]["account_count"] == 1
    assert vm_by_id["unknown"]["accounts"][0]["account_number"] == "222222"


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
