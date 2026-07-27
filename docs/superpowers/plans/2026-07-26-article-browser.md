# Public Article Browser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a public, server-rendered browser over the babel archive — every collected article in a filterable, date-sorted, keyset-paginated list, plus an article page carrying the stored text, the comment thread and the rescued images.

**Architecture:** A fifth compose service, `web`, built from the existing image and running `babel serve`. FastAPI serves Jinja2 templates; there is no JavaScript and no front-end build. All read-path SQL lives in a new `src/babel/db/browse.py` alongside the existing `repo.py`, so query text stays testable by `EXPLAIN`. The service runs outside gluetun's network namespace on the Docker bridge, connects to Postgres as a SELECT-only role, and is exposed through the operator's existing Cloudflare Tunnel.

**Tech Stack:** Python 3.12+, FastAPI, Jinja2, uvicorn, asyncpg, Postgres 17, pytest + testcontainers, httpx (ASGI transport), ruff.

**Spec:** [docs/superpowers/specs/2026-07-26-article-browser-design.md](../specs/2026-07-26-article-browser-design.md). Read it before Task 1. It records *why* several of these choices are the way they are, and three of them look wrong until you read the measurement.

## Global Constraints

- **Python ≥ 3.12**, line length 110, ruff rules `E,F,W,I,N,B,UP,ASYNC,RET,SIM`. Run `uv run ruff check src tests` before every commit.
- **The full suite must pass before pushing.** The deploy host tracks `main`, so `uv run pytest` is not a formality. Docker must be running (testcontainers `postgres:17`).
- **No SQL outside `src/babel/db/`.** Route handlers call functions; they never carry query text.
- **Never `($1 IS NULL OR col = $1)`.** Build query text per filter combination. Postgres proves a partial or composite index applicable only from a `Const`; this project has been bitten twice.
- **No `|safe` in any template.** Bodies and comments are plain text and must render escaped.
- **Every value a template places in a query string goes through `|urlencode`,** never `|e` alone.
- **Page size is pinned at 50** and never appears in a URL. No request parameter may increase server cost.
- **All reader-facing dates render in `America/Los_Angeles`** (`parser.GAME_TZ`), never UTC.
- **Commit message style:** imperative sentence-case, no `feat:`/`fix:` prefixes — match the existing log (`git log --oneline -10`). Append `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `migrations/005_browse.sql` | four browse indexes, suppression columns, blob back-reference index |
| `migrations/006_drop_redundant_indexes.sql` | drops the two indexes 005 supersedes — separate so a failed build never leaves neither |
| `src/babel/db/browse.py` | every read-path SQL statement and its row types |
| `src/babel/web/__init__.py` | package marker |
| `src/babel/web/cursor.py` | keyset cursor encode/decode, game-time conversion |
| `src/babel/web/blobs.py` | sha256 validation, on-disk path, serving type from bytes |
| `src/babel/web/app.py` | app factory, pool lifespan, security headers, error handlers |
| `src/babel/web/routes.py` | the five route handlers |
| `src/babel/web/templates/{base,list,article,error}.html` | markup |
| `src/babel/web/static/style.css` | styling |
| `tests/web/test_*.py`, `tests/db/test_browse*.py` | tests |

**Modified:** `src/babel/crawler/parser.py` (block boundaries), `src/babel/crawler/images.py` (address filter), `src/babel/config.py` (web settings), `src/babel/cli.py` (`serve`, `hide`), `src/babel/db/repo.py` (docstring pointer), `tests/conftest.py` (one added fixture), `pyproject.toml`, `docker-compose.yml`, `.gitignore`, `.dockerignore`, `.env.example`, `README.md`, `CLAUDE.md`.

## Test Fixtures

`tests/conftest.py` currently provides exactly two fixtures, and every task below depends on knowing what they actually are:

- **`pg`** — an `asyncpg.Connection` against a **fresh `postgres:17` container with all migrations applied, per test**. It is function-scoped, so each test starts with an empty schema; tests never need to clean up after each other or worry about colliding ids.
- **`fake_pool`** — a *builder*: `fake_pool(conn)` returns an object exposing `acquire()` as an async context manager over that one connection, serialised by a lock.

Task 4 adds one more, and every later task uses it:

```python
@pytest.fixture
def pool(pg, fake_pool):
    """A pool-shaped façade over the per-test connection.

    Application code takes an asyncpg.Pool and calls `async with pool.acquire()`.
    Handing it this keeps the tests' database access identical to production's
    without a second container per test.
    """
    return fake_pool(pg)
```

There is **no** DSN fixture and none is added. The web tests inject this `pool` into `create_app` rather than letting it dial out, which is why `create_app` takes an optional pool (Task 9). The two tests that exercise `open_pool`'s refusal paths (Task 13) need no database at all — both raise before connecting.

## Task Order

Tasks 1–2 are the spec's prerequisites and touch the crawler, not the web package. Task 3 is the schema. Tasks 4–7 are pure logic with no HTTP. Tasks 8–12 build the site. Task 13 deploys it. Each task ends green and committable.

---

### Task 1: Parser emits block boundaries

The stored body currently contains zero newlines, so the planned `white-space: pre-wrap` rendering would produce one unbroken wall of text. Measured on the three fixtures: 4 079 chars / 0 newlines / 41 `<br>` in source; 4 556 / 0 / 25; 315 / 0 / 10.

**Files:**
- Modify: `src/babel/crawler/parser.py:82` (the `body=` argument) and the imports/constants block near line 45
- Test: `tests/crawler/test_parser.py`

**Interfaces:**
- Consumes: nothing
- Produces: `parser._body_text(body_node: HTMLNode) -> str` — used only inside `parse_article`; the observable change is that `Article.body` now contains `\n` at block boundaries.

- [ ] **Step 1: Write the failing tests**

Add to `tests/crawler/test_parser.py`:

```python
from selectolax.parser import HTMLParser

from babel.crawler.parser import _body_text


def _body(fragment: str) -> str:
    node = HTMLParser(f"<div class='postBody'>{fragment}</div>").css_first("div.postBody")
    return _body_text(node)


def test_paragraphs_become_separate_lines():
    assert _body("<p>First.</p><p>Second.</p>") == "First.\nSecond."


def test_br_breaks_a_line():
    assert _body("Alpha<br>Beta") == "Alpha\nBeta"


def test_double_br_keeps_one_blank_line():
    assert _body("Alpha<br><br>Beta") == "Alpha\n\nBeta"


def test_runs_of_blank_lines_collapse_to_one():
    assert _body("<p>A</p><br><br><br><p>B</p>") == "A\n\nB"


def test_list_items_are_separate_lines():
    assert _body("<ul><li>one</li><li>two</li></ul>") == "one\ntwo"


def test_heading_is_not_welded_to_the_next_sentence():
    text = _body("<h3>Mendirikan Perusahaan</h3><p>Langkah pertama.</p>")
    assert text == "Mendirikan Perusahaan\nLangkah pertama."


def test_inline_markup_does_not_break_a_line():
    assert _body("<p>A <b>bold</b> word.</p>") == "A bold word."


def test_real_fixture_gains_line_breaks(indonesia_html):
    article = parse_article(indonesia_html, 123)
    assert article is not None
    assert "\n" in article.body
    assert "<" not in article.body
```

`indonesia_html` is the existing fixture loader in this file — reuse whatever name the module already uses for `article_indonesia.html`; do not invent a new fixture.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/crawler/test_parser.py -v`
Expected: FAIL — `ImportError: cannot import name '_body_text'`.

- [ ] **Step 3: Implement `_body_text`**

Add next to the other module-level regexes in `src/babel/crawler/parser.py`:

```python
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLOCK_CLOSE_RE = re.compile(
    r"</(?:p|div|li|ul|ol|h[1-6]|tr|table|blockquote|pre)\s*>", re.IGNORECASE
)
```

And the function, placed just above `_parse_published_at`:

```python
def _body_text(body_node: HTMLNode) -> str:
    """The article text with block structure preserved as newlines.

    selectolax joins text nodes with a single separator and knows nothing about
    block boundaries, so `text(separator=" ")` welded a heading to the sentence
    after it and produced bodies containing no newline at all — measured at 0
    newlines against 41 `<br>` in one fixture. The stored column is the only copy
    (raw HTML is deliberately not archived, see SPEC.md "Decisions"), so the
    structure has to survive the parse or it is gone.

    Breaks are injected into the markup rather than reconstructed from the text,
    because by the time selectolax has flattened it the boundary is a space and
    indistinguishable from the spaces inside a sentence.
    """
    html = body_node.html or ""
    html = _BR_RE.sub("\n", html)
    html = _BLOCK_CLOSE_RE.sub(lambda m: "\n" + m.group(0), html)

    raw = HTMLParser(html).text(separator=" ", strip=False)

    lines: list[str] = []
    for line in raw.split("\n"):
        stripped = " ".join(line.split())
        # Keep at most one blank line: authors separate paragraphs with runs of
        # <br>, and every one of them would otherwise become its own gap.
        if stripped or (lines and lines[-1]):
            lines.append(stripped)
    return "\n".join(lines).strip()
```

- [ ] **Step 4: Use it in `parse_article`**

In `src/babel/crawler/parser.py`, replace line 82:

```python
        body=body_node.text(separator=" ", strip=True),
```

with:

```python
        body=_body_text(body_node),
```

- [ ] **Step 5: Run the parser tests**

Run: `uv run pytest tests/crawler/test_parser.py -v`
Expected: PASS, all tests including the three pre-existing body assertions at lines 43–45.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest`
Expected: PASS. The ingest tests write `Article.body` to Postgres and read it back; a newline is just text to them. If anything fails on an exact-string body comparison, update that assertion — the new value is correct.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src tests
git add src/babel/crawler/parser.py tests/crawler/test_parser.py
git commit -m "Keep block boundaries in the stored article text

selectolax joins text nodes with one separator and knows nothing about
block structure, so text(separator=\" \") produced bodies with zero
newlines — measured at 4079 characters and 0 newlines against 41 <br>
in the Indonesian fixture, with headings welded to the next sentence.
Raw HTML is deliberately not archived, so the structure had to survive
the parse or be gone for good.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Image capture refuses non-public addresses (closes M2)

CLAUDE.md rates M2 Minor because the SSRF is "blind and read-only". Publishing `/img/{sha256}` removes the blindness — a stored blob becomes readable on a public page — so this must close before Task 13.

**Files:**
- Modify: `src/babel/crawler/images.py` (imports, new `classify_url`, guard at the top of `capture_image`)
- Test: `tests/crawler/test_images.py`

**Interfaces:**
- Consumes: `images.normalise_url(src: str) -> str` (existing)
- Produces: `async def classify_url(url: str) -> str` returning exactly `"ok"`, `"blocked"` or `"unresolved"`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/crawler/test_images.py`:

```python
import pytest

from babel.crawler.images import ImageOutcome, capture_image, classify_url


async def test_public_host_is_ok():
    assert await classify_url("https://example.com/a.png") == "ok"


async def test_non_http_scheme_is_blocked():
    assert await classify_url("file:///etc/passwd") == "blocked"
    assert await classify_url("ftp://example.com/a.png") == "blocked"


async def test_loopback_is_blocked():
    assert await classify_url("http://127.0.0.1/internal") == "blocked"
    assert await classify_url("http://[::1]/internal") == "blocked"


async def test_rfc1918_literal_is_blocked():
    assert await classify_url("http://172.18.0.5:8080/internal") == "blocked"
    assert await classify_url("http://192.168.10.18/internal") == "blocked"
    assert await classify_url("http://10.0.0.1/internal") == "blocked"


async def test_link_local_metadata_address_is_blocked():
    assert await classify_url("http://169.254.169.254/latest/meta-data/") == "blocked"


async def test_unresolvable_host_is_unresolved_not_blocked():
    # A DNS failure is transient. Calling it 'blocked' would write a permanent
    # 'dead' for an image that is merely behind a flaky resolver.
    assert await classify_url("https://no-such-host.invalid/a.png") == "unresolved"


async def test_capture_never_fetches_a_private_address(tmp_path):
    calls = []

    async def getter(url, max_bytes):
        calls.append(url)
        raise AssertionError("must not be called")

    outcome = await capture_image(getter, tmp_path, "http://127.0.0.1/x.png", max_bytes=1024)
    assert outcome == ImageOutcome(status="dead")
    assert calls == []


async def test_capture_marks_dns_failure_retryable(tmp_path):
    async def getter(url, max_bytes):
        raise AssertionError("must not be called")

    outcome = await capture_image(
        getter, tmp_path, "https://no-such-host.invalid/x.png", max_bytes=1024
    )
    assert outcome == ImageOutcome(status="error")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/crawler/test_images.py -v -k classify or capture_never or dns_failure`
Expected: FAIL — `ImportError: cannot import name 'classify_url'`.

- [ ] **Step 3: Implement `classify_url`**

Add to the imports at the top of `src/babel/crawler/images.py`:

```python
import asyncio
import ipaddress
import socket
```

Add above `capture_image`:

```python
_ALLOWED_SCHEMES = frozenset({"http", "https"})


async def classify_url(url: str) -> str:
    """Whether this image URL may be fetched at all: ok | blocked | unresolved.

    `source_url` is raw `img@src` from article HTML written by anyone, and the
    worker runs inside gluetun's namespace with FIREWALL_OUTBOUND_SUBNETS
    covering the whole Docker bridge range. Without this an author could point an
    <img> at an internal service; once the browser publishes /img/{sha256} they
    could then read the response back off their own article page.

    The three outcomes are not cosmetic. 'blocked' is a permanent property of the
    URL and maps to 'dead'; 'unresolved' is a resolver having a bad minute and
    must stay retryable, because a false 'dead' is the expensive mistake in this
    project and DNS is exactly the kind of thing that fails transiently.

    Residual, deliberately accepted: the address is checked before the fetch, so
    a host that answers this lookup publicly and the fetch privately (DNS
    rebinding) is not covered. Closing that needs connect-time pinning inside
    curl_cffi, which is a larger change than the exposure warrants.
    """
    parts = urlsplit(normalise_url(url))
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return "blocked"
    host = parts.hostname
    if not host:
        return "blocked"
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except socket.gaierror:
        return "unresolved"
    if not infos:
        return "unresolved"
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return "blocked"
        # is_global is False for loopback, link-local, private, reserved,
        # multicast and CGNAT alike, which is exactly the set we refuse.
        if not address.is_global:
            return "blocked"
    return "ok"
```

- [ ] **Step 4: Guard `capture_image`**

Insert at the very start of `capture_image`'s body, before the `try:` at line 162:

```python
    verdict = await classify_url(source_url)
    if verdict == "blocked":
        log.warning("%s is not a publicly routable address, refusing", url_host(source_url))
        return ImageOutcome(status="dead")
    if verdict == "unresolved":
        return ImageOutcome(status="error")
```

- [ ] **Step 5: Run the image tests**

Run: `uv run pytest tests/crawler/test_images.py -v`
Expected: PASS. Existing tests pass hosts like `example.com` and `i.imgur.com`, which resolve publicly; if any existing test uses a fake host that does not resolve, it will now return `error` — change that test's URL to a resolvable public host rather than weakening the guard.

- [ ] **Step 6: Run the full suite, lint, commit**

```bash
uv run pytest
uv run ruff check src tests
git add src/babel/crawler/images.py tests/crawler/test_images.py
git commit -m "Refuse image URLs that are not publicly routable

Closes M2. source_url is raw img@src from untrusted article HTML and the
worker runs inside gluetun's namespace with the Docker bridge range open,
so an author could aim an <img> at an internal service. That was rated
Minor while the fetch was blind; publishing /img/{sha256} would have let
the author read the response back off their own article page.

A blocked address is a permanent property of the URL and records 'dead'.
A resolver failure records 'error' and stays retryable — a false 'dead'
is the expensive mistake here, and DNS fails transiently.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Migration 005 and 006

**Files:**
- Create: `migrations/005_browse.sql`, `migrations/006_drop_redundant_indexes.sql`
- Test: `tests/db/test_migration.py`

**Interfaces:**
- Consumes: `babel.db.migrate.apply_migrations(conn, directory) -> list[str]` (existing; globs `*.sql` in filename order, wraps each file in one transaction)
- Produces: indexes `articles_list_idx`, `articles_country_list_idx`, `articles_author_list_idx`, `articles_country_author_list_idx`, `article_images_sha256_idx`; columns `articles.hidden_at`, `images.withheld_at`.

- [ ] **Step 1: Write the failing test**

Add to `tests/db/test_migration.py` (reuse the module's existing migrated-pool fixture; do not create a second container):

```python
async def test_browse_indexes_and_tombstones_exist(migrated_conn):
    names = {
        r["indexname"]
        for r in await migrated_conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
        )
    }
    assert "articles_list_idx" in names
    assert "articles_country_list_idx" in names
    assert "articles_author_list_idx" in names
    assert "articles_country_author_list_idx" in names
    assert "article_images_sha256_idx" in names
    # 006 removes what 005 supersedes.
    assert "articles_published_at_idx" not in names
    assert "articles_country_idx" not in names

    columns = {
        (r["table_name"], r["column_name"])
        for r in await migrated_conn.fetch(
            """SELECT table_name, column_name FROM information_schema.columns
               WHERE table_schema = 'public'"""
        )
    }
    assert ("articles", "hidden_at") in columns
    assert ("images", "withheld_at") in columns
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/db/test_migration.py -v`
Expected: FAIL — `assert 'articles_list_idx' in names`.

- [ ] **Step 3: Write `migrations/005_browse.sql`**

```sql
-- Browse indexes, suppression tombstones, and the blob back-reference.
--
-- APPLY THIS DELIBERATELY, never as a side effect of `docker compose up -d`.
-- Both `babel run` and `babel images` call apply_migrations at startup, and
-- apply_migrations wraps a whole file in one transaction, so a per-service
-- restart would run this DDL against a live walk. Plain CREATE INDEX takes
-- ShareLock, which blocks concurrent writers — not readers — for the whole
-- build, not just while the lock is being acquired; the two ADD COLUMN
-- statements below take ACCESS EXCLUSIVE, but neither column has a default,
-- so both are metadata-only and effectively instant. lock_timeout changes
-- neither hold: it only bounds how long this migration itself waits to
-- *acquire* a lock, so a session already holding a conflicting one makes
-- this fail in 5s instead of queueing behind it. That queueing is the actual
-- danger: once an ACCESS EXCLUSIVE request is waiting, Postgres's lock queue
-- is FIFO, so every later reader and writer queues behind it too, even ones
-- that would otherwise coexist fine with whatever lock is currently held.
-- What keeps this migration from doing that to the live walk is the
-- runbook, not lock_timeout: stop `crawler` and `images` first, then
-- migrate. The runbook is in README.md.
--
-- CREATE INDEX CONCURRENTLY is not available through this runner: it is illegal
-- inside a transaction block, and asyncpg wraps a multi-statement file in an
-- implicit one even without the explicit transaction. Index builds at scale are
-- an operator step through psql.
SET lock_timeout = '5s';

-- One index per filter combination the list can produce. The query text is
-- built per combination too, deliberately: `($1 IS NULL OR country = $1)` is
-- opaque to the planner and degrades to a scan under a generic plan.
CREATE INDEX IF NOT EXISTS articles_list_idx
    ON articles (published_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS articles_country_list_idx
    ON articles (country, published_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS articles_author_list_idx
    ON articles (lower(author_name), published_at DESC, id DESC);
-- Measured at 2.8M rows: without this the two-filter query still uses an index
-- (the planner anchors on the author predicate, ~150k distinct values against
-- 70 countries) at 10ms warm, 58ms cold, 49.7ms once the prepared statement
-- flips to a generic plan; with it, 0.064ms, for ~157MB.
CREATE INDEX IF NOT EXISTS articles_country_author_list_idx
    ON articles (country, lower(author_name), published_at DESC, id DESC);

-- Takedown is a tombstone, never a DELETE. Measured on a fresh database: a
-- DELETE cascades comments and article_images but leaves the images row and the
-- blob on disk, and fetch_log has no FK to articles, so its row survives as
-- 'ok' — `babel refetch` then flips it to 'stale', the sweep re-collects, and
-- the taken-down article comes back. Deletion is silently reversible by tooling
-- this project already ships. Do not "simplify" these columns away.
ALTER TABLE articles ADD COLUMN IF NOT EXISTS hidden_at   timestamptz;
ALTER TABLE images   ADD COLUMN IF NOT EXISTS withheld_at timestamptz;

-- Answers "which other articles cite this blob", which is what makes a
-- per-image takedown decision possible at all. Content-addressing means a blob
-- is shared, so withholding one is never a single-article act.
CREATE INDEX IF NOT EXISTS article_images_sha256_idx ON article_images (sha256);
```

- [ ] **Step 4: Write `migrations/006_drop_redundant_indexes.sql`**

```sql
-- Separate from 005 on purpose: if the index build in 005 fails, its whole
-- transaction rolls back, and dropping the old indexes in the same file would
-- leave `articles` with neither set.
DROP INDEX IF EXISTS articles_published_at_idx;  -- superseded by articles_list_idx
DROP INDEX IF EXISTS articles_country_idx;       -- superseded by articles_country_list_idx
```

- [ ] **Step 5: Run the migration test**

Run: `uv run pytest tests/db/test_migration.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite, lint, commit**

```bash
uv run pytest
uv run ruff check src tests
git add migrations/005_browse.sql migrations/006_drop_redundant_indexes.sql tests/db/test_migration.py
git commit -m "Add the browse indexes and the suppression tombstones

One index per filter combination, because the list builds one query text
per combination and an IS NULL OR predicate is opaque to the planner.
The two-filter index is latency hygiene rather than a limit: measured at
2.8M rows the planner already anchors on the author predicate at 10-58ms,
and the composite takes that to 0.064ms for 157MB.

hidden_at and withheld_at exist because DELETE FROM articles does not
work as a takedown: fetch_log has no FK to articles, so the row survives
as 'ok' and babel refetch brings the article back.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: The list query — keyset paging and filters

**Files:**
- Create: `src/babel/db/browse.py`
- Modify: `src/babel/db/repo.py:1-6` (docstring)
- Test: `tests/db/test_browse_list.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `PAGE_SIZE: int = 50`
  - `@dataclass(frozen=True, slots=True) class ListFilters: country: str | None = None; author: str | None = None`
  - `@dataclass(frozen=True, slots=True) class Cursor: published_at: datetime.datetime; article_id: int`
  - `@dataclass(frozen=True, slots=True) class ArticleRow: id: int; title: str; author_name: str | None; country: str | None; published_at: datetime.datetime; comment_count: int`
  - `@dataclass(frozen=True, slots=True) class ListPage: rows: tuple[ArticleRow, ...]; has_more: bool`
  - `def build_list_query(filters: ListFilters, cursor: Cursor | None, *, descending: bool, limit: int) -> tuple[str, list[object]]`
  - `async def list_articles(conn, filters: ListFilters, *, order: str, cursor: Cursor | None, going: str, limit: int = PAGE_SIZE) -> ListPage` — `order` is `"new"` or `"old"`, `going` is `"next"` or `"prev"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/db/test_browse_list.py`:

```python
import datetime

import pytest

from babel.db.browse import (
    ArticleRow,
    Cursor,
    ListFilters,
    build_list_query,
    list_articles,
)

UTC = datetime.UTC


async def _seed(conn, rows):
    """rows: (id, published_at, country, author_name)"""
    await conn.executemany(
        """INSERT INTO articles (id, title, body, author_name, country,
                                 published_at, comment_count)
           VALUES ($1, 'T' || $1::text, 'body', $2, $3, $4, 0)""",
        [(i, author, country, ts) for i, ts, country, author in rows],
    )


def _ts(day: int, second: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 1, day, 12, 0, second, tzinfo=UTC)


async def test_pages_cover_every_row_exactly_once_with_tied_timestamps(pool):
    # Ten articles sharing ONE timestamp. published_at alone cannot order them,
    # so a page boundary landing inside the tie is exactly where a keyset that
    # forgets `id` drops or repeats a row.
    tied = _ts(5)
    async with pool.acquire() as conn:
        await _seed(conn, [(100 + i, tied, "Poland", "ann") for i in range(10)])

        seen: list[int] = []
        cursor = None
        for _ in range(10):
            page = await list_articles(
                conn, ListFilters(), order="new", cursor=cursor, going="next", limit=3
            )
            seen.extend(r.id for r in page.rows)
            if not page.has_more:
                break
            last = page.rows[-1]
            cursor = Cursor(published_at=last.published_at, article_id=last.id)

    assert sorted(seen) == list(range(100, 110))
    assert len(seen) == len(set(seen))
    assert seen == sorted(seen, reverse=True)


async def test_prev_page_is_symmetric(pool):
    async with pool.acquire() as conn:
        await _seed(conn, [(200 + i, _ts(1 + i), "Poland", "ann") for i in range(6)])

        first = await list_articles(
            conn, ListFilters(), order="new", cursor=None, going="next", limit=2
        )
        second_cursor = Cursor(first.rows[-1].published_at, first.rows[-1].id)
        second = await list_articles(
            conn, ListFilters(), order="new", cursor=second_cursor, going="next", limit=2
        )
        back_cursor = Cursor(second.rows[0].published_at, second.rows[0].id)
        back = await list_articles(
            conn, ListFilters(), order="new", cursor=back_cursor, going="prev", limit=2
        )

    assert [r.id for r in back.rows] == [r.id for r in first.rows]


async def test_order_old_reverses(pool):
    async with pool.acquire() as conn:
        await _seed(conn, [(300 + i, _ts(1 + i), "Poland", "ann") for i in range(4)])
        page = await list_articles(
            conn, ListFilters(), order="old", cursor=None, going="next", limit=10
        )
    assert [r.id for r in page.rows] == [300, 301, 302, 303]


async def test_filters_compose(pool):
    async with pool.acquire() as conn:
        await _seed(
            conn,
            [
                (400, _ts(1), "Poland", "ann"),
                (401, _ts(2), "Poland", "bob"),
                (402, _ts(3), "Serbia", "ann"),
            ],
        )
        page = await list_articles(
            conn,
            ListFilters(country="Poland", author="ann"),
            order="new",
            cursor=None,
            going="next",
            limit=10,
        )
    assert [r.id for r in page.rows] == [400]


async def test_author_filter_ignores_case(pool):
    async with pool.acquire() as conn:
        await _seed(conn, [(500, _ts(1), "Poland", "AnnaBanana")])
        page = await list_articles(
            conn, ListFilters(author="annabanana"), order="new",
            cursor=None, going="next", limit=10,
        )
    assert [r.id for r in page.rows] == [500]


async def test_has_more_is_false_on_the_last_page(pool):
    async with pool.acquire() as conn:
        await _seed(conn, [(600 + i, _ts(1 + i), "Poland", "ann") for i in range(3)])
        page = await list_articles(
            conn, ListFilters(), order="new", cursor=None, going="next", limit=10
        )
    assert page.has_more is False
    assert len(page.rows) == 3


async def test_hidden_articles_never_appear(pool):
    async with pool.acquire() as conn:
        await _seed(conn, [(700, _ts(1), "Poland", "ann"), (701, _ts(2), "Poland", "ann")])
        await conn.execute("UPDATE articles SET hidden_at = now() WHERE id = 700")
        page = await list_articles(
            conn, ListFilters(), order="new", cursor=None, going="next", limit=10
        )
    assert [r.id for r in page.rows] == [701]


def test_builder_never_emits_the_is_null_or_form():
    for filters in (
        ListFilters(),
        ListFilters(country="Poland"),
        ListFilters(author="ann"),
        ListFilters(country="Poland", author="ann"),
    ):
        sql, _ = build_list_query(filters, None, descending=True, limit=50)
        assert "IS NULL OR" not in sql.upper()


def test_builder_emits_a_distinct_text_per_filter_combination():
    texts = {
        build_list_query(f, None, descending=True, limit=50)[0]
        for f in (
            ListFilters(),
            ListFilters(country="Poland"),
            ListFilters(author="ann"),
            ListFilters(country="Poland", author="ann"),
        )
    }
    assert len(texts) == 4
```

`pool` does not exist yet. Add it to `tests/conftest.py` as the first step of this task, exactly as given in the "Test Fixtures" section near the top of this plan — it is a three-line façade over the existing `pg` and `fake_pool` fixtures. Because `pg` builds a fresh container per test, every test above starts against an empty schema; no truncation or id-collision handling is needed anywhere in this plan.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/db/test_browse_list.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'babel.db.browse'`.

- [ ] **Step 3: Write `src/babel/db/browse.py`**

```python
"""Every read-path SQL statement in the project.

Split from repo.py, which holds the write path. The two have opposite
requirements: repo.py's statements are fixed text with bound parameters, while
the list query's text varies with which filters are active — and it varies
deliberately. `($1 IS NULL OR country = $1)` is one statement instead of four,
and it is the wrong trade: Postgres proves a composite index applicable only
from a Const, so under a generic plan that form degrades to a scan over a table
heading for 2.8M rows. repo.py already carries two comments about the same
mechanism biting this project. Four combinations, four texts.
"""

import datetime
from dataclasses import dataclass

import asyncpg

PAGE_SIZE = 50


@dataclass(frozen=True, slots=True)
class ListFilters:
    country: str | None = None
    author: str | None = None


@dataclass(frozen=True, slots=True)
class Cursor:
    published_at: datetime.datetime
    article_id: int


@dataclass(frozen=True, slots=True)
class ArticleRow:
    id: int
    title: str
    author_name: str | None
    country: str | None
    published_at: datetime.datetime
    comment_count: int


@dataclass(frozen=True, slots=True)
class ListPage:
    rows: tuple[ArticleRow, ...]
    has_more: bool


_COLUMNS = "id, title, author_name, country, published_at, comment_count"


def build_list_query(
    filters: ListFilters, cursor: Cursor | None, *, descending: bool, limit: int
) -> tuple[str, list[object]]:
    """The list statement and its parameters, for one filter combination.

    Returned rather than executed so the EXPLAIN tests can plan the query the
    application actually sends. Asserting against a copy of the SQL pasted into a
    test proves only that Postgres can use an index from a Const — which is the
    mistake finding M3 records against the existing suite.
    """
    where: list[str] = ["hidden_at IS NULL"]
    params: list[object] = []

    if filters.country is not None:
        params.append(filters.country)
        where.append(f"country = ${len(params)}")
    if filters.author is not None:
        params.append(filters.author.lower())
        where.append(f"lower(author_name) = ${len(params)}")
    if cursor is not None:
        params.append(cursor.published_at)
        params.append(cursor.article_id)
        comparison = "<" if descending else ">"
        # Row-wise comparison, not `published_at < $n OR (published_at = $n AND
        # id < $m)`: only this form is an Index Cond against
        # (published_at DESC, id DESC). published_at has second granularity and
        # ties are common, so a boundary inside a tie is where a key of
        # published_at alone drops or repeats a row.
        where.append(f"(published_at, id) {comparison} (${len(params) - 1}, ${len(params)})")

    params.append(limit)
    order = "DESC" if descending else "ASC"
    sql = f"""
        SELECT {_COLUMNS}
        FROM articles
        WHERE {" AND ".join(where)}
        ORDER BY published_at {order}, id {order}
        LIMIT ${len(params)}
    """
    return sql, params


async def list_articles(
    conn: asyncpg.Connection,
    filters: ListFilters,
    *,
    order: str,
    cursor: Cursor | None,
    going: str,
    limit: int = PAGE_SIZE,
) -> ListPage:
    """One page of the list, already in display order.

    Two independent flips decide the SQL direction: `order` is what the reader
    asked for (new or old first) and `going` is which way they are paging. Going
    back is the same query run the other way round, so the rows come out
    reversed and are flipped back here rather than in the template.
    """
    forward = going == "next"
    descending = (order == "new") == forward

    sql, params = build_list_query(filters, cursor, descending=descending, limit=limit + 1)
    records = await conn.fetch(sql, *params)

    has_more = len(records) > limit
    rows = [ArticleRow(**dict(r)) for r in records[:limit]]
    if not forward:
        rows.reverse()
    return ListPage(rows=tuple(rows), has_more=has_more)
```

- [ ] **Step 4: Point repo.py's docstring at the split**

In `src/babel/db/repo.py`, change the first line of the module docstring from:

```python
"""Every SQL statement in the project lives here.
```

to:

```python
"""Every write-path SQL statement in the project lives here.

The read path lives in browse.py. SQL still never appears outside this package.
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/db/test_browse_list.py -v`
Expected: PASS, all ten.

- [ ] **Step 6: Run the full suite, lint, commit**

```bash
uv run pytest
uv run ruff check src tests
git add src/babel/db/browse.py src/babel/db/repo.py tests/db/test_browse_list.py
git commit -m "Add the keyset list query

The sort key is (published_at, id), not published_at: the column has
second granularity, ties are common, and a page boundary inside a tie is
where a keyset that forgets the tiebreak drops or repeats a row. The test
seeds ten articles sharing one timestamp for exactly that reason.

Query text is built per filter combination and returned rather than
executed, so the EXPLAIN tests can plan what the application sends.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: EXPLAIN tests that can actually fail

Finding M3 in CLAUDE.md: the two existing EXPLAIN tests plan a string written in the test, so they prove only that Postgres can use an index from a `Const`. Asserting that an index *name* appears is just as weak — an index scan carrying a filter still names its index.

**Files:**
- Create: `tests/db/test_browse_plans.py`

**Interfaces:**
- Consumes: `browse.build_list_query`, `browse.ListFilters`, `browse.Cursor`
- Produces: nothing

- [ ] **Step 1: Write the failing tests**

Create `tests/db/test_browse_plans.py`:

```python
import datetime

from babel.db.browse import Cursor, ListFilters, build_list_query

UTC = datetime.UTC
CURSOR = Cursor(datetime.datetime(2026, 1, 5, 12, 0, tzinfo=UTC), 500)


async def _plan(conn, sql: str, params: list[object]) -> str:
    rows = await conn.fetch(f"EXPLAIN {sql}", *params)
    return "\n".join(r[0] for r in rows)


def _index_conds(plan: str) -> str:
    return "\n".join(line for line in plan.splitlines() if "Index Cond:" in line)


def _filters(plan: str) -> str:
    return "\n".join(line for line in plan.splitlines() if "Filter:" in line)


async def _populated(pool):
    """Enough rows, and ANALYZEd, so the planner is not choosing off a zero-page
    estimate. On an empty table every plan costs about the same and the choice
    carries no information."""
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO articles (id, title, body, author_name, country,
                                     published_at, comment_count)
               SELECT g,
                      'T' || g,
                      'body',
                      'author' || (g % 500),
                      (ARRAY['Poland','Serbia','Hungary'])[1 + g % 3],
                      timestamptz '2026-01-01' + (g || ' seconds')::interval,
                      0
                 FROM generate_series(1, 20000) g
               ON CONFLICT (id) DO NOTHING"""
        )
        await conn.execute("ANALYZE articles")
    return pool


async def test_cursor_is_an_index_condition_not_a_filter(pool):
    await _populated(pool)
    sql, params = build_list_query(ListFilters(), CURSOR, descending=True, limit=50)
    async with pool.acquire() as conn:
        plan = await _plan(conn, sql, params)
    assert "articles_list_idx" in plan
    assert "published_at" in _index_conds(plan)
    assert "published_at" not in _filters(plan)


async def test_country_is_an_index_condition(pool):
    await _populated(pool)
    sql, params = build_list_query(ListFilters(country="Poland"), CURSOR, descending=True, limit=50)
    async with pool.acquire() as conn:
        plan = await _plan(conn, sql, params)
    assert "country" in _index_conds(plan)
    assert "country" not in _filters(plan)


async def test_author_is_an_index_condition(pool):
    await _populated(pool)
    sql, params = build_list_query(ListFilters(author="author7"), CURSOR, descending=True, limit=50)
    async with pool.acquire() as conn:
        plan = await _plan(conn, sql, params)
    assert "lower(author_name)" in _index_conds(plan) or "lower((author_name" in _index_conds(plan)
    assert "author_name" not in _filters(plan)


async def test_both_filters_use_the_composite_index(pool):
    await _populated(pool)
    sql, params = build_list_query(
        ListFilters(country="Poland", author="author7"), CURSOR, descending=True, limit=50
    )
    async with pool.acquire() as conn:
        plan = await _plan(conn, sql, params)
    assert "articles_country_author_list_idx" in plan
    conds = _index_conds(plan)
    assert "country" in conds
    assert "author_name" in conds
    assert _filters(plan) == ""


async def test_negative_control_the_is_null_or_form_degrades(pool):
    """The shape build_list_query exists to avoid.

    It still names an index in its plan — which is why asserting on the index
    name proves nothing — but the predicate lands in Filter: rather than
    Index Cond:. If this test ever passes the same assertions as the real
    queries above, those assertions have stopped testing anything.
    """
    await _populated(pool)
    sql = """
        SELECT id FROM articles
        WHERE hidden_at IS NULL AND ($1::text IS NULL OR country = $1::text)
        ORDER BY published_at DESC, id DESC
        LIMIT 50
    """
    async with pool.acquire() as conn:
        plan = await _plan(conn, sql, ["Poland"])
    assert "country" in _filters(plan)
    assert "country" not in _index_conds(plan)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/db/test_browse_plans.py -v`
Expected: FAIL — the plans will not match until migration 005 is applied by the fixture. If the `pool` fixture already applies all migrations, these should pass immediately after Task 3; in that case run them and confirm they pass, then deliberately break one assertion to confirm it can fail, and restore it. A test that cannot fail is the thing this task exists to prevent.

- [ ] **Step 3: Make them pass**

No implementation is needed beyond Tasks 3 and 4. If `test_both_filters_use_the_composite_index` fails because the planner prefers `articles_author_list_idx`, that is a real signal, not a flaky test: check that `ANALYZE` ran and that migration 005's fourth index exists. Do not weaken the assertion to match whatever plan appears.

- [ ] **Step 4: Lint and commit**

```bash
uv run ruff check src tests
git add tests/db/test_browse_plans.py
git commit -m "Assert on Index Cond, not on the index name

Half of finding M3 is that the existing EXPLAIN tests plan a string
written in the test. The other half is that an index name in the plan
proves nothing: an index scan carrying a filter still names its index,
so the assertion passes for exactly the IS NULL OR form it is meant to
forbid. These tests take the SQL from the builder, assert the predicates
land in Index Cond and not in Filter, seed 20k rows so the plan is not
chosen off a zero-page estimate, and keep the degraded form as an
explicit negative control.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: The rest of the read path

**Files:**
- Modify: `src/babel/db/browse.py`
- Test: `tests/db/test_browse_reads.py`

**Interfaces:**
- Consumes: everything from Task 4
- Produces:
  - `@dataclass(frozen=True, slots=True) class ArticleDetail: id: int; title: str; body: str; author_name: str | None; author_id: int | None; country: str | None; published_at: datetime.datetime; e_day: int | None; comment_count: int`
  - `@dataclass(frozen=True, slots=True) class CommentRow: id: int; position: int; depth: int; author_id: int | None; author_name: str | None; posted_at: datetime.datetime | None; body: str | None`
  - `@dataclass(frozen=True, slots=True) class ArchiveStats: articles: int; oldest: datetime.datetime | None; newest: datetime.datetime | None; frontier: int | None`
  - `@dataclass(frozen=True, slots=True) class BlobRow: sha256: bytes; withheld_at: datetime.datetime | None`
  - `async def get_article(conn, article_id: int) -> ArticleDetail | None`
  - `async def get_comments(conn, article_id: int) -> tuple[CommentRow, ...]`
  - `async def get_ok_image_digests(conn, article_id: int) -> tuple[bytes, ...]`
  - `async def image_status_counts(conn, article_id: int) -> dict[str, int]`
  - `async def list_countries(conn) -> tuple[str, ...]`
  - `async def suggest_authors(conn, prefix: str, limit: int = 8) -> tuple[str, ...]`
  - `async def archive_stats(conn) -> ArchiveStats`
  - `async def get_blob(conn, digest: bytes) -> BlobRow | None`
  - `async def fetch_log_status(conn, article_id: int) -> str | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/db/test_browse_reads.py`:

```python
import datetime

from babel.db.browse import (
    archive_stats,
    fetch_log_status,
    get_article,
    get_blob,
    get_comments,
    get_ok_image_digests,
    image_status_counts,
    list_countries,
    suggest_authors,
)

UTC = datetime.UTC


async def _article(conn, article_id, *, country="Poland", author="ann", hidden=False):
    await conn.execute(
        """INSERT INTO articles (id, title, body, author_name, country,
                                 published_at, e_day, comment_count, hidden_at)
           VALUES ($1, 'Title', 'Body text', $2, $3,
                   timestamptz '2026-01-05 12:00Z', 6800, 2, $4)""",
        article_id, author, country, datetime.datetime.now(UTC) if hidden else None,
    )


async def test_get_article_returns_none_for_a_hidden_row(pool):
    async with pool.acquire() as conn:
        await _article(conn, 900, hidden=True)
        assert await get_article(conn, 900) is None


async def test_get_article_returns_the_row(pool):
    async with pool.acquire() as conn:
        await _article(conn, 901)
        detail = await get_article(conn, 901)
    assert detail is not None
    assert detail.title == "Title"
    assert detail.e_day == 6800


async def test_comments_come_back_in_thread_order(pool):
    async with pool.acquire() as conn:
        await _article(conn, 902)
        await conn.executemany(
            """INSERT INTO comments (id, article_id, position, depth, author_name, body)
               VALUES ($1, 902, $2, $3, 'bob', $4)""",
            [(11, 1, 0, "first"), (12, 2, 1, "reply"), (13, 3, 0, None)],
        )
        rows = await get_comments(conn, 902)
    assert [r.position for r in rows] == [1, 2, 3]
    assert rows[1].depth == 1
    assert rows[2].body is None


async def test_image_counts_separate_dead_from_not_yet_captured(pool):
    async with pool.acquire() as conn:
        await _article(conn, 903)
        await conn.executemany(
            """INSERT INTO article_images (article_id, position, source_url, status, attempts)
               VALUES (903, $1, $2, $3, $4)""",
            [
                (1, "https://h/1.png", "ok", 1),
                (2, "https://h/2.png", "dead", 1),
                (3, "https://h/3.png", "pending", 0),
                (4, "https://h/4.png", "error", 2),
                (5, "https://h/5.png", "error", 5),
            ],
        )
        counts = await image_status_counts(conn, 903)
    assert counts["ok"] == 1
    assert counts["dead"] == 1
    assert counts["waiting"] == 2       # pending + error below the attempt ceiling
    assert counts["exhausted"] == 1     # error at the ceiling


async def test_ok_digests_come_back_in_position_order(pool):
    async with pool.acquire() as conn:
        await _article(conn, 904)
        await conn.execute(
            "INSERT INTO images (sha256, mime, bytes) VALUES ($1,'image/png',1), ($2,'image/png',1)",
            b"\x01" * 32, b"\x02" * 32,
        )
        await conn.executemany(
            """INSERT INTO article_images (article_id, position, source_url, status, sha256)
               VALUES (904, $1, $2, 'ok', $3)""",
            [(2, "https://h/b.png", b"\x02" * 32), (1, "https://h/a.png", b"\x01" * 32)],
        )
        digests = await get_ok_image_digests(conn, 904)
    assert digests == (b"\x01" * 32, b"\x02" * 32)


async def test_withheld_blob_is_reported(pool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO images (sha256, mime, bytes, withheld_at) VALUES ($1,'image/png',1,now())",
            b"\x03" * 32,
        )
        row = await get_blob(conn, b"\x03" * 32)
    assert row is not None
    assert row.withheld_at is not None


async def test_countries_are_distinct_and_sorted(pool):
    async with pool.acquire() as conn:
        await _article(conn, 905, country="Serbia")
        await _article(conn, 906, country="Poland")
        await _article(conn, 907, country="Poland")
        countries = await list_countries(conn)
    assert "Poland" in countries
    assert "Serbia" in countries
    assert len(countries) == len(set(countries))
    assert list(countries) == sorted(countries)


async def test_author_suggestions_are_prefix_matched_and_bounded(pool):
    async with pool.acquire() as conn:
        for i in range(20):
            await _article(conn, 1000 + i, author=f"annabel{i}")
        await _article(conn, 1100, author="bertram")
        names = await suggest_authors(conn, "anna", limit=8)
    assert len(names) == 8
    assert all(n.lower().startswith("anna") for n in names)
    assert "bertram" not in names


async def test_author_suggestions_treat_percent_as_a_literal(pool):
    # No LIKE on the request path, so wildcards are ordinary characters.
    async with pool.acquire() as conn:
        await _article(conn, 1200, author="plain")
        assert await suggest_authors(conn, "%", limit=8) == ()


async def test_archive_stats_reports_span_and_frontier(pool):
    async with pool.acquire() as conn:
        await _article(conn, 1300)
        await conn.execute(
            "INSERT INTO crawl_cursor (name, next_id) VALUES ('backfill', 2785850) "
            "ON CONFLICT (name) DO UPDATE SET next_id = 2785850"
        )
        stats = await archive_stats(conn)
    assert stats.articles >= 1
    assert stats.oldest is not None
    assert stats.newest is not None
    assert stats.frontier == 2785850


async def test_fetch_log_status_is_readable(pool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO fetch_log (article_id, status) VALUES (1400, 'missing') "
            "ON CONFLICT (article_id) DO UPDATE SET status = 'missing'"
        )
        assert await fetch_log_status(conn, 1400) == "missing"
        assert await fetch_log_status(conn, 1401) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/db/test_browse_reads.py -v`
Expected: FAIL — `ImportError: cannot import name 'get_article'`.

- [ ] **Step 3: Implement**

Append to `src/babel/db/browse.py`. Add `from babel.db.repo import MAX_IMAGE_ATTEMPTS` to the imports.

```python
@dataclass(frozen=True, slots=True)
class ArticleDetail:
    id: int
    title: str
    body: str
    author_name: str | None
    author_id: int | None
    country: str | None
    published_at: datetime.datetime
    e_day: int | None
    comment_count: int


@dataclass(frozen=True, slots=True)
class CommentRow:
    id: int
    position: int
    depth: int
    author_id: int | None
    author_name: str | None
    posted_at: datetime.datetime | None
    body: str | None


@dataclass(frozen=True, slots=True)
class ArchiveStats:
    articles: int
    oldest: datetime.datetime | None
    newest: datetime.datetime | None
    frontier: int | None


@dataclass(frozen=True, slots=True)
class BlobRow:
    sha256: bytes
    withheld_at: datetime.datetime | None


async def get_article(conn: asyncpg.Connection, article_id: int) -> ArticleDetail | None:
    row = await conn.fetchrow(
        """SELECT id, title, body, author_name, author_id, country,
                  published_at, e_day, comment_count
             FROM articles
            WHERE id = $1 AND hidden_at IS NULL""",
        article_id,
    )
    return ArticleDetail(**dict(row)) if row else None


async def get_comments(conn: asyncpg.Connection, article_id: int) -> tuple[CommentRow, ...]:
    rows = await conn.fetch(
        """SELECT id, position, depth, author_id, author_name, posted_at, body
             FROM comments WHERE article_id = $1 ORDER BY position""",
        article_id,
    )
    return tuple(CommentRow(**dict(r)) for r in rows)


async def get_ok_image_digests(conn: asyncpg.Connection, article_id: int) -> tuple[bytes, ...]:
    rows = await conn.fetch(
        """SELECT ai.sha256
             FROM article_images ai
             JOIN images i ON i.sha256 = ai.sha256
            WHERE ai.article_id = $1 AND ai.status = 'ok'
              AND ai.sha256 IS NOT NULL AND i.withheld_at IS NULL
            ORDER BY ai.position""",
        article_id,
    )
    return tuple(r["sha256"] for r in rows)


async def image_status_counts(conn: asyncpg.Connection, article_id: int) -> dict[str, int]:
    """Four buckets, because 'not captured yet' is not 'gone'.

    The drain runs slower than the walk produces work, so `pending` is the normal
    state for a recently crawled article, not an edge case. Counting
    `total - ok` would render "6 of 6 images were already gone when we looked"
    above an empty gallery on the newest articles — the inverse of the truth, on
    the project's central claim about itself.
    """
    row = await conn.fetchrow(
        """SELECT
             count(*) FILTER (WHERE status = 'ok')                              AS ok,
             count(*) FILTER (WHERE status = 'dead')                            AS dead,
             count(*) FILTER (WHERE status = 'pending'
                                 OR (status = 'error' AND attempts < $2))       AS waiting,
             count(*) FILTER (WHERE status = 'error' AND attempts >= $2)        AS exhausted
           FROM article_images WHERE article_id = $1""",
        article_id, MAX_IMAGE_ATTEMPTS,
    )
    return {k: int(v) for k, v in dict(row).items()}


# A loose index scan: ~70 probes into articles_country_list_idx rather than a
# scan of the table. Postgres has no native skip scan, so the recursion is how
# you ask for one.
_COUNTRIES_SQL = """
WITH RECURSIVE t AS (
    (SELECT country FROM articles
      WHERE country IS NOT NULL AND hidden_at IS NULL
      ORDER BY country LIMIT 1)
    UNION ALL
    SELECT (SELECT country FROM articles
             WHERE country > t.country AND country IS NOT NULL AND hidden_at IS NULL
             ORDER BY country LIMIT 1)
      FROM t WHERE t.country IS NOT NULL
)
SELECT country FROM t WHERE country IS NOT NULL
"""


async def list_countries(conn: asyncpg.Connection) -> tuple[str, ...]:
    rows = await conn.fetch(_COUNTRIES_SQL)
    return tuple(r["country"] for r in rows)


def _prefix_upper_bound(prefix: str) -> str:
    """The smallest string greater than every string starting with `prefix`."""
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


async def suggest_authors(
    conn: asyncpg.Connection, prefix: str, limit: int = 8
) -> tuple[str, ...]:
    """Up to `limit` author names starting with `prefix`, case-insensitively.

    A range scan, not `LIKE` with `DISTINCT`. DISTINCT is a blocking aggregate
    that LIMIT cannot push through, so that form costs O(matching articles):
    measured on 600k rows, `?author=a` aggregated 50,990 rows to return 8, reading
    95MB, and `?author=%` became a 938MB sequential scan. This form reads 127
    buffers in 0.10ms and is O(limit). It also means no LIKE ever sees user
    input, so `%` and `_` need no escaping — they are ordinary characters.
    """
    lowered = prefix.lower()
    if not lowered:
        return ()
    rows = await conn.fetch(
        """SELECT DISTINCT ON (lower(author_name)) author_name
             FROM articles
            WHERE lower(author_name) >= $1 AND lower(author_name) < $2
              AND hidden_at IS NULL
            ORDER BY lower(author_name), published_at DESC, id DESC
            LIMIT $3""",
        lowered, _prefix_upper_bound(lowered), limit,
    )
    return tuple(r["author_name"] for r in rows)


async def archive_stats(conn: asyncpg.Connection) -> ArchiveStats:
    """What the archive holds and how far collection has reached.

    The span is two InitPlan limits over articles_list_idx — 8 buffers, 0.105ms
    at 2.8M rows — so it is not the expensive part. The count is; the caller
    caches this whole result.
    """
    row = await conn.fetchrow(
        """SELECT (SELECT count(*) FROM articles WHERE hidden_at IS NULL)      AS articles,
                  (SELECT min(published_at) FROM articles WHERE hidden_at IS NULL) AS oldest,
                  (SELECT max(published_at) FROM articles WHERE hidden_at IS NULL) AS newest,
                  (SELECT next_id FROM crawl_cursor WHERE name = 'backfill')   AS frontier"""
    )
    return ArchiveStats(**dict(row))


async def get_blob(conn: asyncpg.Connection, digest: bytes) -> BlobRow | None:
    row = await conn.fetchrow(
        "SELECT sha256, withheld_at FROM images WHERE sha256 = $1", digest
    )
    return BlobRow(**dict(row)) if row else None


async def fetch_log_status(conn: asyncpg.Connection, article_id: int) -> str | None:
    return await conn.fetchval("SELECT status FROM fetch_log WHERE article_id = $1", article_id)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/db/test_browse_reads.py -v`
Expected: PASS, all eleven.

- [ ] **Step 5: Run the full suite, lint, commit**

```bash
uv run pytest
uv run ruff check src tests
git add src/babel/db/browse.py tests/db/test_browse_reads.py
git commit -m "Add the article, image, country and author read queries

Author suggestions are a range scan rather than LIKE with DISTINCT:
DISTINCT is a blocking aggregate that LIMIT cannot push through, so that
form costs O(matching articles) — measured at 95MB to return eight rows,
and 938MB for a bare '%'. It also keeps LIKE away from user input
entirely, so wildcards need no escaping.

Image counts have four buckets because 'not captured yet' is not 'gone':
the drain runs behind the walk, so pending is the normal state for the
newest articles and total-minus-ok would call them all lost.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Cursor encoding and game-time conversion

**Files:**
- Create: `src/babel/web/__init__.py`, `src/babel/web/cursor.py`
- Test: `tests/web/__init__.py`, `tests/web/test_cursor.py`

**Interfaces:**
- Consumes: `browse.Cursor`, `parser.GAME_TZ`
- Produces:
  - `def encode_cursor(published_at: datetime.datetime, article_id: int) -> str`
  - `def decode_cursor(raw: str | None) -> Cursor | None`
  - `def to_game_time(ts: datetime.datetime) -> datetime.datetime`
  - `def game_date_to_utc(day: datetime.date) -> datetime.datetime`
  - `def parse_game_date(raw: str | None) -> datetime.date | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/web/__init__.py` (empty) and `tests/web/test_cursor.py`:

```python
import datetime

from babel.db.browse import Cursor
from babel.web.cursor import (
    decode_cursor,
    encode_cursor,
    game_date_to_utc,
    parse_game_date,
    to_game_time,
)

UTC = datetime.UTC


def test_cursor_round_trips():
    ts = datetime.datetime(2026, 7, 21, 5, 53, 10, tzinfo=UTC)
    decoded = decode_cursor(encode_cursor(ts, 2797025))
    assert decoded == Cursor(published_at=ts, article_id=2797025)


def test_malformed_cursors_decode_to_none():
    for raw in (None, "", "abc", "123", "-1", "12-", "-12", "1.5-2", "1-2-3", "١٢٣-٤"):
        assert decode_cursor(raw) is None


def test_cursor_survives_a_url_round_trip():
    ts = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    raw = encode_cursor(ts, 5)
    assert "-" in raw
    assert raw.replace("-", "").isdigit()


def test_evening_publication_reads_as_the_previous_game_day():
    # The fixture case: datePublished 2026-07-21 05:53 GMT is game day 6817,
    # 20 July. Rendering the UTC date would put the list a day ahead of the
    # article page it links to.
    ts = datetime.datetime(2026, 7, 21, 5, 53, 10, tzinfo=UTC)
    assert to_game_time(ts).date() == datetime.date(2026, 7, 20)


def test_game_midnight_converts_to_a_utc_instant():
    bound = game_date_to_utc(datetime.date(2026, 7, 20))
    assert bound.tzinfo is not None
    assert bound.astimezone(UTC) == datetime.datetime(2026, 7, 20, 7, 0, tzinfo=UTC)


def test_parse_game_date_rejects_rubbish():
    assert parse_game_date("2026-07-20") == datetime.date(2026, 7, 20)
    assert parse_game_date(None) is None
    assert parse_game_date("") is None
    assert parse_game_date("20/07/2026") is None
    assert parse_game_date("2026-13-40") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/web/test_cursor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'babel.web'`.

- [ ] **Step 3: Implement**

Create `src/babel/web/__init__.py` (empty) and `src/babel/web/cursor.py`:

```python
"""Cursor encoding and the UTC/game-time boundary.

Two clocks are in play, as SPEC.md's "Date conversion" records: published_at is
a genuine UTC instant, while the game — and the e_day printed on the article
page — reckons in America/Los_Angeles. They disagree for the last 7-8 hours of
every game day, so rendering published_at raw would show a list row one day
ahead of the article it links to, on roughly a third of all rows.

Every conversion happens here, in Python, and never in a WHERE clause. Measured
on 300k rows: the row comparison is an Index Cond at 0.062ms, while
`published_at AT TIME ZONE 'America/Los_Angeles' < $1` degrades to a Filter that
removes 296,641 rows.
"""

import datetime
import re

from babel.crawler.parser import GAME_TZ
from babel.db.browse import Cursor

_CURSOR_RE = re.compile(r"^([0-9]{1,19})-([0-9]{1,19})$")


def encode_cursor(published_at: datetime.datetime, article_id: int) -> str:
    """Microseconds since the epoch and the id, both unsigned decimal."""
    micros = int(published_at.timestamp() * 1_000_000)
    return f"{micros}-{article_id}"


def decode_cursor(raw: str | None) -> Cursor | None:
    """The cursor, or None if it is not one.

    None is not an error path: a link shared into a chat and truncated is the
    normal way this arrives, and the route redirects to the unpositioned list
    rather than showing a 400 for something the reader did not do.
    """
    if not raw:
        return None
    match = _CURSOR_RE.match(raw)
    if match is None:
        return None
    micros, article_id = int(match.group(1)), int(match.group(2))
    try:
        published_at = datetime.datetime.fromtimestamp(micros / 1_000_000, tz=datetime.UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return Cursor(published_at=published_at, article_id=article_id)


def to_game_time(ts: datetime.datetime) -> datetime.datetime:
    return ts.astimezone(GAME_TZ)


def game_date_to_utc(day: datetime.date) -> datetime.datetime:
    """Midnight of a game day, as an instant the cursor can use."""
    return datetime.datetime(day.year, day.month, day.day, tzinfo=GAME_TZ)


def parse_game_date(raw: str | None) -> datetime.date | None:
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return None
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/web/test_cursor.py -v`
Expected: PASS. Note `game_date_to_utc(date(2026, 7, 20))` is 07:00 UTC because July is PDT (UTC−7); a January date would be 08:00.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests
git add src/babel/web/__init__.py src/babel/web/cursor.py tests/web/
git commit -m "Convert between UTC and game time in one place

published_at is a real UTC instant and the game reckons in PST, and they
disagree for the last 7-8 hours of every game day. Rendered raw, a list
row reads 21 July while the article it links to reads day 6,817 — 20
July. The conversion stays in Python: in a WHERE clause it turns an
Index Cond at 0.062ms into a Filter that removes 296,641 rows.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Serving type from the bytes

**Files:**
- Create: `src/babel/web/blobs.py`
- Test: `tests/web/test_blobs.py`

**Interfaces:**
- Consumes: `images.image_path`, `images.sniff_image_mime`
- Produces:
  - `SHA256_RE: re.Pattern`
  - `INLINE_TYPES: frozenset[str]`
  - `def parse_digest(hex_digest: str) -> bytes | None`
  - `def serving_type(head: bytes) -> tuple[str, bool]` — `(content_type, inline)`

- [ ] **Step 1: Write the failing tests**

Create `tests/web/test_blobs.py`:

```python
from babel.web.blobs import parse_digest, serving_type

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HTML = b"<!doctype html><html><body>gone</body></html>"


def test_valid_digest_parses():
    assert parse_digest("ab" * 32) == bytes.fromhex("ab" * 32)


def test_traversal_and_junk_are_rejected():
    for raw in ("../../etc/passwd", "AB" * 32, "ab" * 31, "ab" * 33, "", "zz" * 32, "ab/cd"):
        assert parse_digest(raw) is None


def test_png_is_served_inline_as_png():
    assert serving_type(PNG) == ("image/png", True)


def test_jpeg_is_served_inline_even_though_hosts_call_it_image_jpg():
    # images.mime holds the remote host's header verbatim, so 'image/jpg' and
    # 'image/x-png' are both in the column. The bytes are what decide.
    assert serving_type(JPEG) == ("image/jpeg", True)


def test_webp_is_recognised_at_offset_eight():
    assert serving_type(WEBP) == ("image/webp", True)


def test_svg_is_a_download_not_an_inline_document():
    assert serving_type(SVG) == ("application/octet-stream", False)


def test_a_removal_notice_page_is_a_download():
    assert serving_type(HTML) == ("application/octet-stream", False)


def test_empty_bytes_do_not_crash():
    assert serving_type(b"") == ("application/octet-stream", False)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/web/test_blobs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'babel.web.blobs'`.

- [ ] **Step 3: Implement `src/babel/web/blobs.py`**

```python
"""What to send for a stored blob, decided by its bytes.

`images.mime` is never echoed. It holds the third party's Content-Type stored
verbatim — `resolve_mime` returns the declared string unchanged, and
tests/crawler/test_images.py pins exactly that — so the column contains
`image/jpg`, `image/x-png` and `image/jpeg; charset=binary`. Testing those
against a literal allowlist fails genuine JPEGs and PNGs, and stops nothing,
because the string is chosen by whoever we downloaded from: SVG bytes served as
`image/png` are stored as `image/png`. A stored type carrying a non-latin-1
character cannot be re-emitted as an HTTP header at all — it raises
UnicodeEncodeError, an unhandled 500 for that blob on every request. And
save_image_blob is ON CONFLICT DO NOTHING, so a deduplicated blob keeps whatever
the first host declared and one bad host poisons the type for every article
citing those bytes.

This is the rule SPEC.md already states for ingest — "Content-Type is a hint;
the bytes are the evidence" — applied to serving. Deciding here repairs every
existing row with no migration and no re-collection.

`nosniff` and `default-src 'none'; sandbox` are the security controls. The type
list below is a serving decision, not a boundary.
"""

import re

from babel.crawler.images import sniff_image_mime

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# What may be rendered inline. SVG is deliberately absent: it is a document that
# can carry script, and an archived one is a document written by a stranger.
INLINE_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/tiff"}
)

# WebP identifies itself at offset 8-12, so a shorter read would miss it.
HEAD_BYTES = 16


def parse_digest(hex_digest: str) -> bytes | None:
    """The digest as bytes, or None if this is not one.

    Lowercase hex only, fixed length. The on-disk path is built from the value
    this returns, so traversal is not filtered out — it is unrepresentable.
    """
    if not SHA256_RE.match(hex_digest):
        return None
    return bytes.fromhex(hex_digest)


def serving_type(head: bytes) -> tuple[str, bool]:
    """(Content-Type, render inline?) for a blob's leading bytes."""
    sniffed = sniff_image_mime(head)
    if sniffed in INLINE_TYPES:
        return sniffed, True
    return "application/octet-stream", False
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/web/test_blobs.py -v`
Expected: PASS, all eight.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests
git add src/babel/web/blobs.py tests/web/test_blobs.py
git commit -m "Decide an image's served type from its bytes

images.mime is the remote host's Content-Type stored verbatim, so the
column holds image/jpg, image/x-png and image/jpeg; charset=binary — all
of which fail a literal allowlist, turning real JPEGs into downloads.
It stops nothing either, since SVG bytes served as image/png are stored
as image/png. A non-latin-1 character in there is an unhandled 500.

Sniffing at serve time repairs every existing row with no migration.
SVG stays a download: it is a document written by a stranger.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: App skeleton — pool, headers, robots, health, errors

This is where templates first exist, so it is also where `.gitignore` must be fixed: line 39 is `*.html` and would silently drop every template from the repository.

**Files:**
- Create: `src/babel/web/app.py`, `src/babel/web/templates/base.html`, `src/babel/web/templates/error.html`, `src/babel/web/static/style.css`
- Modify: `src/babel/config.py`, `pyproject.toml`, `.gitignore`
- Test: `tests/web/test_app.py`, `tests/web/test_packaging.py`

**Interfaces:**
- Consumes: `Settings`
- Produces:
  - `def create_app(settings: Settings, pool: object | None = None) -> FastAPI` — app with `app.state.pool` and `app.state.templates` (Jinja2Templates). When `pool` is passed it is used as-is and **not** closed on shutdown; the caller owns it. That seam is what lets the tests drive the app against the per-test connection without a second container, and it never weakens production, where `pool` is omitted and `open_pool` runs.
  - `async def open_pool(settings: Settings) -> asyncpg.Pool`
  - `TEMPLATE_DIR: pathlib.Path`, `STATIC_DIR: pathlib.Path`

- [ ] **Step 1: Add the dependencies**

In `pyproject.toml`, add to `[project].dependencies`:

```toml
    "fastapi>=0.115.0",
    "jinja2>=3.1.0",
    "uvicorn>=0.32.0",
```

and to `[dependency-groups].dev`:

```toml
    "httpx>=0.28.0",
```

Run: `uv sync`

- [ ] **Step 2: Fix `.gitignore` before writing a template**

In `.gitignore`, immediately after line 40 (`!tests/fixtures/**/*.html`), add:

```
# Jinja templates are source, and the *.html rule above would silently drop them:
# `git add -A` exits 0 without mentioning the file, the suite still passes because
# pythonpath reads the working tree, and the deploy host raises TemplateNotFound
# on every route. Verified with `git check-ignore -v`.
!src/babel/**/*.html
```

Verify: `git check-ignore -v src/babel/web/templates/base.html`
Expected: no output, exit code 1 (not ignored).

- [ ] **Step 3: Write the failing tests**

Create `tests/web/test_app.py`:

```python
import subprocess

import httpx
import pytest

from babel.config import Settings
from babel.web.app import create_app


@pytest_asyncio.fixture
async def client(pool, image_root):
    """The app driven over ASGI, with the per-test pool injected.

    The DSNs below are never dialled — the injected pool is the database. They
    are still two different strings, because open_pool refuses to start when
    they are equal and that refusal is production behaviour worth not
    accidentally disabling in the fixture.
    """
    settings = Settings(
        database_url="postgresql://babel@unused/babel",
        web_database_url="postgresql://babel_web@unused/babel",
        contact="archive@example.invalid",
        image_root=str(image_root),
    )
    app = create_app(settings, pool=pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


@pytest.fixture
def image_root(tmp_path):
    return tmp_path


async def test_healthz_is_ok(client):
    response = await client.get("/healthz")
    assert response.status_code == 200


async def test_every_response_carries_noindex(client):
    for path in ("/healthz", "/robots.txt", "/"):
        response = await client.get(path)
        assert response.headers["x-robots-tag"] == "noindex, nofollow"


async def test_robots_allows_articles_and_refuses_the_filter_space(client):
    body = (await client.get("/robots.txt")).text
    # Disallow: / would stop a crawler fetching the page, so it would never see
    # the noindex above — the two controls cancel instead of compounding.
    assert "Disallow: /?" in body
    assert "Allow: /" in body
    assert "Disallow: /\n" not in body


async def test_html_pages_carry_a_script_free_csp(client):
    response = await client.get("/")
    csp = response.headers["content-security-policy"]
    assert "script-src 'none'" in csp
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


async def test_unknown_path_is_a_styled_404_not_a_stack_trace(client):
    response = await client.get("/no/such/thing")
    assert response.status_code == 404
    assert "<html" in response.text.lower()
    assert "Traceback" not in response.text
```

Create `tests/web/test_packaging.py`:

```python
import pathlib
import subprocess

from babel.web.app import STATIC_DIR, TEMPLATE_DIR

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_every_template_is_tracked_by_git():
    """.gitignore line 39 is `*.html`.

    Without an explicit negation this passes locally and fails only on the
    deploy host, because the tests read the working tree while the container
    gets what git actually shipped.
    """
    tracked = set(
        subprocess.run(
            ["git", "ls-files", "src/babel/web/templates"],
            cwd=REPO, capture_output=True, text=True, check=True,
        ).stdout.split()
    )
    on_disk = {
        str(p.relative_to(REPO)) for p in TEMPLATE_DIR.glob("*.html")
    }
    assert on_disk
    assert on_disk <= tracked


def test_static_assets_are_tracked_by_git():
    tracked = set(
        subprocess.run(
            ["git", "ls-files", "src/babel/web/static"],
            cwd=REPO, capture_output=True, text=True, check=True,
        ).stdout.split()
    )
    on_disk = {str(p.relative_to(REPO)) for p in STATIC_DIR.glob("*")}
    assert on_disk
    assert on_disk <= tracked
```

- [ ] **Step 4: Run to verify failure**

Run: `uv run pytest tests/web/test_app.py tests/web/test_packaging.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'babel.web.app'`.

- [ ] **Step 5: Add the settings**

In `src/babel/config.py`, add inside `Settings`:

```python
    # The web service connects as a SELECT-only role. There is deliberately no
    # working default: falling back to `database_url` would run the public site
    # as the database owner, and that failure is silent.
    web_database_url: str | None = Field(default=None)

    # Shown in the footer so a player whose article is archived here has
    # somewhere to write. Never hardcoded — this repository is public.
    contact: str | None = Field(default=None)

    web_pool_size: int = Field(default=10, ge=1, le=50)
```

- [ ] **Step 6: Write `src/babel/web/app.py`**

```python
"""The public read-only site.

Deliberately not in gluetun's network namespace: it needs inbound connections,
which that namespace cannot accept, and its only outbound dependency is
Postgres on the bridge. The site therefore stays up when the tunnel is down.
"""

import contextlib
import logging
import pathlib
from collections.abc import AsyncIterator

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from babel.config import Settings

log = logging.getLogger(__name__)

TEMPLATE_DIR = pathlib.Path(__file__).parent / "templates"
STATIC_DIR = pathlib.Path(__file__).parent / "static"

# Article pages are crawlable so the noindex on them is actually seen: a crawler
# blocked by robots.txt never fetches the page and therefore never reads the
# header, which leaves the URL indexable as a bare entry that noindex can never
# remove. The parameterised list space is refused because filter combinations
# are a crawl trap carrying no content of their own.
ROBOTS_TXT = "User-agent: *\nDisallow: /?\nAllow: /\n"

CSP = (
    "default-src 'self'; script-src 'none'; img-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


async def open_pool(settings: Settings) -> asyncpg.Pool:
    """The read-only pool.

    The read-only guarantee is a property of the ROLE, not of this call. asyncpg
    runs `RESET ALL` on every connection release, which returns
    default_transaction_read_only to the role default — measured against
    postgres:17, a pool built with `init=` had acquire #1 blocked and acquire #2
    writing successfully. So `ALTER ROLE babel_web SET
    default_transaction_read_only = on` is the control, and it is an operator
    step because the role's password does not belong in this repository. See
    README.md.
    """
    dsn = settings.web_database_url
    if not dsn:
        raise RuntimeError(
            "WEB_DATABASE_URL is not set. The public site connects as a SELECT-only "
            "role; falling back to DATABASE_URL would run it as the database owner."
        )
    if dsn == settings.database_url:
        raise RuntimeError(
            "WEB_DATABASE_URL equals DATABASE_URL. The public site must not connect "
            "as the crawler's own role. See README.md for the role setup."
        )
    return await asyncpg.create_pool(dsn, min_size=1, max_size=settings.web_pool_size)


def create_app(settings: Settings, pool: object | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # An injected pool belongs to the caller: used as-is, never closed here.
        # That is the seam the tests drive the app through, and it leaves the
        # production path — pool omitted, open_pool runs — exactly as strict.
        if pool is not None:
            app.state.pool = pool
            yield
            return
        app.state.pool = await open_pool(settings)
        try:
            yield
        finally:
            await app.state.pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault("Content-Security-Policy", CSP)
        return response

    @app.get("/robots.txt", response_class=PlainTextResponse)
    async def robots() -> str:
        return ROBOTS_TXT

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok"

    def render_error(request: Request, status: int, heading: str, detail: str) -> HTMLResponse:
        return app.state.templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"heading": heading, "detail": detail, "settings": settings},
            status_code=status,
        )

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Exception) -> HTMLResponse:
        return render_error(request, 404, "Not here", "There is no such page in this archive.")

    @app.exception_handler(asyncpg.PostgresError)
    @app.exception_handler(OSError)
    async def database_down(request: Request, exc: Exception) -> HTMLResponse:
        # A traceback on a public page tells a stranger about the schema. The log
        # gets the detail; the reader gets a sentence.
        log.exception("database error serving %s", request.url.path)
        return render_error(
            request, 503, "The archive is unavailable",
            "The database is not answering. This is usually brief.",
        )

    from babel.web.routes import register_routes  # noqa: PLC0415 — avoids a cycle

    register_routes(app)
    return app
```

`register_routes` gains its real routes in Task 10. Keep the import and the call above exactly as written, and create the stub it resolves to, `src/babel/web/routes.py`, now — the tests in this task exercise `/healthz`, `/robots.txt` and the 404 handler, none of which live in `routes.py`:

```python
"""Route handlers. Filled in by the next task."""

from fastapi import FastAPI


def register_routes(app: FastAPI) -> None:
    """Attach the list, article and image routes."""
```

- [ ] **Step 7: Write the templates**

`src/babel/web/templates/base.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow">
  <title>{% block title %}babel{% endblock %}</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header><a class="home" href="/">babel</a> <span class="tag">eRepublik article archive</span></header>
  <main>{% block content %}{% endblock %}</main>
  <footer>
    <p>An archive of articles published on eRepublik, kept because the originals
       are deleted and the images they link to stop resolving. Every article is
       the work of its author and links back to the original.</p>
    {% if settings.contact %}<p>Wrote something here and want it removed? {{ settings.contact }}</p>{% endif %}
  </footer>
</body>
</html>
```

`src/babel/web/templates/error.html`:

```html
{% extends "base.html" %}
{% block title %}{{ heading }} — babel{% endblock %}
{% block content %}
<h1>{{ heading }}</h1>
<p>{{ detail }}</p>
<p><a href="/">Back to the list</a></p>
{% endblock %}
```

`src/babel/web/static/style.css`: a minimal readable sheet — a max-width column around 46rem, system font stack, `@media (prefers-color-scheme: dark)` colours, and `.body-text { white-space: pre-wrap; }`. Keep it under 120 lines; it is not the deliverable.

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/web/ -v`
Expected: PASS. Put the `client` and `image_root` fixtures above in `tests/web/conftest.py`; they build on the `pool` fixture Task 4 added to `tests/conftest.py`. Import `pytest_asyncio` there — `client` is an async fixture and needs `@pytest_asyncio.fixture`, not `@pytest.fixture`.

- [ ] **Step 9: Run the full suite, lint, commit**

```bash
uv run pytest
uv run ruff check src tests
git add pyproject.toml uv.lock .gitignore src/babel/config.py src/babel/web/ tests/web/
git commit -m "Add the web app skeleton, and stop gitignore eating the templates

.gitignore line 39 is *.html, so every Jinja template would have been
silently untracked: git add -A exits 0 without mentioning them, the suite
passes because pythonpath reads the working tree, and the deploy host
raises TemplateNotFound on every route. A test asserts the templates are
tracked, which is the only place that failure is visible before deploy.

robots.txt allows article pages on purpose. Disallow: / would stop a
crawler fetching the page, so it would never read the noindex header —
the two controls cancel rather than compound.

WEB_DATABASE_URL has no default and refuses to equal DATABASE_URL: the
public site must not hold the database owner's role.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: The list page

**Files:**
- Modify: `src/babel/web/routes.py`
- Create: `src/babel/web/templates/list.html`
- Test: `tests/web/test_list_page.py`

**Interfaces:**
- Consumes: `browse.list_articles`, `browse.list_countries`, `browse.suggest_authors`, `browse.archive_stats`, `cursor.*`
- Produces: `def register_routes(app: FastAPI) -> None` attaching `GET /`

- [ ] **Step 1: Write the failing tests**

Create `tests/web/test_list_page.py`:

```python
import datetime
import urllib.parse

import pytest

UTC = datetime.UTC


async def _seed(pool, rows):
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO articles (id, title, body, author_name, country,
                                     published_at, comment_count)
               VALUES ($1, $2, 'Body', $3, $4, $5, 0)
               ON CONFLICT (id) DO NOTHING""",
            rows,
        )


async def test_list_shows_titles_newest_first(client, pool):
    await _seed(pool, [
        (10, "Older", "ann", "Poland", datetime.datetime(2026, 1, 1, tzinfo=UTC)),
        (11, "Newer", "ann", "Poland", datetime.datetime(2026, 1, 2, tzinfo=UTC)),
    ])
    body = (await client.get("/")).text
    assert body.index("Newer") < body.index("Older")


async def test_country_filter_narrows_the_list(client, pool):
    await _seed(pool, [
        (20, "PL", "ann", "Poland", datetime.datetime(2026, 2, 1, tzinfo=UTC)),
        (21, "RS", "ann", "Serbia", datetime.datetime(2026, 2, 2, tzinfo=UTC)),
    ])
    body = (await client.get("/", params={"country": "Poland"})).text
    assert "PL" in body
    assert "RS" not in body


async def test_dates_render_in_game_time(client, pool):
    # 2026-07-21 05:53 UTC is game day 20 July. A UTC render would say 21.
    await _seed(pool, [
        (30, "Evening", "ann", "Bulgaria", datetime.datetime(2026, 7, 21, 5, 53, tzinfo=UTC)),
    ])
    body = (await client.get("/")).text
    assert "2026-07-20" in body
    assert "2026-07-21" not in body


async def test_coverage_line_states_the_span_and_the_frontier(client, pool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crawl_cursor (name, next_id) VALUES ('backfill', 2785850) "
            "ON CONFLICT (name) DO UPDATE SET next_id = 2785850"
        )
    body = (await client.get("/")).text
    assert "2 785 850" in body or "2785850" in body
    assert "not in the archive yet" in body


async def test_author_link_round_trips_a_hostile_name(client, pool):
    hostile = 'a"><img src=x onerror=alert(1)>#&+ b'
    await _seed(pool, [
        (40, "Hostile", hostile, "Poland", datetime.datetime(2026, 3, 1, tzinfo=UTC)),
    ])
    page = (await client.get("/")).text
    assert '"><img src=x' not in page          # no attribute break
    assert "onerror=alert(1)" not in page or "&lt;img" in page

    # The generated link must actually filter to that author. |e alone would
    # truncate at the '#' and silently return the unfiltered list.
    expected = "/?author=" + urllib.parse.quote(hostile, safe="")
    assert expected in page.replace("&amp;", "&")
    filtered = (await client.get("/", params={"author": hostile})).text
    assert "Hostile" in filtered


async def test_unknown_author_offers_prefix_suggestions(client, pool):
    await _seed(pool, [
        (50, "A", "annabelle", "Poland", datetime.datetime(2026, 4, 1, tzinfo=UTC)),
    ])
    body = (await client.get("/", params={"author": "anna"})).text
    assert "annabelle" in body


async def test_malformed_cursor_redirects_rather_than_erroring(client):
    response = await client.get("/", params={"after": "not-a-cursor"}, follow_redirects=False)
    assert response.status_code == 302
    assert "after" not in response.headers["location"]


async def test_pager_link_appears_only_when_there_is_another_page(client, pool):
    await _seed(pool, [
        (60 + i, f"P{i}", "ann", "Poland",
         datetime.datetime(2026, 5, 1, tzinfo=UTC) + datetime.timedelta(seconds=i))
        for i in range(3)
    ])
    body = (await client.get("/")).text
    assert "after=" not in body  # three rows, page size 50


async def test_empty_result_explains_coverage_instead_of_showing_nothing(client):
    body = (await client.get("/", params={"country": "Nowhere"})).text
    assert "not in the archive yet" in body
```

Add the `client` and `pool` fixtures to `tests/web/conftest.py`, reusing the app fixture from Task 9 and the database fixture from `tests/conftest.py`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/web/test_list_page.py -v`
Expected: FAIL — 404 for `/`, since `register_routes` is still a stub.

- [ ] **Step 3: Implement the route**

Replace `src/babel/web/routes.py`:

```python
"""Route handlers. No SQL here — everything comes from babel.db.browse."""

import datetime
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from babel.db import browse
from babel.web.cursor import (
    decode_cursor,
    encode_cursor,
    game_date_to_utc,
    parse_game_date,
    to_game_time,
)

_STATS_TTL_SEC = 300


async def _stats(app, conn) -> browse.ArchiveStats:
    """Archive-wide totals, on a five-minute clock.

    The span is two index-only limits and costs nothing; the count is a heap
    scan at 2.8M rows, which is why this is cached rather than computed per
    request.

    The cache lives on app.state, not in a module global. A module global
    outlives the app that filled it, and the tests build one app per test
    against a fresh database — the second test would read the first one's
    numbers. It is also simply the truthful scope: the cache belongs to a
    running service, not to an imported module.
    """
    cached: tuple[float, browse.ArchiveStats] | None = getattr(app.state, "stats_cache", None)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _STATS_TTL_SEC:
        return cached[1]
    stats = await browse.archive_stats(conn)
    app.state.stats_cache = (now, stats)
    return stats


def register_routes(app: FastAPI) -> None:
    templates = app.state.templates

    @app.get("/", response_class=HTMLResponse)
    async def index(  # noqa: PLR0913 — these are the query parameters, not a signature to shrink
        request: Request,
        country: str | None = None,
        author: str | None = None,
        order: str = "new",
        after: str | None = None,
        before: str | None = None,
        on: str | None = None,
    ):
        if order not in ("new", "old"):
            order = "new"

        # A truncated link pasted from a chat is the normal way a bad cursor
        # arrives, so it redirects rather than showing the reader a 400 for
        # something they did not do.
        raw_cursor = after or before
        cursor = decode_cursor(raw_cursor)
        if raw_cursor and cursor is None:
            return RedirectResponse(request.url.remove_query_params(["after", "before"]), 302)

        jump = parse_game_date(on)
        if jump is not None:
            cursor = browse.Cursor(published_at=game_date_to_utc(jump), article_id=0)

        going = "prev" if before else "next"
        filters = browse.ListFilters(country=country or None, author=author or None)

        async with app.state.pool.acquire() as conn:
            page = await browse.list_articles(
                conn, filters, order=order, cursor=cursor, going=going
            )
            countries = await browse.list_countries(conn)
            stats = await _stats(app, conn)
            suggestions: tuple[str, ...] = ()
            if filters.author and not page.rows:
                suggestions = await browse.suggest_authors(conn, filters.author)

        next_cursor = (
            encode_cursor(page.rows[-1].published_at, page.rows[-1].id)
            if page.rows and page.has_more
            else None
        )
        prev_cursor = (
            encode_cursor(page.rows[0].published_at, page.rows[0].id)
            if page.rows and (cursor is not None)
            else None
        )

        return templates.TemplateResponse(
            request=request,
            name="list.html",
            context={
                "rows": page.rows,
                "countries": countries,
                "stats": stats,
                "filters": filters,
                "order": order,
                "suggestions": suggestions,
                "next_cursor": next_cursor,
                "prev_cursor": prev_cursor,
                "game_time": to_game_time,
                "settings": app.state.settings,
            },
            headers={"Cache-Control": "public, max-age=300"},
        )
```

Note on `prev_cursor`: going back from an unpositioned first page is meaningless, hence the `cursor is not None` guard.

- [ ] **Step 4: Write `src/babel/web/templates/list.html`**

```html
{% extends "base.html" %}
{% block title %}babel — eRepublik article archive{% endblock %}
{% block content %}

<p class="coverage">
  {{ "{:,}".format(stats.articles).replace(",", " ") }} articles so far{% if stats.oldest %},
  covering {{ game_time(stats.oldest).strftime("%Y-%m-%d") }} –
  {{ game_time(stats.newest).strftime("%Y-%m-%d") }}{% endif %}.
  {% if stats.frontier %}Collection walks backwards through article IDs and has reached
  {{ "{:,}".format(stats.frontier).replace(",", " ") }}; anything published earlier is
  not in the archive yet.{% endif %}
</p>

<form class="filters" method="get" action="/">
  <label>Country
    <select name="country">
      <option value="">any</option>
      {% for c in countries %}
      <option value="{{ c }}"{% if c == filters.country %} selected{% endif %}>{{ c }}</option>
      {% endfor %}
    </select>
  </label>
  <label>Author <input type="text" name="author" value="{{ filters.author or '' }}"></label>
  <label>Order
    <select name="order">
      <option value="new"{% if order == 'new' %} selected{% endif %}>newest first</option>
      <option value="old"{% if order == 'old' %} selected{% endif %}>oldest first</option>
    </select>
  </label>
  <label>Jump to
    <input type="date" name="on"
           {% if stats.oldest %}min="{{ game_time(stats.oldest).strftime('%Y-%m-%d') }}"
           max="{{ game_time(stats.newest).strftime('%Y-%m-%d') }}"{% endif %}>
  </label>
  <button type="submit">Apply</button>
</form>

{% if rows %}
<ol class="articles">
  {% for row in rows %}
  <li>
    <time>{{ game_time(row.published_at).strftime("%Y-%m-%d") }}</time>
    <a class="title" href="/article/{{ row.id }}">{{ row.title }}</a>
    {% if row.author_name %}
    <a class="author" href="/?author={{ row.author_name|urlencode }}">{{ row.author_name }}</a>
    {% endif %}
    {% if row.country %}
    <a class="country" href="/?country={{ row.country|urlencode }}">{{ row.country }}</a>
    {% endif %}
    <span class="comments">{{ row.comment_count }} comments</span>
  </li>
  {% endfor %}
</ol>

<nav class="pager">
  {% if prev_cursor %}<a href="/?before={{ prev_cursor|urlencode }}{% if filters.country %}&country={{ filters.country|urlencode }}{% endif %}{% if filters.author %}&author={{ filters.author|urlencode }}{% endif %}&order={{ order }}">← newer</a>{% endif %}
  {% if next_cursor %}<a href="/?after={{ next_cursor|urlencode }}{% if filters.country %}&country={{ filters.country|urlencode }}{% endif %}{% if filters.author %}&author={{ filters.author|urlencode }}{% endif %}&order={{ order }}">older →</a>{% endif %}
</nav>
{% else %}
<p class="empty">Nothing here.
  {% if stats.frontier %}Collection has reached article
  {{ "{:,}".format(stats.frontier).replace(",", " ") }}; anything published earlier is
  not in the archive yet.{% endif %}</p>
{% endif %}

{% if suggestions %}
<p class="suggestions">Did you mean:
  {% for name in suggestions %}
  <a href="/?author={{ name|urlencode }}">{{ name }}</a>{% if not loop.last %}, {% endif %}
  {% endfor %}
</p>
{% endif %}

{% if not next_cursor and rows %}
<p class="coverage end">That is everything collected so far.
  {% if stats.frontier %}Collection has reached article
  {{ "{:,}".format(stats.frontier).replace(",", " ") }} and is still walking backwards.{% endif %}</p>
{% endif %}

{% endblock %}
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/web/test_list_page.py -v`
Expected: PASS, all nine.

- [ ] **Step 6: Run the full suite, lint, commit**

```bash
uv run pytest
uv run ruff check src tests
git add src/babel/web/routes.py src/babel/web/templates/list.html tests/web/
git commit -m "Add the list page

Author and country links are urlencoded, not html-escaped: measured
against 2.15M real citizen names, 807 contain &, #, % or +, and an author
named #1PAKI yields href=\"/?author=#1PAKI\" with |e alone — the browser
drops the fragment and the archive's own link returns everything.

Dates render in game time, and the page states its own coverage: for the
length of the backfill this is a recent slice of a 19-year corpus, and
'oldest first' landing in 2024 needs to say why.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11: The article page and the typed 404

**Files:**
- Modify: `src/babel/web/routes.py`
- Create: `src/babel/web/templates/article.html`
- Test: `tests/web/test_article_page.py`

**Interfaces:**
- Consumes: `browse.get_article`, `browse.get_comments`, `browse.get_ok_image_digests`, `browse.image_status_counts`, `browse.fetch_log_status`, `repo.get_cursor`
- Produces: `GET /article/{article_id}`

- [ ] **Step 1: Write the failing tests**

Create `tests/web/test_article_page.py`:

```python
import datetime

UTC = datetime.UTC


async def _article(pool, article_id, **kw):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO articles (id, title, body, author_name, country,
                                     published_at, e_day, comment_count)
               VALUES ($1, $2, $3, 'ann', 'Poland',
                       timestamptz '2026-07-21 05:53Z', 6817, $4)
               ON CONFLICT (id) DO NOTHING""",
            article_id, kw.get("title", "A title"), kw.get("body", "Line one.\nLine two."),
            kw.get("comment_count", 0),
        )


async def test_article_renders_title_body_and_game_date(client, pool):
    await _article(pool, 2000)
    body = (await client.get("/article/2000")).text
    assert "A title" in body
    assert "Line one." in body
    assert "2026-07-20" in body          # game day, not the UTC 21st
    assert "6,817" in body or "6817" in body


async def test_script_in_stored_text_is_escaped(client, pool):
    await _article(pool, 2001, title="<script>alert(1)</script>", body="<script>alert(2)</script>")
    body = (await client.get("/article/2001")).text
    assert "<script>alert(1)</script>" not in body
    assert "<script>alert(2)</script>" not in body
    assert "&lt;script&gt;" in body


async def test_comments_render_with_depth_and_removed_markers(client, pool):
    await _article(pool, 2002, comment_count=2)
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO comments (id, article_id, position, depth, author_name, body)
               VALUES ($1, 2002, $2, $3, 'bob', $4)""",
            [(31, 1, 0, "hello"), (32, 2, 1, None)],
        )
    body = (await client.get("/article/2002")).text
    assert "hello" in body
    assert "[removed]" in body


async def test_only_dead_images_are_reported_as_gone(client, pool):
    await _article(pool, 2003)
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO article_images (article_id, position, source_url, status, attempts)
               VALUES (2003, $1, $2, $3, $4)""",
            [(1, "https://h/1.png", "pending", 0),
             (2, "https://h/2.png", "pending", 0),
             (3, "https://h/3.png", "dead", 1)],
        )
    body = (await client.get("/article/2003")).text
    assert "1 image was already gone" in body
    assert "2 not captured yet" in body
    assert "3 of 3" not in body


async def test_a_fresh_article_is_never_called_lost(client, pool):
    await _article(pool, 2004)
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO article_images (article_id, position, source_url, status, attempts)
               VALUES (2004, $1, $2, 'pending', 0)""",
            [(i, f"https://h/{i}.png") for i in range(1, 7)],
        )
    body = (await client.get("/article/2004")).text
    assert "already gone" not in body
    assert "6 not captured yet" in body


async def test_404_says_deleted_upstream(client, pool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO fetch_log (article_id, status) VALUES (2100, 'missing') "
            "ON CONFLICT (article_id) DO UPDATE SET status = 'missing'"
        )
    response = await client.get("/article/2100")
    assert response.status_code == 404
    assert "already deleted" in response.text


async def test_404_says_collection_failed(client, pool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO fetch_log (article_id, status) VALUES (2101, 'error') "
            "ON CONFLICT (article_id) DO UPDATE SET status = 'error'"
        )
    assert "will try again" in (await client.get("/article/2101")).text


async def test_404_says_not_collected_yet_and_names_the_frontier(client, pool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crawl_cursor (name, next_id) VALUES ('backfill', 2785850) "
            "ON CONFLICT (name) DO UPDATE SET next_id = 2785850"
        )
    text = (await client.get("/article/2102")).text
    assert "not collected yet" in text
    assert "2 785 850" in text or "2785850" in text


async def test_hidden_article_falls_through_to_the_404(client, pool):
    await _article(pool, 2103)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE articles SET hidden_at = now() WHERE id = 2103")
    assert (await client.get("/article/2103")).status_code == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/web/test_article_page.py -v`
Expected: FAIL — 404 with the generic message for every case.

- [ ] **Step 3: Implement the route**

Add to `register_routes` in `src/babel/web/routes.py`:

```python
    @app.get("/article/{article_id}", response_class=HTMLResponse)
    async def article(request: Request, article_id: int):
        async with app.state.pool.acquire() as conn:
            detail = await browse.get_article(conn, article_id)
            if detail is None:
                status = await browse.fetch_log_status(conn, article_id)
                stats = await _stats(app, conn)
                return templates.TemplateResponse(
                    request=request,
                    name="error.html",
                    context={
                        "heading": "Not in the archive",
                        "detail": _missing_detail(status, stats.frontier),
                        "settings": app.state.settings,
                    },
                    status_code=404,
                )
            comments = await browse.get_comments(conn, article_id)
            digests = await browse.get_ok_image_digests(conn, article_id)
            counts = await browse.image_status_counts(conn, article_id)

        return templates.TemplateResponse(
            request=request,
            name="article.html",
            context={
                "article": detail,
                "comments": comments,
                "digests": [d.hex() for d in digests],
                "counts": counts,
                "game_time": to_game_time,
                "settings": app.state.settings,
            },
            headers={"Cache-Control": "public, max-age=300"},
        )
```

And, at module level:

```python
def _missing_detail(status: str | None, frontier: int | None) -> str:
    """Why this article is not here — three different facts a bare 404 flattens into one."""
    if status == "missing":
        return "This article had already been deleted when the crawler reached it."
    if status in ("error", "stale"):
        return "Collecting this article failed. The crawler will try again."
    reached = f" Collection has reached article {frontier:,}.".replace(",", " ") if frontier else ""
    return f"This article has not been collected yet.{reached}"
```

- [ ] **Step 4: Write `src/babel/web/templates/article.html`**

```html
{% extends "base.html" %}
{% block title %}{{ article.title }} — babel{% endblock %}
{% block content %}

<article>
  <h1>{{ article.title }}</h1>
  <p class="meta">
    <time>{{ game_time(article.published_at).strftime("%Y-%m-%d %H:%M") }} game time</time>
    {% if article.e_day %}· day {{ "{:,}".format(article.e_day) }}{% endif %}
    {% if article.author_name %}
    · <a href="/?author={{ article.author_name|urlencode }}">{{ article.author_name }}</a>
    {% endif %}
    {% if article.country %}
    · <a href="/?country={{ article.country|urlencode }}">{{ article.country }}</a>
    {% endif %}
    · <a rel="nofollow noreferrer"
         href="https://www.erepublik.com/en/article/{{ article.id }}">original</a>
  </p>

  <div class="body-text">{{ article.body }}</div>

  {% if digests %}
  <div class="gallery">
    {% for hex in digests %}<img src="/img/{{ hex }}" alt="" loading="lazy">{% endfor %}
  </div>
  {% endif %}

  <p class="images-note">
    {% if counts.dead %}{{ counts.dead }} image{{ "s were" if counts.dead != 1 else " was" }}
      already gone when we looked.{% endif %}
    {% if counts.waiting %}{{ counts.waiting }} not captured yet.{% endif %}
    {% if counts.exhausted %}{{ counts.exhausted }} we could not retrieve.{% endif %}
  </p>
</article>

{% if comments %}
<section class="comments">
  <h2>{{ comments|length }} comments</h2>
  {% for c in comments %}
  <div class="comment" style="margin-left: {{ c.depth * 1.5 }}rem">
    <p class="meta">{{ c.author_name or "someone" }}{% if c.posted_at %} ·
      {{ game_time(c.posted_at).strftime("%Y-%m-%d %H:%M") }}{% endif %}</p>
    <p class="body-text">{% if c.body is none %}[removed]{% else %}{{ c.body }}{% endif %}</p>
  </div>
  {% endfor %}
</section>
{% endif %}

{% endblock %}
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/web/test_article_page.py -v`
Expected: PASS, all nine. If the image-note wording assertions fail on singular/plural, fix the template rather than the test — the test spells the reader-facing sentence.

- [ ] **Step 6: Run the full suite, lint, commit**

```bash
uv run pytest
uv run ruff check src tests
git add src/babel/web/routes.py src/babel/web/templates/article.html tests/web/test_article_page.py
git commit -m "Add the article page and a 404 that says which kind

Only 'dead' images are reported as gone. The drain runs behind the walk,
so a freshly collected article has six pending rows and no ok rows, and
a total-minus-ok count would print '6 of 6 images were already gone' over
an empty gallery — the inverse of the truth, on the newest articles,
about the one thing this project claims about itself.

The 404 reads fetch_log: already deleted upstream, collection failed, or
not reached yet with the frontier named. Three facts a bare 404 flattens
into one.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 12: The image route

**Files:**
- Modify: `src/babel/web/routes.py`
- Test: `tests/web/test_image_route.py`

**Interfaces:**
- Consumes: `blobs.parse_digest`, `blobs.serving_type`, `blobs.HEAD_BYTES`, `images.image_path`, `browse.get_blob`
- Produces: `GET /img/{hex_digest}`

- [ ] **Step 1: Write the failing tests**

Create `tests/web/test_image_route.py`:

```python
import hashlib

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


async def _store(pool, image_root, data, *, declared_mime, withheld=False):
    from babel.crawler.images import store_bytes

    digest = store_bytes(image_root, data)
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO images (sha256, mime, bytes, withheld_at)
               VALUES ($1, $2, $3, CASE WHEN $4 THEN now() END)
               ON CONFLICT (sha256) DO UPDATE SET mime = EXCLUDED.mime,
                                                  withheld_at = EXCLUDED.withheld_at""",
            digest, declared_mime, len(data), withheld,
        )
    return digest.hex()


async def test_png_declared_as_image_jpg_is_still_served_as_png(client, pool, image_root):
    # The declaration is the remote host's and is never echoed.
    hex_digest = await _store(pool, image_root, PNG, declared_mime="image/jpg")
    response = await client.get(f"/img/{hex_digest}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert "content-disposition" not in response.headers


async def test_svg_declared_as_png_is_a_download(client, pool, image_root):
    hex_digest = await _store(pool, image_root, SVG, declared_mime="image/png")
    response = await client.get(f"/img/{hex_digest}")
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert "attachment" in response.headers["content-disposition"]


async def test_non_latin1_declared_mime_does_not_500(client, pool, image_root):
    hex_digest = await _store(pool, image_root, PNG, declared_mime="image/png;charset=€")
    response = await client.get(f"/img/{hex_digest}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")


async def test_image_responses_are_sandboxed(client, pool, image_root):
    hex_digest = await _store(pool, image_root, PNG, declared_mime="image/png")
    response = await client.get(f"/img/{hex_digest}")
    assert response.headers["x-content-type-options"] == "nosniff"
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "sandbox" in csp


async def test_traversal_and_junk_are_404(client):
    for raw in ("../../etc/passwd", "AB" * 32, "ab" * 31, "zz" * 32):
        assert (await client.get(f"/img/{raw}")).status_code == 404


async def test_missing_file_is_404_not_500(client, pool, image_root):
    digest = hashlib.sha256(b"never written").digest()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO images (sha256, mime, bytes) VALUES ($1,'image/png',5) "
            "ON CONFLICT (sha256) DO NOTHING",
            digest,
        )
    assert (await client.get(f"/img/{digest.hex()}")).status_code == 404


async def test_withheld_blob_is_404(client, pool, image_root):
    hex_digest = await _store(pool, image_root, PNG, declared_mime="image/png", withheld=True)
    assert (await client.get(f"/img/{hex_digest}")).status_code == 404


async def test_cache_is_revalidatable_not_immutable(client, pool, image_root):
    # Content addressing would justify immutable, but this content is other
    # people's and has to stay retractable.
    hex_digest = await _store(pool, image_root, PNG, declared_mime="image/png")
    cache = (await client.get(f"/img/{hex_digest}")).headers["cache-control"]
    assert "immutable" not in cache
    assert "must-revalidate" in cache
```

The `image_root` fixture already exists in `tests/web/conftest.py` from Task 9, and the `client` fixture there already passes it as `Settings(image_root=...)`. Nothing new to add — these tests just request both.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/web/test_image_route.py -v`
Expected: FAIL — 404 on every request, `/img/{...}` is not registered.

- [ ] **Step 3: Implement**

Add to the imports in `src/babel/web/routes.py`:

```python
import pathlib

from fastapi.responses import Response

from babel.crawler.images import image_path
from babel.web.blobs import HEAD_BYTES, parse_digest, serving_type
```

And to `register_routes`:

```python
    @app.get("/img/{hex_digest}")
    async def image(hex_digest: str):
        digest = parse_digest(hex_digest)
        if digest is None:
            raise HTTPException(status_code=404)

        async with app.state.pool.acquire() as conn:
            blob = await browse.get_blob(conn, digest)
        if blob is None or blob.withheld_at is not None:
            raise HTTPException(status_code=404)

        path = image_path(pathlib.Path(app.state.settings.image_root), digest)
        try:
            data = path.read_bytes()
        except OSError:
            # The row says we have it and the disk says otherwise — IMAGE_ROOT
            # moved, or the volume is not mounted. Name the blob in the log; the
            # reader gets a missing image, not a 500.
            log.warning("blob %s recorded but not readable at %s", hex_digest, path)
            raise HTTPException(status_code=404) from None

        content_type, inline = serving_type(data[:HEAD_BYTES])
        headers = {
            # Not immutable: content addressing would justify it, but this is
            # other people's content and withholding has to be able to reach it.
            "Cache-Control": "public, max-age=86400, must-revalidate",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        }
        if not inline:
            headers["Content-Disposition"] = f'attachment; filename="{hex_digest}"'
        return Response(content=data, media_type=content_type, headers=headers)
```

Add `from fastapi import HTTPException` and `import logging` / `log = logging.getLogger(__name__)` to the module if not already present.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/web/test_image_route.py -v`
Expected: PASS, all eight.

- [ ] **Step 5: Run the full suite, lint, commit**

```bash
uv run pytest
uv run ruff check src tests
git add src/babel/web/routes.py tests/web/test_image_route.py
git commit -m "Serve stored image blobs

The digest is parsed before it becomes a path, so traversal is
unrepresentable rather than filtered. The type comes from the bytes, so
a PNG a host called image/jpg still renders and an SVG a host called
image/png still downloads. Every response is sandboxed and nosniffed;
those are the controls, not the type list.

Cache is revalidatable rather than immutable: content addressing would
justify immutable, but this is other people's content and a withheld
blob has to be able to disappear.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 13: `babel serve`, `babel hide`, and the deploy

**Files:**
- Modify: `src/babel/cli.py`, `docker-compose.yml`, `.env.example`, `README.md`, `CLAUDE.md`
- Create: `.dockerignore`
- Test: `tests/web/test_cli_serve.py`, `tests/db/test_hide.py`

**Interfaces:**
- Consumes: `create_app`, `browse`
- Produces: `babel serve`, `babel hide --article <id> | --image <sha256>`; `repo.hide_article`, `repo.withhold_image`

- [ ] **Step 1: Write the failing tests**

Create `tests/db/test_hide.py`:

```python
from babel.db.repo import hide_article, withhold_image


async def test_hide_article_sets_the_tombstone(pool):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO articles (id, title, body, published_at, comment_count)
               VALUES (3000, 'T', 'B', now(), 0) ON CONFLICT (id) DO NOTHING"""
        )
        assert await hide_article(conn, 3000) == 1
        assert await conn.fetchval("SELECT hidden_at FROM articles WHERE id = 3000") is not None


async def test_hiding_twice_is_idempotent(pool):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO articles (id, title, body, published_at, comment_count)
               VALUES (3001, 'T', 'B', now(), 0) ON CONFLICT (id) DO NOTHING"""
        )
        await hide_article(conn, 3001)
        first = await conn.fetchval("SELECT hidden_at FROM articles WHERE id = 3001")
        await hide_article(conn, 3001)
        assert await conn.fetchval("SELECT hidden_at FROM articles WHERE id = 3001") == first


async def test_withhold_image_reports_how_many_articles_cite_it(pool):
    digest = b"\x09" * 32
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO images (sha256, mime, bytes) VALUES ($1,'image/png',1) "
            "ON CONFLICT (sha256) DO NOTHING", digest,
        )
        for article_id in (3100, 3101):
            await conn.execute(
                """INSERT INTO articles (id, title, body, published_at, comment_count)
                   VALUES ($1, 'T', 'B', now(), 0) ON CONFLICT (id) DO NOTHING""",
                article_id,
            )
            await conn.execute(
                """INSERT INTO article_images (article_id, position, source_url, status, sha256)
                   VALUES ($1, 1, 'https://h/a.png', 'ok', $2)
                   ON CONFLICT (article_id, source_url) DO NOTHING""",
                article_id, digest,
            )
        citing = await withhold_image(conn, digest)
    assert citing == 2
```

Create `tests/web/test_cli_serve.py`:

```python
import pytest

from babel.config import Settings
from babel.web.app import open_pool


DSN = "postgresql://babel:babel@db:5432/babel"


async def test_serve_refuses_to_run_as_the_owner_role():
    # No database needed: both refusals raise before any connection is opened.
    settings = Settings(database_url=DSN, web_database_url=DSN)
    with pytest.raises(RuntimeError, match="must not connect"):
        await open_pool(settings)


async def test_serve_refuses_without_a_web_dsn():
    settings = Settings(database_url=DSN, web_database_url=None)
    with pytest.raises(RuntimeError, match="WEB_DATABASE_URL is not set"):
        await open_pool(settings)


def test_serve_does_not_apply_migrations():
    """A read-only role cannot CREATE TABLE schema_migrations, and both existing
    long-running commands call apply_migrations at startup. A third written by
    symmetry would crash-loop under restart: unless-stopped."""
    import inspect

    from babel import cli

    source = inspect.getsource(cli._serve)
    assert "apply_migrations" not in source
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/db/test_hide.py tests/web/test_cli_serve.py -v`
Expected: FAIL — `ImportError: cannot import name 'hide_article'`.

- [ ] **Step 3: Add the repository functions**

Append to `src/babel/db/repo.py`:

```python
async def hide_article(conn: asyncpg.Connection, article_id: int) -> int:
    """Suppress an article from the public site. Returns rows changed.

    A tombstone rather than a DELETE, and the difference is not stylistic.
    Measured on a fresh database: DELETE FROM articles cascades comments and
    article_images, but the images row and its blob survive (the FK runs
    article_images.sha256 -> images, not the reverse), and fetch_log has no FK to
    articles at all, so its row stays 'ok'. `babel refetch` then flips it to
    'stale', the sweep re-collects it, and the taken-down article comes back.
    Deletion is silently reversible by tooling this project already ships.

    Idempotent: an already-hidden row keeps its original timestamp, so re-running
    the command does not rewrite when the request arrived.
    """
    result = await conn.execute(
        "UPDATE articles SET hidden_at = now() WHERE id = $1 AND hidden_at IS NULL",
        article_id,
    )
    return int(result.split()[-1])


async def withhold_image(conn: asyncpg.Connection, digest: bytes) -> int:
    """Stop serving one blob. Returns how many articles cite it.

    The count is the point. Content addressing means a blob is shared — flags,
    avatars and recycled memes recur across thousands of articles — so
    withholding is never a single-article act, and the operator has to see the
    blast radius before deciding.
    """
    await conn.execute(
        "UPDATE images SET withheld_at = now() WHERE sha256 = $1 AND withheld_at IS NULL",
        digest,
    )
    return await conn.fetchval(
        "SELECT count(DISTINCT article_id) FROM article_images WHERE sha256 = $1", digest
    )
```

- [ ] **Step 4: Add the CLI commands**

Append to `src/babel/cli.py`:

```python
@main.command()
def serve() -> None:
    """Run the public read-only web archive."""
    asyncio.run(_serve())


async def _serve() -> None:
    import uvicorn

    from babel.web.app import create_app

    settings = Settings()
    # Deliberately no apply_migrations here. Both long-running crawler commands
    # apply migrations at startup; this one connects as a SELECT-only role and
    # would crash-loop under restart: unless-stopped. The schema is the
    # operator's step, documented in README.md.
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
```

Add the imports `from babel.db.repo import hide_article, withhold_image` and `from babel.web.blobs import parse_digest` at the top of `cli.py`, next to the existing repo imports.

- [ ] **Step 5: Add the compose service**

Append to `docker-compose.yml`:

```yaml
  web:
    build: .
    container_name: babel-web
    command: ["babel", "serve"]
    env_file: .env
    volumes:
      # Read-only: the site serves blobs, it never writes one.
      - ${IMAGE_ROOT_HOST:-./data/images}:/data/images:ro
    # Deliberately NOT network_mode: service:gluetun. That namespace cannot
    # accept inbound connections, and this service has no reason to egress:
    # its only outbound dependency is Postgres on the bridge. The site
    # therefore stays up when the tunnel is down.
    #
    # WEB_BIND must be the host's LAN address, matching the Cloudflare tunnel
    # ingress rule, and never 0.0.0.0 — Docker's published-port rules install
    # into the DOCKER chain and bypass the host firewall.
    ports:
      - "${WEB_BIND:-127.0.0.1}:${WEB_PORT:-8080}:8080"
    depends_on:
      db: {condition: service_healthy}
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"]
      interval: 30s
      timeout: 5s
      retries: 3
```

- [ ] **Step 6: Add `.dockerignore`**

```
# BuildKit does not read .gitignore, and pgdata/ and data/images live inside
# the build context. Without this, `docker compose build` reads the running
# Postgres data directory and the whole rescued-image tree — hundreds of GB at
# the projected archive size — and transfers them to the daemon before a single
# layer is evaluated. None of it reaches the image.
pgdata/
data/
gluetun/
.git/
.venv/
.pytest_cache/
.ruff_cache/
.idea/
.superpowers/
__pycache__/
docs/
```

- [ ] **Step 7: Extend `.env.example`**

```bash
# --- public web archive ---
# The LAN address the site binds to. It MUST equal the target in the Cloudflare
# tunnel ingress rule, and must never be 0.0.0.0: Docker publishes ports through
# the DOCKER chain, which bypasses the host firewall. The host needs a static or
# reserved address — if it changes, the tunnel 502s and the container fails to
# start, and neither failure names the other.
WEB_BIND=127.0.0.1
WEB_PORT=8080

# A SELECT-only role, never the crawler's own DSN. babel serve refuses to start
# if this is unset or equal to DATABASE_URL. Create it with:
#   CREATE ROLE babel_web LOGIN PASSWORD '...';
#   GRANT CONNECT ON DATABASE babel TO babel_web;
#   GRANT USAGE ON SCHEMA public TO babel_web;
#   GRANT SELECT ON ALL TABLES IN SCHEMA public TO babel_web;
#   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO babel_web;
#   ALTER ROLE babel_web SET default_transaction_read_only = on;
#   ALTER ROLE babel_web SET statement_timeout = '10s';
# The last two are the actual controls: asyncpg runs RESET ALL on every
# connection release, which resets a session-level SET back to the role default.
WEB_DATABASE_URL=postgresql://babel_web:changeme@db:5432/babel

# Shown in the footer so an author can ask for their article to be removed.
CONTACT=
```

- [ ] **Step 8: Run everything**

```bash
uv run pytest
uv run ruff check src tests
```

Expected: PASS.

- [ ] **Step 9: Update the docs**

In `README.md` add a "Public archive" section with the runbook from the spec, and in `CLAUDE.md` add `web` to the Commands list and to "Operating the live run" (four services becomes five). State in both that migration 005 is an explicit stop/migrate/start step and never a bare `docker compose up -d`.

- [ ] **Step 10: Commit**

```bash
git add src/babel/cli.py src/babel/db/repo.py docker-compose.yml .dockerignore \
        .env.example README.md CLAUDE.md tests/db/test_hide.py tests/web/test_cli_serve.py
git commit -m "Add babel serve, babel hide, and the web compose service

serve never applies migrations: both existing long-running commands do,
and a third written by symmetry would crash-loop under the read-only
role. It also refuses to start if WEB_DATABASE_URL is unset or equal to
DATABASE_URL, because that fallback runs the public site as the database
owner and does it silently.

hide writes a tombstone and reports how many articles cite a withheld
blob — content addressing means withholding one is never a
single-article act.

.dockerignore is not housekeeping: pgdata/ and data/images sit inside the
build context, so every build was reading the live Postgres directory and
the whole image tree before evaluating a layer.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Deployment (operator, after Task 13 merges)

Not a code task. Run in this order — `up -d` alone would apply migration 005 against a live walk.

```bash
git pull
docker compose stop crawler images
docker compose run --rm crawler babel migrate
docker compose build web
docker compose up -d crawler images web
docker compose logs -f web
```

Then add the public hostname in the Cloudflare Zero Trust dashboard, targeting `http://<WEB_BIND>:<WEB_PORT>`.

**Re-collect the existing rows** so bodies gain their paragraph breaks (Task 1 only changes new fetches):

```bash
docker compose run --rm crawler babel refetch --from 1 --to 2797025 --yes
docker compose stop crawler
docker compose run --rm crawler babel run --no-poll   # runs the sweep; stop it when the queue drains
docker compose up -d crawler
```

The middle step is needed because finding M1 in CLAUDE.md means the running service only reaches its sweep phase once the walk bottoms out — queued `stale` rows are not picked up during the walk.

## Self-Review

**Spec coverage.** Architecture → Task 13; read-only role → Tasks 9, 13; pool size → Task 9; build inputs → Tasks 9, 13; cursor → Tasks 4, 7; filters compose in Python → Task 4; indexes → Task 3; CIC unavailable → Task 3; country list → Task 6; author suggestions → Task 6; counts and coverage → Tasks 6, 10; pages → Tasks 10–12; dates in game time → Tasks 7, 10, 11; what was lost → Tasks 6, 11; typed 404 → Task 11; caching → Tasks 10–12; suppression → Tasks 3, 13; escaping and urlencode → Tasks 10, 11; image type from bytes → Tasks 8, 12; no outbound requests → Task 9 (CSP `default-src 'self'`); robots → Task 9; errors → Tasks 9, 12; the nine spec tests → Tasks 4, 5, 9, 10, 11, 12, 13; prerequisites → Tasks 1, 2.

**Type consistency.** `browse.Cursor` is constructed in `cursor.py`, `routes.py` and the tests with the same two fields; `ListFilters(country=, author=)` is used identically in Tasks 4, 10; `image_status_counts` returns the four keys `ok/dead/waiting/exhausted` that Task 11's template reads; `serving_type` returns `(str, bool)` in both Task 8 and Task 12; `parse_digest` is used in `routes.py` and `cli.py`.

**Known ordering wrinkle.** Task 9 creates a stub `routes.py` so `create_app` imports cleanly; Task 10 replaces it. This is called out in Task 9 Step 6 rather than left for the implementer to discover.
