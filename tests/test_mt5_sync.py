import os

import pytest
from cryptography.fernet import Fernet

from celery_app import celery
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


def _log_in_root_admin(client, *, email="root-admin@example.com"):
    os.environ["ADMIN_USER_EMAILS"] = email
    user, trade_account = _create_user_with_account(
        username="root-admin",
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
    assert first_response.get_json() == {"saved": 1, "skipped": 0, "errors": 0}
    assert duplicate_response.status_code == 200
    assert duplicate_response.get_json() == {"saved": 0, "skipped": 1, "errors": 0}
    assert second_account_response.status_code == 200
    assert second_account_response.get_json() == {"saved": 1, "skipped": 0, "errors": 0}

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


def test_admin_mt5_create_list_and_trigger_sync(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    root_user, trade_account = _log_in_root_admin(client)

    create_response = client.post(
        "/dashboard/admin/access/mt5/create",
        data={
            "user_id": str(root_user.id),
            "trade_account_id": str(trade_account.id),
            "account_number": "33333333",
            "investor_password": "investor-pass",
            "server": "Broker-Server",
            "terminal_path": r"C:\MT5\terminal64.exe",
        },
        follow_redirects=False,
    )

    mt5_account = MT5Account.query.filter_by(account_number="33333333").one()
    list_response = client.get("/dashboard/admin/access/mt5")

    captured = {}

    def _fake_apply_async(*, args, queue):
        captured["args"] = args
        captured["queue"] = queue

    import celery_workers.mt5_sync as mt5_sync_module

    monkeypatch.setattr(mt5_sync_module.sync_mt5_account, "apply_async", _fake_apply_async)

    trigger_response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/sync",
        data={},
        follow_redirects=False,
    )

    assert create_response.status_code == 302
    assert mt5_account.investor_password_encrypted != "investor-pass"
    assert decrypt_password(mt5_account.investor_password_encrypted) == "investor-pass"
    assert list_response.status_code == 200
    assert b"33333333" in list_response.data
    assert trigger_response.status_code == 302
    assert captured["queue"] == "mt5_sync"
    assert captured["args"][0] == mt5_account.id
    assert captured["args"][4] == "investor-pass"


def test_celery_includes_mt5_sync_module():
    includes = set(celery.conf.include or [])
    assert "celery_workers.mt5_sync" in includes
