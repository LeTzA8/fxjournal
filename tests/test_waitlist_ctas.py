import pytest

from extensions import limiter
from models import UpgradeWaitlistEntry, User, db


@pytest.fixture(autouse=True)
def reset_waitlist_rate_limiter():
    limiter.reset()


@pytest.mark.parametrize(
    "source",
    [
        "replay_lock",
        "ai_followup_lock",
        "landing_planned",
        "dashboard_planned",
    ],
)
def test_waitlist_accepts_new_contextual_sources(client, app_ctx, source):
    response = client.post(
        "/pricing/waitlist",
        json={
            "email": f"{source}-user@example.com",
            "tier": "trader",
            "source": source,
            "feature_interest": "advanced_replay",
            "cta_context": f"{source}_context",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True

    row = UpgradeWaitlistEntry.query.filter_by(email=f"{source}-user@example.com").one()
    assert row.source == source
    assert row.feature_interest == "advanced_replay"
    assert row.cta_context == f"{source}_context"


@pytest.mark.parametrize(
    "feature_interest",
    ["weekly_followup_chat", "mt5_trial_extension"],
)
def test_waitlist_accepts_new_feature_interests(client, app_ctx, feature_interest):
    response = client.post(
        "/pricing/waitlist",
        json={
            "email": f"{feature_interest}@example.com",
            "tier": "trader",
            "source": "pricing_page",
            "feature_interest": feature_interest,
            "cta_context": "pricing_tier_card",
        },
    )

    assert response.status_code == 200
    row = UpgradeWaitlistEntry.query.filter_by(email=f"{feature_interest}@example.com").one()
    assert row.feature_interest == feature_interest


def test_replay_gate_source_still_accepted_as_deprecated_alias(client, app_ctx):
    response = client.post(
        "/pricing/waitlist",
        json={
            "email": "replay-gate-alias@example.com",
            "tier": "trader",
            "source": "replay_gate",
            "feature_interest": "advanced_replay",
            "cta_context": "legacy_replay_gate",
        },
    )

    assert response.status_code == 200
    row = UpgradeWaitlistEntry.query.filter_by(email="replay-gate-alias@example.com").one()
    assert row.source == "replay_gate"


def test_waitlist_duplicate_preserves_first_cta_context(client, app_ctx):
    """Confirms first-touch invariant — `_enrich_existing_entry` fills missing values, never overwrites existing `cta_context`."""
    payload = {
        "email": "cta-context-user@example.com",
        "tier": "trader",
        "source": "ai_followup_lock",
        "feature_interest": "weekly_followup_chat",
        "cta_context": "weekly_followup_trial_limit",
    }
    first = client.post("/pricing/waitlist", json=payload)
    assert first.status_code == 200

    second_payload = dict(payload)
    second_payload["cta_context"] = "weekly_followup_trial_required"
    second = client.post("/pricing/waitlist", json=second_payload)
    assert second.status_code == 200

    row = UpgradeWaitlistEntry.query.filter_by(email=payload["email"]).one()
    assert row.cta_context == "weekly_followup_trial_limit"


def test_waitlist_rejects_invalid_source(client, app_ctx):
    response = client.post(
        "/pricing/waitlist",
        json={
            "email": "invalid-source@example.com",
            "tier": "trader",
            "source": "not_in_allowlist",
            "feature_interest": "advanced_replay",
        },
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error"]


def test_waitlist_rejects_invalid_feature_interest(client, app_ctx):
    response = client.post(
        "/pricing/waitlist",
        json={
            "email": "invalid-feature@example.com",
            "tier": "trader",
            "source": "pricing_page",
            "feature_interest": "not_in_allowlist",
        },
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error"]


def test_dashboard_mt5_expired_card_renders_both_buttons(app_ctx, client, monkeypatch):
    from datetime import datetime, timedelta

    from cryptography.fernet import Fernet

    from helpers.entitlements import MT5_TRIAL_DAYS
    from helpers.utils import encrypt_password, utcnow_naive
    from models import MT5Account
    from tests.test_mt5_access_requests import (
        _create_user_with_account,
        _log_in_user,
        _stub_weekly_ai_state,
    )

    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _stub_weekly_ai_state(monkeypatch)

    user, trade_account = _create_user_with_account(
        username="waitlist-mt5-expired-user",
        email="waitlist-mt5-expired@example.com",
        account_name="Expired Trial Account",
    )
    user.plan_tier = "free"
    user.premium_trial_started_at = datetime.utcnow() - timedelta(days=MT5_TRIAL_DAYS + 1)
    _log_in_user(client, user, trade_account)

    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=trade_account.id,
            account_number="70019901",
            investor_password_encrypted=encrypt_password("investor-pass"),
            server="Broker-Live",
            is_active=True,
            last_synced_at=utcnow_naive(),
            mt5_trial_started_at=datetime.utcnow() - timedelta(days=MT5_TRIAL_DAYS + 1),
        )
    )
    db.session.commit()

    response = client.get("/dashboard")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert 'data-waitlist-cta-context="mt5_trial_expired_extension"' in html
    assert 'data-waitlist-cta-context="mt5_trial_expired_waitlist"' in html


def test_dashboard_mt5_paused_card_renders_join_only(app_ctx, client, monkeypatch):
    from cryptography.fernet import Fernet

    from helpers.utils import encrypt_password, utcnow_naive
    from models import MT5Account
    from tests.test_mt5_access_requests import (
        _create_user_with_account,
        _log_in_user,
        _stub_weekly_ai_state,
    )

    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _stub_weekly_ai_state(monkeypatch)

    user, trade_account = _create_user_with_account(
        username="waitlist-mt5-paused-user",
        email="waitlist-mt5-paused@example.com",
        account_name="Paused Account",
    )
    _log_in_user(client, user, trade_account)

    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=trade_account.id,
            account_number="70019902",
            investor_password_encrypted=encrypt_password("investor-pass"),
            server="Broker-Live",
            is_active=True,
            sync_paused_at=utcnow_naive(),
            sync_pause_reason="trial_expired",
        )
    )
    db.session.commit()

    response = client.get("/dashboard")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert html.count('data-waitlist-cta-context="mt5_sync_paused_waitlist"') == 1
    assert 'data-waitlist-feature="mt5_trial_extension"' not in html
    assert 'data-waitlist-cta-context="mt5_trial_expired_extension"' not in html


def test_trade_chart_upgrade_required_includes_replay_lock_cta(client, app_ctx):
    from tests.test_trades_routes import _create_closed_mt5_trade_with_m5_bars, _create_logged_in_user

    user, trade_account = _create_logged_in_user(
        client,
        username="waitlist-chart-lock-user",
        email="waitlist-chart-lock@example.com",
    )
    user.plan_tier = "free"
    db.session.commit()
    trade = _create_closed_mt5_trade_with_m5_bars(user, trade_account, "8880001-waitlist")

    response = client.get(f"/api/trades/{trade.pubkey}/chart-data?timeframe=M1")

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["error"] == "upgrade_required"
    assert payload["cta"]["source"] == "replay_lock"
    assert payload["cta"]["feature_interest"] == "advanced_replay"
    assert payload["cta"]["cta_context"] == "trade_replay_1m"


def test_weekly_followup_chat_cta_uses_ai_followup_lock_source(app_ctx, client):
    from datetime import datetime, timedelta

    from tests.test_dashboard_weekly_ai import _create_logged_in_user, _create_weekly_review

    user, trade_account = _create_logged_in_user(
        client,
        username="waitlist-chat-cta-user",
        email="waitlist-chat-cta@example.com",
    )
    user.premium_trial_started_at = datetime.utcnow() - timedelta(days=30)
    db.session.commit()
    review = _create_weekly_review(user, trade_account, prompt_id="waitlist-chat-cta")

    response = client.post(
        f"/dashboard/weekly-review/{review.id}/chat",
        json={"message": "Can I ask one more?"},
    )
    payload = response.get_json()
    assert response.status_code == 403
    assert payload["cta"]["source"] == "ai_followup_lock"
    assert payload["cta"]["feature_interest"] == "weekly_followup_chat"


def test_dashboard_chat_lock_renders_waitlist_trigger_attributes(app_ctx, client, monkeypatch):
    from datetime import datetime, timedelta

    import routes.dashboard as dashboard_routes
    from tests.test_dashboard_weekly_ai import _create_logged_in_user, _create_weekly_review

    user, trade_account = _create_logged_in_user(
        client,
        username="waitlist-chat-render-user",
        email="waitlist-chat-render@example.com",
    )
    user.premium_trial_started_at = datetime.utcnow() - timedelta(days=30)
    db.session.commit()
    review = _create_weekly_review(user, trade_account, prompt_id="waitlist-chat-render")
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
            "weekly_ai_generated_at_label": "Generated 1 Jan",
            "weekly_ai_period_label": "Week of 1 Jan",
            "weekly_ai_empty_message": "",
            "weekly_ai_is_generating": False,
        },
    )

    response = client.get("/dashboard")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert 'data-waitlist-source="ai_followup_lock"' in html
    assert 'data-waitlist-feature="weekly_followup_chat"' in html
    assert 'data-waitlist-cta-context="weekly_followup_trial_required"' in html


def test_landing_includes_waitlist_modal_and_roadmap_cta(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert 'id="waitlistModal"' in html
    assert "css/app_pages.css" in html
    assert 'data-waitlist-source="landing_planned"' in html
    assert "landing-roadmap" in html


def test_seo_page_does_not_include_waitlist_modal(client):
    response = client.get("/trading-journal")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert 'id="waitlistModal"' not in html


def test_trade_entry_includes_waitlist_modal_for_chart_lock(client, app_ctx):
    from tests.test_trades_routes import _create_closed_mt5_trade_with_m5_bars, _create_logged_in_user

    user, trade_account = _create_logged_in_user(
        client,
        username="waitlist-trade-entry-user",
        email="waitlist-trade-entry@example.com",
    )
    trade = _create_closed_mt5_trade_with_m5_bars(user, trade_account, "8880002-waitlist-entry")

    response = client.get(f"/dashboard/trades/{trade.pubkey}")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert 'id="waitlistModal"' in html
    assert 'data-trade-tf="M1"' in html
    assert 'id="tradeChartLockMount"' in html


def test_admin_waitlist_page_is_admin_gated(client):
    response = client.get("/dashboard/admin/access/waitlist")
    assert response.status_code == 404


def test_admin_waitlist_csv_export(app_ctx, client, monkeypatch):
    from tests.test_admin_route_gating import _create_user, _login_as

    monkeypatch.setenv("ADMIN_USER_EMAILS", "admin-waitlist-export@example.com")
    admin = _create_user(
        username="admin-waitlist-export",
        email="admin-waitlist-export@example.com",
        is_admin=True,
    )
    db.session.add(
        UpgradeWaitlistEntry(
            email="csv-waitlist@example.com",
            tier_intent="trader",
            source="replay_lock",
            feature_interest="advanced_replay",
            cta_context="trade_replay_1m",
        )
    )
    db.session.commit()
    _login_as(client, admin)

    response = client.get("/dashboard/admin/access/waitlist/export")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    body = response.data.decode("utf-8")
    assert "email,user,tier_intent,feature_interest,source,cta_context,created_at" in body
    assert "csv-waitlist@example.com" in body
    assert "replay_lock" in body
