import httpx

from babel.config import Settings
from babel.web.app import create_app


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


async def test_an_unhandled_exception_still_gets_the_styled_page_and_headers(pool, image_root):
    """A plain exception — not asyncpg.PostgresError/OSError/HTTPException — must
    still come back as our 500 page carrying every security header.

    Starlette pulls a handler registered for the bare `Exception` class out to
    `ServerErrorMiddleware` itself (see `Starlette.build_middleware_stack`),
    which sits *outside* the app's `security_headers` middleware. A response
    built there never runs back through that middleware's post-`call_next`
    code, so the headers have to be set where the response is built —
    `render_error` — not only in the middleware.

    Uses a fresh app with an extra route rather than the shared `client`
    fixture, so this test doesn't depend on any route existing elsewhere.
    `raise_app_exceptions=False` makes the transport behave like a real HTTP
    client: Starlette's ServerErrorMiddleware always re-raises after sending
    its response (so servers can log it), and a real client never sees that —
    it only ever sees the bytes already sent.
    """
    settings = Settings(
        database_url="postgresql://babel@unused/babel",
        web_database_url="postgresql://babel_web@unused/babel",
        contact="archive@example.invalid",
        image_root=str(image_root),
    )
    app = create_app(settings, pool=pool)

    @app.get("/__boom__")
    async def boom():
        raise ValueError("kaboom — must never reach the response body")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://t") as c,
    ):
        response = await c.get("/__boom__")

    assert response.status_code == 500
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "content-security-policy" in response.headers
    assert "<html" in response.text.lower()
    assert "Traceback" not in response.text
    assert "kaboom" not in response.text
