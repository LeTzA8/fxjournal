from datetime import datetime

import json

from sqlalchemy import inspect as sa_inspect

from ai_service import WEEKLY_DASHBOARD_KIND
from models import AIGeneratedResponse, AIPromptHistory

import routes.dashboard as dashboard_routes
from models import Trade, TradeAccount, TradeProfile, TradeProfileVersion, User, UserProfile, db


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


def test_dashboard_home_marks_bundled_recent_trade_rows(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-bundled-trade-user",
        email="dashboard-bundled-trade@example.com",
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

    bundled_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.085,
        exit_price=1.091,
        lot_size=0.01,
        pnl=60.0,
        opened_at=datetime(2026, 3, 22, 8, 0, 0),
        closed_at=datetime(2026, 3, 22, 10, 0, 0),
        bundle_pubkey="bundle-dashboard-test",
    )
    db.session.add(bundled_trade)
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'data-bundle="bundle-dashboard-test"' in response.data
    assert b">Bundled</span>" in response.data


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


def test_dashboard_home_shows_bundle_review_banner_only_when_pending(app_ctx, client, monkeypatch):
    _user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-bundle-banner-user",
        email="dashboard-bundle-banner@example.com",
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

    initial_response = client.get("/dashboard")

    trade_account.bundle_review_requested_at = datetime(2026, 3, 29, 9, 0, 0)
    db.session.commit()
    pending_response = client.get("/dashboard")

    trade_account.bundle_review_completed_at = datetime(2026, 3, 29, 10, 0, 0)
    db.session.commit()
    completed_response = client.get("/dashboard")

    assert initial_response.status_code == 200
    assert b"Review Bundles" not in initial_response.data
    assert pending_response.status_code == 200
    assert b"Review Bundles" in pending_response.data
    assert completed_response.status_code == 200
    assert b"Review Bundles" not in completed_response.data


def test_dashboard_home_prioritizes_bundle_review_over_weekly_checkin(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(dashboard_routes, "utcnow_naive", lambda: fixed_now)
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-bundle-priority-user",
        email="dashboard-bundle-priority@example.com",
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

    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.085,
            exit_price=1.091,
            lot_size=0.01,
            pnl=60.0,
            opened_at=datetime(2026, 3, 17, 8, 0, 0),
            closed_at=datetime(2026, 3, 17, 10, 0, 0),
        )
    )
    trade_account.bundle_review_requested_at = datetime(2026, 3, 21, 12, 5, 0)
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Review Bundles" in response.data
    assert b"Open Check-In" not in response.data
    assert b"Review Revenge Signals" not in response.data


def test_dashboard_home_shows_classification_banner_before_weekly_checkin(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(dashboard_routes, "utcnow_naive", lambda: fixed_now)
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-classification-user",
        email="dashboard-classification@example.com",
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

    trades = [
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.0990,
            lot_size=1.0,
            pnl=-45.0,
            opened_at=datetime(2026, 3, 17, 9, 50, 0),
            closed_at=datetime(2026, 3, 17, 10, 15, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.0995,
            exit_price=1.1002,
            lot_size=0.5,
            pnl=22.0,
            opened_at=datetime(2026, 3, 17, 10, 20, 0),
            closed_at=datetime(2026, 3, 17, 10, 40, 0),
        ),
    ]
    db.session.add_all(trades)
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Review Revenge Signals" in response.data
    assert b"Review possible revenge sequences next." in response.data
    assert b"Open Check-In" not in response.data
    assert b"Review Bundles" not in response.data


def test_load_user_trades_preloads_trade_profile_relationships(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-preload-user",
        email="dashboard-preload@example.com",
    )
    profile = TradeProfile(
        user_id=user.id,
        name="Trend Pullback",
        current_version_number=1,
    )
    db.session.add(profile)
    db.session.flush()

    profile_version = TradeProfileVersion(
        trade_profile_id=profile.id,
        version_number=1,
        name="Trend Pullback v1",
    )
    db.session.add(profile_version)
    db.session.flush()

    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.085,
        exit_price=1.091,
        lot_size=0.01,
        pnl=60.0,
        opened_at=datetime(2026, 3, 22, 8, 0, 0),
        closed_at=datetime(2026, 3, 22, 10, 0, 0),
        trade_profile_id=profile.id,
        trade_profile_version_id=profile_version.id,
    )
    db.session.add(trade)
    db.session.commit()
    db.session.expire_all()

    loaded_trades = dashboard_routes._load_user_trades(user.id, trade_account)

    assert len(loaded_trades) == 1
    trade_state = sa_inspect(loaded_trades[0])
    assert "trade_profile" not in trade_state.unloaded
    assert "trade_profile_version" not in trade_state.unloaded
    assert loaded_trades[0].trade_profile.name == "Trend Pullback"
    assert loaded_trades[0].trade_profile_version.name == "Trend Pullback v1"


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


def test_weekly_ai_review_display_rewrites_internal_refs_into_inline_pills():
    review = type(
        "Review",
        (),
        {
            "response_text": "Unused fallback text",
            "response_meta_json": json.dumps(
                {
                    "summary": {
                        "text": "T1 was the cleanest winner and B1 carried the best sequence.",
                        "refs": ["T1", "B1"],
                    },
                    "takeaways": [
                        {
                            "text": "T1 showed the best entry quality of the week.",
                            "refs": ["T1"],
                        },
                        {
                            "text": "B1 was still worth keeping in the review.",
                            "refs": ["B1"],
                        },
                    ],
                    "rule": {
                        "text": "Rule: Use the same filter that made T1 clean before adding back into B1.",
                        "refs": ["T1", "B1"],
                    },
                }
            ),
            "payload_json": json.dumps(
                {
                    "trades": [
                        {
                            "review_ref": "T1",
                            "trade_id": 101,
                            "symbol": "XAUUSD",
                            "opened_at": "2026-04-01T09:00:00Z",
                            "pnl": 125.0,
                            "is_bundle": False,
                            "bundle_pubkey": None,
                        },
                        {
                            "review_ref": "B1",
                            "trade_id": 202,
                            "symbol": "GBPUSD",
                            "opened_at": "2026-04-02T10:00:00Z",
                            "pnl": -42.0,
                            "is_bundle": True,
                            "bundle_pubkey": "bundle-xyz",
                        },
                    ]
                }
            ),
        },
    )()

    display = dashboard_routes._build_weekly_ai_review_display(review, "UTC")

    assert "T1" not in display["summary"]["text"]
    assert "B1" not in display["summary"]["text"]
    assert display["summary"]["text"].startswith("XAUUSD")
    assert [segment["type"] for segment in display["summary"]["segments"]] == [
        "citation",
        "text",
        "citation",
        "text",
    ]
    assert display["summary"]["segments"][0]["label"] == "XAUUSD | 01 Apr 2026 (Wed)"
    assert display["summary"]["segments"][0]["tone"] == "good"
    assert display["summary"]["segments"][2]["label"] == "GBPUSD bundle | 02 Apr 2026 (Thu)"
    assert display["summary"]["segments"][2]["tone"] == "bad"
    assert display["takeaways"][0]["segments"][0]["type"] == "citation"
    assert display["takeaways"][0]["segments"][0]["label"] == "XAUUSD | 01 Apr 2026 (Wed)"
    assert display["takeaways"][0]["segments"][0]["tone"] == "good"
    assert display["takeaways"][1]["segments"][0]["type"] == "citation"
    assert display["takeaways"][1]["segments"][0]["label"] == "GBPUSD bundle | 02 Apr 2026 (Thu)"
    assert display["takeaways"][1]["segments"][0]["tone"] == "bad"
    assert "XAUUSD" in display["rule"]["text"]
    assert "GBPUSD bundle" in display["rule"]["text"]
    assert display["rule"]["citations"] == []
    assert display["rule"]["segments"] == [
        {"type": "text", "text": display["rule"]["text"]},
    ]


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
