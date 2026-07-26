"""Drains the image queue.

The queue is `article_images` rows at status 'pending' or 'error'. `save_article`
fills it; nothing else does. Separating this from article ingest is what makes a
kill mid-download harmless — an unfinished row is simply still queued.

Newest article first. 66% of 2021-2026 images still resolve against 7% of
2007-2014, so draining oldest-first would spend the crawl on links already gone.
"""

import asyncio
import logging

from babel.crawler.images import capture_image, have_space
from babel.db import repo

log = logging.getLogger("babel.images")

DISK_ALERT_KEY = "image-disk-full"


async def run_image_worker(
    pool,
    get_bytes,
    limiter,
    host_limiter,
    notifier,
    settings,
    *,
    sleep=asyncio.sleep,
    max_cycles: int | None = None,
) -> None:
    """Fetch queued images until stopped. `max_cycles` bounds the loop for tests."""
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1

        if not have_space(settings.image_root, settings.min_free_bytes):
            # Deliberately no row is touched. Marking one would take it out of the
            # queue, and freeing disk space would never bring it back.
            log.warning("below the free-space floor — pausing image capture")
            await notifier.send_once(
                DISK_ALERT_KEY,
                f"babel: image capture paused, less than {settings.min_free_bytes} bytes free",
            )
            await sleep(settings.image_disk_full_sleep_sec)
            continue

        async with pool.acquire() as conn:
            batch = await repo.claim_pending_images(conn, settings.image_batch_size)

        if not batch:
            await sleep(settings.image_idle_sleep_sec)
            continue

        for item in batch:
            await _capture_one(pool, get_bytes, limiter, host_limiter, settings, item)


async def _capture_one(pool, get_bytes, limiter, host_limiter, settings, item) -> None:
    await limiter.acquire()
    try:
        async with host_limiter.slot(item.source_url):
            outcome = await capture_image(
                get_bytes,
                settings.image_root,
                item.source_url,
                max_bytes=settings.max_image_bytes,
            )
    except Exception:  # noqa: BLE001 — a filesystem or transport fault is not fatal
        log.exception("capturing %s failed", item.source_url)
        outcome = None

    async with pool.acquire() as conn:
        if outcome is None:
            await repo.record_image_result(conn, item.article_id, item.position, "error")
            return
        if outcome.digest is not None:
            await repo.save_image_blob(conn, outcome.digest, outcome.mime, outcome.size)
        await repo.record_image_result(
            conn, item.article_id, item.position, outcome.status, outcome.digest
        )
