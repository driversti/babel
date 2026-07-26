"""One global pacing gate.

Both producers share a single limiter, so the configured rate is the rate the
site actually sees, not the rate per worker. A lock rather than a token bucket:
bursting is exactly what we do not want from a month-long crawl.
"""

import asyncio


class RateLimiter:
    def __init__(self, rate_per_sec: float) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self._interval = 1.0 / rate_per_sec
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
            self._next_at = now + self._interval
