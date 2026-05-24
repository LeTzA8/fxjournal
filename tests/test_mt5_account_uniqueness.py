import os

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.exc import IntegrityError

import celery_workers.mt5_setup_tasks as mt5_setup_module
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


def test_admin_mt5_create_rejects_duplicate_trade_account_link(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    root_user, trade_account = _log_in_root_admin(
        client,
        email="mt5-unique-admin@example.com",
        username="mt5-unique-admin",
    )

    captured = []

    def _fake_dispatch(task, mt5_account_id, **options):
        captured.append((mt5_account_id, "mt5_setup"))

    monkeypatch.setattr("auth_account.dispatch_mt5_setup", _fake_dispatch)

    first_response = client.post(
        "/dashboard/admin/access/mt5/create",
        data={
            "user_id": str(root_user.id),
            "trade_account_id": str(trade_account.id),
            "account_number": "88110001",
            "investor_password": "investor-pass",
            "server": "Broker-Server",
        },
        follow_redirects=False,
    )

    second_response = client.post(
        "/dashboard/admin/access/mt5/create",
        data={
            "user_id": str(root_user.id),
            "trade_account_id": str(trade_account.id),
            "account_number": "88110002",
            "investor_password": "investor-pass",
            "server": "Broker-Server",
        },
        follow_redirects=True,
    )

    accounts = MT5Account.query.filter_by(trade_account_id=trade_account.id).order_by(MT5Account.id.asc()).all()

    assert first_response.status_code == 302
    assert second_response.status_code == 200
    assert len(accounts) == 1
    assert accounts[0].account_number == "88110001"
    assert captured == [(accounts[0].id, "mt5_setup")]
    assert b"already has MT5 account 88110001 linked to it" in second_response.data


def test_mt5_account_trade_account_id_is_unique_in_db(app_ctx, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    user, trade_account = _create_user_with_account(
        username="mt5-unique-db",
        email="mt5-unique-db@example.com",
    )

    first_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="88110003",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Server",
        is_active=False,
    )
    duplicate_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="88110004",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Server",
        is_active=False,
    )

    db.session.add(first_account)
    db.session.commit()

    db.session.add(duplicate_account)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
