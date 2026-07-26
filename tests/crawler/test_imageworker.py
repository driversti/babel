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
