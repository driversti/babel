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


async def test_start_id_is_required_when_there_is_no_cursor(pg):
    async def ingest(article_id: int) -> str:
        return "ok"

    try:
        await run_backfill(pg, ingest, stop_at=1)
    except ValueError as e:
        assert "start_id" in str(e)
    else:
        raise AssertionError("expected ValueError")
