from datetime import datetime, timedelta, timezone
from pathlib import Path

import json

import pytest
from sqlalchemy import inspect as sa_inspect

from ai_service import (
    DEFAULT_WEEKLY_REVIEW_CHAT_PROMPT_FILE,
    WEEKLY_DASHBOARD_KIND,
    build_weekly_review_chat_messages,
    load_prompt_text,
)
from models import AIGeneratedResponse, AIPromptHistory, JournalSession, MT5Account, MT5SyncBatch, WeeklyReviewChatMessage

import routes.dashboard as dashboard_routes
from helpers.trade_interpretation import apply_interpretation
from models import Trade, TradeAccount, TradeProfile, TradeProfileVersion, User, UserProfile, db


@pytest.fixture(autouse=True)
def _reset_rate_limiter_for_isolation():
    """Flask-Limiter is process-global; reset so chat rate tests do not cross-contaminate."""
    from extensions import limiter

    limiter.reset()
    yield


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


def _create_prompt_history(prompt_id):
    prompt_history = AIPromptHistory(
        prompt_id=prompt_id,
        prompt_sha256=f"{prompt_id}-sha",
        prompt_text="Prompt text",
        source_path="prompts/dashboard_advice.txt",
    )
    db.session.add(prompt_history)
    db.session.flush()
    return prompt_history


def _create_weekly_review(user, trade_account, prompt_id="weekly-chat"):
    prompt_history = _create_prompt_history(prompt_id)
    review = AIGeneratedResponse(
        user_id=user.id,
        trade_account_id=trade_account.id,
        prompt_history_id=prompt_history.id,
        kind=WEEKLY_DASHBOARD_KIND,
        model="gpt-5-mini",
        response_text="This week was driven by one oversized XAUUSD loss.",
        pass_1_output="Pass one: sizing was the main issue.",
        payload_json=json.dumps(
            {
                "trades": [
                    {
                        "ref": "T1",
                        "trade_id": 101,
                        "symbol": "XAUUSD",
                        "opened_at": "2026-04-08T14:30:00Z",
                        "pnl": -120.0,
                        "is_bundle": False,
                    }
                ]
            }
        ),
        payload_hash="weekly-chat-hash",
        trade_count_used=3,
        period_start_utc=datetime(2026, 4, 6, 21, 30, 0),
        period_end_utc=datetime(2026, 4, 13, 21, 30, 0),
        generated_at=datetime(2026, 4, 14, 12, 0, 0),
    )
    db.session.add(review)
    db.session.commit()
    return review


def test_week_on_week_performance_trends_compare_this_week_to_previous_week():
    current_week_stats = {
        "trade_count": 12,
        "wins": 1,
        "win_rate": 8.3333333333,
        "net_pnl": -3375.35,
        "week_sample_is_reliable": True,
    }
    previous_week_stats = {
        "trade_count": 4,
        "wins": 1,
        "win_rate": 25.0,
        "net_pnl": -709.00,
        "week_sample_is_reliable": False,
    }

    result = dashboard_routes._build_week_on_week_performance_trends(
        current_week_stats,
        previous_week_stats,
    )

    assert result["current_expectancy"] == pytest.approx(-281.28, abs=0.01)
    assert result["previous_expectancy"] == pytest.approx(-177.25, abs=0.01)
    assert result["win_rate_trend"] == "declining"
    assert result["expectancy_trend"] == "declining"
    assert result["has_limited_sample"] is True


def test_week_on_week_behavior_trend_uses_current_and_previous_week_trades(app_ctx):
    user = User(username="wow-behavior-user", email="wow-behavior@example.com", password="hashed")
    db.session.add(user)
    db.session.flush()
    trade_account = TradeAccount(
        user_id=user.id,
        name="Main Account",
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    previous_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1000,
        exit_price=1.1010,
        lot_size=1.0,
        pnl=40.0,
        opened_at=datetime(2026, 4, 29, 9, 0, 0),
        closed_at=datetime(2026, 4, 29, 10, 0, 0),
    )
    current_trade_one = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="XAUUSD",
        side="BUY",
        entry_price=2300.0,
        exit_price=2295.0,
        lot_size=1.0,
        pnl=-120.0,
        opened_at=datetime(2026, 5, 6, 9, 0, 0),
        closed_at=datetime(2026, 5, 6, 9, 20, 0),
    )
    current_trade_two = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="XAUUSD",
        side="BUY",
        entry_price=2296.0,
        exit_price=2290.0,
        lot_size=1.0,
        pnl=-180.0,
        opened_at=datetime(2026, 5, 6, 9, 30, 0),
        closed_at=datetime(2026, 5, 6, 9, 45, 0),
    )
    db.session.add_all([previous_trade, current_trade_one, current_trade_two])
    db.session.flush()
    apply_interpretation(
        current_trade_one,
        is_reactive=True,
        source="test",
        user_id=user.id,
    )
    apply_interpretation(
        current_trade_two,
        is_reactive=True,
        source="test",
        user_id=user.id,
    )
    db.session.commit()

    result = dashboard_routes._build_week_on_week_behavior_trend(
        [previous_trade, current_trade_one, current_trade_two],
        "UTC",
        datetime(2026, 5, 4, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 27, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 4, 0, 0, 0, tzinfo=timezone.utc),
    )

    assert result["current_behavior_score"] > result["previous_behavior_score"]
    assert result["behavior_trend"] == "declining"


def test_weekly_review_chat_prompt_exposes_trade_link_refs_without_raw_codes_in_review_text(app_ctx):
    review = type(
        "Review",
        (),
        {
            "response_text": "T1 was clean compared with B1.",
            "pass_1_output": "Pass: watch B1 sizing.",
            "response_meta_json": json.dumps(
                {
                    "summary": {"text": "T1 vs B1 story.", "refs": ["T1", "B1"]},
                    "takeaways": [{"text": "B1 mattered", "refs": ["B1"]}],
                }
            ),
            "payload_json": json.dumps(
                {
                    "trades": [
                        {
                            "ref": "T1",
                            "trade_id": 101,
                            "symbol": "EURUSD",
                            "opened_at": "2026-04-01T09:00:00Z",
                            "pnl": 10.0,
                            "is_bundle": False,
                        },
                        {
                            "ref": "B1",
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
    messages = build_weekly_review_chat_messages(
        review,
        "What does T1 mean?",
        timezone_name="UTC",
    )
    user_blob = messages[1]["content"][0]["text"]
    assert "TRADE_LINK_REFS_AVAILABLE_FOR_OUTPUT" in user_blob
    assert "T1: EURUSD | 01 Apr 2026 (Wed) (trade)" in user_blob
    assert "B1: GBPUSD bundle | 02 Apr 2026 (Thu) (bundle)" in user_blob
    assert '"refs"' not in user_blob
    assert '"ref":"T1"' in user_blob
    assert '"ref":"B1"' in user_blob
    assert "EURUSD" in user_blob
    assert "GBPUSD" in user_blob


def test_weekly_review_chat_prompt_uses_prompt_file(app_ctx):
    review = type(
        "Review",
        (),
        {
            "response_text": "This week was driven by one oversized XAUUSD loss.",
            "pass_1_output": "",
            "response_meta_json": "",
            "payload_json": "",
        },
    )()
    messages = build_weekly_review_chat_messages(review, "Explain this simply")
    system_prompt = messages[0]["content"][0]["text"]

    assert system_prompt == load_prompt_text(DEFAULT_WEEKLY_REVIEW_CHAT_PROMPT_FILE)["prompt_text"]


def test_weekly_review_chat_route_stores_user_and_assistant_messages(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-review-chat-user",
        email="dashboard-review-chat@example.com",
    )
    review = _create_weekly_review(user, trade_account, prompt_id="weekly-chat-success")
    db.session.add(
        WeeklyReviewChatMessage(
            user_id=user.id,
            trade_account_id=trade_account.id,
            ai_response_id=review.id,
            role=WeeklyReviewChatMessage.ROLE_USER,
            content="Explain this simply",
        )
    )
    db.session.commit()

    calls = {}

    def fake_generate_weekly_review_chat_reply(review_record, user_message, chat_history=None, **kwargs):
        calls["review_id"] = review_record.id
        calls["message"] = user_message
        calls["history"] = [message.content for message in chat_history or []]
        return "Start with the XAUUSD loss [T1] because it drove most of the damage.", {}, "gpt-test"

    monkeypatch.setattr(
        dashboard_routes,
        "generate_weekly_review_chat_reply",
        fake_generate_weekly_review_chat_reply,
    )

    response = client.post(
        f"/dashboard/weekly-review/{review.id}/chat",
        json={"message": "Which trade should I review first?"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["reply"] == "Start with the XAUUSD loss because it drove most of the damage."
    assert payload["segments"] == [
        {"text": "Start with the ", "type": "text"},
        {
            "bundle_key": None,
            "citation_type": "trade",
            "label": "XAUUSD | 08 Apr 2026 (Wed)",
            "tone": "bad",
            "trade_id": 101,
            "type": "citation",
        },
        {"text": " loss because it drove most of the damage.", "type": "text"},
    ]
    assert calls == {
        "review_id": review.id,
        "message": "Which trade should I review first?",
        "history": ["Explain this simply"],
    }
    messages = (
        WeeklyReviewChatMessage.query.filter_by(ai_response_id=review.id)
        .order_by(WeeklyReviewChatMessage.id.asc())
        .all()
    )
    assert [message.role for message in messages] == ["user", "user", "assistant"]
    assert messages[-2].content == "Which trade should I review first?"
    assert messages[-1].content.startswith("Start with the XAUUSD loss")
    assert messages[-1].model_used == "gpt-test"


def test_weekly_review_chat_route_rejects_wrong_active_account(app_ctx, client, monkeypatch):
    user, active_trade_account = _create_logged_in_user(
        client,
        username="dashboard-review-chat-scope-user",
        email="dashboard-review-chat-scope@example.com",
    )
    other_account = TradeAccount(
        user_id=user.id,
        name="Other Account",
        account_type="CFD",
        is_default=False,
    )
    db.session.add(other_account)
    db.session.flush()
    review = _create_weekly_review(user, other_account, prompt_id="weekly-chat-scope")

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("AI should not be called for another account's review")

    monkeypatch.setattr(dashboard_routes, "generate_weekly_review_chat_reply", fail_if_called)

    response = client.post(
        f"/dashboard/weekly-review/{review.id}/chat",
        json={"message": "Explain this simply"},
    )

    assert active_trade_account.id != other_account.id
    assert response.status_code == 404
    assert WeeklyReviewChatMessage.query.filter_by(ai_response_id=review.id).count() == 0


def test_weekly_review_chat_route_rejects_empty_and_long_messages(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-review-chat-validation-user",
        email="dashboard-review-chat-validation@example.com",
    )
    review = _create_weekly_review(user, trade_account, prompt_id="weekly-chat-validation")

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("AI should not be called for invalid messages")

    monkeypatch.setattr(dashboard_routes, "generate_weekly_review_chat_reply", fail_if_called)

    empty_response = client.post(
        f"/dashboard/weekly-review/{review.id}/chat",
        json={"message": "   "},
    )
    long_response = client.post(
        f"/dashboard/weekly-review/{review.id}/chat",
        json={"message": "x" * (dashboard_routes.WEEKLY_REVIEW_CHAT_MAX_CHARS + 1)},
    )

    assert empty_response.status_code == 400
    assert long_response.status_code == 400
    assert WeeklyReviewChatMessage.query.filter_by(ai_response_id=review.id).count() == 0


def test_weekly_review_chat_route_is_rate_limited(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-review-chat-rl-user",
        email="dashboard-review-chat-rl@example.com",
    )
    review = _create_weekly_review(user, trade_account, prompt_id="weekly-chat-rl")

    def fake_reply(*_a, **_k):
        return "brief", {}, "gpt-test"

    monkeypatch.setattr(dashboard_routes, "generate_weekly_review_chat_reply", fake_reply)

    for i in range(3):
        response = client.post(
            f"/dashboard/weekly-review/{review.id}/chat",
            json={"message": f"q{i}"},
        )
        assert response.status_code == 200, f"unexpected at {i}"
    over = client.post(
        f"/dashboard/weekly-review/{review.id}/chat",
        json={"message": "q3"},
    )
    assert over.status_code == 429
    assert (over.get_json() or {}).get("error") == "rate_limit_exceeded"


def test_weekly_review_chat_admin_bypasses_usage_limits(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-review-chat-admin-rl-user",
        email="dashboard-review-chat-admin-rl@example.com",
    )
    user.email_verified = True
    user.is_admin = True
    db.session.commit()

    review = _create_weekly_review(user, trade_account, prompt_id="weekly-chat-admin-rl")

    def fake_reply(*_a, **_k):
        return "brief", {}, "gpt-test"

    monkeypatch.setattr(dashboard_routes, "generate_weekly_review_chat_reply", fake_reply)

    for i in range(6):
        response = client.post(
            f"/dashboard/weekly-review/{review.id}/chat",
            json={"message": f"q{i}"},
        )
        assert response.status_code == 200, f"unexpected at {i}"


def test_weekly_review_chat_route_enforces_trial_message_limit(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-review-chat-review-cap-user",
        email="dashboard-review-chat-review-cap@example.com",
    )
    user.premium_trial_started_at = datetime.utcnow() - timedelta(days=1)
    review = _create_weekly_review(user, trade_account, prompt_id="weekly-chat-review-cap")
    for i in range(5):
        db.session.add(
            WeeklyReviewChatMessage(
                user_id=user.id,
                trade_account_id=trade_account.id,
                ai_response_id=review.id,
                role=WeeklyReviewChatMessage.ROLE_USER,
                content=f"prior-{i}",
            )
        )
    db.session.commit()

    def fail_if_called(*_a, **_k):
        raise AssertionError("AI should not run when trial message cap is reached")

    monkeypatch.setattr(dashboard_routes, "generate_weekly_review_chat_reply", fail_if_called)

    response = client.post(
        f"/dashboard/weekly-review/{review.id}/chat",
        json={"message": "sixth question"},
    )
    assert response.status_code == 429
    payload = response.get_json()
    assert payload["error"] == "weekly_followup_trial_limit_reached"
    assert payload["usage"]["used"] == 5
    assert payload["cta"]["feature_interest"] == "weekly_followup_chat"


def test_weekly_review_chat_route_counts_only_user_messages_for_trial_limit(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-review-chat-daily-cap-user",
        email="dashboard-review-chat-daily-cap@example.com",
    )
    review = _create_weekly_review(user, trade_account, prompt_id="weekly-chat-assistant-not-counted")
    for j in range(5):
        db.session.add(
            WeeklyReviewChatMessage(
                user_id=user.id,
                trade_account_id=trade_account.id,
                ai_response_id=review.id,
                role=WeeklyReviewChatMessage.ROLE_ASSISTANT,
                content=f"assistant-seed-{j}",
            )
        )
    db.session.commit()

    def fake_reply(*_a, **_k):
        return "brief", {}, "gpt-test"

    monkeypatch.setattr(dashboard_routes, "generate_weekly_review_chat_reply", fake_reply)

    response = client.post(
        f"/dashboard/weekly-review/{review.id}/chat",
        json={"message": "first user question"},
    )
    assert response.status_code == 200
    assert db.session.get(User, user.id).premium_trial_started_at is not None


def test_weekly_review_chat_route_blocks_after_trial_expiry(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-review-chat-expired-user",
        email="dashboard-review-chat-expired@example.com",
    )
    user.premium_trial_started_at = datetime.utcnow() - timedelta(days=20)
    db.session.commit()
    review = _create_weekly_review(user, trade_account, prompt_id="weekly-chat-expired")

    def fail_if_called(*_a, **_k):
        raise AssertionError("AI should not run after trial expiry")

    monkeypatch.setattr(dashboard_routes, "generate_weekly_review_chat_reply", fail_if_called)

    response = client.post(
        f"/dashboard/weekly-review/{review.id}/chat",
        json={"message": "Can I ask one more?"},
    )
    payload = response.get_json()
    assert response.status_code == 403
    assert payload["error"] == "weekly_followup_trial_required"
    assert payload["cta"]["feature_interest"] == "weekly_followup_chat"


def test_weekly_review_chat_grandfathered_user_bypasses_trial_expiry(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-review-chat-grandfathered-user",
        email="dashboard-review-chat-grandfathered@example.com",
    )
    user.premium_trial_started_at = datetime.utcnow() - timedelta(days=200)
    user.plan_grandfathered = True
    db.session.commit()
    review = _create_weekly_review(user, trade_account, prompt_id="weekly-chat-grandfathered")

    def fake_reply(*_a, **_k):
        return "brief", {}, "gpt-test"

    monkeypatch.setattr(dashboard_routes, "generate_weekly_review_chat_reply", fake_reply)

    response = client.post(
        f"/dashboard/weekly-review/{review.id}/chat",
        json={"message": "Still available?"},
    )
    assert response.status_code == 200


def test_dashboard_home_renders_weekly_review_chat_inside_review_panel(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-review-chat-render-user",
        email="dashboard-review-chat-render@example.com",
    )
    review = _create_weekly_review(user, trade_account, prompt_id="weekly-chat-render")
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": review,
            "weekly_ai_review_display": {
                "summary": {"text": "Summary", "segments": [{"type": "text", "text": "Summary"}], "refs": [], "citations": []},
                "takeaways": [],
                "improvement": {"text": "Improve this week: Keep risk fixed.", "segments": [{"type": "text", "text": "Improve this week: Keep risk fixed."}], "refs": [], "citations": []},
                "strength": {"text": "You're already strong at: Waiting.", "segments": [{"type": "text", "text": "You're already strong at: Waiting."}], "refs": [], "citations": []},
                "experiment": {},
                "has_citations": False,
            },
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "",
            "weekly_ai_is_generating": False,
        },
    )

    response = client.get("/dashboard")
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "💬 Ask about this review" in response_text
    assert f"/dashboard/weekly-review/{review.id}/chat" in response_text
    assert "Why did risk drive this review?" in response_text
    assert "Was this bad luck or my execution?" not in response_text
    assert "weekly_review_chat.js" in response_text


def test_dashboard_home_shows_admin_ai_journal_carousel_tab(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-journal-preview-admin",
        email="dashboard-journal-preview-admin@example.com",
    )
    user.is_admin = True
    user.email_verified = True
    user.signup_status = "approved"
    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1,
        exit_price=1.105,
        lot_size=0.1,
        pnl=50.0,
        opened_at=datetime(2026, 5, 8, 9, 0),
        closed_at=datetime(2026, 5, 8, 10, 0),
    )
    db.session.add(trade)
    db.session.add(
        JournalSession(
            user_id=user.id,
            trade_account_id=trade_account.id,
            scope_type="freeform",
            title="revenge reflection",
            started_at=datetime(2026, 5, 9, 9, 0),
        )
    )
    db.session.commit()
    review = _create_weekly_review(user, trade_account, prompt_id="weekly-journal-preview")
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": review,
            "weekly_ai_review_display": {
                "summary": {"text": "Summary", "segments": [{"type": "text", "text": "Summary"}], "refs": [], "citations": []},
                "takeaways": [],
                "improvement": {"text": "Improve this week: Keep risk fixed.", "segments": [{"type": "text", "text": "Improve this week: Keep risk fixed."}], "refs": [], "citations": []},
                "strength": {"text": "You're already strong at: Waiting.", "segments": [{"type": "text", "text": "You're already strong at: Waiting."}], "refs": [], "citations": []},
                "experiment": {},
                "has_citations": False,
            },
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "",
            "weekly_ai_is_generating": False,
        },
    )

    response = client.get("/dashboard")
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'data-weekly-ai-carousel-tab="1"' in response_text
    assert "AI Journal" in response_text
    assert "EURUSD" in response_text
    assert "revenge reflection" in response_text
    assert "/dashboard/journal/sessions" in response_text
    assert "/dashboard/journal/context-candidates" in response_text
    assert "dashboard_journal.js" in response_text


def test_dashboard_home_hides_ai_journal_carousel_tab_for_non_admin(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-journal-preview-plain",
        email="dashboard-journal-preview-plain@example.com",
    )
    review = _create_weekly_review(user, trade_account, prompt_id="weekly-journal-preview-hidden")
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": review,
            "weekly_ai_review_display": {},
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "",
            "weekly_ai_is_generating": False,
        },
    )

    response = client.get("/dashboard")
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'data-weekly-ai-carousel-tab="1"' not in response_text
    assert "AI Journal" not in response_text


def test_dashboard_journal_inline_renderer_is_chat_first():
    script = Path("static/js/dashboard_journal.js").read_text(encoding="utf-8")

    assert "data-dashboard-journal-start-form" not in script
    assert "data-journal-scope" not in script
    assert "[data-dashboard-journal-resolve-form]" in script
    assert "dashboardJournalInlineTitle" not in script
    assert "dashboardJournalInlineTags" not in script
    assert "dashboardJournalInlineNotes" not in script
    assert 'setAttribute("data-journal-tags-form"' not in script
    assert "[data-journal-chat-input]" in script
    assert ".focus()" in script


def test_dashboard_journal_unified_start_source_confirms_context_then_posts_message():
    template = Path("templates/index.html").read_text(encoding="utf-8")
    script = Path("static/js/dashboard_journal.js").read_text(encoding="utf-8")

    assert "dashboardJournalTrade" not in template
    assert "dashboardJournalDay" not in template
    assert "Open reflection" not in template
    assert "data-dashboard-journal-resolve-form" in template
    assert "data-dashboard-journal-candidates" in template
    assert "Choose the context for this reflection" in script
    assert "const visibleCandidates = candidates;" in script
    assert "postJson(createUrl, sessionBodyForCandidate(candidate))" in script
    assert "FXJSendJournalMessage" in script


def test_dashboard_journal_context_candidates_match_symbol_and_date(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-journal-resolver-trade",
        email="dashboard-journal-resolver-trade@example.com",
    )
    user.is_admin = True
    user.email_verified = True
    user.signup_status = "approved"
    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="BTCUSD",
        side="BUY",
        entry_price=65000,
        exit_price=65300,
        pnl=120,
        opened_at=datetime(2026, 5, 14, 9, 0),
        closed_at=datetime(2026, 5, 14, 10, 0),
    )
    db.session.add(trade)
    db.session.commit()

    response = client.post(
        "/dashboard/journal/context-candidates",
        json={"message": "Can we reflect on BTCUSD 2026-05-14?"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["recommended"]["scope_type"] == JournalSession.SCOPE_TRADE
    assert payload["recommended"]["session_payload"]["scope_trade_pubkey"] == trade.pubkey
    assert "BTCUSD" in payload["recommended"]["label"]


def test_dashboard_journal_context_candidates_vague_message_defaults_to_review_week(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-journal-resolver-week",
        email="dashboard-journal-resolver-week@example.com",
    )
    user.is_admin = True
    user.email_verified = True
    user.signup_status = "approved"
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1,
            exit_price=1.105,
            pnl=50,
            opened_at=datetime(2026, 5, 14, 9, 0),
            closed_at=datetime(2026, 5, 14, 10, 0),
        )
    )
    db.session.commit()

    response = client.post(
        "/dashboard/journal/context-candidates",
        json={"message": "What should I take away from this week?"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["recommended"]["scope_type"] == JournalSession.SCOPE_WEEK
    assert payload["recommended"]["session_payload"]["scope_date"] == "2026-05-11"
    assert any(candidate["scope_type"] == JournalSession.SCOPE_FREEFORM for candidate in payload["candidates"])
    assert any(candidate["scope_type"] == JournalSession.SCOPE_TRADE for candidate in payload["candidates"])


def test_dashboard_journal_context_candidates_unmatched_specific_ask_uses_open_context(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-journal-resolver-freeform",
        email="dashboard-journal-resolver-freeform@example.com",
    )
    user.is_admin = True
    user.email_verified = True
    user.signup_status = "approved"
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1,
            exit_price=1.105,
            pnl=50,
            opened_at=datetime(2026, 5, 8, 9, 0),
            closed_at=datetime(2026, 5, 8, 10, 0),
        )
    )
    db.session.commit()

    response = client.post(
        "/dashboard/journal/context-candidates",
        json={"message": "Reflect on DOGE"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["recommended"]["scope_type"] == JournalSession.SCOPE_FREEFORM


def test_dashboard_journal_context_candidates_blocks_non_admin_and_support_view(app_ctx, client, monkeypatch):
    _create_logged_in_user(
        client,
        username="dashboard-journal-resolver-plain",
        email="dashboard-journal-resolver-plain@example.com",
    )

    response = client.post(
        "/dashboard/journal/context-candidates",
        json={"message": "What happened this week?"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "admin_only"

    root_email = "dashboard-journal-resolver-support-root@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root, root_account = _create_logged_in_user(
        client,
        username="dashboard-journal-resolver-support-root",
        email=root_email,
    )
    root.is_admin = True
    root.email_verified = True
    root.signup_status = "approved"
    target = User(
        username="dashboard-journal-resolver-support-target",
        email="dashboard-journal-resolver-support-target@example.com",
        password="hashed-password",
        email_verified=True,
    )
    db.session.add(target)
    db.session.flush()
    db.session.add(
        TradeAccount(
            user_id=target.id,
            name="Target Account",
            account_type="CFD",
            is_default=True,
        )
    )
    db.session.commit()
    with client.session_transaction() as session_state:
        session_state["user_id"] = root.id
        session_state["username"] = root.username
        session_state["active_trade_account_id"] = root_account.id

    start_response = client.get(f"/dashboard/admin/users/{target.id}/view-dashboard", follow_redirects=False)
    assert start_response.status_code == 302

    support_response = client.post(
        "/dashboard/journal/context-candidates",
        json={"message": "What happened this week?"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert support_response.status_code == 403
    assert support_response.get_json()["error"] == "support_view_read_only"


def test_dashboard_mt5_card_uses_trial_setup_capacity_copy(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-mt5-trial-copy",
        email="dashboard-mt5-trial-copy@example.com",
    )
    batch = MT5SyncBatch(
        name="Internal Capacity",
        capacity_total=5,
        total_slots_claimed=0,
        is_open=True,
        opened_at=datetime(2026, 5, 12, 9, 0),
        created_at=datetime(2026, 5, 12, 9, 0),
        updated_at=datetime(2026, 5, 12, 9, 0),
    )
    db.session.add(batch)
    db.session.commit()

    try:
        response = client.get("/dashboard")
        response_text = response.get_data(as_text=True)

        assert response.status_code == 200
        assert "14-day premium workflow trial" not in response_text

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
                opened_at=datetime(2026, 3, 22, 8, 0, 0),
                closed_at=datetime(2026, 3, 22, 10, 0, 0),
            )
        )
        db.session.add(
            MT5Account(
                user_id=user.id,
                trade_account_id=trade_account.id,
                account_number="88442211",
                investor_password_encrypted="stored-token",
                server="Broker-Live",
                is_active=False,
                connection_status=MT5Account.CONNECTION_STATUS_FAILED,
                connection_error_message="Login failed.",
            )
        )
        db.session.commit()

        response = client.get("/dashboard")
        response_text = response.get_data(as_text=True)

        assert response.status_code == 200
        assert "14-day premium workflow trial" in response_text
        assert "Core journal stays free" in response_text
        assert "MT5 sync setup is available for this account during your premium workflow trial" in response_text
        assert "free MT5 sync slot" not in response_text
        assert "slots left" not in response_text
        assert "claim a sync slot" not in response_text
        assert "Fills fast" not in response_text
    finally:
        db.session.delete(batch)
        db.session.commit()


def _assert_dashboard_journal_session_payload(payload):
    assert payload["session_id"]
    assert payload["chat_url"].startswith("/dashboard/journal/sessions/")
    assert payload["tags_url"].startswith("/dashboard/journal/sessions/")
    assert payload["feedback_url_template"].endswith("/dashboard/journal/messages/0/feedback")
    assert isinstance(payload["context_summary"], dict)
    assert isinstance(payload["messages"], list)


def test_dashboard_journal_create_session_returns_inline_json_for_admin(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-journal-create-admin",
        email="dashboard-journal-create-admin@example.com",
    )
    user.is_admin = True
    user.email_verified = True
    user.signup_status = "approved"
    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1,
        exit_price=1.105,
        pnl=50,
        opened_at=datetime(2026, 5, 8, 9, 0),
        closed_at=datetime(2026, 5, 8, 10, 0),
    )
    db.session.add(trade)
    db.session.commit()

    trade_response = client.post(
        "/dashboard/journal/sessions",
        json={"scope_type": "trade", "scope_trade_pubkey": trade.pubkey},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert trade_response.status_code == 200
    trade_payload = trade_response.get_json()
    _assert_dashboard_journal_session_payload(trade_payload)
    assert trade_payload["context_summary"]["refs"][0]["label"].startswith("EURUSD")

    day_response = client.post(
        "/dashboard/journal/sessions",
        json={"scope_type": "day", "scope_date": "2026-05-08"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert day_response.status_code == 200
    _assert_dashboard_journal_session_payload(day_response.get_json())

    week_response = client.post(
        "/dashboard/journal/sessions",
        json={"scope_type": "week", "scope_date": "2026-05-04"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert week_response.status_code == 200
    _assert_dashboard_journal_session_payload(week_response.get_json())

    freeform_response = client.post(
        "/dashboard/journal/sessions",
        json={"scope_type": "freeform"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert freeform_response.status_code == 200
    _assert_dashboard_journal_session_payload(freeform_response.get_json())


def test_dashboard_journal_create_session_blocks_non_admin_with_json(app_ctx, client):
    _create_logged_in_user(
        client,
        username="dashboard-journal-create-plain",
        email="dashboard-journal-create-plain@example.com",
    )

    response = client.post(
        "/dashboard/journal/sessions",
        json={"scope_type": "freeform"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["error"] == "admin_only"
    assert payload["message"] == "AI journal is only available to admin accounts."


def test_dashboard_journal_create_session_blocks_support_view_with_json(app_ctx, client, monkeypatch):
    root_email = "dashboard-journal-support-root@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root, root_account = _create_logged_in_user(
        client,
        username="dashboard-journal-support-root",
        email=root_email,
    )
    root.is_admin = True
    root.email_verified = True
    root.signup_status = "approved"
    target = User(
        username="dashboard-journal-support-target",
        email="dashboard-journal-support-target@example.com",
        password="hashed-password",
        email_verified=True,
    )
    db.session.add(target)
    db.session.flush()
    db.session.add(
        TradeAccount(
            user_id=target.id,
            name="Target Account",
            account_type="CFD",
            is_default=True,
        )
    )
    db.session.commit()
    with client.session_transaction() as session_state:
        session_state["active_trade_account_id"] = root_account.id

    start_response = client.get(f"/dashboard/admin/users/{target.id}/view-dashboard", follow_redirects=False)
    assert start_response.status_code == 302

    response = client.post(
        "/dashboard/journal/sessions",
        json={"scope_type": "freeform"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["error"] == "support_view_read_only"
    assert payload["message"] == "That action is not available in read-only support view."


def test_dashboard_home_keeps_existing_chat_history_visible_after_trial_expiry(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-review-chat-history-expired-user",
        email="dashboard-review-chat-history-expired@example.com",
    )
    user.premium_trial_started_at = datetime.utcnow() - timedelta(days=20)
    db.session.commit()
    review = _create_weekly_review(user, trade_account, prompt_id="weekly-chat-history-expired")
    db.session.add_all(
        [
            WeeklyReviewChatMessage(
                user_id=user.id,
                trade_account_id=trade_account.id,
                ai_response_id=review.id,
                role=WeeklyReviewChatMessage.ROLE_USER,
                content="Old user question",
            ),
            WeeklyReviewChatMessage(
                user_id=user.id,
                trade_account_id=trade_account.id,
                ai_response_id=review.id,
                role=WeeklyReviewChatMessage.ROLE_ASSISTANT,
                content="Old assistant answer [T1]",
            ),
        ]
    )
    db.session.commit()
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": review,
            "weekly_ai_review_display": {
                "summary": {"text": "Summary", "segments": [{"type": "text", "text": "Summary"}], "refs": [], "citations": []},
                "takeaways": [],
                "improvement": {"text": "Improve this week: Keep risk fixed.", "segments": [{"type": "text", "text": "Improve this week: Keep risk fixed."}], "refs": [], "citations": []},
                "strength": {"text": "You're already strong at: Waiting.", "segments": [{"type": "text", "text": "You're already strong at: Waiting."}], "refs": [], "citations": []},
                "experiment": {},
                "has_citations": False,
            },
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "",
            "weekly_ai_is_generating": False,
        },
    )

    response = client.get("/dashboard")
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Summary" in response_text
    assert "Old user question" in response_text
    assert "Old assistant answer" in response_text
    assert "data-can-send=\"false\"" in response_text
    assert "Your free trial has ended" in response_text


def test_dashboard_home_keeps_existing_chat_history_visible_after_trial_message_cap(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-review-chat-history-cap-user",
        email="dashboard-review-chat-history-cap@example.com",
    )
    user.premium_trial_started_at = datetime.utcnow() - timedelta(days=1)
    review = _create_weekly_review(user, trade_account, prompt_id="weekly-chat-history-cap")
    for i in range(5):
        db.session.add(
            WeeklyReviewChatMessage(
                user_id=user.id,
                trade_account_id=trade_account.id,
                ai_response_id=review.id,
                role=WeeklyReviewChatMessage.ROLE_USER,
                content=f"Prior cap question {i}",
            )
        )
    db.session.commit()
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": review,
            "weekly_ai_review_display": {},
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "",
            "weekly_ai_is_generating": False,
        },
    )

    response = client.get("/dashboard")
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Prior cap question 4" in response_text
    assert "data-can-send=\"false\"" in response_text
    assert "Your free trial includes 5 follow-up messages" in response_text


def test_dashboard_home_keeps_legacy_weekly_review_chat_prompts_without_dynamic_context(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-review-chat-legacy-user",
        email="dashboard-review-chat-legacy@example.com",
    )
    review = _create_weekly_review(user, trade_account, prompt_id="weekly-chat-legacy")
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": review,
            "weekly_ai_review_display": {},
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "",
            "weekly_ai_is_generating": False,
        },
    )

    response = client.get("/dashboard")
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Explain this simply" in response_text
    assert "Was this bad luck or my execution?" in response_text
    assert "Which trade should I review first?" in response_text


def test_weekly_review_chat_prompts_use_review_insight_and_evidence_label():
    prompts = dashboard_routes._build_weekly_review_chat_prompts(
        {
            "summary": {
                "text": "One oversized XAUUSD loss made risk the main issue this week.",
                "citations": [{"inline_label": "XAUUSD", "label": "XAUUSD | 08 Apr 2026 (Wed)"}],
            },
            "takeaways": [],
            "improvement": {"text": "Improve this week: Keep risk fixed before entry."},
            "strength": {},
            "experiment": {},
        }
    )

    assert prompts[0] == "Why did XAUUSD carry so much weight?"
    assert "Was this bad luck or my execution?" not in prompts
    assert len(prompts) >= 4


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
    # Zero-data state: choose-your-path card + onboarding sample both render.
    assert b"Sample \xc2\xb7 Not your data" in response.data  # sample badge
    assert b"sample data, not your trading" in response.data  # choose-your-path card
    assert b"sizing up on" in response.data  # sample review insight text


def test_dashboard_home_shows_too_few_trades_weekly_ai_message(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-ai-thin-user",
        email="dashboard-ai-thin@example.com",
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
            opened_at=datetime(2026, 3, 22, 8, 0, 0),
            closed_at=datetime(2026, 3, 22, 10, 0, 0),
        )
    )
    db.session.commit()
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": None,
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "This week has limited trade data, so the AI review will stay cautious and avoid overconfident conclusions.",
            "weekly_ai_is_generating": False,
        },
    )

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"This week has limited trade data, so the AI review will stay cautious and avoid overconfident conclusions." in response.data


def test_dashboard_home_renders_experiment_card_when_present(app_ctx, client, monkeypatch):
    _create_logged_in_user(
        client,
        username="dashboard-ai-experiment-user",
        email="dashboard-ai-experiment@example.com",
    )
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": type("Review", (), {"response_text": "Summary"})(),
            "weekly_ai_review_display": {
                "summary": {"text": "Summary", "segments": [{"type": "text", "text": "Summary"}], "refs": [], "citations": []},
                "takeaways": [],
                "improvement": {"text": "Improve this week: Keep risk fixed.", "segments": [{"type": "text", "text": "Improve this week: Keep risk fixed."}], "refs": [], "citations": []},
                "strength": {"text": "You're already strong at: Staying selective.", "segments": [{"type": "text", "text": "You're already strong at: Staying selective."}], "refs": [], "citations": []},
                "experiment": {"text": "Run one-week London-only execution test.", "segments": [{"type": "text", "text": "Run one-week London-only execution test."}], "refs": [], "citations": []},
                "has_citations": False,
            },
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "",
            "weekly_ai_is_generating": False,
        },
    )

    response = client.get("/dashboard")
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "This Week's Experiment" in response_text
    assert "Run one-week London-only execution test." in response_text


def test_dashboard_home_renders_experiment_empty_state_when_review_has_no_experiment(app_ctx, client, monkeypatch):
    _create_logged_in_user(
        client,
        username="dashboard-ai-no-experiment-user",
        email="dashboard-ai-no-experiment@example.com",
    )
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": type("Review", (), {"response_text": "Summary"})(),
            "weekly_ai_review_display": {
                "summary": {"text": "Summary", "segments": [{"type": "text", "text": "Summary"}], "refs": [], "citations": []},
                "takeaways": [],
                "improvement": {"text": "Improve this week: Keep risk fixed.", "segments": [{"type": "text", "text": "Improve this week: Keep risk fixed."}], "refs": [], "citations": []},
                "strength": {"text": "", "segments": [], "refs": [], "citations": []},
                "experiment": {"text": "", "segments": [], "refs": [], "citations": []},
                "has_citations": False,
            },
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "",
            "weekly_ai_is_generating": False,
        },
    )

    response = client.get("/dashboard")
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "This Week's Experiment" in response_text
    assert "No experiment this week — focus on the improvement first" in response_text


def test_dashboard_home_uses_state_1_for_active_account_even_when_other_accounts_have_activity(app_ctx, client, monkeypatch):
    user, active_trade_account = _create_logged_in_user(
        client,
        username="dashboard-state-one-user",
        email="dashboard-state-one@example.com",
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

    second_account = TradeAccount(
        user_id=user.id,
        name="Second Account",
        account_type="CFD",
        is_default=False,
    )
    db.session.add(second_account)
    db.session.flush()

    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=second_account.id,
            symbol="GBPUSD",
            side="BUY",
            entry_price=1.25,
            exit_price=1.255,
            lot_size=0.02,
            pnl=25.0,
            opened_at=datetime(2026, 3, 22, 8, 0, 0),
            closed_at=datetime(2026, 3, 22, 10, 0, 0),
        )
    )
    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=second_account.id,
            account_number="55112233",
            investor_password_encrypted="stored-token",
            server="Broker-Live",
            is_active=True,
        )
    )
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'data-dashboard-state="state-1"' in response.data
    assert b"choose-your-path-card" in response.data
    assert b"panel journey-banner journey-banner--compact" not in response.data
    assert b"Want trades to sync automatically?" in response.data
    assert b"data-mt5-setup-wizard" in response.data
    assert b"Sample \xc2\xb7 Not your data" in response.data  # onboarding sample badge
    assert b"Import while setup runs</a>" not in response.data
    assert b"mt5-workflow-panel is-guided" in response.data
    assert b'id="trade-journal"' not in response.data
    assert b"Your weekly AI review" in response.data
    assert b"Week on Week" not in response.data
    assert b"Session Performance" not in response.data


def test_dashboard_home_uses_state_2_when_active_account_has_trades_without_active_mt5(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-state-two-user",
        email="dashboard-state-two@example.com",
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
            opened_at=datetime(2026, 3, 22, 8, 0, 0),
            closed_at=datetime(2026, 3, 22, 10, 0, 0),
        )
    )
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'data-dashboard-state="state-2"' in response.data
    assert b"Connect MT5 next" in response.data
    assert b"Nice, your report is in" in response.data
    assert b"Connect MT5 \xe2\x80\x94 automatic sync" in response.data
    assert b"data-mt5-setup-wizard" in response.data
    assert b"mt5-workflow-panel--compact" in response.data
    assert b"mt5-workflow-panel is-guided" not in response.data
    assert b"journey-banner is-guided" not in response.data
    assert b'id="trade-journal"' in response.data
    assert b"Weekly AI Review" in response.data
    assert b"Week on Week" in response.data
    assert b"Session Performance" in response.data


def test_dashboard_home_uses_state_3_when_active_account_has_active_mt5(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-state-three-user",
        email="dashboard-state-three@example.com",
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
        MT5Account(
            user_id=user.id,
            trade_account_id=trade_account.id,
            account_number="88442211",
            investor_password_encrypted="stored-token",
            server="Broker-Live",
            is_active=True,
        )
    )
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'data-dashboard-state="state-3"' in response.data
    assert b"Connect MT5 for automatic sync" not in response.data
    assert b"journey-banner is-guided" not in response.data
    assert b"mt5-workflow-panel is-guided" not in response.data
    assert b'id="trade-journal"' in response.data
    assert b"Weekly AI Review" in response.data
    assert b"Week on Week" in response.data
    assert b"Session Performance" in response.data


def test_build_dashboard_continuity_row_returns_status_only_without_next_action():
    review_stub = type("ReviewStub", (), {"response_text": "Weekly review body."})()
    result = dashboard_routes._build_dashboard_continuity_row(
        active_trade_account=type("AccountStub", (), {"id": 7})(),
        user_trades=[
            type("TradeStub", (), {"trade_account_id": 7, "opened_at": datetime(2026, 5, 24, 8, 0, 0)})(),
            type("TradeStub", (), {"trade_account_id": 7, "opened_at": datetime(2026, 5, 23, 8, 0, 0)})(),
        ],
        mt5_selected_row={
            "mt5_account": type(
                "Mt5AccountStub",
                (),
                {"last_synced_at": datetime(2026, 5, 25, 20, 43, 0)},
            )(),
            "status": "linked",
        },
        mt5_selected_status="linked",
        weekly_ai_state={
            "weekly_ai_review": review_stub,
            "weekly_ai_is_generating": False,
        },
        review_workflow_banner_state={"show_workflow_banner": False},
        dashboard_state="state-3",
        timezone_name="UTC",
        has_any_trades=True,
        has_closed_trades=True,
    )

    assert result == {
        "last_sync_label": "Synced 25 May 20:43",
        "new_trades_label": "2 new trades",
        "review_status_label": "Ready",
    }


def test_dashboard_home_embeds_continuity_in_mt5_panel_without_view_review_cta(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-continuity-mt5-user",
        email="dashboard-continuity-mt5@example.com",
    )
    fixed_now = datetime(2026, 5, 25, 20, 43, 0)
    monkeypatch.setattr(dashboard_routes, "utcnow_naive", lambda: fixed_now)
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": type("ReviewStub", (), {"response_text": "Weekly review body."})(),
            "weekly_ai_generated_at_label": "25 May 2026",
            "weekly_ai_period_label": "19-25 May",
            "weekly_ai_empty_message": "",
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
            opened_at=fixed_now - timedelta(days=1),
            closed_at=fixed_now - timedelta(hours=20),
        )
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="GBPUSD",
            side="SELL",
            entry_price=1.265,
            exit_price=1.26,
            lot_size=0.01,
            pnl=50.0,
            opened_at=fixed_now - timedelta(days=2),
            closed_at=fixed_now - timedelta(hours=18),
        )
    )
    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=trade_account.id,
            account_number="88442211",
            investor_password_encrypted="stored-token",
            server="Broker-Live",
            is_active=True,
            last_synced_at=fixed_now,
        )
    )
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'class="dash-continuity-row dash-continuity-row--mt5"' in response.data
    assert b"Synced 25 May 20:43" in response.data
    assert b"2 new trades" in response.data
    assert b"Review</dt>" in response.data
    assert b"View review" not in response.data
    assert b'class="dash-continuity-row panel"' not in response.data


def test_dashboard_home_shows_continuity_in_hero_when_no_cfd_accounts(app_ctx, client, monkeypatch):
    user = User(
        username="dashboard-continuity-futures-user",
        email="dashboard-continuity-futures@example.com",
        password="hashed-password",
    )
    db.session.add(user)
    db.session.flush()

    futures_account = TradeAccount(
        user_id=user.id,
        name="Futures Only",
        account_type="FUTURES",
        is_default=True,
    )
    db.session.add(futures_account)
    db.session.commit()

    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username
        session_state["display_timezone"] = "UTC"
        session_state["active_trade_account_id"] = futures_account.id

    fixed_now = datetime(2026, 5, 25, 20, 43, 0)
    monkeypatch.setattr(dashboard_routes, "utcnow_naive", lambda: fixed_now)
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": None,
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "No trades this week.",
            "weekly_ai_is_generating": False,
        },
    )

    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=futures_account.id,
            symbol="ES",
            side="BUY",
            entry_price=5200.0,
            exit_price=5210.0,
            lot_size=1.0,
            pnl=500.0,
            opened_at=fixed_now - timedelta(days=1),
            closed_at=fixed_now - timedelta(hours=4),
        )
    )
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'class="dash-continuity-row dash-continuity-row--mt5"' not in response.data
    assert b"ai-hero-grid no-mt5" in response.data
    assert b"ES" in response.data


def test_dashboard_futures_state_two_does_not_nudge_mt5(app_ctx, client, monkeypatch):
    user = User(
        username="dashboard-futures-state-two-user",
        email="dashboard-futures-state-two@example.com",
        password="hashed-password",
    )
    db.session.add(user)
    db.session.flush()

    futures_account = TradeAccount(
        user_id=user.id,
        name="Futures Active",
        account_type="FUTURES",
        is_default=True,
    )
    db.session.add(futures_account)
    db.session.commit()

    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username
        session_state["display_timezone"] = "UTC"
        session_state["active_trade_account_id"] = futures_account.id

    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": None,
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "No trades this week.",
            "weekly_ai_is_generating": False,
        },
    )

    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=futures_account.id,
            symbol="ES",
            side="BUY",
            entry_price=5200.0,
            exit_price=5210.0,
            lot_size=1.0,
            pnl=500.0,
            opened_at=datetime(2026, 3, 22, 8, 0, 0),
            closed_at=datetime(2026, 3, 22, 10, 0, 0),
        )
    )
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'data-dashboard-state="state-2"' in response.data
    assert b"Connect MT5 next" not in response.data
    assert b"Keep building your journal" in response.data
    assert b'id="mt5-access"' not in response.data
    assert b"data-mt5-setup-wizard" not in response.data


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
    assert b"running-trade" in response.data
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
    )
    db.session.add(bundled_trade)
    db.session.flush()
    apply_interpretation(
        bundled_trade,
        bundle_pubkey="bundle-dashboard-test",
        source="test",
        user_id=user.id,
    )
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'data-bundle="bundle-dashboard-test"' in response.data
    assert b">Bundled</span>" in response.data


def test_dashboard_recent_trade_rows_link_to_trade_detail(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-trade-row-link-user",
        email="dashboard-trade-row-link@example.com",
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
    )
    db.session.add(trade)
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"data-trade-detail-url=" in response.data
    assert f'/dashboard/trades/{trade.pubkey}"'.encode() in response.data
    assert b"trade-opened-link" in response.data


def test_dashboard_home_shows_possible_behavior_badges_in_recent_trades(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-behavior-badges-user",
        email="dashboard-behavior-badges@example.com",
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
            exit_price=1.0980,
            lot_size=1.0,
            pnl=-50.0,
            opened_at=datetime(2026, 3, 22, 8, 0, 0),
            closed_at=datetime(2026, 3, 22, 8, 20, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.0985,
            exit_price=1.0975,
            lot_size=1.5,
            pnl=-20.0,
            opened_at=datetime(2026, 3, 22, 8, 35, 0),
            closed_at=datetime(2026, 3, 22, 8, 50, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="GBPUSD",
            side="SELL",
            entry_price=1.2500,
            exit_price=1.2520,
            stop_loss=1.2550,
            lot_size=0.4,
            pnl=-10.0,
            opened_at=datetime(2026, 3, 22, 10, 0, 0),
            closed_at=datetime(2026, 3, 22, 10, 5, 0),
        ),
    ]
    db.session.add_all(trades)
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b">Possible Revenge</span>" in response.data
    assert b">Possible Reactive</span>" in response.data
    assert b">Possible Corrective</span>" in response.data


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
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-bundle-banner-user",
        email="dashboard-bundle-banner@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="NAS100",
            side="BUY",
            entry_price=18000.0,
            exit_price=17950.0,
            lot_size=0.01,
            pnl=-50.0,
            opened_at=datetime(2026, 3, 22, 8, 0, 0),
            closed_at=datetime(2026, 3, 22, 10, 0, 0),
        )
    )
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
    assert b"Bundle Review Required" in pending_response.data
    assert pending_response.data.count(b"Review Bundles") == 2
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
    assert b"Bundle Review Required" in response.data
    assert response.data.count(b"Review Bundles") == 2
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
    assert b"Trade Review Required" in response.data
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


def test_dashboard_home_normalizes_broken_improvement_prefix_in_weekly_ai_review(app_ctx, client, monkeypatch):
    _user, _trade_account = _create_logged_in_user(
        client,
        username="dashboard-ai-review-user",
        email="dashboard-ai-review@example.com",
    )

    review = type(
        "Review",
        (),
        {"response_text": "Key Takeaways\n- Supported insight.\n\u2192 Improve this week: Keep risk fixed."},
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
    assert "Improve this week: Keep risk fixed." in response_text
    assert "\u2192 Improve this week:" not in response_text


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
                    "improvement": {
                        "text": "Improve this week: Use the same filter that made T1 clean before adding back into B1.",
                        "refs": [],
                    },
                    "strength": {
                        "text": "You're already strong at: Treating T1 entries as single setups rather than layering early.",
                        "refs": [],
                    },
                    "experiment": {
                        "text": "Run one-session focus: take only London setups this week and compare execution quality.",
                        "refs": [],
                    },
                }
            ),
            "payload_json": json.dumps(
                {
                    "trades": [
                        {
                            "ref": "T1",
                            "trade_id": 101,
                            "symbol": "XAUUSD",
                            "opened_at": "2026-04-01T09:00:00Z",
                            "pnl": 125.0,
                            "is_bundle": False,
                            "bundle_pubkey": None,
                        },
                        {
                            "ref": "B1",
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
    assert "T1" not in display["improvement"]["text"]
    assert display["improvement"]["citations"] == []
    assert display["improvement"]["segments"] == [
        {"type": "text", "text": display["improvement"]["text"]},
    ]
    assert display["strength"]["citations"] == []
    assert display["strength"]["segments"] == [
        {"type": "text", "text": display["strength"]["text"]},
    ]
    assert display["experiment"]["segments"] == [
        {"type": "text", "text": display["experiment"]["text"]},
    ]
    assert display["experiment"]["text"].startswith("Run one-session focus")


def test_weekly_ai_review_display_falls_back_when_rewrite_loses_format():
    review = type(
        "Review",
        (),
        {
            "response_text": "Shorter but unstructured rewrite.",
            "pass_1_output": (
                "Original story.\n\n"
                "Key Takeaways\n"
                "- Original takeaway.\n"
                "Improve this week: Keep the plan simple."
            ),
            "pass_2_output": "Shorter but unstructured rewrite.",
            "prompt_version_pass_2": "rewrite-sha",
            "response_meta_json": json.dumps(
                {
                    "summary": {"text": "Original story.", "refs": []},
                    "takeaways": [{"text": "Original takeaway.", "refs": []}],
                    "improvement": {"text": "Improve this week: Keep the plan simple.", "refs": []},
                }
            ),
            "payload_json": "{}",
        },
    )()

    display = dashboard_routes._build_weekly_ai_review_display(review, "UTC")

    assert display["summary"]["text"] == "Original story."
    assert display["takeaways"][0]["text"] == "Original takeaway."
    assert display["improvement"]["text"] == "Improve this week: Keep the plan simple."


def test_weekly_ai_review_display_keeps_experiment_from_meta_with_rewrite():
    review = type(
        "Review",
        (),
        {
            "response_text": (
                "Rewritten story.\n\n"
                "Key Takeaways\n"
                "- Rewritten takeaway.\n"
                "Improve this week: Keep one rule."
            ),
            "pass_1_output": (
                "Original story.\n\n"
                "Key Takeaways\n"
                "- Original takeaway.\n"
                "Improve this week: Keep one rule."
            ),
            "pass_2_output": (
                "Rewritten story.\n\n"
                "Key Takeaways\n"
                "- Rewritten takeaway.\n"
                "Improve this week: Keep one rule."
            ),
            "prompt_version_pass_2": "rewrite-sha",
            "response_meta_json": json.dumps(
                {
                    "summary": {"text": "Original story.", "refs": []},
                    "takeaways": [{"text": "Original takeaway.", "refs": []}],
                    "improvement": {"text": "Improve this week: Keep one rule.", "refs": []},
                    "experiment": {
                        "text": "Run a one-week rule: take only the first valid setup each session.",
                        "refs": [],
                    },
                }
            ),
            "payload_json": "{}",
        },
    )()

    display = dashboard_routes._build_weekly_ai_review_display(review, "UTC")

    assert display["summary"]["text"] == "Rewritten story."
    assert display["takeaways"][0]["text"] == "Rewritten takeaway."
    assert display["experiment"]["text"] == "Run a one-week rule: take only the first valid setup each session."
    assert display["experiment"]["segments"] == [
        {"type": "text", "text": display["experiment"]["text"]},
    ]


def test_weekly_ai_review_display_drops_original_citations_when_rewrite_omits_trade_name():
    review = type(
        "Review",
        (),
        {
            "response_text": "Unused fallback text",
            "pass_1_output": (
                "Original story.\n\n"
                "Key Takeaways\n"
                "- Original takeaway.\n"
                "Improve this week: Keep the plan simple."
            ),
            "pass_2_output": (
                "This was easier to understand, but it no longer names the trade directly.\n\n"
                "Key Takeaways\n"
                "- The strongest idea mattered because the rest of the week was weaker.\n"
                "Improve this week: Keep the plan simple."
            ),
            "prompt_version_pass_2": "rewrite-sha",
            "response_meta_json": json.dumps(
                {
                    "summary": {"text": "Original story.", "refs": ["T1"]},
                    "takeaways": [{"text": "Original takeaway.", "refs": ["T1"]}],
                    "improvement": {"text": "Improve this week: Keep the plan simple.", "refs": []},
                }
            ),
            "payload_json": json.dumps(
                {
                    "trades": [
                        {
                            "ref": "T1",
                            "trade_id": 101,
                            "symbol": "GBPJPY",
                            "opened_at": "2026-04-20T09:00:00Z",
                            "pnl": 125.0,
                            "is_bundle": False,
                            "bundle_pubkey": None,
                        }
                    ]
                }
            ),
        },
    )()

    display = dashboard_routes._build_weekly_ai_review_display(review, "UTC")

    assert display["summary"]["citations"] == []
    assert display["takeaways"][0]["citations"] == []
    assert all(segment.get("type") != "citation" for segment in display["summary"]["segments"])
    assert all(segment.get("type") != "citation" for segment in display["takeaways"][0]["segments"])


def test_weekly_ai_review_display_does_not_append_unmentioned_ref_as_trailing_pill():
    review = type(
        "Review",
        (),
        {
            "response_text": "Unused fallback text",
            "response_meta_json": json.dumps(
                {
                    "summary": {
                        "text": "NAS100 rewarded the retry, while USDCAD exposed the cost.",
                        "refs": ["T1", "T2"],
                    },
                    "takeaways": [
                        {
                            "text": "USDCAD showed the same post-loss move could still be costly.",
                            "refs": ["T1", "T2"],
                        }
                    ],
                    "improvement": {"text": "Improve this week: Treat the next idea as fresh.", "refs": []},
                    "strength": {"text": "You're already strong at: Letting winners breathe.", "refs": []},
                }
            ),
            "payload_json": json.dumps(
                {
                    "trades": [
                        {
                            "ref": "T1",
                            "trade_id": 101,
                            "symbol": "NAS100",
                            "opened_at": "2026-04-27T09:00:00Z",
                            "pnl": 125.0,
                            "is_bundle": False,
                            "bundle_pubkey": None,
                        },
                        {
                            "ref": "T2",
                            "trade_id": 202,
                            "symbol": "USDCAD",
                            "opened_at": "2026-04-28T09:00:00Z",
                            "pnl": -75.0,
                            "is_bundle": False,
                            "bundle_pubkey": None,
                        },
                    ]
                }
            ),
        },
    )()

    display = dashboard_routes._build_weekly_ai_review_display(review, "UTC")

    summary_labels = [
        segment["label"]
        for segment in display["summary"]["segments"]
        if segment.get("type") == "citation"
    ]
    takeaway_labels = [
        segment["label"]
        for segment in display["takeaways"][0]["segments"]
        if segment.get("type") == "citation"
    ]

    assert summary_labels == [
        "NAS100 | 27 Apr 2026 (Mon)",
        "USDCAD | 28 Apr 2026 (Tue)",
    ]
    assert takeaway_labels == ["USDCAD | 28 Apr 2026 (Tue)"]
    assert "NAS100 | 27 Apr 2026 (Mon)" not in takeaway_labels


def test_rewrite_review_text_refs_drops_stray_clitic_after_brackets_and_labels():
    """Model sometimes emits '[B1]d' or 'B1 d' before 'trade'; avoid a lone 'd' after the pill."""
    lookup = {
        "B1": {
            "ref": "B1",
            "type": "bundle",
            "bundle_key": "bundle-key",
            "inline_label": "GBPUSD bundle",
            "label": "GBPUSD bundle | 31 Mar 2026 (Tue)",
            "tone": "bad",
        }
    }
    out = dashboard_routes._rewrite_review_text_refs(
        "The B1 d trade was the lone loss.",
        lookup,
    )
    assert out == "The GBPUSD bundle trade was the lone loss."

    out_bracket = dashboard_routes._rewrite_review_text_refs(
        "The [B1]d trade was the lone loss.",
        lookup,
    )
    assert out_bracket == "The trade was the lone loss."

    out_day = dashboard_routes._rewrite_review_text_refs(
        "The [B1] day trade was fine.",
        lookup,
    )
    assert out_day == "The day trade was fine."


def test_rewrite_review_text_refs_drops_clitic_after_full_dated_label():
    """Strip 'd/s' glued to closing paren when the model wrote the full pill text (e.g. gold bleed)."""
    lookup = {
        "B1": {
            "ref": "B1",
            "type": "bundle",
            "bundle_key": "bundle-key",
            "inline_label": "XAUUSD bundle",
            "label": "XAUUSD bundle | 06 Apr 2026 (Mon)",
            "tone": "bad",
        }
    }
    out = dashboard_routes._rewrite_review_text_refs(
        "Drawdown from XAUUSD bundle | 06 Apr 2026 (Mon)d position dominated.",
        lookup,
    )
    assert out == "Drawdown from XAUUSD bundle | 06 Apr 2026 (Mon) position dominated."

    out_spaced = dashboard_routes._rewrite_review_text_refs(
        "Drawdown from XAUUSD bundle | 06 Apr 2026 (Mon) d position dominated.",
        lookup,
    )
    assert out_spaced == "Drawdown from XAUUSD bundle | 06 Apr 2026 (Mon) position dominated."


def test_weekly_ai_review_display_segments_span_full_label_when_present():
    """Prefer matching the full dated label so the pill replaces the entire phrase (no stray tail)."""
    review = type(
        "Review",
        (),
        {
            "response_text": "Unused",
            "response_meta_json": json.dumps(
                {
                    "summary": {
                        "text": (
                            "Loss tied to XAUUSD bundle | 06 Apr 2026 (Mon) "
                            "when risk spiked."
                        ),
                        "refs": ["B1"],
                    },
                    "takeaways": [],
                    "improvement": {"text": "", "refs": []},
                    "strength": {"text": "", "refs": []},
                }
            ),
            "payload_json": json.dumps(
                {
                    "trades": [
                        {
                            "ref": "B1",
                            "trade_id": 1,
                            "symbol": "XAUUSD",
                            "opened_at": "2026-04-06T12:00:00Z",
                            "pnl": -50.0,
                            "is_bundle": True,
                            "bundle_pubkey": "bundle-abc",
                        }
                    ]
                }
            ),
        },
    )()

    display = dashboard_routes._build_weekly_ai_review_display(review, "UTC")
    segs = display["summary"]["segments"]
    cite = next(s for s in segs if s["type"] == "citation")
    assert cite["label"] == "XAUUSD bundle | 06 Apr 2026 (Mon)"
    after = segs[segs.index(cite) + 1]
    assert after["type"] == "text"
    assert "| 06 Apr" not in after["text"]
    assert after["text"].startswith(" when risk")


def test_weekly_ai_review_display_autocites_unique_symbol_mentions():
    review = type(
        "Review",
        (),
        {
            "response_text": "Unused fallback text",
            "response_meta_json": json.dumps(
                {
                    "summary": {
                        "text": "EURCHF used split entries cleanly while the rest of the week stayed mixed.",
                        "refs": [],
                    },
                    "takeaways": [
                        {
                            "text": "EURCHF held together better than the other continuation ideas.",
                            "refs": [],
                        }
                    ],
                    "improvement": {
                        "text": "Improve this week: Keep the same confirmation standard on split entries next week.",
                        "refs": [],
                    },
                    "strength": {
                        "text": "You're already strong at: Maintaining consistent sizing across continuation ideas.",
                        "refs": [],
                    },
                }
            ),
            "payload_json": json.dumps(
                {
                    "trades": [
                        {
                            "ref": "T1",
                            "trade_id": 303,
                            "symbol": "EURCHF",
                            "opened_at": "2026-04-03T08:00:00Z",
                            "pnl": 48.0,
                            "is_bundle": False,
                            "bundle_pubkey": None,
                        },
                        {
                            "ref": "T2",
                            "trade_id": 404,
                            "symbol": "XAUUSD",
                            "opened_at": "2026-04-03T11:00:00Z",
                            "pnl": -22.0,
                            "is_bundle": False,
                            "bundle_pubkey": None,
                        },
                    ]
                }
            ),
        },
    )()

    display = dashboard_routes._build_weekly_ai_review_display(review, "UTC")

    assert display["summary"]["segments"][0]["type"] == "citation"
    assert display["summary"]["segments"][0]["label"] == "EURCHF | 03 Apr 2026 (Fri)"
    assert display["summary"]["segments"][0]["tone"] == "good"
    assert display["takeaways"][0]["segments"][0]["type"] == "citation"
    assert display["takeaways"][0]["segments"][0]["label"] == "EURCHF | 03 Apr 2026 (Fri)"
    assert display["improvement"]["segments"] == [
        {"type": "text", "text": display["improvement"]["text"]},
    ]
    assert display["strength"]["segments"] == [
        {"type": "text", "text": display["strength"]["text"]},
    ]
    assert display["experiment"]["segments"] == []


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

    weekly_ai_state = dashboard_routes._get_weekly_ai_state(user.id, trade_account, "UTC", [])

    assert weekly_ai_state["weekly_ai_review"] is not None
    assert weekly_ai_state["weekly_ai_review"].id == review.id
    assert weekly_ai_state["weekly_ai_review_text"] == "Older but valid weekly review"
    assert weekly_ai_state["weekly_ai_period_label"] == "Sat 14 Mar 2026 21:30 UTC"
    assert weekly_ai_state["weekly_ai_is_generating"] is False


def test_dashboard_home_shows_onboarding_banner_when_profile_was_skipped(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="dashboard-onboarding-skip-user",
        email="dashboard-onboarding-skip@example.com",
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
            opened_at=datetime(2026, 3, 22, 8, 0, 0),
            closed_at=datetime(2026, 3, 22, 10, 0, 0),
        )
    )
    review = _create_weekly_review(user, trade_account)
    db.session.add(UserProfile(user_id=user.id, skipped=True))
    db.session.commit()

    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": review,
            "weekly_ai_review_display": None,
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "",
            "weekly_ai_is_generating": False,
        },
    )

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Complete profile" in response.data
    assert b"Finish your trading profile" in response.data
