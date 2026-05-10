from extensions import limiter
from models import UpgradeWaitlistEntry, User, db


def test_pricing_waitlist_stores_intent_fields(client, app_ctx):
    response = client.post(
        "/pricing/waitlist",
        json={
            "email": "waitlist-user@example.com",
            "tier": "trader",
            "source": "pricing_page",
            "feature_interest": "advanced_replay",
            "cta_context": "pricing_tier_card",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True

    row = UpgradeWaitlistEntry.query.filter_by(email="waitlist-user@example.com").first()
    assert row is not None
    assert row.tier_intent == "trader"
    assert row.source == "pricing_page"
    assert row.feature_interest == "advanced_replay"
    assert row.cta_context == "pricing_tier_card"


def test_pricing_waitlist_duplicate_submission_is_graceful(client, app_ctx):
    payload = {
        "email": "duplicate-user@example.com",
        "tier": "pro",
        "source": "pricing_page",
        "feature_interest": "multi_timeframe_replay",
        "cta_context": "pricing_tier_card",
    }
    first = client.post("/pricing/waitlist", json=payload)
    second = client.post("/pricing/waitlist", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()["ok"] is True
    assert second.get_json()["ok"] is True

    count = (
        db.session.query(UpgradeWaitlistEntry)
        .filter_by(
            email=payload["email"],
            tier_intent=payload["tier"],
            source=payload["source"],
            feature_interest=payload["feature_interest"],
        )
        .count()
    )
    assert count == 1


def test_pricing_waitlist_duplicate_logged_in_submission_enriches_user_id(client, app_ctx):
    limiter.reset()
    payload = {
        "email": "duplicate-auth-user@example.com",
        "tier": "trader",
        "source": "pricing_page",
        "feature_interest": "advanced_replay",
        "cta_context": "pricing_tier_card",
    }

    first = client.post("/pricing/waitlist", json=payload)
    user = User(
        username="waitlist-auth-user",
        email="waitlist-auth-user@example.com",
        password="hashed-password",
    )
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username

    second = client.post("/pricing/waitlist", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    row = UpgradeWaitlistEntry.query.filter_by(email=payload["email"]).one()
    assert row.user_id == user.id


def test_pricing_waitlist_rate_limit_returns_json(client, app_ctx):
    limiter.reset()
    try:
        blocked_response = None
        for index in range(6):
            blocked_response = client.post(
                "/pricing/waitlist",
                json={
                    "email": f"rate-limit-{index}@example.com",
                    "tier": "trader",
                    "source": "pricing_page",
                    "feature_interest": "advanced_replay",
                },
            )

        assert blocked_response.status_code == 429
        payload = blocked_response.get_json()
        assert payload["ok"] is False
        assert payload["error"] == "Too many requests. Please try again later."
    finally:
        limiter.reset()
