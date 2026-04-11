import os

from cryptography.fernet import Fernet

import celery_workers.mt5_sync_tasks as mt5_sync_module
from helpers.core import delete_users_with_related_data
from helpers.utils import encrypt_password
from models import MT5Account, TradeAccount, User, db


def _create_user_with_account(*, username, email, account_name="Main Account"):
    user = User(
        username=username,
        email=email,
        password="hashed-password",
        email_verified=True,
        signup_status="approved",
    )
    db.session.add(user)
    db.session.flush()

    trade_account = TradeAccount(
        user_id=user.id,
        name=account_name,
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.commit()
    return user, trade_account


def _create_mt5_account(*, user_id, trade_account_id, account_number):
    mt5_account = MT5Account(
        user_id=user_id,
        trade_account_id=trade_account_id,
        account_number=account_number,
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Server",
        is_active=True,
    )
    db.session.add(mt5_account)
    db.session.commit()
    return mt5_account


def _log_in_root_admin(client, *, email, username):
    os.environ["ADMIN_USER_EMAILS"] = email
    user, trade_account = _create_user_with_account(
        username=username,
        email=email,
    )
    user.is_admin = True
    db.session.commit()

    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username
        session_state["display_timezone"] = "UTC"
        session_state["active_trade_account_id"] = trade_account.id

    return user, trade_account


def test_mt5_account_remains_orphaned_after_trade_account_delete(app_ctx, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    user, trade_account = _create_user_with_account(
        username="orphan-trade-owner",
        email="orphan-trade-owner@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="77110001",
    )

    db.session.delete(trade_account)
    db.session.commit()
    db.session.expire_all()

    orphaned_account = db.session.get(MT5Account, mt5_account.id)

    assert orphaned_account is not None
    assert orphaned_account.user_id is None
    assert orphaned_account.trade_account_id is None
    assert orphaned_account.is_orphaned is True
    assert orphaned_account.is_cleanup_only is True
    assert orphaned_account.account_number == "cleanup-0001"
    assert orphaned_account.investor_password_encrypted is None
    assert orphaned_account.is_active is False
    assert orphaned_account.cleanup_marked_at is not None


def test_mt5_account_remains_orphaned_after_user_delete(app_ctx, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    user, trade_account = _create_user_with_account(
        username="orphan-user-owner",
        email="orphan-user-owner@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="77110002",
    )

    deleted_count = delete_users_with_related_data([user.id])
    db.session.commit()
    db.session.expire_all()

    orphaned_account = db.session.get(MT5Account, mt5_account.id)

    assert deleted_count == 1
    assert orphaned_account is not None
    assert orphaned_account.user_id is None
    assert orphaned_account.trade_account_id is None
    assert orphaned_account.is_orphaned is True
    assert orphaned_account.is_cleanup_only is True
    assert orphaned_account.account_number == "cleanup-0002"
    assert orphaned_account.investor_password_encrypted is None
    assert orphaned_account.is_active is False
    assert orphaned_account.cleanup_marked_at is not None


def test_admin_mt5_panel_marks_orphaned_accounts_and_blocks_actions(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    _log_in_root_admin(
        client,
        email="orphan-admin@example.com",
        username="orphan-admin",
    )
    managed_user, managed_trade_account = _create_user_with_account(
        username="orphan-panel-user",
        email="orphan-panel-user@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=managed_user.id,
        trade_account_id=managed_trade_account.id,
        account_number="77110003",
    )

    db.session.delete(managed_trade_account)
    db.session.commit()

    list_response = client.get("/dashboard/admin/access/mt5")
    setup_response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/setup",
        data={},
        follow_redirects=True,
    )
    sync_response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/sync",
        data={},
        follow_redirects=True,
    )

    assert list_response.status_code == 200
    assert b"cleanup-0003" in list_response.data
    assert b"Cleanup Pending" in list_response.data
    assert b"Cleanup-only record awaiting manual delete" in list_response.data
    assert setup_response.status_code == 200
    assert b"That MT5 record is cleanup-only now." in setup_response.data
    assert sync_response.status_code == 200
    assert b"That MT5 record is cleanup-only now." in sync_response.data


def test_sync_all_active_mt5_accounts_skips_orphaned_accounts(app_ctx, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    active_user, active_trade_account = _create_user_with_account(
        username="sync-active-user",
        email="sync-active-user@example.com",
    )
    active_account = _create_mt5_account(
        user_id=active_user.id,
        trade_account_id=active_trade_account.id,
        account_number="77110004",
    )

    orphan_user, orphan_trade_account = _create_user_with_account(
        username="sync-orphan-user",
        email="sync-orphan-user@example.com",
    )
    orphan_account = _create_mt5_account(
        user_id=orphan_user.id,
        trade_account_id=orphan_trade_account.id,
        account_number="77110005",
    )

    db.session.delete(orphan_trade_account)
    db.session.commit()
    db.session.expire_all()

    captured_ids = []

    def _fake_apply_async(*, args, queue, kwargs=None):
        captured_ids.append((args[0], queue))

    monkeypatch.setattr(mt5_sync_module.sync_mt5_account, "apply_async", _fake_apply_async)

    mt5_sync_module.sync_all_active_mt5_accounts.run()

    enqueued_mt5_ids = [args_id for args_id, q in captured_ids if q == "mt5_sync"]
    assert active_account.id in enqueued_mt5_ids
    assert orphan_account.id not in enqueued_mt5_ids
    scrubbed_account = db.session.get(MT5Account, orphan_account.id)
    assert scrubbed_account.is_orphaned is True
    assert scrubbed_account.is_cleanup_only is True
    assert scrubbed_account.account_number == "cleanup-0005"
