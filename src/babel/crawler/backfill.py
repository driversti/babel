"""Walk article IDs downward from the newest.

Newest-first is deliberate and the spec argues it twice: the recent years are
the ones people actually search, and they hold the only images still alive.
Stopping the backfill at any point therefore leaves a useful archive rather than
half of a chronological one.

The cursor is written after every batch. The full walk is roughly a month; it
will be interrupted, and re-fetching from the top would be intolerable.
"""

from collections.abc import Awaitable, Callable

import asyncpg

from babel.db import repo

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
        for article_id in await repo.filter_unseen(conn, batch):
            await ingest(article_id)
        current = batch[-1] - 1
        await repo.set_cursor(conn, cursor_name, current)
