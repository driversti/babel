"""Command-line entrypoints."""

import asyncio
import contextlib
import json
import logging
import pathlib
import random

import asyncpg
import click
from curl_cffi.requests import AsyncSession

from babel.config import Settings
from babel.crawler.backfill import run_backfill
from babel.crawler.fetcher import curl_getter
from babel.crawler.ingest import Ingestor
from babel.crawler.poller import poll_once
from babel.crawler.ratelimit import RateLimiter
from babel.db.migrate import apply_migrations
from babel.vpn import IpLeak, check_ip_leak

log = logging.getLogger("babel")
MIGRATIONS = pathlib.Path(__file__).parent.parent.parent / "migrations"


@click.group()
def main() -> None:
    """babel — eRepublik article archive."""


@main.command()
@click.option("--count", default=100, help="How many article IDs to probe.")
@click.option("--newest", required=True, type=int, help="Highest known article ID.")
@click.option("--save-fixtures", default=0, help="How many successful pages to write to tests/fixtures/.")
def probe(count: int, newest: int, save_fixtures: int) -> None:
    """Fetch sample articles through the tunnel and report what came back."""
    asyncio.run(_probe(count, newest, save_fixtures))


async def _probe(count: int, newest: int, save_fixtures: int) -> None:
    settings = Settings()
    info = await check_ip_leak(home_country=settings.home_country)
    click.echo(f"egress: {info.ip} ({info.country})")

    ids = random.sample(range(newest - 200_000, newest), count)
    stats: dict[str, int] = {}
    saved = 0
    async with AsyncSession(impersonate="chrome") as session:
        for article_id in ids:
            try:
                r = await session.get(
                    settings.article_url(article_id), timeout=settings.request_timeout_sec
                )
                body = r.text
                if r.status_code == 200 and "postBody" in body:
                    key = "ok"
                elif r.status_code == 404:
                    key = "missing"
                elif "Just a moment" in body or "cf_chl" in body:
                    key = "CLOUDFLARE_CHALLENGE"
                else:
                    key = f"http_{r.status_code}"
                if key == "ok" and saved < save_fixtures:
                    path = pathlib.Path("tests/fixtures") / f"article_{article_id}.html"
                    path.write_text(body, encoding="utf-8")
                    saved += 1
            except Exception as e:  # noqa: BLE001
                key = type(e).__name__
            stats[key] = stats.get(key, 0) + 1
            await asyncio.sleep(1.0 / settings.requests_per_second)

    click.echo(json.dumps(stats, indent=2))
    if stats.get("CLOUDFLARE_CHALLENGE"):
        raise SystemExit("Cloudflare challenged us from this exit node — stop and reconsider.")


def _bytes_getter(session: AsyncSession, timeout_sec: int):
    async def get_bytes(url: str) -> tuple[int, bytes, str | None]:
        response = await session.get(url, timeout=timeout_sec)
        return response.status_code, response.content, response.headers.get("content-type")

    return get_bytes


@main.command()
def migrate() -> None:
    """Apply pending migrations."""
    asyncio.run(_migrate())


async def _migrate() -> None:
    settings = Settings()
    conn = await asyncpg.connect(settings.database_url)
    try:
        applied = await apply_migrations(conn, MIGRATIONS)
        click.echo(f"applied: {applied or 'nothing to do'}")
    finally:
        await conn.close()


@main.command()
@click.option("--start-id", type=int, default=None, help="Required only on the very first run.")
@click.option("--no-poll", is_flag=True, help="Backfill only.")
@click.option("--no-backfill", is_flag=True, help="Live polling only.")
def run(start_id: int | None, no_poll: bool, no_backfill: bool) -> None:
    """Run the crawler until stopped."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_run(start_id, no_poll, no_backfill))


async def _run(start_id: int | None, no_poll: bool, no_backfill: bool) -> None:
    settings = Settings()

    info = await check_ip_leak(home_country=settings.home_country)
    log.info("egress %s (%s)", info.ip, info.country)

    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await apply_migrations(conn, MIGRATIONS)

    limiter = RateLimiter(settings.requests_per_second)
    async with AsyncSession(impersonate="chrome") as session:
        ingestor = Ingestor(
            pool,
            curl_getter(session, settings.request_timeout_sec),
            _bytes_getter(session, settings.request_timeout_sec),
            limiter,
            settings,
        )

        async def fetch_rss(page: int) -> str:
            await limiter.acquire()
            response = await session.get(
                settings.rss_url(page), timeout=settings.request_timeout_sec
            )
            return response.text

        tasks = [asyncio.create_task(_watch_egress(settings))]
        if not no_poll:
            tasks.append(asyncio.create_task(_poll_forever(pool, ingestor, fetch_rss, settings)))
        if not no_backfill:
            tasks.append(asyncio.create_task(_backfill_forever(pool, ingestor, start_id)))

        # Any task exiting means something is wrong — an IP leak, an exhausted
        # backfill, a crash. Bring the rest down rather than limping on.
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:
            task.result()  # re-raise


async def _watch_egress(settings: Settings) -> None:
    while True:
        await asyncio.sleep(settings.ip_check_interval_sec)
        try:
            await check_ip_leak(home_country=settings.home_country)
        except IpLeak:
            log.exception("egress IP is in the home country — stopping")
            raise


async def _poll_forever(pool, ingestor: Ingestor, fetch_rss, settings: Settings) -> None:
    while True:
        try:
            async with pool.acquire() as conn:
                ingested = await poll_once(conn, ingestor.ingest, fetch_rss, settings.rss_pages)
            if ingested:
                log.info("poll ingested %d new articles", len(ingested))
        except Exception:  # noqa: BLE001 — the poller must survive a bad cycle
            log.exception("poll cycle failed")
        await asyncio.sleep(settings.poll_interval_sec)


async def _backfill_forever(pool, ingestor: Ingestor, start_id: int | None) -> None:
    async with pool.acquire() as conn:
        await run_backfill(conn, ingestor.ingest, start_id=start_id, stop_at=1)
    log.info("backfill reached article 1 — archive complete")
