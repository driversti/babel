import pathlib

import pytest

from babel.config import Settings
from babel.crawler.ingest import Ingestor
from babel.crawler.ratelimit import RateLimiter

FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures"
ARTICLE_HTML = (FIXTURES / "article_with_images.html").read_text(encoding="utf-8")


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, image_root=str(tmp_path), min_free_bytes=0)


async def test_ingests_article_comments_and_queues_its_images(pg, fake_pool, settings):
    async def get_page(url):
        return 200, ARTICLE_HTML

    ingestor = Ingestor(fake_pool(pg), get_page, RateLimiter(1000), settings)
    assert await ingestor.ingest(2797005) == "ok"

    assert await pg.fetchval("SELECT count(*) FROM articles WHERE id = 2797005") == 1
    assert await pg.fetchval("SELECT count(*) FROM comments WHERE article_id = 2797005") > 0
    assert await pg.fetchval("SELECT status FROM fetch_log WHERE article_id = 2797005") == "ok"
    statuses = await pg.fetch("SELECT DISTINCT status FROM article_images WHERE article_id = 2797005")
    # Capturing the bytes is now the image worker's job. Ingest's whole
    # contribution is handing the URLs to the queue, so every row it writes
    # must still be sitting at 'pending'.
    assert {r["status"] for r in statuses} == {"pending"}


async def test_missing_article_is_logged_and_stores_nothing(pg, fake_pool, settings):
    async def get_page(url):
        return 404, "not found"

    ingestor = Ingestor(fake_pool(pg), get_page, RateLimiter(1000), settings)
    assert await ingestor.ingest(999) == "missing"
    assert await pg.fetchval("SELECT count(*) FROM articles WHERE id = 999") == 0
    assert await pg.fetchval("SELECT status FROM fetch_log WHERE article_id = 999") == "missing"
