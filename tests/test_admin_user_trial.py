import re
from datetime import timedelta

import auth_account
from helpers.utils import utcnow_naive
from helpers import entitlements as ent
from models import MT5Account, TradeAccount, User, db


def _create_user(*, username, email, premium_trial_started_at=None, plan_grandfathered=False):
    user = User(
        username=username,
        email=email,
        password="hashed-password",
        email_verified=True,
        signup_status="approved",
        premium_trial_started_at=premium_trial_started_at,
        plan_grandfathered=plan_grandfathered,
    )
    db.session.add(user)
    db.session.flush()
    return user


def _login_as(client, user):
    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username


def _csrf_token(client):
    response = client.get("/dashboard/admin/access/users")
    assert response.status_code == 200
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match is not None
    return match.group(1).decode("utf-8")


def test_build_admin_user_trial_display_shows_grandfathered(app_ctx):
    user = _create_user(
        username="trial-grandfathered",
        email="trial-grandfathered@example.com",
        plan_grandfathered=True,
    )
    display = ent.build_admin_user_trial_display(user)
    assert display["summary"] == "Grandfathered"
    assert display["can_extend"] is False


def test_build_admin_user_trial_display_shows_active_dates(app_ctx):
    started = utcnow_naive() - timedelta(days=4)
    user = _create_user(
        username="trial-active",
        email="trial-active@example.com",
        premium_trial_started_at=started,
    )
    display = ent.build_admin_user_trial_display(user)
    assert display["state"] == "active"
    assert "Active" in display["summary"]
    assert display["started_label"] == started.strftime("%Y-%m-%d %H:%M UTC")
    assert display["ends_label"] == (started + timedelta(days=ent.PREMIUM_TRIAL_DAYS)).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    assert display["can_extend"] is False


def test_build_admin_user_trial_display_marks_expired_extendable(app_ctx):
    started = utcnow_naive() - timedelta(days=ent.PREMIUM_TRIAL_DAYS + 2)
    user = _create_user(
        username="trial-expired",
        email="trial-expired@example.com",
        premium_trial_started_at=started,
    )
    display = ent.build_admin_user_trial_display(user)
    assert display["state"] == "expired"
    assert display["summary"] == "Expired"
    assert display["can_extend"] is True


def test_extend_premium_trial_grants_days_and_clears_mt5_pause(app_ctx, monkeypatch):
    user = _create_user(
        username="trial-extend-target",
        email="trial-extend-target@example.com",
        premium_trial_started_at=utcnow_naive() - timedelta(days=ent.PREMIUM_TRIAL_DAYS + 3),
    )
    trade_account = TradeAccount(user_id=user.id, name="Primary", account_type="CFD")
    db.session.add(trade_account)
    db.session.flush()
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="123456",
        server="Test-Server",
        investor_password_encrypted="enc",
        sync_paused_at=utcnow_naive(),
        sync_pause_reason="expired",
    )
    db.session.add(mt5_account)
    db.session.commit()

    dispatched = []

    def _fake_dispatch(task, mt5_account_id, **kwargs):
        dispatched.append(mt5_account_id)

    monkeypatch.setattr("helpers.mt5_dispatch.dispatch_mt5_setup", _fake_dispatch)

    result = ent.extend_premium_trial(user, 7)
    db.session.commit()

    refreshed = db.session.get(MT5Account, mt5_account.id)
    assert result["days_granted"] == 7
    assert result["trial_state"]["state"] == "active"
    assert result["trial_state"]["days_remaining"] == 7
    assert refreshed.sync_paused_at is None
    assert refreshed.sync_pause_reason is None
    assert dispatched == [mt5_account.id]


def test_admin_users_page_shows_trial_summary(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "admin-trial-ui@example.com")
    admin = _create_user(
        username="admin-trial-ui",
        email="admin-trial-ui@example.com",
    )
    admin.is_admin = True
    target = _create_user(
        username="visible-trial-user",
        email="visible-trial-user@example.com",
        premium_trial_started_at=utcnow_naive() - timedelta(days=3),
    )
    db.session.commit()
    _login_as(client, admin)

    response = client.get("/dashboard/admin/access/users?status=all&q=visible-trial-user")

    assert response.status_code == 200
    assert b"Trial started:" in response.data
    assert b"Trial ends:" in response.data
    assert b"Active" in response.data


def test_admin_extend_trial_route_updates_user(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "admin-trial-extend@example.com")
    admin = _create_user(
        username="admin-trial-extend",
        email="admin-trial-extend@example.com",
    )
    admin.is_admin = True
    target = _create_user(
        username="extend-me",
        email="extend-me@example.com",
        premium_trial_started_at=utcnow_naive() - timedelta(days=ent.PREMIUM_TRIAL_DAYS + 1),
    )
    db.session.commit()
    _login_as(client, admin)

    monkeypatch.setattr("helpers.mt5_dispatch.dispatch_mt5_setup", lambda *args, **kwargs: None)

    response = client.post(
        f"/dashboard/admin/access/users/{target.id}/extend-trial",
        data={"days": "10", "csrf_token": _csrf_token(client)},
        follow_redirects=False,
    )

    assert response.status_code == 302
    refreshed = db.session.get(User, target.id)
    trial_state = ent.get_trial_state(refreshed)
    assert trial_state["state"] == "active"
    assert trial_state["days_remaining"] == 10
