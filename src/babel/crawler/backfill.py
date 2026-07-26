"""The backfill: three phases, cycling, never finishing.

Walk the IDs downward from the cursor; when the walk reaches bottom, sweep up
whatever failed or was marked stale and still has attempts left; when neither
produces work, sleep.

The old version did only the first of those and returned when it hit article 1.
Two defects came from that. An ID that errored inside a completed batch was
never offered again, because the cursor had already moved past it — a single
timeout was permanent data loss across 2.8M articles. And returning ended the
process, which under `restart: unless-stopped` is a crash loop rather than a
completion.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import asyncpg

from babel.db import repo

log = logging.getLogger("babel.backfill")

Ingest = Callable[[int], Awaitable[str]]


async def run_backfill(
    conn: asyncpg.Connection,
    ingest: Ingest,
    *,
    cursor_name: str = "backfill",
    start_id: int | None = None,
    stop_at: int = 1,
    batch_size: int = 50,
    cooldown_sec: int = 3600,
    idle_sleep_sec: float = 300.0,
    sleep=asyncio.sleep,
    max_cycles: int | None = None,
) -> None:
    """Collect articles until stopped. `max_cycles` bounds the loop for tests."""
    cursor = await repo.get_cursor(conn, cursor_name)
    if cursor is None:
        if start_id is None:
            raise ValueError("no cursor stored yet: start_id is required for the first run")
        cursor = start_id

    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1

        if cursor >= stop_at:
            batch = list(range(cursor, max(stop_at - 1, cursor - batch_size), -1))
            for article_id in await repo.filter_unseen(conn, batch):
                await _ingest_one(conn, ingest, article_id)
            cursor = batch[-1] - 1
            await repo.set_cursor(conn, cursor_name, cursor)
            continue

        retryable = await repo.claim_retryable(conn, batch_size, cooldown_sec)
        if retryable:
            log.info("sweeping %d article(s) for another attempt", len(retryable))
            for article_id in retryable:
                await _ingest_one(conn, ingest, article_id)
            continue

        await sleep(idle_sleep_sec)


async def _ingest_one(conn: asyncpg.Connection, ingest: Ingest, article_id: int) -> None:
    """Ingest one article, recording an unexpected failure rather than propagating it.

    One article must never abort a batch. Recording 'error' is what puts it in
    front of the sweep later.
    """
    try:
        await ingest(article_id)
    except Exception as e:  # noqa: BLE001
        log.exception("ingesting article %d failed", article_id)
        await repo.record_fetch(conn, article_id, "error", f"{type(e).__name__}: {e}")
