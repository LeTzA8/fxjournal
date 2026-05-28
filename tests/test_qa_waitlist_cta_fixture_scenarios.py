"""Scenario render and waitlist integration tests for QA CTA fixtures (landing 2)."""

from __future__ import annotations

import pytest

import routes.dashboard as dashboard_routes
from extensions import limiter
from cli.qa_fixtures.registry import SCENARIO_REGISTRY, all_scenario_keys, fixture_emails_for_keys
from cli.qa_fixtures.scenarios import reset_fixture_users, seed_all_fixture_scenarios
from models import QaTestAccount, Trade, TradeAccount, UpgradeWaitlistEntry, User, db


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def seeded_cta_fixtures(app_ctx):
    emails = fixture_emails_for_keys(all_scenario_keys())
    UpgradeWaitlistEntry.query.filter(UpgradeWaitlistEntry.email.in_(emails)).delete(
        synchronize_session=False
    )
    db.session.commit()
    seed_all_fixture_scenarios()
    for key in all_scenario_keys():
        email = SCENARIO_REGISTRY[key]["email"]
        assert User.query.filter_by(email=email).one_or_none() is not None, (
            f"Fixture user missing after seed: {email}"
        )
    yield
    db.session.rollback()
    reset_fixture_users()


def _login_fixture(client, scenario_key: str):
    """Use session login to avoid login rate limits when the suite runs many tests."""
    meta = SCENARIO_REGISTRY[scenario_key]
    user = User.query.filter_by(email=meta["email"]).one()
    trade_account = (
        TradeAccount.query.filter_by(user_id=user.id, is_default=True).first()
        or TradeAccount.query.filter_by(user_id=user.id)
        .order_by(TradeAccount.id.asc())
        .first()
    )
    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username
        session_state["display_timezone"] = "UTC"
        if trade_account is not None:
            session_state["active_trade_account_id"] = trade_account.id


def _get_fixture_trade(scenario_key: str) -> Trade | None:
    email = SCENARIO_REGISTRY[scenario_key]["email"]
    user = User.query.filter_by(email=email).one()
    return Trade.query.filter_by(user_id=user.id).order_by(Trade.id.asc()).first()


def test_scenario_replay_lock_renders_1m_lock_card(client, app_ctx, seeded_cta_fixtures):
    _login_fixture(client, "replay_lock")
    trade = _get_fixture_trade("replay_lock")
    assert trade is not None

    detail = client.get(f"/dashboard/trades/{trade.pubkey}")
    assert detail.status_code == 200
    html = detail.data.decode("utf-8")
    assert "trade-chart-tf-btn--gated" in html
    assert 'data-trade-tf="M1"' in html
    assert "tradeChartLockMount" in html

    chart = client.get(f"/api/trades/{trade.pubkey}/chart-data?timeframe=M1")
    assert chart.status_code == 403
    payload = chart.get_json()
    assert payload["error"] == "upgrade_required"
    assert payload["cta"]["source"] == "replay_lock"
    assert payload["cta"]["feature_interest"] == "advanced_replay"
    assert payload["cta"]["cta_context"] == "trade_replay_1m"

    m5 = client.get(f"/api/trades/{trade.pubkey}/chart-data?timeframe=M5")
    assert m5.status_code == 200
    assert m5.get_json()["status"] == "ready"


def test_scenario_ai_followup_exhausted(client, app_ctx, seeded_cta_fixtures):
    _login_fixture(client, "ai_followup")
    response = client.get("/dashboard")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert 'data-waitlist-source="ai_followup_lock"' in html
    assert 'data-waitlist-feature="weekly_followup_chat"' in html
    assert 'data-waitlist-cta-context="weekly_followup_trial_limit"' in html


def test_scenario_mt5_expired_card_renders_both_buttons(client, app_ctx, seeded_cta_fixtures):
    _login_fixture(client, "mt5_expired")
    response = client.get("/dashboard")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert 'data-waitlist-cta-context="mt5_trial_expired_extension"' in html
    assert 'data-waitlist-cta-context="mt5_trial_expired_waitlist"' in html


def test_scenario_mt5_paused_renders_join_only(client, app_ctx, seeded_cta_fixtures):
    _login_fixture(client, "mt5_paused")
    response = client.get("/dashboard")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert html.count('data-waitlist-cta-context="mt5_sync_paused_waitlist"') == 1
    assert 'data-waitlist-cta-context="mt5_trial_expired_extension"' not in html


def test_scenario_zero_data(client, app_ctx, seeded_cta_fixtures, monkeypatch):
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": None,
            "weekly_ai_review_display": None,
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "No trades this week.",
            "weekly_ai_is_generating": False,
        },
    )
    _login_fixture(client, "zero_data")
    response = client.get("/dashboard")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert b"choose-your-path-card" in response.data
    assert 'data-waitlist-source="ai_followup_lock"' not in html
    assert 'data-waitlist-cta-context="mt5_trial_expired_extension"' not in html
    assert 'data-waitlist-cta-context="trade_replay_1m"' not in html


def test_scenario_normal_active_no_expired_card(client, app_ctx, seeded_cta_fixtures, monkeypatch):
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": None,
            "weekly_ai_review_display": None,
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "",
            "weekly_ai_is_generating": False,
        },
    )
    _login_fixture(client, "normal_active")
    response = client.get("/dashboard")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert 'data-waitlist-cta-context="mt5_trial_expired_extension"' not in html
    assert 'data-waitlist-cta-context="mt5_sync_paused_waitlist"' not in html
    assert 'data-waitlist-source="ai_followup_lock"' not in html


@pytest.mark.parametrize(
    ("scenario_key", "waitlist_payload"),
    [
        (
            "replay_lock",
            {
                "tier": "trader",
                "source": "replay_lock",
                "feature_interest": "advanced_replay",
                "cta_context": "trade_replay_1m",
            },
        ),
        (
            "ai_followup",
            {
                "tier": "trader",
                "source": "ai_followup_lock",
                "feature_interest": "weekly_followup_chat",
                "cta_context": "weekly_followup_trial_limit",
            },
        ),
        (
            "mt5_expired",
            {
                "tier": "trader",
                "source": "mt5_trial_expired",
                "feature_interest": "mt5_trial_extension",
                "cta_context": "mt5_trial_expired_extension",
            },
        ),
        (
            "mt5_expired",
            {
                "tier": "trader",
                "source": "mt5_trial_expired",
                "feature_interest": "mt5_sync",
                "cta_context": "mt5_trial_expired_waitlist",
            },
        ),
        (
            "mt5_paused",
            {
                "tier": "trader",
                "source": "mt5_trial_expired",
                "feature_interest": "mt5_sync",
                "cta_context": "mt5_sync_paused_waitlist",
            },
        ),
    ],
)
def test_fixture_waitlist_submission_appears_in_admin_waitlist(
    client,
    app_ctx,
    seeded_cta_fixtures,
    scenario_key,
    waitlist_payload,
):
    _login_fixture(client, scenario_key)
    email = SCENARIO_REGISTRY[scenario_key]["email"]
    submit = client.post(
        "/pricing/waitlist",
        json={"email": email, **waitlist_payload},
    )
    assert submit.status_code == 200
    assert submit.get_json()["ok"] is True

    admin = User(
        username=f"cta-fixture-admin-{scenario_key}",
        email=f"cta-fixture-admin-{scenario_key}@example.com",
        password="hashed",
        email_verified=True,
        signup_status="approved",
        is_admin=True,
    )
    db.session.add(admin)
    db.session.commit()
    admin_id = admin.id
    with client.session_transaction() as session_state:
        session_state.clear()
        session_state["user_id"] = admin.id
        session_state["username"] = admin.username

    admin_page = client.get(
        f"/dashboard/admin/access/waitlist?source={waitlist_payload['source']}"
    )
    assert admin_page.status_code == 200
    assert email.encode() in admin_page.data

    row = UpgradeWaitlistEntry.query.filter_by(
        email=email,
        source=waitlist_payload["source"],
        feature_interest=waitlist_payload["feature_interest"],
    ).one()
    assert row.cta_context == waitlist_payload["cta_context"]

    db.session.delete(admin)
    db.session.commit()
