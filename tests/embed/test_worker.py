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
