"""Command-line entrypoints."""

import asyncio
import contextlib
import functools
import json
import logging
import pathlib
import random
from collections.abc import Awaitable, Callable

import asyncpg
import click
from curl_cffi.requests import AsyncSession

from babel.config import Settings
from babel.crawler.backfill import run_backfill
from babel.crawler.fetcher import curl_getter
from babel.crawler.hostlimit import HostLimiter
from babel.crawler.images import ImageTooLarge
from babel.crawler.imageworker import run_image_worker
from babel.crawler.ingest import Ingestor
from babel.crawler.poller import poll_once
from babel.crawler.ratelimit import RateLimiter
from babel.db import repo
from babel.db.migrate import apply_migrations
from babel.notify import Throttled, build_notifier
from babel.vpn import IpInfo, IpLeak, check_ip_leak

log = logging.getLogger("babel")
MIGRATIONS = pathlib.Path(__file__).parent.parent.parent / "migrations"

# A failed egress lookup is not evidence of a leak (see _watch_egress below),
# but it must not be tolerated forever either — this bounds how long the
# watchdog runs blind before treating persistent lookup failure as fatal.
MAX_CONSECUTIVE_LOOKUP_FAILURES = 5

# Marking a range stale means re-crawling it at 1 req/s. Ten thousand articles
# is already the better part of a day, so anything larger asks for confirmation
# rather than trusting a typed range.
REFETCH_CONFIRM_THRESHOLD = 10_000


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
    """Fetch image bytes, abandoning anything past max_bytes.

    Image URLs point at arbitrary author-chosen hosts. Buffering first and
    checking the size afterwards means one bad URL can exhaust memory on a
    machine that is also running Postgres.
    """

    async def get_bytes(url: str, max_bytes: int) -> tuple[int, bytes, str | None]:
        response = await session.get(url, timeout=timeout_sec, stream=True)
        try:
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ImageTooLarge(f"{url} declares {declared} bytes")
            chunks, total = [], 0
            async for chunk in response.aiter_content():
                total += len(chunk)
                if total > max_bytes:
                    raise ImageTooLarge(f"{url} exceeded {max_bytes} bytes")
                chunks.append(chunk)
            return response.status_code, b"".join(chunks), response.headers.get("content-type")
        finally:
            await response.aclose()

    return get_bytes


@main.command()
def images() -> None:
    """Drain the image queue until stopped."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_images())


async def _images() -> None:
    settings = Settings()
    notifier = Throttled(build_notifier(settings), settings.alert_repeat_sec)

    info = await check_ip_leak(home_country=settings.home_country)
    log.info("egress %s (%s)", info.ip, info.country)

    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await apply_migrations(conn, MIGRATIONS)

    limiter = RateLimiter(settings.image_requests_per_second)
    async with AsyncSession(impersonate="chrome") as session:
        await asyncio.gather(
            _watch_egress(
                lambda: check_ip_leak(home_country=settings.home_country),
                settings.ip_check_interval_sec,
                notifier,
            ),
            run_image_worker(
                pool,
                _bytes_getter(session, settings.request_timeout_sec),
                limiter,
                HostLimiter(),
                notifier,
                settings,
            ),
        )


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
    notifier = Throttled(build_notifier(settings), settings.alert_repeat_sec)

    info = await check_ip_leak(home_country=settings.home_country)
    log.info("egress %s (%s)", info.ip, info.country)

    # Four consumers hold connections at peak: the backfill holds one for the
    # life of its now-infinite loop, the poller one per cycle, and Ingestor
    # nests another inside each. A max_size of 4 sat exactly on that limit, so
    # one more consumer would block forever with no error.
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=8)
    async with pool.acquire() as conn:
        await apply_migrations(conn, MIGRATIONS)

    limiter = RateLimiter(settings.requests_per_second)
    async with AsyncSession(impersonate="chrome") as session:
        ingestor = Ingestor(
            pool,
            curl_getter(session, settings.request_timeout_sec),
            limiter,
            settings,
        )

        async def fetch_rss(page: int) -> str:
            await limiter.acquire()
            response = await session.get(
                settings.rss_url(page), timeout=settings.request_timeout_sec
            )
            return response.text

        egress_check = functools.partial(check_ip_leak, home_country=settings.home_country)
        tasks = [
            asyncio.create_task(
                _watch_egress(egress_check, settings.ip_check_interval_sec, notifier)
            )
        ]
        if not no_poll:
            tasks.append(asyncio.create_task(_poll_forever(pool, ingestor, fetch_rss, settings)))
        if not no_backfill:
            tasks.append(
                asyncio.create_task(_backfill_forever(pool, ingestor, start_id, settings))
            )

        # Any task exiting means something is wrong — an IP leak, a crash. Bring
        # the rest down rather than limping on.
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:
            task.result()  # re-raise


async def _watch_egress(
    check: Callable[[], Awaitable[IpInfo]],
    interval_sec: float,
    notifier: Throttled | None = None,
) -> None:
    """Poll egress IP on an interval.

    ``check`` and ``interval_sec`` are injected (rather than read from
    ``Settings`` inside the loop) so this is testable without a network or a
    real clock; production passes a `check_ip_leak` call bound to `Settings`.
    ``notifier`` is optional so the unit tests below, which only care about
    the retry/exit logic, do not need to construct one.

    `IpLeak` means the egress IP reported the operator's home country and is
    fatal on the very first occurrence — never retried, because a confirmed
    leak is a reason to stop, not to wait. The spec has always called for
    "log, alert and exit" here; an operator asleep when the tunnel fails
    should not learn about it only from a dead container hours later.

    A bare `RuntimeError` out of `check` means every IP-info provider failed
    to answer after `check_ip_leak`'s own internal retries — those providers
    rate-limit aggressively and Gluetun's DNS has been observed to block some
    outright. That is a failed lookup, not evidence of a leak: the crawler's
    shared network namespace with the VPN container already makes a real leak
    architecturally impossible. Treating every failed lookup as fatal would
    take down a month-long crawl over provider flakiness, so consecutive
    failures are tolerated up to MAX_CONSECUTIVE_LOOKUP_FAILURES; any
    successful check resets the count.
    """
    consecutive_failures = 0
    while True:
        await asyncio.sleep(interval_sec)
        try:
            await check()
        except IpLeak as e:
            log.exception("egress IP is in the home country — stopping")
            if notifier is not None:
                await notifier.send_once("egress-leak", f"babel: egress IP leak detected — {e}")
            raise
        except RuntimeError as e:
            consecutive_failures += 1
            log.warning(
                "egress lookup failed (%d/%d consecutive): %s",
                consecutive_failures,
                MAX_CONSECUTIVE_LOOKUP_FAILURES,
                e,
            )
            if consecutive_failures >= MAX_CONSECUTIVE_LOOKUP_FAILURES:
                log.error(
                    "egress lookup failed %d times in a row — stopping", consecutive_failures
                )
                raise
        else:
            consecutive_failures = 0


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


async def _backfill_forever(
    pool, ingestor: Ingestor, start_id: int | None, settings: Settings
) -> None:
    async with pool.acquire() as conn:
        await run_backfill(
            conn,
            ingestor.ingest,
            start_id=start_id,
            stop_at=1,
            cooldown_sec=settings.retry_cooldown_sec,
            idle_sleep_sec=settings.backfill_idle_sleep_sec,
        )


def parse_id_selection(ids: str | None, from_id: int | None, to_id: int | None) -> list[int]:
    """Turn --ids or --from/--to into a sorted, deduplicated list of article IDs."""
    if ids and (from_id is not None or to_id is not None):
        raise ValueError("give either --ids or --from/--to, not both")
    if ids:
        out = set()
        for part in ids.split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit():
                raise ValueError(f"{part!r} is not a number")
            out.add(int(part))
        if not out:
            raise ValueError("either --ids or --from/--to is required")
        return sorted(out)
    if from_id is None and to_id is None:
        raise ValueError("either --ids or --from/--to is required")
    if from_id is None or to_id is None:
        raise ValueError("give both --from and --to")
    if from_id > to_id:
        raise ValueError("--from must not exceed --to")
    return list(range(from_id, to_id + 1))


@main.command()
@click.option("--ids", default=None, help="Comma-separated article IDs.")
@click.option("--from", "from_id", type=int, default=None, help="Range start, inclusive.")
@click.option("--to", "to_id", type=int, default=None, help="Range end, inclusive.")
@click.option("--yes", is_flag=True, help="Skip confirmation for large selections.")
def refetch(ids: str | None, from_id: int | None, to_id: int | None, yes: bool) -> None:
    """Queue already-collected articles for re-collection.

    Use after fixing a parser bug, or when the site's markup has changed. Only
    'ok' and 'error' rows are touched: a 404 says something about the article,
    not about our copy of it, and an ID never fetched will be reached by the
    walk anyway.
    """
    selection = parse_id_selection(ids, from_id, to_id)
    if len(selection) > REFETCH_CONFIRM_THRESHOLD and not yes:
        click.confirm(
            f"This queues {len(selection):,} articles for re-collection, "
            f"roughly {len(selection) / 86400:.1f} days at 1 request/second. Continue?",
            abort=True,
        )
    asyncio.run(_refetch(selection))


async def _refetch(selection: list[int]) -> None:
    settings = Settings()
    conn = await asyncpg.connect(settings.database_url)
    try:
        changed = await repo.mark_stale(conn, selection)
    finally:
        await conn.close()
    click.echo(f"queued {changed:,} of {len(selection):,} selected article(s) for re-collection")
