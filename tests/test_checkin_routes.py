from datetime import datetime

import routes.checkin as checkin_routes
import routes.dashboard as dashboard_routes
from models import Trade, TradeAccount, User, WeeklyCheckin, db


def _create_logged_in_user(client, username, email):
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


def test_dashboard_checkin_banner_respects_active_account_only(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(dashboard_routes, "utcnow_naive", lambda: fixed_now)

    user, active_account = _create_logged_in_user(
        client,
        username="checkin-active-account-user",
        email="checkin-active-account@example.com",
    )
    other_account = TradeAccount(
        user_id=user.id,
        name="Second Account",
        account_type="CFD",
        is_default=False,
    )
    db.session.add(other_account)
    db.session.flush()
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=other_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            lot_size=1.0,
            pnl=55.0,
            opened_at=datetime(2026, 3, 17, 10, 0, 0),
            closed_at=datetime(2026, 3, 17, 11, 0, 0),
        )
    )
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Open Check-In" not in response.data
    assert active_account.id != other_account.id


def test_checkin_route_saves_answers_for_current_week(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(checkin_routes, "utcnow_naive", lambda: fixed_now)
    monkeypatch.setattr(dashboard_routes, "utcnow_naive", lambda: fixed_now)

    user, trade_account = _create_logged_in_user(
        client,
        username="checkin-save-user",
        email="checkin-save@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="GBPUSD",
            side="SELL",
            entry_price=1.2700,
            exit_price=1.2690,
            lot_size=1.0,
            pnl=45.0,
            opened_at=datetime(2026, 3, 17, 12, 0, 0),
            closed_at=datetime(2026, 3, 17, 13, 0, 0),
        )
    )
    db.session.commit()

    get_response = client.get("/checkin")
    post_response = client.post(
        "/checkin",
        data={
            "emotional_state": "stressed",
            "plan_adherence": "impulsive",
            "execution_quality": "poor",
            "additional_context": "Felt reactive after the first loss.",
        },
        follow_redirects=False,
    )

    record = WeeklyCheckin.query.filter_by(
        user_id=user.id,
        trade_account_id=trade_account.id,
    ).first()

    assert get_response.status_code == 200
    assert b"Give the AI a little context" in get_response.data
    assert post_response.status_code == 302
    assert record is not None
    assert record.emotional_state == "stressed"
    assert record.plan_adherence == "impulsive"
    assert record.execution_quality == "poor"
    assert record.additional_context == "Felt reactive after the first loss."


def test_checkin_skip_creates_placeholder_row(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(checkin_routes, "utcnow_naive", lambda: fixed_now)

    user, trade_account = _create_logged_in_user(
        client,
        username="checkin-skip-user",
        email="checkin-skip@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="USDJPY",
            side="BUY",
            entry_price=149.20,
            exit_price=149.55,
            lot_size=1.0,
            pnl=32.0,
            opened_at=datetime(2026, 3, 17, 8, 0, 0),
            closed_at=datetime(2026, 3, 17, 8, 30, 0),
        )
    )
    db.session.commit()

    response = client.post("/checkin/skip", follow_redirects=False)
    record = WeeklyCheckin.query.filter_by(
        user_id=user.id,
        trade_account_id=trade_account.id,
    ).first()

    assert response.status_code == 302
    assert record is not None
    assert record.emotional_state is None
    assert record.plan_adherence is None
    assert record.execution_quality is None
    assert record.additional_context is None


def test_checkin_skip_still_allows_returning_to_form(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(checkin_routes, "utcnow_naive", lambda: fixed_now)

    user, trade_account = _create_logged_in_user(
        client,
        username="checkin-return-user",
        email="checkin-return@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="XAUUSD",
            side="BUY",
            entry_price=3000.0,
            exit_price=3012.0,
            lot_size=1.0,
            pnl=84.0,
            opened_at=datetime(2026, 3, 17, 8, 0, 0),
            closed_at=datetime(2026, 3, 17, 10, 0, 0),
        )
    )
    db.session.commit()

    skip_response = client.post("/checkin/skip", follow_redirects=False)
    reopen_response = client.get("/checkin")

    assert skip_response.status_code == 302
    assert reopen_response.status_code == 200
    assert b"Give the AI a little context" in reopen_response.data


def test_dashboard_shows_finish_checkin_after_skip(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(checkin_routes, "utcnow_naive", lambda: fixed_now)
    monkeypatch.setattr(dashboard_routes, "utcnow_naive", lambda: fixed_now)

    user, trade_account = _create_logged_in_user(
        client,
        username="checkin-banner-return-user",
        email="checkin-banner-return@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="SELL",
            entry_price=1.1000,
            exit_price=1.0980,
            lot_size=1.0,
            pnl=50.0,
            opened_at=datetime(2026, 3, 17, 12, 0, 0),
            closed_at=datetime(2026, 3, 17, 13, 0, 0),
        )
    )
    db.session.commit()

    client.post("/checkin/skip", follow_redirects=False)
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Finish Check-In" in response.data
    assert b"You skipped it earlier" in response.data


def test_dashboard_does_not_show_checkin_before_friday_close(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 18, 12, 0, 0)
    monkeypatch.setattr(checkin_routes, "utcnow_naive", lambda: fixed_now)
    monkeypatch.setattr(dashboard_routes, "utcnow_naive", lambda: fixed_now)

    user, trade_account = _create_logged_in_user(
        client,
        username="checkin-before-cutoff-user",
        email="checkin-before-cutoff@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="GBPUSD",
            side="BUY",
            entry_price=1.2800,
            exit_price=1.2820,
            lot_size=1.0,
            pnl=40.0,
            opened_at=datetime(2026, 3, 17, 9, 0, 0),
            closed_at=datetime(2026, 3, 17, 10, 0, 0),
        )
    )
    db.session.commit()

    dashboard_response = client.get("/dashboard")
    checkin_response = client.get("/checkin", follow_redirects=False)

    assert dashboard_response.status_code == 200
    assert b"Open Check-In" not in dashboard_response.data
    assert b"Finish Check-In" not in dashboard_response.data
    assert checkin_response.status_code == 302
