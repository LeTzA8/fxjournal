import html

from cryptography.fernet import Fernet

from helpers.utils import encrypt_password, utcnow_naive
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
    import os

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


def test_admin_mt5_main_table_excludes_cleanup_records(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _log_in_root_admin(
        client,
        email="mt5-view-main-root@example.com",
        username="mt5-view-main-root",
    )

    active_user, active_trade_account = _create_user_with_account(
        username="mt5-view-active-user",
        email="mt5-view-active-user@example.com",
        account_name="Active View Account",
    )
    failed_user, failed_trade_account = _create_user_with_account(
        username="mt5-view-failed-user",
        email="mt5-view-failed-user@example.com",
        account_name="Failed View Account",
    )
    cleanup_user, cleanup_trade_account = _create_user_with_account(
        username="mt5-view-cleanup-user",
        email="mt5-view-cleanup-user@example.com",
        account_name="Cleanup View Account",
    )

    db.session.add_all(
        [
            MT5Account(
                user_id=active_user.id,
                trade_account_id=active_trade_account.id,
                account_number="88110001",
                investor_password_encrypted=encrypt_password("investor-pass"),
                server="Broker-Active-View",
                is_active=True,
                connection_status=MT5Account.CONNECTION_STATUS_CONNECTED,
            ),
            MT5Account(
                user_id=failed_user.id,
                trade_account_id=failed_trade_account.id,
                account_number="88110002",
                investor_password_encrypted=encrypt_password("investor-pass"),
                server="Broker-Failed-View",
                is_active=False,
                connection_status=MT5Account.CONNECTION_STATUS_FAILED,
                connection_error_message="Invalid investor password.",
            ),
            MT5Account(
                user_id=cleanup_user.id,
                trade_account_id=cleanup_trade_account.id,
                account_number="88110003",
                investor_password_encrypted=encrypt_password("investor-pass"),
                server="Broker-Cleanup-View",
                is_active=False,
            ),
        ]
    )
    db.session.commit()

    cleanup_account = MT5Account.query.filter_by(account_number="88110003").one()
    cleanup_account.mark_for_cleanup()
    db.session.commit()

    response = client.get("/dashboard/admin/access/mt5")
    response_text = html.unescape(response.get_data(as_text=True))

    assert response.status_code == 200
    assert "88110001" in response_text
    assert "88110002" in response_text
    assert "88110003" not in response_text
    assert "cleanup-0003" not in response_text
    assert "Cleanup Records (1)" in response_text


def test_admin_mt5_cleanup_tab_shows_tombstone_rows(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _log_in_root_admin(
        client,
        email="mt5-view-cleanup-root@example.com",
        username="mt5-view-cleanup-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-view-cleanup-only-user",
        email="mt5-view-cleanup-only-user@example.com",
        account_name="Cleanup Tab Account",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="88110010",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Cleanup-Tab",
        terminal_path=r"C:\MT5 User Terminals\tombstone\terminal64.exe",
        appdata_hash="TOMBSTONEHASH",
        is_active=False,
    )
    db.session.add(mt5_account)
    db.session.commit()
    mt5_account.mark_for_cleanup()
    db.session.commit()

    response = client.get("/dashboard/admin/access/mt5?view=cleanup")
    response_text = html.unescape(response.get_data(as_text=True))

    assert response.status_code == 200
    assert "Cleanup Records" in response_text
    assert "cleanup-0010" in response_text
    assert "Queue cleanup" in response_text
    assert "Setup Terminal" not in response_text
    assert "Trigger Sync" not in response_text
    assert "Recalibrate times" not in response_text
    assert "Backfill Bars" not in response_text


def test_admin_mt5_cleanup_tab_shows_credentials_removed_status(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _log_in_root_admin(
        client,
        email="mt5-view-status-root@example.com",
        username="mt5-view-status-root",
    )

    mt5_account = MT5Account(
        user_id=None,
        trade_account_id=None,
        account_number="cleanup-88110011",
        investor_password_encrypted=None,
        server="Broker-Cleanup-Complete",
        is_active=False,
        cleanup_marked_at=utcnow_naive(),
    )
    db.session.add(mt5_account)
    db.session.commit()

    response = client.get("/dashboard/admin/access/mt5?view=cleanup")
    response_text = html.unescape(response.get_data(as_text=True))

    assert response.status_code == 200
    assert "Credentials Removed" in response_text
    assert "Delete record" in response_text
