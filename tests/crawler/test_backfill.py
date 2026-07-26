from babel.crawler.backfill import run_backfill
from babel.db import repo


async def noop_sleep(_seconds):
    return None


async def test_walks_ids_downward_from_the_start(pg):
    seen: list[int] = []

    async def ingest(article_id: int) -> str:
        seen.append(article_id)
        await repo.record_fetch(pg, article_id, "ok")
        return "ok"

    await run_backfill(pg, ingest, start_id=100, stop_at=96, batch_size=2,
                       sleep=noop_sleep, max_cycles=3)
    assert seen == [100, 99, 98, 97, 96]


async def test_persists_the_cursor_so_a_restart_resumes(pg):
    async def ingest(article_id: int) -> str:
        await repo.record_fetch(pg, article_id, "ok")
        return "ok"

    await run_backfill(pg, ingest, start_id=100, stop_at=98, batch_size=1,
                       sleep=noop_sleep, max_cycles=2)
    assert await repo.get_cursor(pg, "backfill") == 98

    seen: list[int] = []

    async def ingest2(article_id: int) -> str:
        seen.append(article_id)
        await repo.record_fetch(pg, article_id, "ok")
        return "ok"

    await run_backfill(pg, ingest2, stop_at=96, batch_size=1, sleep=noop_sleep, max_cycles=3)
    assert seen == [98, 97, 96]


async def test_skips_ids_already_recorded(pg):
    await repo.record_fetch(pg, 99, "ok")
    await repo.record_fetch(pg, 98, "missing")
    seen: list[int] = []

    async def ingest(article_id: int) -> str:
        seen.append(article_id)
        await repo.record_fetch(pg, article_id, "ok")
        return "ok"

    await run_backfill(pg, ingest, start_id=100, stop_at=97, batch_size=10,
                       sleep=noop_sleep, max_cycles=2)
    assert seen == [100, 97]


async def test_start_id_is_required_when_there_is_no_cursor(pg):
    async def ingest(article_id: int) -> str:
        return "ok"

    try:
        await run_backfill(pg, ingest, stop_at=1, sleep=noop_sleep, max_cycles=1)
    except ValueError as e:
        assert "start_id" in str(e)
    else:
        raise AssertionError("expected ValueError")


async def test_a_failing_ingest_does_not_abort_the_walk(pg):
    seen: list[int] = []

    async def ingest(article_id: int) -> str:
        seen.append(article_id)
        if article_id == 99:
            raise RuntimeError("boom")
        await repo.record_fetch(pg, article_id, "ok")
        return "ok"

    await run_backfill(pg, ingest, start_id=100, stop_at=98, batch_size=10,
                       sleep=noop_sleep, max_cycles=1)
    assert seen == [100, 99, 98]
    assert await pg.fetchval("SELECT status FROM fetch_log WHERE article_id = 99") == "error"


async def test_the_sweep_retries_an_errored_id_after_the_walk_is_done(pg):
    # The walk records 99 as an error and moves past it. Nothing in the old
    # design would ever return; the sweep must.
    attempts: list[int] = []

    async def ingest(article_id: int) -> str:
        attempts.append(article_id)
        if article_id == 99 and attempts.count(99) == 1:
            raise RuntimeError("transient")
        await repo.record_fetch(pg, article_id, "ok")
        return "ok"

    await run_backfill(pg, ingest, start_id=100, stop_at=99, batch_size=10,
                       cooldown_sec=0, sleep=noop_sleep, max_cycles=3)
    assert attempts.count(99) == 2
    assert await pg.fetchval("SELECT status FROM fetch_log WHERE article_id = 99") == "ok"


async def test_the_sweep_picks_up_a_stale_row(pg):
    await repo.record_fetch(pg, 50, "ok")
    await repo.mark_stale(pg, [50])
    seen: list[int] = []

    async def ingest(article_id: int) -> str:
        seen.append(article_id)
        await repo.record_fetch(pg, article_id, "ok")
        return "ok"

    # Cursor already at the bottom: only the sweep can produce work.
    await repo.set_cursor(pg, "backfill", 0)
    await run_backfill(pg, ingest, stop_at=1, cooldown_sec=0, sleep=noop_sleep, max_cycles=2)
    assert seen == [50]


async def test_it_sleeps_instead_of_exiting_when_there_is_nothing_to_do(pg):
    # Finding I4: reaching article 1 used to end the process, and the container
    # then crash-looped under `restart: unless-stopped`.
    slept: list[float] = []

    async def record_sleep(seconds):
        slept.append(seconds)

    async def ingest(article_id: int) -> str:
        raise AssertionError("there is no work")

    await repo.set_cursor(pg, "backfill", 0)
    await run_backfill(pg, ingest, stop_at=1, idle_sleep_sec=300.0,
                       sleep=record_sleep, max_cycles=3)
    assert slept == [300.0, 300.0, 300.0]
