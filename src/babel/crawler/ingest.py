"""fetch → parse → persist, for exactly one article ID.

The only module that knows about the network, the parser and the database at
once. Both producers call this and nothing else, which is what keeps the
backfill and the poller free of duplicated pipeline logic.
"""

import asyncpg

from babel.config import Settings
from babel.crawler.fetcher import Getter, fetch_article
from babel.crawler.parser import parse_article
from babel.crawler.ratelimit import RateLimiter
from babel.db import repo


class Ingestor:
    def __init__(
        self,
        pool: asyncpg.Pool,
        get_page: Getter,
        limiter: RateLimiter,
        settings: Settings,
    ) -> None:
        self._pool = pool
        self._get_page = get_page
        self._limiter = limiter
        self._settings = settings

    async def ingest(self, article_id: int) -> str:
        """Fetch, parse and store one article. Returns the fetch status."""
        await self._limiter.acquire()
        result = await fetch_article(
            self._get_page,
            self._settings.article_url(article_id),
            max_attempts=self._settings.max_attempts,
        )

        if result.status != "ok":
            async with self._pool.acquire() as conn:
                await repo.record_fetch(conn, article_id, result.status, result.error)
            return result.status

        article = parse_article(result.html, article_id)
        if article is None:
            async with self._pool.acquire() as conn:
                await repo.record_fetch(conn, article_id, "error", "unparseable page")
            return "error"

        async with self._pool.acquire() as conn:
            await repo.save_article(conn, article)
            await repo.record_fetch(conn, article_id, "ok")

        return "ok"
