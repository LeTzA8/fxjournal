from datetime import datetime

import routes.dashboard as dashboard_routes
from models import TradeAccount, User, db


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


def test_dashboard_home_shows_no_trades_weekly_ai_message(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-ai-no-trades-user",
        email="dashboard-ai-no-trades@example.com",
    )
    period = {
        "period_start_utc": datetime(2026, 3, 7, 21, 30, 0),
        "period_end_utc": datetime(2026, 3, 14, 21, 30, 0),
    }

    monkeypatch.setattr(
        dashboard_routes,
        "maybe_generate_weekly_dashboard_advice",
        lambda **kwargs: {"record": None, "generated": False, "period": period, "skip_reason": "no_trades"},
    )
    monkeypatch.setattr(dashboard_routes, "get_latest_weekly_dashboard_advice", lambda **kwargs: None)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"No trades this week. Add closed trades to generate your AI review." in response.data


def test_dashboard_home_shows_too_few_trades_weekly_ai_message(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-ai-thin-user",
        email="dashboard-ai-thin@example.com",
    )
    period = {
        "period_start_utc": datetime(2026, 3, 7, 21, 30, 0),
        "period_end_utc": datetime(2026, 3, 14, 21, 30, 0),
    }

    monkeypatch.setattr(
        dashboard_routes,
        "maybe_generate_weekly_dashboard_advice",
        lambda **kwargs: {"record": None, "generated": False, "period": period, "skip_reason": "too_few_trades"},
    )
    monkeypatch.setattr(dashboard_routes, "get_latest_weekly_dashboard_advice", lambda **kwargs: None)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Not enough data for a meaningful review. Add at least 3 closed trades this week." in response.data
