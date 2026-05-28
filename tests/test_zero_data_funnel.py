from datetime import datetime, timedelta

import pytest

import routes.dashboard as dashboard_routes
from helpers.utils import utcnow_naive
from models import MT5AccessRequest, MT5Account, Trade, TradeAccount, User, db


@pytest.fixture(autouse=True)
def _reset_rate_limiter_for_isolation():
    from extensions import limiter

    limiter.reset()
    yield


def _create_logged_in_user(client, username, email, *, created_at=None):
    user = User(
        username=username,
        email=email,
        password="hashed-password",
        email_verified=True,
        signup_status="approved",
    )
    if created_at is not None:
        user.created_at = created_at
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


def _patch_weekly_ai_empty(monkeypatch):
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": None,
            "weekly_ai_review_display": None,
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "No trades this week. Add closed trades to generate your AI review.",
            "weekly_ai_is_generating": False,
        },
    )


def test_dashboard_zero_data_shows_choose_your_path_card(app_ctx, client, monkeypatch):
    _create_logged_in_user(
        client,
        username="zero-data-card-user",
        email="zero-data-card@example.com",
    )
    _patch_weekly_ai_empty(monkeypatch)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"choose-your-path-card" in response.data
    assert b"Upload report" in response.data
    assert b"Add first trade" in response.data
    assert b"Submit MT5 details" in response.data
    assert b"Preview sample" in response.data
    assert b"panel journey-banner journey-banner--compact" not in response.data
    assert b'journey-banner-kicker">What\'s next' not in response.data
    assert b"Import while setup runs" not in response.data


def test_dashboard_zero_data_card_links_to_correct_routes(app_ctx, client, monkeypatch):
    _create_logged_in_user(
        client,
        username="zero-data-links-user",
        email="zero-data-links@example.com",
    )
    _patch_weekly_ai_empty(monkeypatch)

    response = client.get("/dashboard")
    html = response.data.decode("utf-8", errors="ignore")

    assert response.status_code == 200
    assert "/dashboard/trades/new" in html
    assert 'href="#mt5-access"' in html
    assert 'id="weekly-ai-sample"' in html


def test_dashboard_zero_data_does_not_show_card_when_user_has_trades(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="zero-data-has-trades",
        email="zero-data-has-trades@example.com",
    )
    _patch_weekly_ai_empty(monkeypatch)
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.08,
            exit_price=1.09,
            lot_size=0.01,
            pnl=10.0,
            opened_at=datetime(2026, 3, 20, 8, 0, 0),
            closed_at=datetime(2026, 3, 20, 10, 0, 0),
        )
    )
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"choose-your-path-card" not in response.data
    assert b"journey-banner" in response.data


def test_dashboard_zero_data_does_not_show_card_when_user_has_mt5_submission(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="zero-data-mt5-user",
        email="zero-data-mt5@example.com",
    )
    _patch_weekly_ai_empty(monkeypatch)
    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=trade_account.id,
            account_number="99887766",
            investor_password_encrypted="stored-token",
            server="Broker-Demo",
            is_active=False,
        )
    )
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"choose-your-path-card" not in response.data
    assert b"journey-banner" in response.data


def test_dashboard_zero_data_does_not_show_card_when_user_has_mt5_access_request(
    app_ctx, client, monkeypatch
):
    user, trade_account = _create_logged_in_user(
        client,
        username="zero-data-mt5-req",
        email="zero-data-mt5-req@example.com",
    )
    _patch_weekly_ai_empty(monkeypatch)
    db.session.add(
        MT5AccessRequest(
            user_id=user.id,
            trade_account_id=trade_account.id,
            status=MT5AccessRequest.STATUS_PENDING,
        )
    )
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"choose-your-path-card" not in response.data


def test_dashboard_zero_data_sample_review_renders_with_badge(app_ctx, client, monkeypatch):
    _create_logged_in_user(
        client,
        username="zero-data-sample-user",
        email="zero-data-sample@example.com",
    )
    _patch_weekly_ai_empty(monkeypatch)

    response = client.get("/dashboard")

    assert response.status_code == 200
    html = response.data.decode("utf-8", errors="ignore")
    assert "Sample · Not your data" in html
    assert 'id="weekly-ai-sample"' in html
    assert "Upload your trades to get your own review" in html
    assert "What mattered this week" in html
    assert "What this suggests" in html
    assert "Improve this week:" in html
    assert "You're already strong at:" in html
    assert "dash-content--pure-zero" in html
    assert "mt5-side-stack--zero-data-deferred" in html
    assert "is-guided" in html
    assert "choose-your-path-option--highlight" not in html


def test_dashboard_zero_data_soft_waitlist_hidden_for_new_signup(app_ctx, client, monkeypatch):
    _create_logged_in_user(
        client,
        username="zero-data-waitlist-new",
        email="zero-data-waitlist-new@example.com",
        created_at=utcnow_naive(),
    )
    _patch_weekly_ai_empty(monkeypatch)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"zero-data-soft-waitlist-link" not in response.data


def test_dashboard_zero_data_soft_waitlist_visible_after_one_day(app_ctx, client, monkeypatch):
    _create_logged_in_user(
        client,
        username="zero-data-waitlist-old",
        email="zero-data-waitlist-old@example.com",
        created_at=utcnow_naive() - timedelta(days=2),
    )
    _patch_weekly_ai_empty(monkeypatch)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'class="zero-data-soft-waitlist-link"' in response.data
    assert b'data-waitlist-source="zero_data_dashboard"' in response.data
    assert b'data-waitlist-feature="advanced_replay"' in response.data
    assert b'data-waitlist-cta-context="zero_data_soft_waitlist"' in response.data


def test_waitlist_post_accepts_zero_data_dashboard_source(client, app_ctx):
    response = client.post(
        "/pricing/waitlist",
        json={
            "email": "zero-data-dashboard@example.com",
            "tier": "trader",
            "source": "zero_data_dashboard",
            "feature_interest": "advanced_replay",
            "cta_context": "zero_data_soft_waitlist",
        },
    )

    assert response.status_code == 200
    from models import UpgradeWaitlistEntry

    row = UpgradeWaitlistEntry.query.filter_by(email="zero-data-dashboard@example.com").one()
    assert row.source == "zero_data_dashboard"
    assert row.feature_interest == "advanced_replay"
    assert row.cta_context == "zero_data_soft_waitlist"


def test_zero_data_card_does_not_render_waitlist_trigger_inside_card(app_ctx, client, monkeypatch):
    _create_logged_in_user(
        client,
        username="zero-data-waitlist-placement",
        email="zero-data-waitlist-placement@example.com",
        created_at=utcnow_naive() - timedelta(days=2),
    )
    _patch_weekly_ai_empty(monkeypatch)

    response = client.get("/dashboard")
    html = response.data.decode("utf-8", errors="ignore")

    card_start = html.find("choose-your-path-card")
    card_end = html.find("choose-your-path-foot")
    if card_end == -1:
        card_end = html.find("</section>", card_start)
    card_html = html[card_start:card_end]
    assert "data-waitlist-trigger" not in card_html


def _create_admin(client, *, suffix):
    admin = User(
        username=f"zero-data-admin-{suffix}",
        email=f"zero-data-admin-{suffix}@example.com",
        password="hashed-password",
        email_verified=True,
        signup_status="approved",
        is_admin=True,
    )
    db.session.add(admin)
    db.session.commit()
    with client.session_transaction() as session_state:
        session_state["user_id"] = admin.id
        session_state["username"] = admin.username
    return admin


def _create_zero_data_user(*, username, email, created_at):
    user = User(
        username=username,
        email=email,
        password="hashed-password",
        email_verified=True,
        signup_status="approved",
    )
    user.created_at = created_at
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
    return user


def test_count_pre_activation_users_helper(app_ctx):
    from helpers.admin_activation import count_pre_activation_users

    now = utcnow_naive()
    _create_zero_data_user(
        username="zd-helper-recent",
        email="zd-helper-recent@example.com",
        created_at=now - timedelta(days=2),
    )
    _create_zero_data_user(
        username="zd-helper-stuck",
        email="zd-helper-stuck@example.com",
        created_at=now - timedelta(days=9),
    )

    counts = count_pre_activation_users()
    assert counts["pre_activation_recent"] >= 1
    assert counts["pre_activation_stuck"] >= 1


def test_admin_pre_activation_tile_counts(app_ctx, client, monkeypatch):
    suffix = "tile"
    monkeypatch.setenv("ADMIN_USER_EMAILS", f"zero-data-admin-{suffix}@example.com")
    _create_admin(client, suffix=suffix)
    now = utcnow_naive()
    _create_zero_data_user(
        username="zd-recent-1",
        email="zd-recent-1@example.com",
        created_at=now - timedelta(days=2),
    )
    _create_zero_data_user(
        username="zd-recent-2",
        email="zd-recent-2@example.com",
        created_at=now - timedelta(days=3),
    )
    _create_zero_data_user(
        username="zd-stuck-1",
        email="zd-stuck-1@example.com",
        created_at=now - timedelta(days=10),
    )
    activated = _create_zero_data_user(
        username="zd-activated",
        email="zd-activated@example.com",
        created_at=now - timedelta(days=2),
    )
    account = TradeAccount.query.filter_by(user_id=activated.id).first()
    db.session.add(
        Trade(
            user_id=activated.id,
            trade_account_id=account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.0,
            exit_price=1.01,
            lot_size=0.01,
            pnl=5.0,
            opened_at=datetime(2026, 1, 1, 8, 0, 0),
            closed_at=datetime(2026, 1, 1, 9, 0, 0),
        )
    )
    db.session.commit()

    response = client.get("/dashboard/admin/access/users")

    assert response.status_code == 200
    assert b"Pre-activation" in response.data
    assert b"recent" in response.data
    assert b"stuck" in response.data


def test_admin_pre_activation_filter_lists(app_ctx, client, monkeypatch):
    suffix = "filter"
    monkeypatch.setenv("ADMIN_USER_EMAILS", f"zero-data-admin-{suffix}@example.com")
    _create_admin(client, suffix=suffix)
    now = utcnow_naive()
    recent_a = _create_zero_data_user(
        username="zd-filter-recent-a",
        email="zd-filter-recent-a@example.com",
        created_at=now - timedelta(days=1),
    )
    recent_b = _create_zero_data_user(
        username="zd-filter-recent-b",
        email="zd-filter-recent-b@example.com",
        created_at=now - timedelta(days=4),
    )
    stuck = _create_zero_data_user(
        username="zd-filter-stuck",
        email="zd-filter-stuck@example.com",
        created_at=now - timedelta(days=12),
    )
    activated = _create_zero_data_user(
        username="zd-filter-active",
        email="zd-filter-active@example.com",
        created_at=now - timedelta(days=1),
    )
    account = TradeAccount.query.filter_by(user_id=activated.id).first()
    db.session.add(
        Trade(
            user_id=activated.id,
            trade_account_id=account.id,
            symbol="GBPUSD",
            side="SELL",
            entry_price=1.25,
            exit_price=1.24,
            lot_size=0.01,
            pnl=8.0,
            opened_at=datetime(2026, 2, 1, 8, 0, 0),
            closed_at=datetime(2026, 2, 1, 9, 0, 0),
        )
    )
    db.session.commit()

    recent_response = client.get(
        "/dashboard/admin/access/users?activation=zero_data_recent&status=all"
    )
    stuck_response = client.get(
        "/dashboard/admin/access/users?activation=zero_data_stuck&status=all"
    )

    assert recent_response.status_code == 200
    recent_html = recent_response.data.decode("utf-8", errors="ignore")
    assert recent_a.username in recent_html
    assert recent_b.username in recent_html
    assert stuck.username not in recent_html
    assert activated.username not in recent_html

    assert stuck_response.status_code == 200
    stuck_html = stuck_response.data.decode("utf-8", errors="ignore")
    assert stuck.username in stuck_html
    assert recent_a.username not in stuck_html
