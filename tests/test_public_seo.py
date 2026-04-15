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
    assert b"Sitemap: http://localhost:5000/sitemap.xml" in response.data


def test_sitemap_xml_lists_public_pages(client):
    response = client.get("/sitemap.xml")

    assert response.status_code == 200
    assert b'<?xml version="1.0" encoding="UTF-8"?>' in response.data
    assert b"<loc>http://localhost:5000/</loc>" in response.data
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
    assert b"Trade Replay Chart for Review" in response.data
    assert b'<meta name="robots" content="index, follow">' in response.data
    assert b'href="http://localhost:5000/trade-replay-chart"' in response.data


def test_free_mt5_sync_page_has_indexable_metadata(client):
    response = client.get("/free-mt5-sync")

    assert response.status_code == 200
    assert b"MyFXJournal | Free MT5 Sync" in response.data
    assert b"Free during open beta" in response.data
    assert b'<meta name="robots" content="index, follow">' in response.data
    assert b'href="http://localhost:5000/free-mt5-sync"' in response.data
