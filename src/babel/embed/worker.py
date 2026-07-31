"""Drains the embedding queue.

The queue is `article_embeddings` rows with a NULL vector. Nothing is claimed
or marked: a row leaves the queue when its vector is written, so a worker
killed mid-batch has changed nothing and the same rows are offered again.

Newest article first, matching the image drain and the article walk — the
most-read part of the archive becomes searchable first.
"""

import asyncio
import logging

import aiohttp

from babel.db import repo
from babel.embed.client import EmbedError

log = logging.getLogger("babel.embed")

SERVICE_ALERT_KEY = "embed-service-down"


async def run_embed_worker(
    pool, client, notifier, settings, *, sleep=asyncio.sleep, max_cycles: int | None = None
) -> None:
    """Embed queued articles until stopped. `max_cycles` bounds the loop for tests."""
    cycles = 0
    consecutive_failures = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1

        async with pool.acquire() as conn:
            batch = await repo.claim_pending_embeddings(conn, settings.embed_batch_size)

        if not batch:
            await sleep(settings.embed_idle_sleep_sec)
            continue

        texts = [
            f"{item.title}\n\n{item.body}"[: settings.embed_max_chars] for item in batch
        ]
        try:
            vectors = await client.embed(texts)
        except (TimeoutError, EmbedError, aiohttp.ClientError, OSError) as exc:
            # aiohttp.ClientError is in the tuple because EmbedClient does not wrap
            # every aiohttp fault: a 200 carrying a non-JSON body raises
            # aiohttp.ContentTypeError from resp.json(), and that inherits from
            # Exception, not OSError, so without this it escapes the handler and
            # kills the worker. Found reviewing task 4.
            # Nothing is written and nothing is marked, so every row in this
            # batch is still queued. The back-off is the only state the failure
            # leaves behind, and a restart discards it — which is right, because
            # "the service is down" is a fact about now.
            consecutive_failures += 1
            # min(consecutive_failures, 32) caps the exponent, not just the
            # result: `2 ** (consecutive_failures - 1)` as an int has no
            # ceiling, and at consecutive_failures = 1025 it is too large to
            # convert to a float at all — OverflowError, from arithmetic that
            # runs after client.embed's own exception has already been caught,
            # so it is not inside the try/except above and escapes
            # run_embed_worker uncaught. ~7 days of a down embed service is
            # not a contrived count for a service meant to run indefinitely.
            # 32 is already far past where doubling stops mattering: 2**31
            # seconds dwarfs embed_backoff_max_sec, and the min() below
            # discards the uncapped value exactly as it did before.
            delay = min(
                settings.embed_backoff_base_sec * 2 ** (min(consecutive_failures, 32) - 1),
                settings.embed_backoff_max_sec,
            )
            log.warning("embed batch of %d failed (%s) — waiting %.0fs", len(batch), exc, delay)
            await notifier.send_once(
                SERVICE_ALERT_KEY, f"babel: the embed service is not answering ({exc})"
            )
            await sleep(delay)
            continue

        consecutive_failures = 0
        rows = [
            (item.article_id, repo.vector_literal(vec))
            for item, vec in zip(batch, vectors, strict=True)
        ]
        async with pool.acquire() as conn:
            await repo.save_embeddings(conn, settings.embed_model, rows)
        log.info("embedded %d article(s), newest %d", len(rows), batch[0].article_id)
