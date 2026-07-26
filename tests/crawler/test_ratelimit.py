import asyncio

from babel.crawler.ratelimit import RateLimiter


async def test_first_acquire_is_immediate():
    limiter = RateLimiter(rate_per_sec=2.0)
    start = asyncio.get_running_loop().time()
    await limiter.acquire()
    assert asyncio.get_running_loop().time() - start < 0.2


async def test_spaces_calls_by_the_configured_interval():
    limiter = RateLimiter(rate_per_sec=20.0)  # 50ms apart
    start = asyncio.get_running_loop().time()
    for _ in range(4):
        await limiter.acquire()
    elapsed = asyncio.get_running_loop().time() - start
    assert elapsed >= 0.15  # three gaps of 50ms


async def test_concurrent_callers_are_serialised():
    limiter = RateLimiter(rate_per_sec=20.0)
    start = asyncio.get_running_loop().time()
    await asyncio.gather(*(limiter.acquire() for _ in range(4)))
    assert asyncio.get_running_loop().time() - start >= 0.15
