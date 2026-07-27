"""Drains the image queue.

The queue is `article_images` rows at status 'pending' or 'error'. `save_article`
fills it; nothing else does. Separating this from article ingest is what makes a
kill mid-download harmless — an unfinished row is simply still queued.

Newest article first. 66% of 2021-2026 images still resolve against 7% of
2007-2014, so draining oldest-first would spend the crawl on links already gone.
"""

import asyncio
import logging

from babel.crawler.circuit import HostCircuit
from babel.crawler.images import capture_image, have_space, url_host
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
    circuit=None,
    sleep=asyncio.sleep,
    max_cycles: int | None = None,
    resolve=None,
) -> None:
    """Fetch queued images until stopped. `max_cycles` bounds the loop for tests.

    `resolve` is threaded straight down to `classify_url` (see images.py) — it
    lets tests of this worker's own behaviour (retries, concurrency, the host
    circuit breaker) stub out DNS instead of needing it live, without touching
    classify_url's own tests, which are about the resolver's real behaviour.
    """
    if circuit is None:
        circuit = HostCircuit(settings.host_failure_threshold, settings.host_open_sec)
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

        # Hosts in trouble are excluded by the claim, not skipped after it. The
        # drain is newest-article-first and a failing host clusters at the head,
        # so skipping in here would hand back the same rows every cycle — the
        # worker would spin on them, or sleep, while every other host's images
        # waited behind.
        excluded = circuit.open_hosts()
        if excluded:
            log.info("holding off %d host(s): %s", len(excluded), ", ".join(excluded))
        async with pool.acquire() as conn:
            batch = await repo.claim_pending_images(
                conn,
                settings.image_batch_size,
                settings.image_retry_cooldown_sec,
                excluded,
            )

        if not batch:
            await sleep(settings.image_idle_sleep_sec)
            continue

        await _capture_batch(pool, get_bytes, limiter, host_limiter, settings, batch, circuit, resolve)


def _interleave_by_host(batch):
    """Round-robin the batch across hosts, keeping each host's own order.

    Rows arrive ordered by (article_id, position) and an article's images almost
    always share a host, so a batch is runs of one host — measured live, 39 of 50
    rows across three. With one in-flight request permitted per hostname, workers
    taking that order all pile onto the same host and wait: one of eight makes
    progress. Pulling from a shared queue does not help by itself, because the
    workers pull in that same order.

    Interleaved, as many hosts are busy at once as there are hosts in the batch,
    which is the most one-request-per-host allows. Each host's images stay in
    their original relative order, so the newest-article-first drain the survival
    curve calls for is preserved within a host.
    """
    by_host: dict[str, list] = {}
    for item in batch:
        by_host.setdefault(url_host(item.source_url), []).append(item)

    out = []
    queues = list(by_host.values())
    while queues:
        queues = [q for q in queues if q]
        for q in queues:
            out.append(q.pop(0))
    return out


async def _capture_batch(
    pool, get_bytes, limiter, host_limiter, settings, batch, circuit, resolve=None
) -> None:
    """Fetch a batch through a fixed set of workers pulling from a shared queue.

    Not `gather` over the batch. HostLimiter allows one in-flight request per
    hostname, and an article's images almost always come from one host, so a
    batch concentrates on a handful of hosts — measured live, 39 of 50 rows on
    three of them. Under `gather` each coroutine owned its item and simply waited
    its turn on the host lock, so the batch took as long as its slowest host's
    whole queue while other workers sat idle: 0.05 images/second against the 5.3
    the article walk produces.

    Pulling from a queue instead, a worker blocked on a busy host is one worker,
    and everyone else moves on to hosts that are free.
    """
    queue: asyncio.Queue = asyncio.Queue()
    for item in _interleave_by_host(batch):
        queue.put_nowait(item)

    async def worker() -> None:
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if circuit is not None and circuit.is_open(item.source_url):
                # Opened partway through this batch. Leave the row untouched — a
                # skipped image is not a failed one, and it is still queued.
                continue
            try:
                await _capture_one(
                    pool, get_bytes, limiter, host_limiter, settings, item, circuit, resolve
                )
            except Exception:  # noqa: BLE001 — one row's fault must not end this worker
                log.exception("recording %s failed", item.source_url)

    await asyncio.gather(*(worker() for _ in range(settings.image_concurrency)))


async def _capture_one(
    pool, get_bytes, limiter, host_limiter, settings, item, circuit=None, resolve=None
) -> None:
    try:
        # The host slot first, then the rate token. The token paces requests to
        # the fleet, so it has to be spent on a request that is about to happen —
        # taken first, a worker consumed a slot and then waited on a busy host,
        # charging the configured rate for queueing rather than for fetching.
        async with host_limiter.slot(item.source_url):
            await limiter.acquire()
            outcome = await asyncio.wait_for(
                capture_image(
                    get_bytes,
                    settings.image_root,
                    item.source_url,
                    max_bytes=settings.max_image_bytes,
                    resolve=resolve,
                ),
                timeout=settings.image_timeout_sec,
            )
    except TimeoutError:
        # Must precede the general handler: TimeoutError is an Exception. A host
        # that stops responding mid-body says nothing about whether the image
        # exists, so this becomes 'error' and stays retryable.
        log.warning(
            "%s did not finish within %.0fs", item.source_url, settings.image_timeout_sec
        )
        outcome = None
    except Exception:  # noqa: BLE001 — a filesystem or transport fault is not fatal
        log.exception("capturing %s failed", item.source_url)
        outcome = None

    # The last attempt this row will ever get. Without a line here a 429 storm is
    # invisible: a clean 429 raises nothing, so nothing else logs, and the only
    # symptom is images quietly missing months later. Named by host, because that
    # is the unit `requeue-images --host` recovers.
    failed = outcome is None or outcome.status != "ok"
    if circuit is not None:
        # 'dead' counts as an answer, not a failure: the host replied and told us
        # the image is gone. Only a timeout or a transport fault says the host
        # itself is in trouble.
        if outcome is not None and outcome.status in ("ok", "dead"):
            circuit.record_success(item.source_url)
        else:
            circuit.record_failure(item.source_url)
    if failed and item.attempts + 1 >= repo.MAX_IMAGE_ATTEMPTS:
        log.warning(
            "%s: giving up on %s after %d attempts — `babel requeue-images --host %s` retries it",
            url_host(item.source_url), item.source_url,
            repo.MAX_IMAGE_ATTEMPTS, url_host(item.source_url),
        )

    async with pool.acquire() as conn:
        if outcome is None:
            await repo.record_image_result(conn, item.article_id, item.source_url, "error")
            return
        if outcome.digest is not None:
            await repo.save_image_blob(conn, outcome.digest, outcome.mime, outcome.size)
        await repo.record_image_result(
            conn, item.article_id, item.source_url, outcome.status, outcome.digest
        )
