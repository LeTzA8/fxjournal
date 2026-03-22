import os

import os

import pytest
from cryptography.fernet import Fernet

from celery_app import celery
from helpers.core import delete_users_with_related_data
from helpers.utils import decrypt_password, encrypt_password
from models import MT5Account, Trade, TradeAccount, User, db


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


def _log_in_root_admin(client, *, email="root-admin@example.com", username="root-admin"):
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


def _create_mt5_account(*, user_id, trade_account_id, account_number="12345678"):
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


def test_encrypt_password_round_trip_requires_key(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError):
        encrypt_password("secret")

    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)

    encrypted = encrypt_password("secret")

    assert encrypted != "secret"
    assert decrypt_password(encrypted) == "secret"


def test_internal_mt5_sync_requires_shared_secret(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="sync-user",
        email="sync-user@example.com",
    )
    mt5_account = _create_mt5_account(user_id=user.id, trade_account_id=trade_account.id)

    payload = {"mt5_account_id": mt5_account.id, "trades": []}

    missing_secret = client.post("/api/internal/mt5/sync", json=payload)
    wrong_secret = client.post(
        "/api/internal/mt5/sync",
        json=payload,
        headers={"X-Sync-Secret": "wrong"},
    )

    assert missing_secret.status_code == 403
    assert wrong_secret.status_code == 403


def test_internal_mt5_sync_saves_and_skips_duplicates(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="mt5-sync-user",
        email="mt5-sync@example.com",
    )
    second_account = TradeAccount(
        user_id=user.id,
        name="Second CFD Account",
        account_type="CFD",
        is_default=False,
    )
    db.session.add(second_account)
    db.session.commit()

    first_mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="11111111",
    )
    second_mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=second_account.id,
        account_number="22222222",
    )

    payload = {
        "mt5_account_id": first_mt5_account.id,
        "trades": [
            {
                "symbol": "EURUSD",
                "side": "buy",
                "entry_price": 1.085,
                "exit_price": 1.09,
                "lot_size": 0.01,
                "pnl": 50.0,
                "commission": -0.5,
                "swap": 0.0,
                "stop_loss": 1.08,
                "take_profit": 1.095,
                "opened_at": "2026-03-21T08:00:00+00:00",
                "closed_at": "2026-03-21T10:00:00+00:00",
                "mt5_position": 12345678,
                "trade_note": "",
            }
        ],
    }

    first_response = client.post(
        "/api/internal/mt5/sync",
        json=payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )
    duplicate_response = client.post(
        "/api/internal/mt5/sync",
        json=payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )
    second_account_response = client.post(
        "/api/internal/mt5/sync",
        json={**payload, "mt5_account_id": second_mt5_account.id},
        headers={"X-Sync-Secret": "sync-secret"},
    )

    assert first_response.status_code == 200
    assert first_response.get_json() == {"saved": 1, "updated": 0, "skipped": 0, "errors": 0}
    assert duplicate_response.status_code == 200
    assert duplicate_response.get_json() == {"saved": 0, "updated": 0, "skipped": 1, "errors": 0}
    assert second_account_response.status_code == 200
    assert second_account_response.get_json() == {"saved": 1, "updated": 0, "skipped": 0, "errors": 0}

    first_trade = Trade.query.filter_by(trade_account_id=trade_account.id, mt5_position="12345678").one()
    second_trade = Trade.query.filter_by(trade_account_id=second_account.id, mt5_position="12345678").one()
    first_mt5_account = db.session.get(MT5Account, first_mt5_account.id)
    second_mt5_account = db.session.get(MT5Account, second_mt5_account.id)

    assert first_trade.import_signature is None
    assert first_trade.import_dedupe_key is None
    assert first_trade.source_timezone == "UTC"
    assert second_trade.import_signature is None
    assert second_trade.import_dedupe_key is None
    assert first_mt5_account.last_synced_at is not None
    assert second_mt5_account.last_synced_at is not None


def test_internal_mt5_sync_updates_existing_open_trade_when_close_arrives(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="mt5-open-close-user",
        email="mt5-open-close@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="12121212",
    )

    open_payload = {
        "mt5_account_id": mt5_account.id,
        "trades": [
            {
                "symbol": "EURUSD",
                "side": "buy",
                "entry_price": 1.085,
                "exit_price": None,
                "lot_size": 0.01,
                "pnl": None,
                "commission": -0.25,
                "swap": 0.0,
                "stop_loss": None,
                "take_profit": None,
                "opened_at": "2026-03-21T08:00:00+00:00",
                "closed_at": None,
                "mt5_position": 99999999,
                "trade_note": "open",
                "is_open": True,
            }
        ],
    }
    closed_payload = {
        "mt5_account_id": mt5_account.id,
        "trades": [
            {
                "symbol": "EURUSD",
                "side": "buy",
                "entry_price": 1.085,
                "exit_price": 1.09,
                "lot_size": 0.01,
                "pnl": 48.5,
                "commission": -0.5,
                "swap": -0.1,
                "stop_loss": None,
                "take_profit": None,
                "opened_at": "2026-03-21T08:00:00+00:00",
                "closed_at": "2026-03-21T10:00:00+00:00",
                "mt5_position": 99999999,
                "trade_note": "closed",
                "is_open": False,
            }
        ],
    }

    open_response = client.post(
        "/api/internal/mt5/sync",
        json=open_payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )
    close_response = client.post(
        "/api/internal/mt5/sync",
        json=closed_payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )

    trade = Trade.query.filter_by(
        trade_account_id=trade_account.id,
        mt5_position="99999999",
    ).one()

    assert open_response.status_code == 200
    assert open_response.get_json() == {"saved": 1, "updated": 0, "skipped": 0, "errors": 0}
    assert close_response.status_code == 200
    assert close_response.get_json() == {"saved": 0, "updated": 1, "skipped": 0, "errors": 0}
    assert trade.exit_price == pytest.approx(1.09)
    assert trade.pnl == pytest.approx(48.5)
    assert trade.commission == pytest.approx(-0.5)
    assert trade.swap == pytest.approx(-0.1)
    assert trade.closed_at is not None


def test_admin_mt5_create_list_setup_and_trigger_sync(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    root_user, trade_account = _log_in_root_admin(client)

    create_captured = {}

    def _fake_create_apply_async(*, args, queue):
        create_captured["args"] = args
        create_captured["queue"] = queue

    import celery_workers.mt5_setup as mt5_setup_module

    monkeypatch.setattr(mt5_setup_module.setup_mt5_terminal, "apply_async", _fake_create_apply_async)

    create_response = client.post(
        "/dashboard/admin/access/mt5/create",
        data={
            "user_id": str(root_user.id),
            "trade_account_id": str(trade_account.id),
            "account_number": "33333333",
            "investor_password": "investor-pass",
            "server": "Broker-Server",
        },
        follow_redirects=False,
    )

    mt5_account = MT5Account.query.filter_by(account_number="33333333").one()
    list_response = client.get("/dashboard/admin/access/mt5")

    setup_captured = {}

    def _fake_setup_apply_async(*, args, queue):
        setup_captured["args"] = args
        setup_captured["queue"] = queue

    monkeypatch.setattr(mt5_setup_module.setup_mt5_terminal, "apply_async", _fake_setup_apply_async)

    setup_response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/setup",
        data={},
        follow_redirects=False,
    )

    sync_captured = {}

    def _fake_sync_apply_async(*, args, queue):
        sync_captured["args"] = args
        sync_captured["queue"] = queue

    import celery_workers.mt5_sync as mt5_sync_module
    monkeypatch.setattr(mt5_sync_module.sync_mt5_account, "apply_async", _fake_sync_apply_async)

    trigger_response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/sync",
        data={},
        follow_redirects=False,
    )

    mt5_account = db.session.get(MT5Account, mt5_account.id)

    assert create_response.status_code == 302
    assert mt5_account.investor_password_encrypted != "investor-pass"
    assert decrypt_password(mt5_account.investor_password_encrypted) == "investor-pass"
    assert mt5_account.is_active is False
    assert mt5_account.terminal_path is None
    assert mt5_account.appdata_hash is None
    assert create_captured["queue"] == "mt5_sync"
    assert create_captured["args"] == [mt5_account.id]
    assert list_response.status_code == 200
    assert b"33333333" in list_response.data
    assert b"Setup Terminal" in list_response.data
    assert b"Terminal not set up yet" in list_response.data
    assert b"AppData hash pending" in list_response.data
    assert setup_response.status_code == 302
    assert setup_captured["queue"] == "mt5_sync"
    assert setup_captured["args"] == [mt5_account.id]
    assert trigger_response.status_code == 302
    assert sync_captured == {}


def test_admin_mt5_create_persists_inactive_account_when_setup_queue_fails(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    root_user, trade_account = _log_in_root_admin(
        client,
        email="root-admin-queue-fail@example.com",
        username="root-admin-queue-fail",
    )

    import celery_workers.mt5_setup as mt5_setup_module

    def _failing_apply_async(*, args, queue):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(mt5_setup_module.setup_mt5_terminal, "apply_async", _failing_apply_async)

    response = client.post(
        "/dashboard/admin/access/mt5/create",
        data={
            "user_id": str(root_user.id),
            "trade_account_id": str(trade_account.id),
            "account_number": "66666666",
            "investor_password": "investor-pass",
            "server": "Broker-Server",
        },
        follow_redirects=True,
    )

    mt5_account = MT5Account.query.filter_by(account_number="66666666").one()

    assert response.status_code == 200
    assert b"Account created but setup could not be queued. Click Setup Terminal to retry." in response.data
    assert mt5_account.is_active is False
    assert mt5_account.terminal_path is None
    assert mt5_account.appdata_hash is None


def test_mt5_account_becomes_orphaned_with_trade_account_delete(app_ctx, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)

    user, trade_account = _create_user_with_account(
        username="mt5-owner",
        email="mt5-owner@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="44444444",
    )
    mt5_account_id = mt5_account.id

    db.session.delete(trade_account)
    db.session.commit()
    db.session.expire_all()

    orphaned_account = db.session.get(MT5Account, mt5_account_id)

    assert orphaned_account is not None
    assert orphaned_account.user_id == user.id
    assert orphaned_account.trade_account_id is None
    assert orphaned_account.is_orphaned is True
    assert db.session.get(User, user.id) is not None


def test_mt5_account_becomes_orphaned_with_user_delete(app_ctx, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)

    user, trade_account = _create_user_with_account(
        username="mt5-user-delete",
        email="mt5-user-delete@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="55555555",
    )
    mt5_account_id = mt5_account.id

    deleted_count = delete_users_with_related_data([user.id])
    db.session.commit()
    db.session.expire_all()

    assert deleted_count == 1
    assert db.session.get(User, user.id) is None
    orphaned_account = db.session.get(MT5Account, mt5_account_id)
    assert orphaned_account is not None
    assert orphaned_account.user_id is None
    assert orphaned_account.trade_account_id is None
    assert orphaned_account.is_orphaned is True


def test_celery_includes_mt5_modules():
    includes = set(celery.conf.include or [])
    assert "celery_workers.mt5_sync" in includes
    assert "celery_workers.mt5_setup" in includes
