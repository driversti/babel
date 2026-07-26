"""Live collection from the RSS feed.

The feed holds about three days across its five pages, which is a generous
buffer: a poll every fifteen minutes could miss dozens of cycles and still lose
nothing. Pages beyond the fifth return an empty feed, so there is no history
here — that is the backfill's job.
"""

import logging
import re
from collections.abc import Awaitable, Callable

import asyncpg

from babel.db import repo

logger = logging.getLogger(__name__)

Ingest = Callable[[int], Awaitable[str]]
RssFetcher = Callable[[int], Awaitable[str]]

_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
_ARTICLE_ID_RE = re.compile(r"/en/article/[^<\s]*?-?(\d+)(?:/\d+/\d+)?\s*</link>")


def parse_rss_ids(xml: str) -> list[int]:
    """Article IDs from <item><link>, in feed order.

    Scoped to <item> blocks so the channel's own <link> cannot be mistaken for
    an article.
    """
    ids: list[int] = []
    for item in _ITEM_RE.findall(xml):
        m = _ARTICLE_ID_RE.search(item)
        if m:
            ids.append(int(m.group(1)))
    return ids


async def poll_once(
    conn: asyncpg.Connection, ingest: Ingest, fetch_rss: RssFetcher, pages: int
) -> list[int]:
    """Read the feed and ingest anything not already recorded."""
    candidates: list[int] = []
    for page in range(1, pages + 1):
        for article_id in parse_rss_ids(await fetch_rss(page)):
            if article_id not in candidates:
                candidates.append(article_id)

    ingested: list[int] = []
    for article_id in await repo.filter_unseen(conn, candidates, retry_errors=True):
        try:
            await ingest(article_id)
        except Exception as exc:
            # One article's unexpected failure must not stop the rest of the poll
            # cycle. Record it as 'error' so a future cycle can retry it via
            # filter_unseen(retry_errors=True), instead of losing it silently.
            logger.exception("ingest failed for article %s", article_id)
            await repo.record_fetch(conn, article_id, "error", str(exc))
            continue
        ingested.append(article_id)
    return ingested
