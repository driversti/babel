"""Walk article IDs downward from the newest.

Newest-first is deliberate and the spec argues it twice: the recent years are
the ones people actually search, and they hold the only images still alive.
Stopping the backfill at any point therefore leaves a useful archive rather than
half of a chronological one.

The cursor is written after every batch. The full walk is roughly a month; it
will be interrupted, and re-fetching from the top would be intolerable.
"""

import logging
from collections.abc import Awaitable, Callable

import asyncpg

from babel.db import repo

logger = logging.getLogger(__name__)

Ingest = Callable[[int], Awaitable[str]]


async def run_backfill(
    conn: asyncpg.Connection,
    ingest: Ingest,
    *,
    cursor_name: str = "backfill",
    start_id: int | None = None,
    stop_at: int = 1,
    batch_size: int = 50,
) -> None:
    """Ingest IDs from the cursor (or start_id) down to stop_at inclusive."""
    current = await repo.get_cursor(conn, cursor_name)
    if current is None:
        if start_id is None:
            raise ValueError("no cursor stored yet: start_id is required for the first run")
        current = start_id

    while current >= stop_at:
        batch = list(range(current, max(stop_at - 1, current - batch_size), -1))
        for article_id in await repo.filter_unseen(conn, batch, retry_errors=True):
            try:
                await ingest(article_id)
            except Exception as exc:
                # A single article's unexpected failure (network blip, parser edge
                # case, transient DB error) must not abort a walk that takes about a
                # month. Record it as 'error' so filter_unseen(retry_errors=True)
                # offers it again later, instead of losing it silently or crashing
                # the whole batch.
                logger.exception("ingest failed for article %s", article_id)
                await repo.record_fetch(conn, article_id, "error", str(exc))
        current = batch[-1] - 1
        await repo.set_cursor(conn, cursor_name, current)
