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
    assert b"Disallow: /dashboard/" in response.data
    assert b"Sitemap: http://localhost:5000/sitemap.xml" in response.data


def test_sitemap_xml_lists_public_pages(client):
    response = client.get("/sitemap.xml")

    assert response.status_code == 200
    assert b'<?xml version="1.0" encoding="UTF-8"?>' in response.data
    assert b"<loc>http://localhost:5000/</loc>" in response.data
    assert b"<loc>http://localhost:5000/contact</loc>" in response.data
    assert b"<loc>http://localhost:5000/privacy</loc>" in response.data
    assert b"<loc>http://localhost:5000/terms</loc>" in response.data
    assert b"<loc>http://localhost:5000/free-mt5-sync</loc>" in response.data
    assert b"<loc>http://localhost:5000/mt5-trading-journal</loc>" in response.data
    assert b"<loc>http://localhost:5000/forex-trading-journal</loc>" in response.data
    assert b"<loc>http://localhost:5000/weekly-trading-review</loc>" in response.data


def test_free_mt5_sync_page_has_indexable_metadata(client):
    response = client.get("/free-mt5-sync")

    assert response.status_code == 200
    assert b"MyFXJournal | Free MT5 Sync" in response.data
    assert b"Free during open beta" in response.data
    assert b'<meta name="robots" content="index, follow">' in response.data
    assert b'href="http://localhost:5000/free-mt5-sync"' in response.data
