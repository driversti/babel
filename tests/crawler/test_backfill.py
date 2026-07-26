from babel.crawler.backfill import run_backfill
from babel.db import repo


async def test_walks_ids_downward_from_the_start(pg):
    seen: list[int] = []

    async def ingest(article_id: int) -> str:
        seen.append(article_id)
        return "ok"

    await run_backfill(pg, ingest, start_id=100, stop_at=96, batch_size=2)
    assert seen == [100, 99, 98, 97, 96]


async def test_persists_the_cursor_so_a_restart_resumes(pg):
    async def ingest(article_id: int) -> str:
        return "ok"

    await run_backfill(pg, ingest, start_id=100, stop_at=98, batch_size=1)
    assert await repo.get_cursor(pg, "backfill") == 97

    seen: list[int] = []

    async def ingest2(article_id: int) -> str:
        seen.append(article_id)
        return "ok"

    await run_backfill(pg, ingest2, stop_at=96, batch_size=1)
    assert seen == [97, 96]


async def test_skips_ids_already_recorded(pg):
    await repo.record_fetch(pg, 99, "ok")
    await repo.record_fetch(pg, 98, "missing")
    seen: list[int] = []

    async def ingest(article_id: int) -> str:
        seen.append(article_id)
        return "ok"

    await run_backfill(pg, ingest, start_id=100, stop_at=97, batch_size=10)
    assert seen == [100, 97]


async def test_ingest_failure_does_not_abort_the_batch(pg):
    seen: list[int] = []

    async def ingest(article_id: int) -> str:
        if article_id == 98:
            raise RuntimeError("boom")
        seen.append(article_id)
        return "ok"

    await run_backfill(pg, ingest, start_id=100, stop_at=96, batch_size=10)

    assert seen == [100, 99, 97, 96]
    assert await repo.get_cursor(pg, "backfill") == 95
    assert await pg.fetchval("SELECT status FROM fetch_log WHERE article_id = 98") == "error"


async def test_errored_id_is_retried_on_a_later_walk_over_the_same_range(pg):
    # Successes must record 'ok' the way the real Ingestor does (run_backfill
    # itself only writes fetch_log on failure), or the second walk would treat
    # every ID as never-seen instead of exercising the retry-only-errors path.
    async def failing_ingest(article_id: int) -> str:
        if article_id == 98:
            raise RuntimeError("boom")
        await repo.record_fetch(pg, article_id, "ok")
        return "ok"

    await run_backfill(pg, failing_ingest, start_id=100, stop_at=96, batch_size=10)
    assert await pg.fetchval("SELECT status FROM fetch_log WHERE article_id = 98") == "error"

    seen: list[int] = []

    async def ingest(article_id: int) -> str:
        seen.append(article_id)
        return "ok"

    # Simulate a later backfill pass walking the same ID range again. A fresh
    # cursor_name is used because the first walk already advanced "backfill"
    # past this range; a second pass (e.g. a restarted crawl with a wider
    # stop_at, or a dedicated re-check job) would revisit these IDs the same
    # way.
    await run_backfill(
        pg, ingest, cursor_name="backfill-retry", start_id=100, stop_at=96, batch_size=10
    )

    # 100, 99, 97, 96 already succeeded and must not be re-fetched; only the
    # previously errored 98 should come back.
    assert seen == [98]


async def test_start_id_is_required_when_there_is_no_cursor(pg):
    async def ingest(article_id: int) -> str:
        return "ok"

    try:
        await run_backfill(pg, ingest, stop_at=1)
    except ValueError as e:
        assert "start_id" in str(e)
    else:
        raise AssertionError("expected ValueError")
