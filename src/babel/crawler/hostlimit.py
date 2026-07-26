"""At most one in-flight request per hostname.

Articles cite a CDN and someone's personal server side by side. The global rate
limit is sized for the fleet; without this, an article with six images from one
small host hands it the whole budget at once.

Locks are created on demand and never evicted. The host set is bounded by the
number of image hosts eRepublik authors have ever used — thousands, not
millions — so the dictionary is not a leak worth managing.
"""

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from urllib.parse import urlparse


class HostLimiter:
    def __init__(self) -> None:
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @asynccontextmanager
    async def slot(self, url: str):
        host = urlparse(url).netloc or "<unparseable>"
        async with self._locks[host]:
            yield
