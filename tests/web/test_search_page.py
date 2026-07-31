import asyncio
import datetime
import time

import httpx
import pytest

from babel.config import Settings
from babel.db import repo, search
from babel.embed.client import EmbedError
from babel.models import Article
from babel.web.app import create_app

UTC = datetime.UTC


def _article(article_id: int, title: str) -> Article:
    return Article(
        id=article_id, title=title, body="body", body_raw=None,
        author_id=1, author_name="a", country="Poland",
        published_at=datetime.datetime(2026, 1, 1, tzinfo=UTC),
        e_day=6616, comment_count=0, comments=(), images=(),
    )


@pytest.fixture
async def seeded(pg):
    await repo.save_article(pg, _article(10, "About elections"))
    await repo.save_embeddings(
        pg, "test/model", [(10, repo.vector_literal([0.1] * repo.EMBED_DIM))]
    )
    await pg.execute(search.HNSW_INDEX_SQL)
    return pg


async def test_a_query_returns_matching_articles(client, seeded):
    resp = await client.get("/search", params={"q": "вибори"})
    assert resp.status_code == 200
    assert "About elections" in resp.text


async def test_an_empty_query_shows_the_form_and_calls_nothing(client, seeded, embedder):
    resp = await client.get("/search", params={"q": "   "})
    assert resp.status_code == 200
    assert embedder.calls == []


async def test_an_overlong_query_is_truncated_not_sent_whole(client, seeded, embedder):
    await client.get("/search", params={"q": "x" * 5000})
    assert len(embedder.calls[0][0]) == 512


async def test_the_service_being_down_degrades_honestly(client, seeded, embedder):
    embedder.error = EmbedError("down")
    resp = await client.get("/search", params={"q": "вибори"})
    assert resp.status_code == 200
    assert "unavailable" in resp.text.lower()
    # Not a 500, and not a fabricated empty result set — the reader is told the
    # search could not run, rather than being shown "no matches" for a query
    # that was never actually asked.
    assert "no matches" not in resp.text.lower()


async def test_a_timeout_degrades_the_same_way(client, seeded, embedder):
    embedder.error = TimeoutError()
    resp = await client.get("/search", params={"q": "вибори"})
    assert resp.status_code == 200
    assert "unavailable" in resp.text.lower()


async def test_search_is_disallowed_in_robots(client):
    body = (await client.get("/robots.txt")).text
    assert "Disallow: /search" in body


class _HangingEmbedder:
    """Never raises and never returns inside the test's own patience — the
    only thing that can end the request is the route's own timeout wrapper.

    The stub in conftest.py cannot stand in for this: its `error` attribute
    raises synchronously, so a TimeoutError from it proves the except clause
    catches TimeoutError, not that anything upstream actually enforced a
    deadline. `asyncio.wait_for` could be deleted from the route entirely and
    every test above would still pass, because none of them ever makes the
    embed call take real time. This one does.
    """

    def __init__(self):
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        await asyncio.sleep(10)  # far longer than any timeout under test
        raise AssertionError("embed() ran to completion — the timeout did not cut it off")


async def test_a_slow_embed_service_is_cut_off_at_the_configured_timeout(pool, image_root):
    """search_timeout_sec bounds the request even when the embed call itself
    never fails and never returns. Built on its own app rather than the shared
    `client` fixture because that fixture's Settings carries the production
    default (2.0s), and this test needs a small one to stay fast without
    weakening what it proves — the margin between the cap and the assertion
    below is what matters, not its absolute size.
    """
    embedder = _HangingEmbedder()
    settings = Settings(
        database_url="postgresql://babel@unused/babel",
        web_database_url="postgresql://babel_web@unused/babel",
        contact="archive@example.invalid",
        image_root=str(image_root),
        embed_service_url="http://embed.invalid",
        embed_model="test/model",
        search_timeout_sec=0.05,
    )
    app = create_app(settings, pool=pool, embedder=embedder)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://t") as c,
    ):
        started = time.monotonic()
        resp = await c.get("/search", params={"q": "вибори"})
        elapsed = time.monotonic() - started

    assert resp.status_code == 200
    assert "unavailable" in resp.text.lower()
    # Generous next to the 0.05s cap, but two orders of magnitude under the
    # embedder's 10s sleep — a wrapper that had been deleted would make this
    # assertion fail long before the 10s AssertionError inside embed() ever
    # fired.
    assert elapsed < 2.0
