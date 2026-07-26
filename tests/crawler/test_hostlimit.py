import asyncio

from babel.crawler.hostlimit import HostLimiter


async def test_same_host_requests_do_not_overlap():
    limiter = HostLimiter()
    overlap = {"max": 0, "current": 0}

    async def work():
        async with limiter.slot("https://one.example/a.png"):
            overlap["current"] += 1
            overlap["max"] = max(overlap["max"], overlap["current"])
            await asyncio.sleep(0.01)
            overlap["current"] -= 1

    await asyncio.gather(*(work() for _ in range(5)))
    assert overlap["max"] == 1


async def test_different_hosts_run_concurrently():
    limiter = HostLimiter()
    started = []

    async def work(host: str):
        async with limiter.slot(f"https://{host}.example/a.png"):
            started.append(host)
            await asyncio.sleep(0.05)

    await asyncio.wait_for(
        asyncio.gather(work("a"), work("b"), work("c")), timeout=0.2
    )
    assert sorted(started) == ["a", "b", "c"]


async def test_a_raising_body_still_releases_the_slot():
    limiter = HostLimiter()
    try:
        async with limiter.slot("https://one.example/a.png"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    await asyncio.wait_for(limiter.slot("https://one.example/b.png").__aenter__(), timeout=0.1)


async def test_a_malformed_url_does_not_crash():
    limiter = HostLimiter()
    async with limiter.slot("not a url"):
        pass
