from datetime import datetime
from itertools import count

from models import Trade, TradeAccount, User, db


_UNIQUE_COUNTER = count(1)


def _unique_suffix():
    return next(_UNIQUE_COUNTER)


def _create_user(*, username, email, is_admin=False):
    user = User(
        username=username,
        email=email,
        password="hashed-password",
        email_verified=True,
        signup_status="approved",
        is_admin=is_admin,
    )
    db.session.add(user)
    db.session.flush()
    return user


def _create_trade_account(*, user_id, name, account_type="CFD", is_default=False):
    account = TradeAccount(
        user_id=user_id,
        name=name,
        account_type=account_type,
        is_default=is_default,
    )
    db.session.add(account)
    db.session.flush()
    return account


def _create_trade(*, user_id, trade_account_id, symbol):
    trade = Trade(
        user_id=user_id,
        trade_account_id=trade_account_id,
        symbol=symbol,
        side="BUY",
        entry_price=1.1000,
        exit_price=1.1015,
        lot_size=1.0,
        pnl=150.0,
        opened_at=datetime(2026, 4, 7, 9, 0, 0),
        closed_at=datetime(2026, 4, 7, 10, 0, 0),
    )
    db.session.add(trade)
    db.session.flush()
    return trade


def _login_as(client, user, *, active_trade_account_id=None):
    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username
        session_state["display_timezone"] = "UTC"
        if active_trade_account_id is not None:
            session_state["active_trade_account_id"] = active_trade_account_id


def test_root_admin_support_view_uses_target_user_context_across_pages(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"support-root-{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)

    root = _create_user(
        username=f"support-root-{suffix}",
        email=root_email,
        is_admin=True,
    )
    target = _create_user(
        username=f"support-target-{suffix}",
        email=f"support-target-{suffix}@example.com",
    )
    root_account = _create_trade_account(
        user_id=root.id,
        name="Root Account",
        is_default=True,
    )
    target_account = _create_trade_account(
        user_id=target.id,
        name="Target Account",
        is_default=True,
    )
    root_trade = _create_trade(
        user_id=root.id,
        trade_account_id=root_account.id,
        symbol="GBPUSD",
    )
    target_trade = _create_trade(
        user_id=target.id,
        trade_account_id=target_account.id,
        symbol="EURUSD",
    )
    db.session.commit()

    _login_as(client, root, active_trade_account_id=root_account.id)

    response = client.get(
        f"/dashboard/admin/users/{target.id}/view-dashboard",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")

    with client.session_transaction() as session_state:
        assert session_state["support_view_target_user_id"] == target.id
        assert session_state["support_view_admin_user_id"] == root.id
        assert session_state["active_trade_account_id"] == root_account.id

    dashboard_response = client.get("/dashboard")
    assert dashboard_response.status_code == 200
    assert b"Read-Only Support View" in dashboard_response.data
    assert target.username.encode() in dashboard_response.data

    trades_response = client.get("/dashboard/trades")
    assert trades_response.status_code == 200
    assert target_trade.symbol.encode() in trades_response.data
    assert root_trade.symbol.encode() not in trades_response.data

    analytics_response = client.get("/dashboard/analytics")
    assert analytics_response.status_code == 200
    assert b"Read-Only Support View" in analytics_response.data

    detail_response = client.get(f"/dashboard/trades/{target_trade.pubkey}")
    assert detail_response.status_code == 200
    assert target_trade.symbol.encode() in detail_response.data

    trade_accounts_response = client.get("/dashboard/trade-accounts")
    assert trade_accounts_response.status_code == 200
    assert target_account.name.encode() in trade_accounts_response.data
    assert root_account.name.encode() not in trade_accounts_response.data

    strategies_response = client.get("/dashboard/strategies")
    assert strategies_response.status_code == 200
    assert b"Read-Only Support View" in strategies_response.data

    account_response = client.get("/account")
    assert account_response.status_code == 200
    assert target.email.encode() in account_response.data


def test_support_view_blocks_user_mutations(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"support-block-root-{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)

    root = _create_user(
        username=f"support-block-root-{suffix}",
        email=root_email,
        is_admin=True,
    )
    target = _create_user(
        username=f"support-block-target-{suffix}",
        email=f"support-block-target-{suffix}@example.com",
    )
    target_account = _create_trade_account(
        user_id=target.id,
        name="Only Target Account",
        is_default=True,
    )
    db.session.commit()

    _login_as(client, root)
    client.get(f"/dashboard/admin/users/{target.id}/view-dashboard", follow_redirects=False)

    account_count_before = TradeAccount.query.filter_by(user_id=target.id).count()

    response = client.post(
        "/dashboard/trade-accounts",
        data={
            "name": "Should Not Save",
            "account_type": "CFD",
        },
        headers={"Referer": "http://localhost/dashboard/trade-accounts"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Support view is read-only. Exit support view before making changes." in response.data
    assert TradeAccount.query.filter_by(user_id=target.id).count() == account_count_before
    assert TradeAccount.query.filter_by(user_id=target.id, name="Should Not Save").first() is None
    assert TradeAccount.query.filter_by(id=target_account.id).first() is not None


def test_exit_support_view_clears_support_session(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"support-exit-root-{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)

    root = _create_user(
        username=f"support-exit-root-{suffix}",
        email=root_email,
        is_admin=True,
    )
    target = _create_user(
        username=f"support-exit-target-{suffix}",
        email=f"support-exit-target-{suffix}@example.com",
    )
    _create_trade_account(
        user_id=target.id,
        name="Exit Target Account",
        is_default=True,
    )
    db.session.commit()

    _login_as(client, root)
    client.get(f"/dashboard/admin/users/{target.id}/view-dashboard", follow_redirects=False)

    exit_response = client.get("/dashboard/admin/support-view/exit", follow_redirects=False)

    assert exit_response.status_code == 302
    assert exit_response.headers["Location"].endswith("/dashboard/admin/access/users")

    with client.session_transaction() as session_state:
        assert "support_view_target_user_id" not in session_state
        assert "support_view_admin_user_id" not in session_state
        assert "support_view_active_trade_account_id" not in session_state
