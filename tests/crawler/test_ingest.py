import pathlib

import pytest

from babel.config import Settings
from babel.crawler.ingest import Ingestor
from babel.crawler.ratelimit import RateLimiter

FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures"
ARTICLE_HTML = (FIXTURES / "article_with_images.html").read_text(encoding="utf-8")


class FakePool:
    """asyncpg.Pool.acquire() is an async context manager; one connection suffices."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, image_root=str(tmp_path), min_free_bytes=0)


async def test_ingests_article_comments_and_images(pg, settings):
    async def get_page(url):
        return 200, ARTICLE_HTML

    async def get_bytes(url):
        return 200, b"\x89PNG fake bytes", "image/png"

    ingestor = Ingestor(FakePool(pg), get_page, get_bytes, RateLimiter(1000), settings)
    assert await ingestor.ingest(2797005) == "ok"

    assert await pg.fetchval("SELECT count(*) FROM articles WHERE id = 2797005") == 1
    assert await pg.fetchval("SELECT count(*) FROM comments WHERE article_id = 2797005") > 0
    assert await pg.fetchval("SELECT status FROM fetch_log WHERE article_id = 2797005") == "ok"
    statuses = await pg.fetch("SELECT DISTINCT status FROM article_images WHERE article_id = 2797005")
    assert {r["status"] for r in statuses} <= {"ok", "dead", "error"}
    assert "pending" not in {r["status"] for r in statuses}


async def test_dead_images_do_not_fail_the_article(pg, settings):
    async def get_page(url):
        return 200, ARTICLE_HTML

    async def get_bytes(url):
        return 404, b"", None

    ingestor = Ingestor(FakePool(pg), get_page, get_bytes, RateLimiter(1000), settings)
    assert await ingestor.ingest(2797005) == "ok"
    assert await pg.fetchval("SELECT count(*) FROM articles WHERE id = 2797005") == 1
    assert await pg.fetchval(
        "SELECT count(*) FROM article_images WHERE article_id = 2797005 AND status = 'dead'"
    ) > 0


async def test_missing_article_is_logged_and_stores_nothing(pg, settings):
    async def get_page(url):
        return 404, "not found"

    async def get_bytes(url):
        raise AssertionError("should not fetch images for a missing article")

    ingestor = Ingestor(FakePool(pg), get_page, get_bytes, RateLimiter(1000), settings)
    assert await ingestor.ingest(999) == "missing"
    assert await pg.fetchval("SELECT count(*) FROM articles WHERE id = 999") == 0
    assert await pg.fetchval("SELECT status FROM fetch_log WHERE article_id = 999") == "missing"


async def test_text_is_saved_even_when_the_disk_is_full(pg, tmp_path):
    full = Settings(_env_file=None, image_root=str(tmp_path), min_free_bytes=10**18)

    async def get_page(url):
        return 200, ARTICLE_HTML

    async def get_bytes(url):
        raise AssertionError("must not hit the network below the free-space floor")

    ingestor = Ingestor(FakePool(pg), get_page, get_bytes, RateLimiter(1000), full)
    assert await ingestor.ingest(2797005) == "ok"
    assert await pg.fetchval("SELECT count(*) FROM articles WHERE id = 2797005") == 1
    assert await pg.fetchval(
        "SELECT count(*) FROM article_images WHERE status = 'skipped_no_space'"
    ) > 0
