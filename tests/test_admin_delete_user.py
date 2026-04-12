from datetime import datetime
from itertools import count

from models import Trade, TradeAccount, User, db


_UNIQUE_COUNTER = count(1)


def _unique_suffix():
    return next(_UNIQUE_COUNTER)


def _create_user(*, username, email, signup_status="approved", is_admin=False):
    user = User(
        username=username,
        email=email,
        password="hashed-password",
        email_verified=True,
        signup_status=signup_status,
        is_admin=is_admin,
        approved_at=datetime(2026, 3, 12, 9, 0, 0) if signup_status == "approved" else None,
    )
    db.session.add(user)
    db.session.flush()
    return user


def _login_as(client, user):
    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username


def test_root_admin_delete_user_removes_user_and_trades(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"root-del-{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root = _create_user(username=f"rootadmin-del-{suffix}", email=root_email, is_admin=True)
    target = _create_user(
        username=f"spam-{suffix}",
        email=f"spam-{suffix}@example.com",
    )
    account = TradeAccount(user_id=target.id, name="Main", is_default=True)
    db.session.add(account)
    db.session.flush()
    trade = Trade(
        user_id=target.id,
        trade_account_id=account.id,
        symbol="EURUSD",
        side="BUY",
        opened_at=datetime(2026, 3, 10, 12, 0, 0),
    )
    db.session.add(trade)
    db.session.commit()

    _login_as(client, root)
    response = client.post(
        f"/dashboard/admin/access/users/{target.id}/delete",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard/admin/access/users")

    assert User.query.filter_by(id=target.id).first() is None
    assert TradeAccount.query.filter_by(id=account.id).first() is None
    assert Trade.query.filter_by(id=trade.id).first() is None


def test_root_admin_cannot_delete_self(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"root-self-{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root = _create_user(username=f"rootadmin-self-{suffix}", email=root_email, is_admin=True)
    db.session.commit()
    _login_as(client, root)
    response = client.post(
        f"/dashboard/admin/access/users/{root.id}/delete",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert User.query.filter_by(id=root.id).first() is not None


def test_root_admin_cannot_delete_other_root_email_user(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"root-a-{suffix}@example.com"
    other_root_email = f"root-b-{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", f"{root_email},{other_root_email}")
    root = _create_user(username=f"rootadmin-a-{suffix}", email=root_email, is_admin=True)
    other = _create_user(username=f"rootadmin-b-{suffix}", email=other_root_email, is_admin=True)
    db.session.commit()
    _login_as(client, root)
    response = client.post(
        f"/dashboard/admin/access/users/{other.id}/delete",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert User.query.filter_by(id=other.id).first() is not None
