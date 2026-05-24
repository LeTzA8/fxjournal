from datetime import timedelta

from helpers.core import create_trade_profile
from helpers.utils import utcnow_naive
from models import Trade, TradeAccount, User, db


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


def test_strategies_page_renders_workbench_card_fields(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="strategy-workbench-user",
        email="strategy-workbench@example.com",
    )
    profile, _version = create_trade_profile(
        user.id,
        "London Break",
        "Line one of playbook.\nLine two of playbook.",
    )
    trade_account.default_trade_profile_id = profile.id
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            trade_profile_id=profile.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1,
            exit_price=1.11,
            lot_size=0.1,
            pnl=10.0,
            opened_at=utcnow_naive() - timedelta(days=2),
            closed_at=utcnow_naive() - timedelta(days=2),
        )
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            trade_profile_id=profile.id,
            symbol="GBPUSD",
            side="SELL",
            entry_price=1.25,
            exit_price=1.24,
            lot_size=0.1,
            pnl=8.0,
            opened_at=utcnow_naive() - timedelta(days=20),
            closed_at=utcnow_naive() - timedelta(days=20),
        )
    )
    db.session.commit()

    response = client.get("/dashboard/strategies")

    assert response.status_code == 200
    assert b"Strategies" in response.data
    assert b"London Break" in response.data
    assert b"Line one of playbook." in response.data
    assert b"Line two of playbook." in response.data
    assert b"Default for Main Account" in response.data
    assert b"Total usage" in response.data
    assert b"Last 7 days" in response.data
    assert b"Last edited" in response.data
    assert b">2<" in response.data
    assert b">1<" in response.data
    assert b"Usage for Main Account" in response.data
    assert b"Setup library" not in response.data


def test_strategies_page_empty_state_keeps_first_use_education(app_ctx, client):
    _create_logged_in_user(
        client,
        username="strategy-empty-user",
        email="strategy-empty@example.com",
    )

    response = client.get("/dashboard/strategies")

    assert response.status_code == 200
    assert b"Create your first strategy" in response.data
    assert b"Strategies tag setups in your trade log and imports" in response.data
    assert b"Shows up in" in response.data
