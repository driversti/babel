# Image Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move image capture out of the article ingest path into its own worker draining a durable queue, so a kill strands nothing, images stop sharing eRepublik's rate budget, and capture can be paused without stopping article collection.

**Architecture:** `save_article` already writes each image reference as a `pending` row in `article_images` — the queue exists and nothing reads it. A separate process, its own container in the same VPN namespace, drains it newest-article-first behind its own rate limit. When disk space runs low it sleeps and notifies rather than consuming the queue. `Ingestor` loses its image loop entirely.

**Tech Stack:** Unchanged — Python 3.12, asyncio, curl-cffi, asyncpg, click, pytest + testcontainers. One new dependency-free module for Telegram over the existing `aiohttp`.

## Global Constraints

- All outbound traffic egresses through the VPN. Both containers use `network_mode: "service:gluetun"` and have no network stack of their own.
- The repository is **public**. No credential, provider name, hostname, IP address or country in any committed file. `BOT_TOKEN` and `CHAT_ID` live in `.env`, which is gitignored.
- eRepublik keeps its own 1 req/s limiter. Image fetches use a **separate** global limit (`IMAGE_REQUESTS_PER_SECOND`, default 5.0) **plus at most one concurrent request per hostname**.
- The queue is drained **newest article first** — `ORDER BY article_id DESC`. Recent images are the ones still alive: 66% of 2021–2026 resolve against 7% of 2007–2014.
- Below `MIN_FREE_BYTES` the worker **sleeps and leaves rows `pending`**. It must never mark them. A marked row has left the queue and nothing would go back for it.
- `article_images.status` is exactly one of `pending`, `ok`, `dead`, `error`. `skipped_no_space` no longer exists.
- Image retries are bounded by `MAX_IMAGE_ATTEMPTS = 5`, counted in `article_images.attempts`.
- Telegram fires for exactly two events: disk below the floor, and an egress IP in the home country. Nothing routine.
- Python 3.12, ruff `line-length = 110`, pytest `asyncio_mode = "auto"`.

---

## File Structure

```
babel/
├── migrations/002_image_queue.sql        attempts column, status backfill
├── src/babel/
│   ├── config.py                         MODIFY — image rate, telegram, sleep intervals
│   ├── notify.py                         NEW — Telegram, with a null implementation
│   ├── cli.py                            MODIFY — `images` command, streaming getter, egress alert
│   ├── crawler/
│   │   ├── hostlimit.py                  NEW — one concurrent request per hostname
│   │   ├── images.py                     MODIFY — drop the space check, add ImageTooLarge
│   │   ├── imageworker.py                NEW — the drain loop
│   │   └── ingest.py                     MODIFY — delete the image loop
│   └── db/repo.py                        MODIFY — queue claim and result recording
├── docker-compose.yml                    MODIFY — second service
└── tests/…                               matching test modules
```

Responsibility split: `imageworker.py` owns the loop and the disk decision; `images.py` stays a pure-ish store that knows nothing about queues; `repo.py` keeps every SQL statement; `notify.py` knows nothing about why it is being called.

---

### Task 1: Queue schema and repository access

**Files:**
- Create: `babel/migrations/002_image_queue.sql`
- Modify: `babel/src/babel/db/repo.py`
- Modify: `babel/tests/db/test_repo.py`

**Interfaces:**
- Produces: `MAX_IMAGE_ATTEMPTS = 5`; `PendingImage(article_id: int, position: int, source_url: str, attempts: int)`; `claim_pending_images(conn, limit: int) -> list[PendingImage]`; `record_image_result(conn, article_id: int, position: int, status: str, sha256: bytes | None = None) -> None`.

- [ ] **Step 1: Write the migration**

`babel/migrations/002_image_queue.sql`:

```sql
ALTER TABLE article_images ADD COLUMN attempts smallint NOT NULL DEFAULT 0;

-- The same-pass design marked rows this way when the disk was low, which took
-- them out of the queue for good. The worker now sleeps instead, so any such
-- row belongs back in the queue.
UPDATE article_images SET status = 'pending' WHERE status = 'skipped_no_space';

-- The drain order: newest article first, because that is where the images that
-- still resolve are. Partial, so the index stays small as rows leave 'pending'.
CREATE INDEX article_images_queue_idx
    ON article_images (article_id DESC, position)
    WHERE status = 'pending';
```

- [ ] **Step 2: Write the failing tests**

Append to `babel/tests/db/test_repo.py`:

```python
async def test_claim_returns_newest_articles_first(pg):
    for article_id in (100, 300, 200):
        await pg.execute(
            "INSERT INTO articles (id, title, body, published_at) VALUES ($1, 't', 'b', now())",
            article_id,
        )
        await pg.execute(
            """INSERT INTO article_images (article_id, position, source_url, status)
               VALUES ($1, 0, 'https://x.example/a.png', 'pending')""",
            article_id,
        )
    claimed = await repo.claim_pending_images(pg, limit=10)
    assert [c.article_id for c in claimed] == [300, 200, 100]


async def test_claim_only_returns_pending(pg):
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    for position, status in enumerate(["pending", "ok", "dead", "error"]):
        await pg.execute(
            """INSERT INTO article_images (article_id, position, source_url, status)
               VALUES (1, $1, 'https://x.example/a.png', $2)""",
            position, status,
        )
    claimed = await repo.claim_pending_images(pg, limit=10)
    assert [c.position for c in claimed] == [0]


async def test_claim_skips_rows_at_the_attempt_ceiling(pg):
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status, attempts)
           VALUES (1, 0, 'https://x.example/a.png', 'pending', $1)""",
        repo.MAX_IMAGE_ATTEMPTS,
    )
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status, attempts)
           VALUES (1, 1, 'https://x.example/b.png', 'pending', $1)""",
        repo.MAX_IMAGE_ATTEMPTS - 1,
    )
    claimed = await repo.claim_pending_images(pg, limit=10)
    assert [c.position for c in claimed] == [1]


async def test_claim_respects_the_limit(pg):
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    for position in range(5):
        await pg.execute(
            """INSERT INTO article_images (article_id, position, source_url, status)
               VALUES (1, $1, 'https://x.example/a.png', 'pending')""",
            position,
        )
    assert len(await repo.claim_pending_images(pg, limit=2)) == 2


async def test_record_result_sets_status_and_bumps_attempts(pg):
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status)
           VALUES (1, 0, 'https://x.example/a.png', 'pending')"""
    )
    await repo.record_image_result(pg, 1, 0, "error")
    row = await pg.fetchrow("SELECT status, attempts FROM article_images WHERE article_id = 1")
    assert row["status"] == "error"
    assert row["attempts"] == 1

    await repo.record_image_result(pg, 1, 0, "error")
    row = await pg.fetchrow("SELECT status, attempts FROM article_images WHERE article_id = 1")
    assert row["attempts"] == 2


async def test_an_errored_row_is_reclaimed_below_the_ceiling(pg):
    # 'error' means we could not tell whether the image is there. Unlike 'dead',
    # it must come back around until the attempt ceiling is reached.
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status)
           VALUES (1, 0, 'https://x.example/a.png', 'pending')"""
    )
    await repo.record_image_result(pg, 1, 0, "error")
    assert [c.position for c in await repo.claim_pending_images(pg, limit=10)] == [0]


async def test_a_dead_row_is_never_reclaimed(pg):
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status)
           VALUES (1, 0, 'https://x.example/a.png', 'pending')"""
    )
    await repo.record_image_result(pg, 1, 0, "dead")
    assert await repo.claim_pending_images(pg, limit=10) == []
```

- [ ] **Step 3: Run and watch them fail**

Run: `uv run pytest tests/db/test_repo.py -v`
Expected: FAIL — `AttributeError: module 'babel.db.repo' has no attribute 'claim_pending_images'`

- [ ] **Step 4: Implement**

Add to `babel/src/babel/db/repo.py`:

```python
from dataclasses import dataclass

# An image host that answers with a timeout rather than a 404 would otherwise sit
# in the queue forever. 'dead' is final and never recounted; only 'error' retries.
MAX_IMAGE_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class PendingImage:
    article_id: int
    position: int
    source_url: str
    attempts: int


async def claim_pending_images(conn: asyncpg.Connection, limit: int) -> list[PendingImage]:
    """Next images to fetch, newest article first.

    Ordering is not cosmetic: 66% of 2021-2026 images still resolve against 7%
    of 2007-2014, so draining oldest-first would spend the crawl on links that
    are already gone while the recoverable ones rot.

    An 'error' row returns to 'pending' via record_image_result only implicitly —
    it is re-offered here because its status is not terminal and its attempts are
    below the ceiling.
    """
    rows = await conn.fetch(
        """
        SELECT article_id, position, source_url, attempts
        FROM article_images
        WHERE status IN ('pending', 'error') AND attempts < $1
        ORDER BY article_id DESC, position
        LIMIT $2
        """,
        MAX_IMAGE_ATTEMPTS, limit,
    )
    return [PendingImage(**dict(r)) for r in rows]


async def record_image_result(
    conn: asyncpg.Connection,
    article_id: int,
    position: int,
    status: str,
    sha256: bytes | None = None,
) -> None:
    """Set an image slot's outcome and count the attempt."""
    await conn.execute(
        """UPDATE article_images
           SET status = $3, sha256 = $4, attempts = attempts + 1, checked_at = now()
           WHERE article_id = $1 AND position = $2""",
        article_id, position, status, sha256,
    )
```

- [ ] **Step 5: Run and watch them pass**

Run: `uv run pytest tests/db/test_repo.py -v`
Expected: all pass, including the 7 new ones

- [ ] **Step 6: Commit**

```bash
git add migrations/002_image_queue.sql src/babel/db/repo.py tests/db/test_repo.py
git commit -m "Turn article_images into a drainable queue"
```

---

### Task 2: Telegram notifier

**Files:**
- Create: `babel/src/babel/notify.py`
- Create: `babel/tests/test_notify.py`
- Modify: `babel/src/babel/config.py`

**Interfaces:**
- Produces: `Notifier` (protocol with `async def send(text: str) -> None`); `TelegramNotifier(token: str, chat_id: str)`; `NullNotifier()`; `build_notifier(settings) -> Notifier`; `Throttled(inner: Notifier, interval_sec: float)` with `async def send_once(key: str, text: str) -> None`.

- [ ] **Step 1: Write the failing tests**

`babel/tests/test_notify.py`:

```python
from babel.config import Settings
from babel.notify import NullNotifier, TelegramNotifier, Throttled, build_notifier


class Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


async def test_null_notifier_accepts_everything_silently():
    await NullNotifier().send("anything")  # must not raise


def test_build_returns_null_when_unconfigured():
    assert isinstance(build_notifier(Settings(_env_file=None)), NullNotifier)


def test_build_returns_telegram_when_configured():
    settings = Settings(_env_file=None, bot_token="t", chat_id="c")
    assert isinstance(build_notifier(settings), TelegramNotifier)


async def test_throttle_suppresses_a_repeat_within_the_interval():
    recorder = Recorder()
    throttled = Throttled(recorder, interval_sec=3600, now=lambda: 0.0)
    await throttled.send_once("disk", "disk is full")
    await throttled.send_once("disk", "disk is full")
    assert recorder.sent == ["disk is full"]


async def test_throttle_allows_a_repeat_after_the_interval():
    recorder = Recorder()
    clock = {"t": 0.0}
    throttled = Throttled(recorder, interval_sec=100, now=lambda: clock["t"])
    await throttled.send_once("disk", "first")
    clock["t"] = 101.0
    await throttled.send_once("disk", "second")
    assert recorder.sent == ["first", "second"]


async def test_throttle_keys_are_independent():
    recorder = Recorder()
    throttled = Throttled(recorder, interval_sec=3600, now=lambda: 0.0)
    await throttled.send_once("disk", "disk")
    await throttled.send_once("leak", "leak")
    assert recorder.sent == ["disk", "leak"]


async def test_a_failing_transport_never_propagates():
    class Broken:
        async def send(self, text: str) -> None:
            raise RuntimeError("telegram is down")

    throttled = Throttled(Broken(), interval_sec=1, now=lambda: 0.0)
    await throttled.send_once("k", "text")  # must not raise
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_notify.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'babel.notify'`

- [ ] **Step 3: Add the settings**

In `babel/src/babel/config.py`, add to `Settings`:

```python
    bot_token: str | None = Field(default=None, description="Telegram bot token. Never committed.")
    chat_id: str | None = Field(default=None, description="Telegram chat id. Never committed.")
    alert_repeat_sec: float = Field(default=3600.0, gt=0)
```

- [ ] **Step 4: Write notify.py**

`babel/src/babel/notify.py`:

```python
"""Alerts for the two events a log line will not reach a human in time for.

Deliberately narrow. A notifier that also reports routine progress is one the
operator learns to swipe away, and then the disk fills unnoticed anyway.

Nothing here may raise. A crawl running for weeks must not die because Telegram
had a bad minute; the alert is a courtesy, the crawl is the job.
"""

import logging
import time
from typing import Protocol

import aiohttp

log = logging.getLogger("babel.notify")


class Notifier(Protocol):
    async def send(self, text: str) -> None: ...


class NullNotifier:
    """Used when no credentials are configured. Logs and moves on."""

    async def send(self, text: str) -> None:
        log.info("notification (no telegram configured): %s", text)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id

    async def send(self, text: str) -> None:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.post(
            self._url, json={"chat_id": self._chat_id, "text": text}
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"telegram returned {resp.status}: {await resp.text()}")


class Throttled:
    """Wraps a notifier so a persistent condition alerts once, not every cycle.

    The disk-full check runs on a loop; without this the operator gets a message
    every few minutes until they act, which is indistinguishable from spam.
    """

    def __init__(self, inner: Notifier, interval_sec: float, now=time.monotonic) -> None:
        self._inner = inner
        self._interval = interval_sec
        self._now = now
        self._last: dict[str, float] = {}

    async def send_once(self, key: str, text: str) -> None:
        now = self._now()
        last = self._last.get(key)
        if last is not None and now - last < self._interval:
            return
        self._last[key] = now
        try:
            await self._inner.send(text)
        except Exception:  # noqa: BLE001 — an alert failing must never stop the crawl
            log.exception("could not deliver notification: %s", text)


def build_notifier(settings) -> Notifier:
    if settings.bot_token and settings.chat_id:
        return TelegramNotifier(settings.bot_token, settings.chat_id)
    return NullNotifier()
```

- [ ] **Step 5: Run and watch them pass**

Run: `uv run pytest tests/test_notify.py -v`
Expected: 7 passed

- [ ] **Step 6: Add the placeholders to `.env.example`**

```bash
# Telegram alerts. Two events only: disk full, and an egress IP in the home
# country. Leave blank to log instead of notifying.
BOT_TOKEN=
CHAT_ID=
```

- [ ] **Step 7: Commit**

```bash
git add src/babel/notify.py src/babel/config.py tests/test_notify.py .env.example
git commit -m "Add a deliberately narrow Telegram notifier"
```

---

### Task 3: Per-host politeness and a streaming size cap

**Files:**
- Create: `babel/src/babel/crawler/hostlimit.py`
- Create: `babel/tests/crawler/test_hostlimit.py`
- Modify: `babel/src/babel/crawler/images.py`
- Modify: `babel/tests/crawler/test_images.py`

**Interfaces:**
- Produces: `HostLimiter()` with `async def slot(url: str)` returning an async context manager; `ImageTooLarge(Exception)`; `capture_image` **loses its `min_free_bytes` parameter** and no longer returns `skipped_no_space`.

- [ ] **Step 1: Write the failing host-limiter tests**

`babel/tests/crawler/test_hostlimit.py`:

```python
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
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/crawler/test_hostlimit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'babel.crawler.hostlimit'`

- [ ] **Step 3: Write hostlimit.py**

```python
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
```

- [ ] **Step 4: Run and watch them pass**

Run: `uv run pytest tests/crawler/test_hostlimit.py -v`
Expected: 4 passed

- [ ] **Step 5: Rework `capture_image`**

In `babel/src/babel/crawler/images.py`:

- Delete the `min_free_bytes` parameter and the `have_space` call from `capture_image`. The worker owns that decision now, because it must sleep rather than mark rows, and only the worker can sleep.
- Keep `have_space` itself — the worker imports it.
- Remove `"skipped_no_space"` from `ImageOutcome`'s documented statuses.
- Add `class ImageTooLarge(Exception)` and treat it as `error`, since a getter that aborts a huge download raises rather than returning bytes.

The new signature and body:

```python
class ImageTooLarge(Exception):
    """Raised by a getter that aborted a download past max_bytes."""


async def capture_image(
    get_bytes: BytesGetter,
    root: pathlib.Path,
    source_url: str,
    *,
    max_bytes: int,
) -> ImageOutcome:
    """Fetch and store one image.

    Free space is deliberately not checked here. The worker checks it, because
    the correct response to a full disk is to sleep with the row still queued,
    and only the loop can do that.
    """
    try:
        status_code, data, mime = await get_bytes(normalise_url(source_url), max_bytes)
    except ImageTooLarge:
        return ImageOutcome(status="error")
    except Exception:  # noqa: BLE001 — could not determine, so not 'dead'
        return ImageOutcome(status="error")

    if status_code != 200 or not data:
        return ImageOutcome(status="dead")
    if mime is None or not mime.startswith("image/"):
        # Parked domains and "file removed" pages answer 200 with HTML.
        return ImageOutcome(status="dead")

    digest = store_bytes(root, data)
    return ImageOutcome(status="ok", digest=digest, mime=mime, size=len(data))
```

`BytesGetter` becomes `Callable[[str, int], Awaitable[tuple[int, bytes, str | None]]]` — the second argument is `max_bytes`.

- [ ] **Step 6: Update the image tests**

In `babel/tests/crawler/test_images.py`: every `capture_image` call loses `min_free_bytes=`, every fake getter takes `(url, max_bytes)`, and `test_capture_skips_when_below_the_free_space_floor` is **deleted** — that behaviour moved to the worker and is tested there. Replace `test_capture_refuses_an_oversized_image` with one whose getter raises `ImageTooLarge` and asserts the outcome is `error`. Leave the `store_bytes`, `image_path`, `have_space` and `normalise_url` tests untouched.

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest -v && uv run ruff check src tests`
Expected: everything passes. `tests/crawler/test_ingest.py` will fail if you have not yet touched `ingest.py` — that is Task 5. If it does, note it and continue; do not fix it here.

- [ ] **Step 8: Commit**

```bash
git add src/babel/crawler/hostlimit.py src/babel/crawler/images.py tests/crawler/test_hostlimit.py tests/crawler/test_images.py
git commit -m "Add per-host politeness and move the disk decision out of capture_image"
```

---

### Task 4: The image worker

**Files:**
- Create: `babel/src/babel/crawler/imageworker.py`
- Create: `babel/tests/crawler/test_imageworker.py`
- Modify: `babel/src/babel/config.py`

**Interfaces:**
- Consumes: `repo.claim_pending_images`, `repo.record_image_result`, `repo.save_image_blob`, `capture_image`, `have_space`, `HostLimiter`, `RateLimiter`, `Throttled`.
- Produces: `run_image_worker(pool, get_bytes, limiter, host_limiter, notifier, settings, *, sleep=asyncio.sleep, max_cycles=None) -> None`.

`max_cycles` exists so tests can run a bounded number of iterations; production passes `None` for an endless loop.

- [ ] **Step 0: Move `FakePool` into `conftest.py`**

`FakePool` currently lives in `tests/crawler/test_ingest.py`. This task's tests need it too, and importing across test modules is fragile. Move the class verbatim into `babel/tests/conftest.py`, expose it as a fixture:

```python
@pytest.fixture
def fake_pool():
    """asyncpg.Pool.acquire() is an async context manager; tests hand it one connection."""

    def build(conn):
        return FakePool(conn)

    return build
```

Update `tests/crawler/test_ingest.py` to use the fixture instead of its local class, and confirm its tests still pass before continuing.

- [ ] **Step 1: Add the settings**

In `babel/src/babel/config.py`:

```python
    image_requests_per_second: float = Field(default=5.0, gt=0, le=100)
    image_batch_size: int = Field(default=50, ge=1)
    image_idle_sleep_sec: float = Field(default=60.0, gt=0)
    image_disk_full_sleep_sec: float = Field(default=300.0, gt=0)
```

- [ ] **Step 2: Write the failing tests**

`babel/tests/crawler/test_imageworker.py`:

```python
from babel.config import Settings
from babel.crawler.hostlimit import HostLimiter
from babel.crawler.imageworker import run_image_worker
from babel.crawler.ratelimit import RateLimiter
from babel.db import repo
from babel.notify import Throttled


class Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


async def seed(pg, article_id: int, urls: list[str]) -> None:
    await pg.execute(
        "INSERT INTO articles (id, title, body, published_at) VALUES ($1,'t','b',now())",
        article_id,
    )
    for position, url in enumerate(urls):
        await pg.execute(
            """INSERT INTO article_images (article_id, position, source_url, status)
               VALUES ($1, $2, $3, 'pending')""",
            article_id, position, url,
        )


def settings(tmp_path, **kw):
    base = dict(_env_file=None, image_root=str(tmp_path), min_free_bytes=0)
    return Settings(**{**base, **kw})


async def noop_sleep(_seconds):
    return None


async def test_drains_the_queue_and_stores_bytes(pg, tmp_path, fake_pool):
    await seed(pg, 1, ["https://a.example/1.png", "https://a.example/2.png"])

    async def get_bytes(url, max_bytes):
        return 200, b"\x89PNG " + url.encode(), "image/png"

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0), settings(tmp_path),
        sleep=noop_sleep, max_cycles=2,
    )
    rows = await pg.fetch("SELECT status FROM article_images ORDER BY position")
    assert [r["status"] for r in rows] == ["ok", "ok"]
    assert await pg.fetchval("SELECT count(*) FROM images") == 2


async def test_drains_newest_article_first(pg, tmp_path, fake_pool):
    await seed(pg, 10, ["https://a.example/old.png"])
    await seed(pg, 20, ["https://a.example/new.png"])
    order: list[str] = []

    async def get_bytes(url, max_bytes):
        order.append(url)
        return 200, b"\x89PNG", "image/png"

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0), settings(tmp_path),
        sleep=noop_sleep, max_cycles=1,
    )
    assert order[0].endswith("new.png")


async def test_a_dead_host_marks_dead_and_does_not_stop_the_batch(pg, tmp_path, fake_pool):
    await seed(pg, 1, ["https://a.example/gone.png", "https://b.example/fine.png"])

    async def get_bytes(url, max_bytes):
        if "gone" in url:
            return 404, b"", None
        return 200, b"\x89PNG", "image/png"

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0), settings(tmp_path),
        sleep=noop_sleep, max_cycles=2,
    )
    rows = await pg.fetch("SELECT status FROM article_images ORDER BY position")
    assert [r["status"] for r in rows] == ["dead", "ok"]


async def test_a_full_disk_leaves_the_queue_untouched_and_notifies(pg, tmp_path, fake_pool):
    await seed(pg, 1, ["https://a.example/1.png"])
    recorder = Recorder()

    async def get_bytes(url, max_bytes):
        raise AssertionError("must not fetch below the free-space floor")

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(recorder, 3600, now=lambda: 0.0),
        settings(tmp_path, min_free_bytes=10**18),
        sleep=noop_sleep, max_cycles=3,
    )
    row = await pg.fetchrow("SELECT status, attempts FROM article_images WHERE article_id = 1")
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert len(recorder.sent) == 1  # throttled: one alert, not one per cycle


async def test_an_errored_image_is_retried_until_the_ceiling(pg, tmp_path, fake_pool):
    await seed(pg, 1, ["https://a.example/flaky.png"])
    calls = {"n": 0}

    async def get_bytes(url, max_bytes):
        calls["n"] += 1
        raise TimeoutError("slow")

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0), settings(tmp_path),
        sleep=noop_sleep, max_cycles=10,
    )
    row = await pg.fetchrow("SELECT status, attempts FROM article_images WHERE article_id = 1")
    assert row["status"] == "error"
    assert row["attempts"] == repo.MAX_IMAGE_ATTEMPTS
    assert calls["n"] == repo.MAX_IMAGE_ATTEMPTS


async def test_an_empty_queue_sleeps_rather_than_spinning(pg, tmp_path, fake_pool):
    slept: list[float] = []

    async def record_sleep(seconds):
        slept.append(seconds)

    async def get_bytes(url, max_bytes):
        raise AssertionError("nothing to fetch")

    await run_image_worker(
        fake_pool(pg), get_bytes, RateLimiter(1000), HostLimiter(),
        Throttled(Recorder(), 1, now=lambda: 0.0), settings(tmp_path),
        sleep=record_sleep, max_cycles=2,
    )
    assert slept == [60.0, 60.0]
```

- [ ] **Step 3: Run and watch them fail**

Run: `uv run pytest tests/crawler/test_imageworker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'babel.crawler.imageworker'`

- [ ] **Step 4: Write imageworker.py**

```python
"""Drains the image queue.

The queue is `article_images` rows at status 'pending' or 'error'. `save_article`
fills it; nothing else does. Separating this from article ingest is what makes a
kill mid-download harmless — an unfinished row is simply still queued.

Newest article first. 66% of 2021-2026 images still resolve against 7% of
2007-2014, so draining oldest-first would spend the crawl on links already gone.
"""

import asyncio
import logging

from babel.crawler.images import capture_image, have_space
from babel.db import repo

log = logging.getLogger("babel.images")

DISK_ALERT_KEY = "image-disk-full"


async def run_image_worker(
    pool,
    get_bytes,
    limiter,
    host_limiter,
    notifier,
    settings,
    *,
    sleep=asyncio.sleep,
    max_cycles: int | None = None,
) -> None:
    """Fetch queued images until stopped. `max_cycles` bounds the loop for tests."""
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1

        if not have_space(settings.image_root, settings.min_free_bytes):
            # Deliberately no row is touched. Marking one would take it out of the
            # queue, and freeing disk space would never bring it back.
            log.warning("below the free-space floor — pausing image capture")
            await notifier.send_once(
                DISK_ALERT_KEY,
                f"babel: image capture paused, less than {settings.min_free_bytes} bytes free",
            )
            await sleep(settings.image_disk_full_sleep_sec)
            continue

        async with pool.acquire() as conn:
            batch = await repo.claim_pending_images(conn, settings.image_batch_size)

        if not batch:
            await sleep(settings.image_idle_sleep_sec)
            continue

        for item in batch:
            await _capture_one(pool, get_bytes, limiter, host_limiter, settings, item)


async def _capture_one(pool, get_bytes, limiter, host_limiter, settings, item) -> None:
    await limiter.acquire()
    try:
        async with host_limiter.slot(item.source_url):
            outcome = await capture_image(
                get_bytes,
                settings.image_root,
                item.source_url,
                max_bytes=settings.max_image_bytes,
            )
    except Exception:  # noqa: BLE001 — a filesystem or transport fault is not fatal
        log.exception("capturing %s failed", item.source_url)
        outcome = None

    async with pool.acquire() as conn:
        if outcome is None:
            await repo.record_image_result(conn, item.article_id, item.position, "error")
            return
        if outcome.digest is not None:
            await repo.save_image_blob(conn, outcome.digest, outcome.mime, outcome.size)
        await repo.record_image_result(
            conn, item.article_id, item.position, outcome.status, outcome.digest
        )
```

- [ ] **Step 5: Run and watch them pass**

Run: `uv run pytest tests/crawler/test_imageworker.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add src/babel/crawler/imageworker.py src/babel/config.py tests/crawler/test_imageworker.py
git commit -m "Add the image worker that drains the queue newest-first"
```

---

### Task 5: Wire it up and cut the old path

**Files:**
- Modify: `babel/src/babel/crawler/ingest.py`
- Modify: `babel/tests/crawler/test_ingest.py`
- Modify: `babel/src/babel/cli.py`
- Modify: `babel/docker-compose.yml`
- Modify: `babel/README.md`, `babel/CLAUDE.md`

**Interfaces:**
- Produces: `babel images` CLI command; a `bytes_getter` that streams and raises `ImageTooLarge`; a second compose service.

- [ ] **Step 1: Delete the image loop from `Ingestor`**

Remove everything after `record_fetch(conn, article_id, "ok")` — the `for ref in article.images:` block and its long explanatory comment. The comment described a hazard that no longer exists. `Ingestor` no longer needs `get_bytes`; drop that constructor parameter and the `capture_image` import.

`ingest` still returns `"ok"`. `save_article` continues to write the `pending` rows, which are now the queue.

- [ ] **Step 2: Update the ingest tests**

In `babel/tests/crawler/test_ingest.py`:
- `Ingestor(...)` calls lose the `get_bytes` argument.
- `test_dead_images_do_not_fail_the_article` and `test_text_is_saved_even_when_the_disk_is_full` are **deleted** — both assert behaviour that now lives in the worker and is tested there.
- `test_ingests_article_comments_and_images` keeps its article and comment assertions, but its image assertion inverts: after ingest every `article_images` row **is** `pending`, because handing them to the queue is now the whole job. Rename it to say so.
- Keep `FakePool` exactly as it is — Task 4's tests import it.

- [ ] **Step 3: Add the streaming getter and the `images` command**

In `babel/src/babel/cli.py`, import `ImageTooLarge`, `HostLimiter`, `run_image_worker`, `Throttled` and
`build_notifier`, then replace the existing `_bytes_getter` with one that streams and aborts:

```python
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
```

Then add the command:

```python
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
```

- [ ] **Step 4: Make the egress watchdog alert**

`_watch_egress` gains a `notifier` parameter. On `IpLeak`, call `await notifier.send_once("egress-leak", ...)` before re-raising. The spec has said "log, alert and exit" since the first commit and only the log and the exit were ever built. Pass the notifier from `_run` too, not just `_images`.

- [ ] **Step 5: Add the compose service**

In `babel/docker-compose.yml`, after `crawler`:

```yaml
  images:
    build: .
    container_name: babel-images
    command: ["babel", "images"]
    env_file: .env
    volumes:
      - ${IMAGE_ROOT_HOST:-./data/images}:/data/images
    # Same tunnel as the crawler. Image hosts are not eRepublik, but going
    # direct would expose the operator's address to save nothing.
    network_mode: "service:gluetun"
    depends_on:
      gluetun: {condition: service_healthy}
      db: {condition: service_healthy}
    restart: unless-stopped
```

- [ ] **Step 6: Run everything**

Run: `uv run pytest -v && uv run ruff check src tests`
Expected: all pass. Then `docker compose build` and `docker compose config --quiet`.

- [ ] **Step 7: Update the docs**

`README.md` and `CLAUDE.md`: document `babel images` as a second long-running service, note that image capture is queue-driven and can be stopped independently, and add `BOT_TOKEN`/`CHAT_ID` to the configuration list. Keep the existing note that the crawler has never been run against the live site.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "Move image capture out of ingest and into its own service"
```

---

## What this does not fix

Two findings from the final review are about articles, not images, and remain open:

- **C1** — no path exists to re-visit an article recorded `ok`, and the backfill cursor only walks downward, so an ID that errored in a completed batch is lost. Needs an error-sweep phase plus a `refetch` command.
- **I4** — once the backfill reaches article 1 the service exits cleanly and the container crash-loops under `restart: unless-stopped`. Fixing C1's sweep loop fixes this too.

Both should be the next plan.
