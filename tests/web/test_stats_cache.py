import asyncio
import datetime

from babel.config import Settings
from babel.db import browse
from babel.web import routes
from babel.web.app import create_app

UTC = datetime.UTC


def _settings(image_root):
    return Settings(
        database_url="postgresql://babel@unused/babel",
        web_database_url="postgresql://babel_web@unused/babel",
        contact="archive@example.invalid",
        image_root=str(image_root),
    )


def _stats_value(n):
    return browse.ArchiveStats(
        articles=n,
        oldest=datetime.datetime(2026, 1, 1, tzinfo=UTC),
        newest=datetime.datetime(2026, 1, 2, tzinfo=UTC),
        frontier=None,
    )


async def test_concurrent_callers_share_one_archive_stats_scan(pg, pool, image_root, monkeypatch):
    """The stampede the five-minute cache does not prevent on its own.

    The cache is written only after the query returns, so every request that
    arrives while the entry is stale used to start its own scan of `articles` —
    up to `web_pool_size` of them, each with its own parallel workers, from one
    ordinary burst of traffic against the table the crawler writes to.

    Driven through `_stats` directly rather than through the app, because the
    tests' pool hands out one exclusive connection: two requests can never be
    inside the handler at the same time, so the app itself cannot show this.
    """
    app = create_app(_settings(image_root), pool=pool)

    calls = 0

    async def counting_stats(conn):
        nonlocal calls
        calls += 1
        # The scan is not instantaneous, and that interval is the whole window
        # in which the herd forms.
        await asyncio.sleep(0.05)
        return _stats_value(7)

    monkeypatch.setattr(browse, "archive_stats", counting_stats)

    async with app.router.lifespan_context(app):
        results = await asyncio.gather(*(routes._stats(app, pg) for _ in range(10)))

    assert calls == 1, f"{calls} concurrent scans of articles instead of one"
    assert [r.articles for r in results] == [7] * 10


async def test_the_waiters_get_the_value_the_winner_computed(pg, pool, image_root, monkeypatch):
    """Sharing the scan must mean sharing its result, not returning stale rubbish.

    A single-flight that let the waiters through to their own query would show
    up here as a second call; one that returned them a pre-refresh value would
    show up as the wrong number.
    """
    app = create_app(_settings(image_root), pool=pool)
    answers = iter([_stats_value(11), _stats_value(22)])

    async def once(conn):
        await asyncio.sleep(0.05)
        return next(answers)

    monkeypatch.setattr(browse, "archive_stats", once)

    async with app.router.lifespan_context(app):
        results = await asyncio.gather(*(routes._stats(app, pg) for _ in range(5)))
        # And a later caller, still inside the TTL, is served from the cache.
        later = await routes._stats(app, pg)

    assert [r.articles for r in results] == [11] * 5
    assert later.articles == 11
