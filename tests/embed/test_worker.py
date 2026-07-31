import dataclasses
import datetime

import pytest

from babel.config import Settings
from babel.db import repo
from babel.embed.client import EmbedError
from babel.embed.worker import run_embed_worker
from babel.models import Article

UTC = datetime.UTC


def _article(article_id: int) -> Article:
    return Article(
        id=article_id, title=f"title {article_id}", body=f"body {article_id}", body_raw=None,
        author_id=1, author_name="a", country="Poland",
        published_at=datetime.datetime(2026, 1, 1, tzinfo=UTC),
        e_day=6616, comment_count=0, comments=(), images=(),
    )


class FakeClient:
    def __init__(self, dim=repo.EMBED_DIM, fail_times=0):
        self.dim = dim
        self.batches = []
        self._fail_times = fail_times

    async def embed(self, texts):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise EmbedError("service down")
        self.batches.append(list(texts))
        return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send_once(self, key, text):
        self.sent.append((key, text))


class Clock:
    def __init__(self):
        self.slept = []

    async def sleep(self, seconds):
        self.slept.append(seconds)


@pytest.fixture
def settings():
    return Settings(
        database_url="postgresql://babel@unused/babel",
        embed_batch_size=2, embed_idle_sleep_sec=60.0,
        embed_backoff_base_sec=5.0, embed_backoff_max_sec=20.0,
    )


async def test_a_batch_is_embedded_and_stored(pool, pg, settings):
    for article_id in (10, 20):
        await repo.save_article(pg, _article(article_id))
    client, clock = FakeClient(), Clock()
    await run_embed_worker(pool, client, FakeNotifier(), settings,
                           sleep=clock.sleep, max_cycles=1)
    assert await repo.claim_pending_embeddings(pg, 10) == ()
    assert await pg.fetchval("SELECT count(*) FROM article_embeddings WHERE embedding IS NOT NULL") == 2


async def test_the_title_is_embedded_with_the_body(pool, pg, settings):
    await repo.save_article(pg, _article(10))
    client, clock = FakeClient(), Clock()
    await run_embed_worker(pool, client, FakeNotifier(), settings,
                           sleep=clock.sleep, max_cycles=1)
    assert client.batches == [["title 10\n\nbody 10"]]


async def test_long_bodies_are_truncated_before_they_are_sent(pool, pg, settings):
    # dataclasses.replace, not `type(a)(**a.__dict__)`: Article is
    # @dataclass(frozen=True, slots=True) and a slots dataclass has no __dict__.
    await repo.save_article(pg, dataclasses.replace(_article(10), body="x" * 50_000))
    settings = settings.model_copy(update={"embed_max_chars": 100})
    client, clock = FakeClient(), Clock()
    await run_embed_worker(pool, client, FakeNotifier(), settings,
                           sleep=clock.sleep, max_cycles=1)
    assert len(client.batches[0][0]) == 100


async def test_an_empty_queue_sleeps_rather_than_spinning(pool, settings):
    client, clock = FakeClient(), Clock()
    await run_embed_worker(pool, client, FakeNotifier(), settings,
                           sleep=clock.sleep, max_cycles=1)
    assert clock.slept == [settings.embed_idle_sleep_sec]
    assert client.batches == []


async def test_a_failed_batch_leaves_the_rows_queued(pool, pg, settings):
    await repo.save_article(pg, _article(10))
    client, clock = FakeClient(fail_times=1), Clock()
    await run_embed_worker(pool, client, FakeNotifier(), settings,
                           sleep=clock.sleep, max_cycles=1)
    assert [p.article_id for p in await repo.claim_pending_embeddings(pg, 10)] == [10]


async def test_repeated_failures_back_off_and_stop_doubling_at_the_ceiling(pool, pg, settings):
    await repo.save_article(pg, _article(10))
    client, clock = FakeClient(fail_times=99), Clock()
    await run_embed_worker(pool, client, FakeNotifier(), settings,
                           sleep=clock.sleep, max_cycles=4)
    assert clock.slept == [5.0, 10.0, 20.0, 20.0]


async def test_a_persistent_failure_alerts_once(pool, pg, settings):
    await repo.save_article(pg, _article(10))
    notifier = FakeNotifier()
    client, clock = FakeClient(fail_times=99), Clock()
    await run_embed_worker(pool, client, notifier, settings,
                           sleep=clock.sleep, max_cycles=3)
    assert [key for key, _ in notifier.sent] == ["embed-service-down"] * 3


async def test_the_backoff_survives_days_of_failure_without_overflowing(pool, pg, settings):
    """`settings.embed_backoff_base_sec * 2 ** (consecutive_failures - 1)` is
    computed in full before `min()` caps it against embed_backoff_max_sec. At
    consecutive_failures = 1025, `2 ** 1024` is an int too large to convert to
    a float, and OverflowError is raised computing `delay` itself — a
    statement that runs *after* `client.embed`'s exception has already been
    caught, so it is not inside the try/except above it and propagates out of
    run_embed_worker uncaught. ~7 days of a down embed service (doubling every
    idle-sleep-free failure cycle) is not a contrived count for a service that
    is meant to run indefinitely. Impact is bounded — a restart recovers — but
    the traceback names arithmetic instead of the embed service.
    """
    await repo.save_article(pg, _article(10))
    client, clock = FakeClient(fail_times=10_000), Clock()
    await run_embed_worker(pool, client, FakeNotifier(), settings,
                           sleep=clock.sleep, max_cycles=1025)
    assert clock.slept[-1] == settings.embed_backoff_max_sec


class _FailFailSucceedThenFailAgain:
    """fail, fail, succeed, fail — all inside one continuous worker run, so a
    deleted `consecutive_failures = 0` reset is actually exercised rather than
    reset for free by a fresh `run_embed_worker` call starting its own local
    variable at 0.

    The third call's "success" queues a fresh article as a side effect —
    standing in for a poll or backfill enqueuing new work while the worker was
    recovering — so the fourth cycle has something to fail on instead of
    finding an empty, already-drained queue.
    """

    dim = repo.EMBED_DIM

    def __init__(self, pg):
        self._pg = pg
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        if self.calls == 3:
            await repo.save_article(self._pg, _article(20))
            return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]
        raise EmbedError("service down")


async def test_a_success_resets_the_backoff_to_base(pool, pg, settings):
    """fail, fail, succeed, fail: the fourth failure's delay must restart at
    embed_backoff_base_sec, not continue doubling from the first two.
    Deleting `consecutive_failures = 0` after a successful embed currently
    leaves the rest of the suite green — nothing else exercises a recovery
    followed by a fresh failure.
    """
    await repo.save_article(pg, _article(10))
    client, clock = _FailFailSucceedThenFailAgain(pg), Clock()
    await run_embed_worker(pool, client, FakeNotifier(), settings,
                           sleep=clock.sleep, max_cycles=4)
    assert clock.slept == [5.0, 10.0, 5.0]
