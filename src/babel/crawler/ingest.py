"""fetch → parse → persist, for exactly one article ID.

The only module that knows about the network, the parser and the database at
once. Both producers call this and nothing else, which is what keeps the
backfill and the poller free of duplicated pipeline logic.
"""

import asyncpg

from babel.config import Settings
from babel.crawler.fetcher import Getter, fetch_article
from babel.crawler.images import BytesGetter, capture_image
from babel.crawler.parser import parse_article
from babel.crawler.ratelimit import RateLimiter
from babel.db import repo


class Ingestor:
    def __init__(
        self,
        pool: asyncpg.Pool,
        get_page: Getter,
        get_bytes: BytesGetter,
        limiter: RateLimiter,
        settings: Settings,
    ) -> None:
        self._pool = pool
        self._get_page = get_page
        self._get_bytes = get_bytes
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

        # Images are best-effort. A dead host must never cost us the article,
        # which is the whole reason this runs after the text is committed.
        for ref in article.images:
            await self._limiter.acquire()
            outcome = await capture_image(
                self._get_bytes,
                self._settings.image_root,
                ref.source_url,
                min_free_bytes=self._settings.min_free_bytes,
                max_bytes=self._settings.max_image_bytes,
            )
            async with self._pool.acquire() as conn:
                if outcome.digest is not None:
                    await repo.save_image_blob(conn, outcome.digest, outcome.mime, outcome.size)
                await repo.record_image(
                    conn, article_id, ref.position, ref.source_url, outcome.status, outcome.digest
                )
        return "ok"
