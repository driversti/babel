import asyncio
import contextlib
import socket

from babel.config import Settings
from babel.crawler.circuit import HostCircuit
from babel.crawler.hostlimit import HostLimiter
from babel.crawler.images import url_host
from babel.crawler.imageworker import _capture_one, _interleave_by_host, run_image_worker
from babel.crawler.ratelimit import RateLimiter
from babel.db import repo
from babel.notify import Throttled


async def _fake_resolve(_host: str) -> list[tuple]:
    """Stand-in for DNS. These tests are about the worker's own behaviour —
    retries, concurrency, the host circuit breaker — and must not depend on
    live resolution for hosts like "a.example" that exist only as labels
    here. classify_url's own resolution behaviour, including real failure
    modes, is covered directly in test_images.py, which does not use this
    seam."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


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


class _Item:
    """Just enough of repo.PendingImage for the ordering tests."""

    def __init__(self, source_url: str) -> None:
        self.source_url = source_url


async def noop_sleep(_seconds):
    return None


async def test_drains_the_queue_and_stores_bytes(pg, tmp_path, fake_pool):
    await seed(pg, 1, ["https://a.example/1.png", "https://a.example/2.png"])

    async def get_bytes(url, max_bytes):
        return 200, b"\x89PNG " + url.encode(), "image/png"

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0), settings(tmp_path),
        sleep=noop_sleep, max_cycles=2, resolve=_fake_resolve,
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
        sleep=noop_sleep, max_cycles=1, resolve=_fake_resolve,
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
        sleep=noop_sleep, max_cycles=2, resolve=_fake_resolve,
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
    """With no cooldown the ceiling still applies — that mechanism is unchanged.

    The circuit breaker is given a threshold it cannot reach, because it would
    otherwise hold the host off after three failures and the ceiling would never
    be exercised. That interaction is real and wanted; it is just not what this
    test is about.
    """
    await seed(pg, 1, ["https://a.example/flaky.png"])
    calls = {"n": 0}

    async def get_bytes(url, max_bytes):
        calls["n"] += 1
        raise TimeoutError("slow")

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0),
        settings(tmp_path, image_retry_cooldown_sec=0),
        circuit=HostCircuit(threshold=999, open_sec=1, now=lambda: 0.0),
        sleep=noop_sleep, max_cycles=10, resolve=_fake_resolve,
    )
    row = await pg.fetchrow("SELECT status, attempts FROM article_images WHERE article_id = 1")
    assert row["status"] == "error"
    assert row["attempts"] == repo.MAX_IMAGE_ATTEMPTS
    assert calls["n"] == repo.MAX_IMAGE_ATTEMPTS


async def test_a_host_having_a_bad_minute_does_not_burn_every_attempt(pg, tmp_path, fake_pool):
    """The article path guards this; the image path did not, and it is the side
    that is actually rate-limit-prone.

    A failed row stays the newest row in the queue — the backfill only ever
    enqueues lower article ids — so it was re-claimed on the very next cycle. Five
    cycles is under a minute at the default batch size and rate, after which the
    row sits at the ceiling, which `claim_pending_images` excludes forever. A 429
    storm or a brief tunnel blip therefore wrote off living images permanently,
    with no log line, because a clean 429 raises nothing.
    """
    await seed(pg, 1, ["https://a.example/flaky.png"])
    calls = {"n": 0}

    async def get_bytes(url, max_bytes):
        calls["n"] += 1
        raise TimeoutError("slow")

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0),
        settings(tmp_path, image_retry_cooldown_sec=3600),
        sleep=noop_sleep, max_cycles=10, resolve=_fake_resolve,
    )
    row = await pg.fetchrow("SELECT status, attempts FROM article_images WHERE article_id = 1")
    assert calls["n"] == 1, f"ten cycles inside the cooldown made {calls['n']} attempts"
    assert row["attempts"] == 1
    assert row["status"] == "error", "still retryable — just not right now"


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
        sleep=noop_sleep, max_cycles=1, resolve=_fake_resolve,
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
        sleep=noop_sleep, max_cycles=1, resolve=_fake_resolve,
    )

    assert peak > 1, f"requests never overlapped (peak={peak}); a slow host still blocks the batch"
    assert peak <= 4, f"peak {peak} exceeded image_concurrency=4"
    assert await pg.fetchval("SELECT count(*) FROM article_images WHERE status='ok'") == 8


def test_a_batch_is_interleaved_across_hosts():
    """The queue arrives grouped by host and must not be worked that way.

    Rows come back ordered by (article_id, position), and an article's images
    almost always share a host, so a batch is runs of one host: measured live,
    39 of 50 rows on three hosts. HostLimiter allows one in-flight request per
    hostname, so workers taking that order in turn all pile onto the same host
    and block — whether they own an item each (gather) or pull from a shared
    queue, since they pull in order too. Only 1 of 8 workers makes progress.

    Interleaving makes as many hosts busy at once as there are hosts, which is
    the most one-request-per-host permits.
    """
    batch = [
        _Item("https://a.example/0.png"), _Item("https://a.example/1.png"),
        _Item("https://a.example/2.png"), _Item("https://b.example/0.png"),
        _Item("https://b.example/1.png"), _Item("https://c.example/0.png"),
    ]
    order = [url_host(i.source_url) for i in _interleave_by_host(batch)]

    assert order == ["a.example", "b.example", "c.example", "a.example", "b.example", "a.example"]


def test_interleaving_keeps_every_row_exactly_once():
    batch = [_Item(f"https://h{i % 3}.example/{i}.png") for i in range(20)]
    assert sorted(i.source_url for i in _interleave_by_host(batch)) == sorted(
        i.source_url for i in batch
    )


def test_interleaving_preserves_order_within_a_host():
    """Newest-article-first is the drain order the survival curve requires, and
    it must survive the shuffle for each host's own images."""
    batch = [_Item(f"https://a.example/{i}.png") for i in range(4)]
    assert [i.source_url for i in _interleave_by_host(batch)] == [
        i.source_url for i in batch
    ]


async def test_the_rate_token_is_taken_after_the_host_slot(tmp_path):
    """The global limiter paces requests to the fleet, so its token must be spent
    on a request that is about to happen.

    Taken before the per-host lock, a worker consumed a slot and then sat waiting
    for a busy host — the configured rate was charged for queueing rather than
    fetching, which is most of the wait when a batch concentrates on a few hosts.
    Ordering, not timing, so this is deterministic.
    """
    events: list[str] = []

    class Limiter:
        async def acquire(self):
            events.append("rate")

    class Hosts:
        @contextlib.asynccontextmanager
        async def slot(self, url):
            events.append("host")
            yield

    async def get_bytes(url, max_bytes):
        events.append("request")
        return 200, b"\x89PNG ok", "image/png"

    class OnePool:
        def acquire(self):
            return contextlib.nullcontext(None)

    async def noop(*a, **kw):
        return None

    item = repo.PendingImage(
        article_id=1, position=0, source_url="https://a.example/x.png", attempts=0
    )
    original = repo.record_image_result, repo.save_image_blob
    repo.record_image_result, repo.save_image_blob = noop, noop
    try:
        await _capture_one(
            OnePool(), get_bytes, Limiter(), Hosts(), settings(tmp_path), item,
            resolve=_fake_resolve,
        )
    finally:
        repo.record_image_result, repo.save_image_blob = original

    assert events == ["host", "rate", "request"], events


async def test_a_failing_host_is_held_off_while_others_keep_going(pg, tmp_path, fake_pool):
    """The whole point: one bad host must not cost the queue its throughput.

    i.postimg.cc began stalling for the full request timeout, and because the
    drain is newest-first and those articles' images clustered there, nearly
    every batch was postimg at 20s each — 0.04 images/second while every other
    host answered in under a second.
    """
    await seed(pg, 1, [f"https://bad.example/{i}.png" for i in range(4)])
    await seed(pg, 2, [f"https://good.example/{i}.png" for i in range(4)])
    attempted: list[str] = []

    async def get_bytes(url, max_bytes):
        attempted.append(url)
        if "bad.example" in url:
            raise TimeoutError("stalled")
        return 200, b"\x89PNG ok", "image/png"

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0),
        settings(tmp_path, image_retry_cooldown_sec=0, image_concurrency=1),
        circuit=HostCircuit(threshold=3, open_sec=900, now=lambda: 0.0),
        sleep=noop_sleep, max_cycles=6, resolve=_fake_resolve,
    )

    bad_attempts = [u for u in attempted if "bad.example" in u]
    assert len(bad_attempts) == 3, (
        f"the host should be left alone after 3 consecutive failures, got {len(bad_attempts)}"
    )
    assert await pg.fetchval(
        "SELECT count(*) FROM article_images WHERE status = 'ok'"
    ) == 4, "the healthy host's images must still be collected"


async def test_a_dead_link_is_an_answer_not_a_host_failure(pg, tmp_path, fake_pool):
    """A 404 means the host is working and the image is gone. Counting those as
    host failures would hold off exactly the hosts still answering — and old
    articles are full of dead links by design."""
    await seed(pg, 1, [f"https://alive.example/{i}.png" for i in range(5)])

    async def get_bytes(url, max_bytes):
        return 404, b"", None

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0),
        settings(tmp_path, image_concurrency=1),
        circuit=HostCircuit(threshold=3, open_sec=900, now=lambda: 0.0),
        sleep=noop_sleep, max_cycles=2, resolve=_fake_resolve,
    )
    assert await pg.fetchval("SELECT count(*) FROM article_images WHERE status='dead'") == 5
