"""Command-line entrypoints."""

import asyncio
import json
import pathlib
import random

import click
from curl_cffi.requests import AsyncSession

from babel.config import Settings
from babel.vpn import check_ip_leak


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
