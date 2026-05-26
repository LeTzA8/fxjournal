from sqlalchemy import event
from werkzeug.security import generate_password_hash

from models import User, db


def test_landing_page_includes_canonical_and_search_metadata(client):
    response = client.get("/")

    assert response.status_code == 200
    assert b"<title>MyFXJournal | Free Forex Trading Journal With Weekly AI Review</title>" in response.data
    assert b'name="description"' in response.data
    assert b'rel="canonical"' in response.data
    assert b'href="http://localhost:5000/"' in response.data
    assert b'<meta name="robots" content="index, follow">' in response.data


def test_robots_txt_exposes_sitemap_and_private_paths(client):
    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert b"User-agent: *" in response.data
    assert b"Disallow: /login" not in response.data
    assert b"Disallow: /register" not in response.data
    assert b"Disallow: /dashboard/" not in response.data
    assert b"Disallow: /password/" in response.data
    assert b"Disallow: /auth/" in response.data
    assert b"Disallow: /session/" in response.data
    assert b"Sitemap: http://localhost:5000/sitemap.xml" in response.data


def test_sitemap_xml_lists_public_pages(client):
    response = client.get("/sitemap.xml")

    assert response.status_code == 200
    assert b'<?xml version="1.0" encoding="UTF-8"?>' in response.data
    assert b"<loc>http://localhost:5000/</loc>" in response.data
    assert b"<loc>http://localhost:5000/pricing</loc>" in response.data
    assert b"<loc>http://localhost:5000/dashboard</loc>" in response.data
    assert b"<loc>http://localhost:5000/login</loc>" in response.data
    assert b"<loc>http://localhost:5000/register</loc>" in response.data
    assert b"<loc>http://localhost:5000/contact</loc>" in response.data
    assert b"<loc>http://localhost:5000/privacy</loc>" in response.data
    assert b"<loc>http://localhost:5000/terms</loc>" in response.data
    assert b"<loc>http://localhost:5000/faq/mt5-server</loc>" in response.data
    assert b"<loc>http://localhost:5000/free-mt5-sync</loc>" in response.data
    assert b"<loc>http://localhost:5000/mt5-trading-journal</loc>" in response.data
    assert b"<loc>http://localhost:5000/forex-trading-journal</loc>" in response.data
    assert b"<loc>http://localhost:5000/weekly-trading-review</loc>" in response.data
    assert b"<loc>http://localhost:5000/trade-replay-chart</loc>" in response.data
    assert b"<loc>http://localhost:5000/free-trading-journal</loc>" in response.data
    assert b"<loc>http://localhost:5000/free-mt5-trading-journal</loc>" in response.data
    assert b"<loc>http://localhost:5000/free-ai-trading-journal</loc>" in response.data
    assert b"<loc>http://localhost:5000/free-forex-trading-journal</loc>" in response.data
    assert b"<loc>http://localhost:5000/free-trading-journal-template</loc>" in response.data
    assert b"<loc>http://localhost:5000/free-prop-firm-trading-journal</loc>" in response.data


FREE_SEO_CLUSTER_PAGES = (
    (
        "/free-trading-journal",
        b"Free Trading Journal",
        b"http://localhost:5000/free-trading-journal",
    ),
    (
        "/free-mt5-trading-journal",
        b"Free MT5 Trading Journal",
        b"http://localhost:5000/free-mt5-trading-journal",
    ),
    (
        "/free-ai-trading-journal",
        b"Free AI Trading Journal",
        b"http://localhost:5000/free-ai-trading-journal",
    ),
    (
        "/free-forex-trading-journal",
        b"Free Forex Trading Journal",
        b"http://localhost:5000/free-forex-trading-journal",
    ),
    (
        "/free-trading-journal-template",
        b"Free Trading Journal Template Alternative",
        b"http://localhost:5000/free-trading-journal-template",
    ),
    (
        "/free-prop-firm-trading-journal",
        b"Free Prop Firm Trading Journal",
        b"http://localhost:5000/free-prop-firm-trading-journal",
    ),
)


def test_free_seo_cluster_pages_have_indexable_metadata(client):
    for path, title_fragment, canonical_url in FREE_SEO_CLUSTER_PAGES:
        response = client.get(path)

        assert response.status_code == 200
        assert title_fragment in response.data
        assert b'name="description"' in response.data
        assert b'<meta name="robots" content="index, follow">' in response.data
        assert b'rel="canonical"' in response.data
        assert b'href="' + canonical_url + b'"' in response.data


def test_legal_aliases_redirect_to_canonical_urls(client):
    r_privacy = client.get("/privacy-policy", follow_redirects=False)
    assert r_privacy.status_code == 301
    assert "/privacy" in (r_privacy.headers.get("Location") or "")
    r_terms = client.get("/terms-and-conditions", follow_redirects=False)
    assert r_terms.status_code == 301
    assert "/terms" in (r_terms.headers.get("Location") or "")


def test_login_page_has_indexable_metadata(client):
    response = client.get("/login")

    assert response.status_code == 200
    assert b"Sign in to MyFXJournal" in response.data
    assert b'<meta name="robots" content="index, follow">' in response.data
    assert b'href="http://localhost:5000/login"' in response.data


def test_register_page_has_indexable_metadata(client):
    response = client.get("/register")

    assert response.status_code == 200
    assert b"Create a MyFXJournal account" in response.data
    assert b'<meta name="robots" content="index, follow">' in response.data
    assert b'href="http://localhost:5000/register"' in response.data


def test_dashboard_public_gate_has_indexable_metadata(client):
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Trading dashboard" in response.data
    assert b'<meta name="robots" content="index, follow">' in response.data
    assert b'href="http://localhost:5000/dashboard"' in response.data


def test_faq_mt5_server_page_reachable_and_shows_help_images(client):
    response = client.get("/faq/mt5-server")

    assert response.status_code == 200
    assert b"Where to find your MT5 server name" in response.data
    assert b"images/help/mt5-desktop-titlebar.png" in response.data
    assert b"images/help/mt5-mobile-settings.png" in response.data


def test_trade_replay_chart_page_has_indexable_metadata(client):
    response = client.get("/trade-replay-chart")

    assert response.status_code == 200
    assert b"Trade Replay Chart (Coming Soon)" in response.data
    assert b'<meta name="robots" content="index, follow">' in response.data
    assert b'href="http://localhost:5000/trade-replay-chart"' in response.data


def test_revenge_trading_journal_page_has_indexable_metadata_and_pricing_link(client):
    response = client.get("/revenge-trading-journal")

    assert response.status_code == 200
    assert b"Revenge Trading Journal" in response.data
    assert b'<meta name="robots" content="index, follow">' in response.data
    assert b'href="http://localhost:5000/revenge-trading-journal"' in response.data
    assert b'href="/pricing"' in response.data


def test_free_mt5_sync_page_has_indexable_metadata(client):
    response = client.get("/free-mt5-sync")

    assert response.status_code == 200
    assert b"MyFXJournal | MT5 Sync Trial" in response.data
    assert b"14-day premium workflow trial" in response.data
    assert b'<meta name="robots" content="index, follow">' in response.data
    assert b'href="http://localhost:5000/free-mt5-sync"' in response.data


def test_public_trial_pages_do_not_show_mt5_slot_scarcity_copy(client):
    for path in ("/", "/pricing", "/free-mt5-sync", "/mt5-trading-journal"):
        response = client.get(path)
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        assert "free MT5 sync slot" not in text
        assert "slots left" not in text
        assert "claim a sync slot" not in text
        assert "Fills fast" not in text

    pricing_text = client.get("/pricing").get_data(as_text=True)
    assert "Core journaling stays free" in pricing_text
    assert "14-day trial" in pricing_text


def test_pricing_page_renders_authenticated_without_selecting_phase2_user_columns(client, app_ctx):
    user = User(
        username="pricing-auth-user",
        email="pricing-auth-user@example.com",
        password=generate_password_hash("test-pass-123"),
        email_verified=True,
    )
    db.session.add(user)
    db.session.commit()

    statements = []

    def _capture_statement(_conn, _cursor, statement, _params, _context, _executemany):
        statements.append(statement.lower())

    event.listen(db.engine, "before_cursor_execute", _capture_statement)
    try:
        with client.session_transaction() as session_data:
            session_data["user_id"] = user.id
        response = client.get("/pricing")
    finally:
        event.remove(db.engine, "before_cursor_execute", _capture_statement)

    assert response.status_code == 200
    user_selects = [statement for statement in statements if " from users " in statement]
    assert all("plan_tier" not in statement for statement in user_selects)
    assert all("plan_grandfathered" not in statement for statement in user_selects)
    assert all("premium_trial_started_at" not in statement for statement in user_selects)
