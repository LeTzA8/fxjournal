from helpers.core import create_trade_profile
from models import TradeAccount, User, db


def _create_logged_in_user(client, *, username, email):
    user = User(
        username=username,
        email=email,
        password="hashed-password",
    )
    db.session.add(user)
    db.session.flush()

    trade_account = TradeAccount(
        user_id=user.id,
        name="Main Account",
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.commit()

    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username
        session_state["display_timezone"] = "UTC"
        session_state["active_trade_account_id"] = trade_account.id

    return user, trade_account


def test_set_default_strategy_for_active_account_updates_account_default(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="strategy-default-user",
        email="strategy-default@example.com",
    )
    profile, _version = create_trade_profile(
        user.id,
        "NY Open Sweep",
        "Sweep and reclaim setup.",
    )
    db.session.commit()

    response = client.post(
        f"/dashboard/strategies/{profile.pubkey}/set-default",
        data={},
        follow_redirects=True,
    )

    db.session.refresh(trade_account)

    assert response.status_code == 200
    assert trade_account.default_trade_profile_id == profile.id
    assert (
        b"Default strategy for &#39;Main Account&#39; set to &#39;NY Open Sweep&#39;."
        in response.data
    )


def test_set_default_strategy_for_active_account_rejects_other_users_strategy(app_ctx, client):
    owner, trade_account = _create_logged_in_user(
        client,
        username="strategy-default-owner",
        email="strategy-default-owner@example.com",
    )

    intruder = User(
        username="strategy-default-other",
        email="strategy-default-other@example.com",
        password="hashed-password",
    )
    db.session.add(intruder)
    db.session.flush()

    foreign_profile, _version = create_trade_profile(
        intruder.id,
        "Foreign Setup",
        "Should not be selectable by another user.",
    )
    db.session.commit()

    response = client.post(
        f"/dashboard/strategies/{foreign_profile.pubkey}/set-default",
        data={},
        follow_redirects=True,
    )

    db.session.refresh(trade_account)

    assert response.status_code == 200
    assert trade_account.default_trade_profile_id is None
    assert b"Strategy not found." in response.data
    assert owner.id != intruder.id
