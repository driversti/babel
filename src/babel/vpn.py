"""Egress IP verification.

The hard guarantee is architectural: the crawler shares the VPN container's
network namespace, so a dead tunnel means no network at all. This module is the
second line — it catches a tunnel that is up but exiting somewhere unexpected.

ipinfo.io rate-limits unauthenticated traffic aggressively and Gluetun's DNS has
been observed to block some lookup services outright, so providers are tried in
order rather than trusted individually.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiohttp

IP_INFO_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("https://ifconfig.co/json", "country_iso"),
    ("https://ipinfo.io/json", "country"),
)


@dataclass(frozen=True, slots=True)
class IpInfo:
    ip: str
    country: str  # two-letter ISO


class IpLeak(RuntimeError):  # noqa: N818 — name is a fixed interface, not open to renaming
    """Egress IP is from the operator's own country."""


def is_leaking(info: IpInfo, home_country: str) -> bool:
    return info.country.upper() == home_country.upper()


async def get_ip_info(timeout_sec: int = 10) -> IpInfo:
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    last_err: Exception | None = None
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url, country_key in IP_INFO_PROVIDERS:
            try:
                async with session.get(url) as resp:
                    data = await resp.json(content_type=None)
                ip, country = data.get("ip"), data.get(country_key)
                if ip and country:
                    return IpInfo(ip=ip, country=country)
                last_err = RuntimeError(f"{url} returned no ip/country: {data}")
            except Exception as e:  # noqa: BLE001 — any failure means try the next provider
                last_err = e
    raise RuntimeError(f"all ip-info providers failed: {last_err}")


async def check_ip_leak(
    *,
    home_country: str,
    lookup: Callable[[], Awaitable[IpInfo]] = get_ip_info,
    retries: int = 5,
    backoff_sec: float = 2.0,
) -> IpInfo:
    """Return egress IP info, or raise IpLeak if it is the home country.

    IpLeak is never retried: a confirmed leak is a reason to stop, not to wait.
    """
    last_err: Exception | None = None
    for _ in range(retries):
        try:
            info = await lookup()
        except Exception as e:  # noqa: BLE001
            last_err = e
            await asyncio.sleep(backoff_sec)
            continue
        if is_leaking(info, home_country):
            raise IpLeak(f"egress IP {info.ip} is in {home_country} — refusing to crawl")
        return info
    raise RuntimeError(f"could not determine egress IP after {retries} attempts: {last_err}")
