from datetime import datetime

from ai_service import WEEKLY_DASHBOARD_KIND
from models import AIGeneratedResponse, AIPromptHistory

import routes.dashboard as dashboard_routes
from models import Trade, TradeAccount, User, UserProfile, db


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
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": None,
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "No trades this week. Add closed trades to generate your AI review.",
            "weekly_ai_is_generating": False,
        },
    )

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"No trades this week. Add closed trades to generate your AI review." in response.data


def test_dashboard_home_shows_too_few_trades_weekly_ai_message(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-ai-thin-user",
        email="dashboard-ai-thin@example.com",
    )
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": None,
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "Not enough data for a meaningful review. Add at least 3 closed trades this week.",
            "weekly_ai_is_generating": False,
        },
    )

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Not enough data for a meaningful review. Add at least 3 closed trades this week." in response.data


def test_dashboard_home_marks_running_trade_rows(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-running-trade-user",
        email="dashboard-running-trade@example.com",
    )
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": None,
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "No trades this week. Add closed trades to generate your AI review.",
            "weekly_ai_is_generating": False,
        },
    )

    running_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.085,
        exit_price=None,
        lot_size=0.01,
        opened_at=datetime(2026, 3, 22, 8, 0, 0),
    )
    db.session.add(running_trade)
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'class="running-trade"' in response.data
    assert b"Running" in response.data


def test_dashboard_home_does_not_mark_closed_timestamp_trade_as_running(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-closed-timestamp-user",
        email="dashboard-closed-timestamp@example.com",
    )
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": None,
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "No trades this week. Add closed trades to generate your AI review.",
            "weekly_ai_is_generating": False,
        },
    )

    closed_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.085,
        exit_price=None,
        lot_size=0.01,
        opened_at=datetime(2026, 3, 22, 8, 0, 0),
        closed_at=datetime(2026, 3, 22, 10, 0, 0),
    )
    db.session.add(closed_trade)
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'class="running-trade"' not in response.data
    assert b'<span class="running-pill">Running</span>' not in response.data


def test_dashboard_home_normalizes_broken_rule_prefix_in_weekly_ai_review(app_ctx, client, monkeypatch):
    _user, _trade_account = _create_logged_in_user(
        client,
        username="dashboard-ai-review-user",
        email="dashboard-ai-review@example.com",
    )

    review = type(
        "Review",
        (),
        {"response_text": "Key Takeaways\n- Supported insight.\n\u00e2\u2020' Rule: Keep risk fixed."},
    )()

    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": review,
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "",
            "weekly_ai_is_generating": False,
        },
    )

    response = client.get("/dashboard")
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Rule: Keep risk fixed." in response_text
    assert "\u00e2\u2020'" not in response_text


def test_weekly_ai_state_falls_back_to_latest_generated_review_for_account(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-ai-fallback-user",
        email="dashboard-ai-fallback@example.com",
    )

    prompt_history = AIPromptHistory(
        prompt_id="dashboard_advice",
        prompt_sha256="weekly-fallback-sha",
        prompt_text="Prompt text",
        source_path="prompts/dashboard_advice.txt",
    )
    db.session.add(prompt_history)
    db.session.flush()

    old_period = {
        "period_start_utc": datetime(2026, 3, 7, 21, 30, 0),
        "period_end_utc": datetime(2026, 3, 14, 21, 30, 0),
    }
    latest_period = {
        "period_start_utc": datetime(2026, 3, 14, 21, 30, 0),
        "period_end_utc": datetime(2026, 3, 21, 21, 30, 0),
    }
    review = AIGeneratedResponse(
        user_id=user.id,
        trade_account_id=trade_account.id,
        prompt_history_id=prompt_history.id,
        kind=WEEKLY_DASHBOARD_KIND,
        model="gpt-5-mini",
        response_text="Older but valid weekly review",
        payload_hash="weekly-fallback-hash",
        trade_count_used=4,
        period_start_utc=old_period["period_start_utc"],
        period_end_utc=old_period["period_end_utc"],
        generated_at=datetime(2026, 3, 16, 12, 0, 0),
    )
    db.session.add(review)
    db.session.commit()

    monkeypatch.setattr(dashboard_routes, "get_latest_trade_week_period", lambda **kwargs: latest_period)
    monkeypatch.setattr(dashboard_routes, "get_ai_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(dashboard_routes, "should_generate_weekly_dashboard_advice", lambda **kwargs: False)

    weekly_ai_state = dashboard_routes._get_weekly_ai_state(user.id, trade_account, "UTC")

    assert weekly_ai_state["weekly_ai_review"] is not None
    assert weekly_ai_state["weekly_ai_review"].id == review.id
    assert weekly_ai_state["weekly_ai_review_text"] == "Older but valid weekly review"
    assert weekly_ai_state["weekly_ai_period_label"] == "Sat 14 Mar 2026 21:30 UTC"
    assert weekly_ai_state["weekly_ai_is_generating"] is False


def test_dashboard_home_shows_onboarding_banner_when_profile_was_skipped(app_ctx, client, monkeypatch):
    user, _trade_account = _create_logged_in_user(
        client,
        username="dashboard-onboarding-skip-user",
        email="dashboard-onboarding-skip@example.com",
    )
    db.session.add(UserProfile(user_id=user.id, skipped=True))
    db.session.commit()

    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": None,
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "No trades this week. Add closed trades to generate your AI review.",
            "weekly_ai_is_generating": False,
        },
    )

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Complete Onboarding" in response.data
    assert b"You skipped the questionnaire earlier" in response.data
