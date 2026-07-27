async def test_healthz_is_ok(client):
    response = await client.get("/healthz")
    assert response.status_code == 200


async def test_every_response_carries_noindex(client):
    for path in ("/healthz", "/robots.txt", "/"):
        response = await client.get(path)
        assert response.headers["x-robots-tag"] == "noindex, nofollow"


async def test_robots_allows_articles_and_refuses_the_filter_space(client):
    body = (await client.get("/robots.txt")).text
    # Disallow: / would stop a crawler fetching the page, so it would never see
    # the noindex above — the two controls cancel instead of compounding.
    assert "Disallow: /?" in body
    assert "Allow: /" in body
    assert "Disallow: /\n" not in body


async def test_html_pages_carry_a_script_free_csp(client):
    response = await client.get("/")
    csp = response.headers["content-security-policy"]
    assert "script-src 'none'" in csp
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


async def test_unknown_path_is_a_styled_404_not_a_stack_trace(client):
    response = await client.get("/no/such/thing")
    assert response.status_code == 404
    assert "<html" in response.text.lower()
    assert "Traceback" not in response.text
