import asyncio

from babel.config import Settings
from babel.crawler.hostlimit import HostLimiter
from babel.crawler.imageworker import run_image_worker
from babel.crawler.ratelimit import RateLimiter
from babel.db import repo
from babel.notify import Throttled


class Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


async def seed(pg, article_id: int, urls: list[str]) -> None:
    await pg.execute(
        "INSERT INTO articles (id, title, body, published_at) VALUES ($1,'t','b',now())",
        article_id,
    )
    for position, url in enumerate(urls):
        await pg.execute(
            """INSERT INTO article_images (article_id, position, source_url, status)
               VALUES ($1, $2, $3, 'pending')""",
            article_id, position, url,
        )


def settings(tmp_path, **kw):
    base = dict(_env_file=None, image_root=str(tmp_path), min_free_bytes=0)
    return Settings(**{**base, **kw})


async def noop_sleep(_seconds):
    return None


async def test_drains_the_queue_and_stores_bytes(pg, tmp_path, fake_pool):
    await seed(pg, 1, ["https://a.example/1.png", "https://a.example/2.png"])

    async def get_bytes(url, max_bytes):
        return 200, b"\x89PNG " + url.encode(), "image/png"

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0), settings(tmp_path),
        sleep=noop_sleep, max_cycles=2,
    )
    rows = await pg.fetch("SELECT status FROM article_images ORDER BY position")
    assert [r["status"] for r in rows] == ["ok", "ok"]
    assert await pg.fetchval("SELECT count(*) FROM images") == 2


async def test_drains_newest_article_first(pg, tmp_path, fake_pool):
    await seed(pg, 10, ["https://a.example/old.png"])
    await seed(pg, 20, ["https://a.example/new.png"])
    order: list[str] = []

    async def get_bytes(url, max_bytes):
        order.append(url)
        return 200, b"\x89PNG", "image/png"

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0), settings(tmp_path),
        sleep=noop_sleep, max_cycles=1,
    )
    assert order[0].endswith("new.png")


async def test_a_dead_host_marks_dead_and_does_not_stop_the_batch(pg, tmp_path, fake_pool):
    await seed(pg, 1, ["https://a.example/gone.png", "https://b.example/fine.png"])

    async def get_bytes(url, max_bytes):
        if "gone" in url:
            return 404, b"", None
        return 200, b"\x89PNG", "image/png"

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0), settings(tmp_path),
        sleep=noop_sleep, max_cycles=2,
    )
    rows = await pg.fetch("SELECT status FROM article_images ORDER BY position")
    assert [r["status"] for r in rows] == ["dead", "ok"]


async def test_a_full_disk_leaves_the_queue_untouched_and_notifies(pg, tmp_path, fake_pool):
    await seed(pg, 1, ["https://a.example/1.png"])
    recorder = Recorder()

    async def get_bytes(url, max_bytes):
        raise AssertionError("must not fetch below the free-space floor")

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(recorder, 3600, now=lambda: 0.0),
        settings(tmp_path, min_free_bytes=10**18),
        sleep=noop_sleep, max_cycles=3,
    )
    row = await pg.fetchrow("SELECT status, attempts FROM article_images WHERE article_id = 1")
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert len(recorder.sent) == 1  # throttled: one alert, not one per cycle


async def test_an_errored_image_is_retried_until_the_ceiling(pg, tmp_path, fake_pool):
    await seed(pg, 1, ["https://a.example/flaky.png"])
    calls = {"n": 0}

    async def get_bytes(url, max_bytes):
        calls["n"] += 1
        raise TimeoutError("slow")

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0), settings(tmp_path),
        sleep=noop_sleep, max_cycles=10,
    )
    row = await pg.fetchrow("SELECT status, attempts FROM article_images WHERE article_id = 1")
    assert row["status"] == "error"
    assert row["attempts"] == repo.MAX_IMAGE_ATTEMPTS
    assert calls["n"] == repo.MAX_IMAGE_ATTEMPTS


async def test_an_empty_queue_sleeps_rather_than_spinning(pg, tmp_path, fake_pool):
    slept: list[float] = []

    async def record_sleep(seconds):
        slept.append(seconds)

    async def get_bytes(url, max_bytes):
        raise AssertionError("nothing to fetch")

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0), settings(tmp_path),
        sleep=record_sleep, max_cycles=2,
    )
    assert slept == [60.0, 60.0]


async def test_a_hanging_host_cannot_wedge_the_worker(pg, tmp_path, fake_pool):
    """Observed live: the worker stopped for good on one host that accepted the
    connection and then sent nothing.

    curl's timeout does not bound a streamed body read, so `aiter_content` never
    returned. Because the batch is processed one item at a time, that single
    request stalled the entire image archive — 22,000 rows queued, CPU at 0.2%,
    container reporting healthy, nothing collected for as long as it stayed up.
    A stuck fetch must cost one row, not the worker.
    """
    await seed(pg, 1, ["https://hangs.example/1.png", "https://works.example/2.png"])

    async def get_bytes(url, max_bytes):
        if "hangs" in url:
            await asyncio.sleep(3600)
        return 200, b"\x89PNG ok", "image/png"

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0),
        settings(tmp_path, image_timeout_sec=0.05),
        sleep=noop_sleep, max_cycles=1,
    )

    rows = dict(await pg.fetch("SELECT source_url, status FROM article_images"))
    assert rows["https://works.example/2.png"] == "ok", "the healthy image must still be collected"
    assert rows["https://hangs.example/1.png"] == "error", (
        "a timeout is not evidence the image is gone, so it must stay retryable"
    )


async def test_a_slow_host_does_not_idle_the_rate_budget(pg, tmp_path, fake_pool):
    """The rate limiter permits 5 requests/second; strictly sequential processing
    delivers that only if every fetch is instantaneous.

    Measured live: 0.13 images/second against the 5.3/second the article walk
    produces, so the queue grew without bound. The fix is concurrency within the
    batch — the global limiter and the one-request-per-host lock still cap the
    load, so politeness is unchanged; what changes is that a slow host no longer
    spends everyone else's budget waiting.
    """
    await seed(pg, 1, [f"https://h{i}.example/x.png" for i in range(8)])
    in_flight = 0
    peak = 0

    async def get_bytes(url, max_bytes):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.05)
            return 200, b"\x89PNG " + url.encode(), "image/png"
        finally:
            in_flight -= 1

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0),
        settings(tmp_path, image_concurrency=4),
        sleep=noop_sleep, max_cycles=1,
    )

    assert peak > 1, f"requests never overlapped (peak={peak}); a slow host still blocks the batch"
    assert peak <= 4, f"peak {peak} exceeded image_concurrency=4"
    assert await pg.fetchval("SELECT count(*) FROM article_images WHERE status='ok'") == 8
