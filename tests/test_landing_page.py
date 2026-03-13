def test_landing_page_shows_ai_demo_section(client):
    response = client.get("/")

    assert response.status_code == 200
    assert b'id="ai-demo"' in response.data
    assert b'data-typewriter-target' in response.data
    assert b'id="ai-demo-text"' in response.data
    assert response.data.index(b'id="ai-demo"') < response.data.index(b'id="features"')
