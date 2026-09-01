"""Command-line entrypoints."""

import asyncio
import contextlib
import functools
import json
import logging
import pathlib
import random
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin

import asyncpg
import click
from curl_cffi.requests import AsyncSession

from babel.config import Settings
from babel.crawler.backfill import run_backfill
from babel.crawler.fetcher import curl_getter
from babel.crawler.hostlimit import HostLimiter
from babel.crawler.hostprobe import WRITE_OFF_VERDICTS, probe_host
from babel.crawler.images import ImageBlocked, ImageTooLarge, classify_url
from babel.crawler.imageworker import run_image_worker
from babel.crawler.ingest import Ingestor
from babel.crawler.poller import newest_article_id, poll_once
from babel.crawler.ratelimit import RateLimiter
from babel.db import repo
from babel.db.migrate import apply_migrations
from babel.db.repo import hide_article, withhold_image
from babel.notify import Throttled, build_notifier
from babel.vpn import IpInfo, IpLeak, check_ip_leak
from babel.web.blobs import parse_digest

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


# What a browser sends when it loads an <img>, and not what it sends when it
# navigates to a page. curl_cffi's impersonation supplies the navigation header
# set, so without this the request looks like a human opening the image's landing
# page — and the big image hosts answer accordingly. Measured on the first live
# run, where 64% of images from same-day articles were recorded as gone:
#   - media.giphy.com  returned 200 text/html instead of the gif
#   - i.postimg.cc     returned its 200 text/html "Postimages" viewer page
#   - i.imgur.com      returned 429
# All three return the actual bytes once these are set. Sec-Fetch-Dest is the
# load-bearing one; a Referer also satisfies postimg but would tell every
# author-chosen third-party host where we crawl from, so we do not send one.
IMAGE_FETCH_HEADERS = {
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Sec-Fetch-Dest": "image",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
}


# curl_cffi 0.15.0 follows up to 30 redirects on its own. classify_url's guard in
# capture_image only ever sees the URL an article wrote, so a public host that
# 302s into 127.0.0.1 would sail straight through it — measured directly: a
# public URL returning "302 Location: http://127.0.0.1/secret" was followed and
# the private body came back. Turning every redirect into a failure is not the
# fix either: real image hosts redirect constantly (CDN migrations, URL
# shorteners), and this project has already lost a batch of images to exactly
# that class of over-eager "not a 200, so it must be dead/broken" mistake. So
# redirects are followed by hand below, one hop at a time, classifying every
# hop before it is dialled — the same guard capture_image applies to the first
# URL, applied again to every URL a redirect ever points at.
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})

# Five hops is headroom, not a target — every legitimate image host redirects
# at most once or twice in practice. Anything past five is far more likely a
# loop than a real chain, and either way must not be followed forever.
MAX_REDIRECT_HOPS = 5


def _bytes_getter(session: AsyncSession, timeout_sec: int):
    """Fetch image bytes, abandoning anything past max_bytes.

    Image URLs point at arbitrary author-chosen hosts. Buffering first and
    checking the size afterwards means one bad URL can exhaust memory on a
    machine that is also running Postgres.
    """

    async def get_bytes(url: str, max_bytes: int) -> tuple[int, bytes, str | None]:
        current = url
        for _ in range(MAX_REDIRECT_HOPS + 1):
            verdict = await classify_url(current)
            if verdict == "blocked":
                raise ImageBlocked(current)
            if verdict == "unresolved":
                # A resolver having a bad minute mid-chain is exactly as
                # retryable as one on the first hop — capture_image's generic
                # handler turns this into 'error', not 'dead'.
                raise RuntimeError(f"{current} did not resolve")

            response = await session.get(
                current,
                timeout=timeout_sec,
                stream=True,
                headers=IMAGE_FETCH_HEADERS,
                allow_redirects=False,
            )
            # Severing is the default for any response this function stops
            # reading before its natural end, and `completed` is the one flag
            # that turns it off — set only once the body has actually been
            # read in full, just before the return below. Getting this
            # backwards once already cost a finding (I9, closed by commit
            # e5461dc): aclose() alone never severs a curl_cffi stream, it
            # only awaits the background fetch finishing on its own —
            # response.quit_now.set() is the one thing that makes curl's
            # write callback abort early, and a default of "do nothing unless
            # told" meant every new exit path from this function — a
            # redirect, an oversize abort, and (found the round after those
            # two were fixed) a timeout cancelling this coroutine mid-read —
            # silently inherited a full drain instead. A `finally` runs for a
            # cancellation exactly as it does for any other exit, so
            # defaulting to sever there closes all of those the same way,
            # including ones not yet written.
            completed = False
            try:
                if response.status_code in _REDIRECT_STATUS_CODES:
                    # A 3xx is a header, not content — its body, if a
                    # misbehaving host sends one, must never be read.
                    location = response.headers.get("location")
                    if not location:
                        # A redirect status with nowhere to go is a host
                        # misbehaving, not evidence the image is gone or here.
                        raise RuntimeError(
                            f"{current} sent {response.status_code} with no Location"
                        )
                    current = urljoin(current, location)
                    continue

                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise ImageTooLarge(f"{current} declares {declared} bytes")
                chunks, total = [], 0
                async for chunk in response.aiter_content():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ImageTooLarge(f"{current} exceeded {max_bytes} bytes")
                    chunks.append(chunk)
                completed = True
                return response.status_code, b"".join(chunks), response.headers.get("content-type")
            finally:
                if not completed and response.quit_now:
                    response.quit_now.set()
                await response.aclose()
        raise RuntimeError(f"{url} exceeded {MAX_REDIRECT_HOPS} redirects")

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

    # Each in-flight capture takes a connection to record its outcome, so the pool
    # has to cover the concurrency plus the batch claim and a little headroom.
    pool = await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=settings.image_concurrency + 3
    )
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
def embed() -> None:
    """Embed queued articles until stopped."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_embed())


async def _embed() -> None:
    from babel.embed.client import EmbedClient
    from babel.embed.worker import run_embed_worker

    settings = Settings()
    notifier = Throttled(build_notifier(settings), settings.alert_repeat_sec)

    # No check_ip_leak and no gluetun namespace, unlike `run` and `images`.
    # This process never touches eRepublik: it talks to Postgres on the bridge
    # and to a LAN address, and routing either through the tunnel would buy
    # nothing and break both.
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await apply_migrations(conn, MIGRATIONS)

    client = EmbedClient(
        settings.embed_service_url,
        model=settings.embed_model,
        dim=repo.EMBED_DIM,
        timeout_sec=settings.embed_timeout_sec,
    )
    await run_embed_worker(pool, client, notifier, settings)


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
@click.option(
    "--sweep-only",
    is_flag=True,
    help="Re-collect queued articles instead of walking. Leaves the cursor alone.",
)
def run(start_id: int | None, no_poll: bool, no_backfill: bool, sweep_only: bool) -> None:
    """Run the crawler until stopped."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_run(start_id, no_poll, no_backfill, sweep_only))


async def _run(
    start_id: int | None, no_poll: bool, no_backfill: bool, sweep_only: bool = False
) -> None:
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
                asyncio.create_task(
                    _backfill_forever(pool, ingestor, start_id, fetch_rss, settings, sweep_only)
                )
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
    pool,
    ingestor: Ingestor,
    start_id: int | None,
    fetch_rss,
    settings: Settings,
    sweep_only: bool = False,
) -> None:
    async with pool.acquire() as conn:
        await run_backfill(
            conn,
            ingestor.ingest,
            start_id=start_id,
            # On an empty database the walk starts at the newest article the feed
            # knows about. `babel run` takes no --start-id, so without this a
            # fresh deployment crash-loops under `restart: unless-stopped`.
            discover_start=functools.partial(newest_article_id, fetch_rss),
            stop_at=1,
            # Hardcoded, and that is why --sweep-only exists: the sweep only
            # runs once the cursor descends past this, which on a live archive
            # is a month away. See run_backfill's docstring.
            sweep_only=sweep_only,
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


STUCK_HOSTS_SHOWN = 15


@main.command("requeue-images")
@click.option("--host", required=True, help="Image host to retry, e.g. i.imgur.com.")
def requeue_images(host: str) -> None:
    """Put one image host's abandoned images back in the queue.

    'dead' is permanent, which is correct when the host told us the truth and
    wrong when it did not. Use this after fixing the reason a host was
    misjudged — the running `images` service picks the rows up on its own.

    Pass a host you do not recognise (or `--host ?`) to list the hosts that
    currently have images stuck.
    """
    asyncio.run(_requeue_images(host))


async def _requeue_images(host: str) -> None:
    settings = Settings()
    conn = await asyncpg.connect(settings.database_url)
    try:
        changed = await repo.requeue_images_by_host(conn, host)
        if changed:
            click.echo(f"queued {changed:,} image(s) from {host} for another attempt")
            return
        click.echo(f"nothing stuck under {host!r} — the host must match exactly")
        stuck = await repo.stuck_image_hosts(conn, STUCK_HOSTS_SHOWN)
        if not stuck:
            click.echo("no host has images stuck at all")
            return
        click.echo("hosts with stuck images:")
        for name, count in stuck:
            click.echo(f"  {count:>9,}  {name}")
    finally:
        await conn.close()


async def _refetch(selection: list[int]) -> None:
    settings = Settings()
    conn = await asyncpg.connect(settings.database_url)
    try:
        changed = await repo.mark_stale(conn, selection)
    finally:
        await conn.close()
    click.echo(f"queued {changed:,} of {len(selection):,} selected article(s) for re-collection")


# One request per host at a time is enough — the report touches hundreds of
# unrelated third-party hosts, not one host hundreds of times, so a small cap
# keeps it quick without hammering anyone.
IMAGE_PROBE_CONCURRENCY = 10


def parse_host_list(raw: str) -> list[str]:
    """Comma-separated hosts to a deduplicated, lowercased list in first-seen order."""
    out: list[str] = []
    for part in raw.split(","):
        host = part.strip().lower()
        if host and host not in out:
            out.append(host)
    if not out:
        raise ValueError("no host given")
    return out


def _echo_stuck_image_hosts(stuck: list[tuple[str, int]]) -> None:
    if not stuck:
        click.echo("no host has images stuck at all")
        return
    click.echo("hosts with stuck images:")
    for name, count in stuck:
        click.echo(f"  {count:>9,}  {name}")


@main.command("image-hosts")
@click.option("--min-rows", default=500, help="Only hosts with at least this many queued images (default 500).")
@click.option("--no-probe", is_flag=True, help="Skip the live DNS/HTTP check; show counts only.")
def image_hosts(min_rows: int, no_probe: bool) -> None:
    """Report image hosts by how much of the queue they hold, with a live check.

    'pending' + 'error' is the depth the worker will still spend attempts on;
    lifetime 'ok' beside it separates a dead host from a slow one. The 'probe'
    column is one request made now: nxdomain, blocked, unreachable, gone (404/410),
    http-error (403/429/5xx — not written off), not-image (a landing page), alive.

    Read-only. Feed the hosts it flags to `babel kill-image-host`.
    """
    asyncio.run(_image_hosts(min_rows, no_probe))


async def _image_hosts(min_rows: int, no_probe: bool) -> None:
    settings = Settings()
    conn = await asyncpg.connect(settings.database_url)
    try:
        hosts = await repo.image_host_backlog(conn, min_rows)
        if not hosts:
            click.echo(f"no host has {min_rows:,}+ images queued")
            return
        samples = (
            {}
            if no_probe
            else {h.host: await repo.sample_url_for_host(conn, h.host) for h in hosts}
        )
    finally:
        await conn.close()

    probes: dict[str, str] = {}
    if not no_probe:
        semaphore = asyncio.Semaphore(IMAGE_PROBE_CONCURRENCY)
        async with AsyncSession(impersonate="chrome") as session:
            get_bytes = _bytes_getter(session, settings.request_timeout_sec)

            async def run_probe(host: str, url: str | None) -> None:
                if url is None:
                    probes[host] = "?"
                    return
                async with semaphore:
                    try:
                        probes[host] = await asyncio.wait_for(
                            probe_host(get_bytes, url, max_bytes=settings.max_image_bytes),
                            timeout=settings.image_timeout_sec,
                        )
                    except Exception:  # noqa: BLE001 — a probe that will not finish is unreachable
                        probes[host] = "unreachable"

            await asyncio.gather(*(run_probe(h.host, samples[h.host]) for h in hosts))

    click.echo(f"{'pending':>10} {'error':>8} {'ok':>8} {'dead':>8}  {'probe':<12} host")
    for h in hosts:
        click.echo(
            f"{h.pending:>10,} {h.error:>8,} {h.ok:>8,} {h.dead:>8,}  "
            f"{probes.get(h.host, ''):<12} {h.host}"
        )

    if no_probe:
        return
    writeoff = [h for h in hosts if h.ok == 0 and probes.get(h.host) in WRITE_OFF_VERDICTS]
    if not writeoff:
        click.echo("\nnothing here looks safe to write off automatically — check the probe column by hand")
        return
    rows = sum(h.pending + h.error for h in writeoff)
    click.echo(
        f"\n{len(writeoff)} host(s), {rows:,} queued image(s) look safe to write off "
        f"(probe says gone, never any ok):"
    )
    click.echo(f"  babel kill-image-host --host {','.join(h.host for h in writeoff)}")


@main.command("kill-image-host")
@click.option("--host", required=True, help="Host(s) to write off, comma-separated. Pass ? to list stuck hosts.")
@click.option("--force", is_flag=True, help="Write off even a host we have stored images from.")
def kill_image_host(host: str, force: bool) -> None:
    """Move a dead image host's queued rows to 'dead' so the worker stops retrying them.

    The exact reverse of `requeue-images`, and undone by it: a host written off by
    mistake comes back with `babel requeue-images --host <h>`. A host with any
    stored image is refused unless --force.
    """
    asyncio.run(_kill_image_hosts(host, force))


async def _kill_image_hosts(host: str, force: bool) -> None:
    settings = Settings()
    conn = await asyncpg.connect(settings.database_url)
    try:
        if host.strip() == "?":
            _echo_stuck_image_hosts(await repo.stuck_image_hosts(conn, STUCK_HOSTS_SHOWN))
            return
        selection = parse_host_list(host)
        total = 0
        for name in selection:
            try:
                changed = await repo.kill_image_host(conn, name, force=force)
            except repo.HostHasLiveImages as refused:
                click.echo(
                    f"refused {name}: {refused.ok_count:,} stored image(s) — "
                    f"add --force to write it off anyway"
                )
                continue
            total += changed
            if changed:
                click.echo(f"marked {changed:,} image(s) from {name} as dead")
            else:
                click.echo(f"nothing queued under {name!r} — the host must match exactly")
        if total == 0 and selection:
            _echo_stuck_image_hosts(await repo.stuck_image_hosts(conn, STUCK_HOSTS_SHOWN))
    finally:
        await conn.close()


@main.command()
def serve() -> None:
    """Run the public read-only web archive."""
    asyncio.run(_serve())


async def _serve() -> None:
    import uvicorn

    from babel.web.app import create_app, verify_schema, web_dsn

    settings = Settings()
    # Deliberately does not migrate the schema here. Both long-running crawler
    # commands do that at startup; this one connects as a SELECT-only role and
    # would crash-loop under restart: unless-stopped. Applying the schema is the
    # operator's step, documented in README.md.
    #
    # It does check that the step was taken, on one throwaway connection before
    # anything is served. An unapplied 005 is otherwise invisible in exactly the
    # wrong way: every page 503s "The database is not answering" while /healthz
    # and the compose healthcheck stay green.
    conn = await asyncpg.connect(web_dsn(settings))
    try:
        await verify_schema(conn)
    finally:
        await conn.close()
    app = create_app(settings)
    config = uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="info")  # noqa: S104
    await uvicorn.Server(config).serve()


@main.command()
@click.option("--article", "article_id", type=int, default=None, help="Article ID to suppress.")
@click.option("--image", "image_hex", default=None, help="Image sha256 (hex) to stop serving.")
def hide(article_id: int | None, image_hex: str | None) -> None:
    """Suppress an article or an image from the public site."""
    if (article_id is None) == (image_hex is None):
        raise click.UsageError("Pass exactly one of --article or --image.")
    asyncio.run(_hide(article_id, image_hex))


async def _hide(article_id: int | None, image_hex: str | None) -> None:
    settings = Settings()
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            if article_id is not None:
                changed = await hide_article(conn, article_id)
                click.echo(
                    f"article {article_id} hidden"
                    if changed
                    else f"article {article_id} was already hidden or is not in the archive"
                )
                return
            digest = parse_digest(image_hex or "")
            if digest is None:
                raise click.UsageError("--image must be 64 lowercase hex characters.")
            citing = await withhold_image(conn, digest)
            click.echo(f"image withheld; it was cited by {citing} article(s)")
    finally:
        await pool.close()
