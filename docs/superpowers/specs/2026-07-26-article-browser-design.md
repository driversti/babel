# babel phase 2, first slice — public article browser

Design spec. Written 2026-07-26.

Read [SPEC.md](../../../SPEC.md) first. This document assumes its measurements and does not repeat
them.

## Problem

Phase 1 collects articles and nothing reads them. There is no way to see what the crawler has, no
way to find an article by who wrote it or where, and no way to read one without going back to
erepublik.com — which is the thing that stops working, and the reason this archive exists.

This slice delivers the browse half of SPEC.md's phase 2: a list of every collected article,
filterable by country and by author, sortable by date, paginated; and an article page that renders
the stored text, the full comment thread and the images we rescued.

Keyword search is the other half of phase 2 and is deliberately not here.

## Decisions

Taken with the operator before design, and not open in implementation:

1. **Public on the internet**, fronted by the existing Cloudflare Tunnel — the same token-based
   `cloudflared` in jupiter LXC 100 (`192.168.10.4`) that serves `battle-stats.yurii.live`,
   `articles.yurii.live` and the rest. This is the SPEC.md risk "Publishing a mirror of other
   players' content is a deliberate decision to make before phase 2 goes live" being decided, in
   the affirmative, with the mitigations below.
2. **A full archived read**, not a link-out index. Our own page, our own copy of the text, the
   comments and the images. An index that sends the reader to erepublik.com is worthless for
   exactly the articles the archive exists to hold.
3. **Keyset pagination**, next/prev plus jump-to-date. No page numbers, no total for a filtered
   result.
4. **FastAPI + Jinja2 server-rendered HTML, no JS build step.** No Node toolchain enters this repo.
5. **Open but noindex**, with attribution and a link to the original on every article.
6. **The parser learns block boundaries and the collected range is re-collected** — see
   "Prerequisite: the body has no line breaks".
7. **Takedown is a tombstone**, never a `DELETE` — see "Suppression".
8. **Crawling is allowed for article pages and refused for the filtered list space** — see
   "robots.txt and noindex do not compound".

## How this document was checked

The design was reviewed by five independent lenses (Postgres plans, security, project fit,
operations alongside the live crawl, and whether it delivers what was asked), producing 42 findings.
Each was then handed to a separate reviewer instructed to *refute* it. 26 were refuted; 16 survived
and are folded in below. Several survivors were established by measurement rather than by reading —
a 2.8M-row Postgres clone for the index work, this repo's own venv for the asyncpg and parser
behaviour. Where a number appears below, it was measured.

That method is recorded because two of the refutations mattered: "the author filter will merge two
citizens who share a name" is false (eRepublik forbids reusing a name a citizen has held), and "the
country+author query scans a whole country" is false (the planner anchors on the author predicate,
which is ~150k distinct values against 70 countries). Both were plausible. Neither was true.

## Prerequisites

Two things must land before or with this work. Neither is optional and both are outside the web
package.

### The body has no line breaks

`parse_article` stores `body_node.text(separator=" ")` (`parser.py:82`), and selectolax inserts
nothing at block boundaries. Measured across all three fixtures: 4 079 characters and **zero
newlines** against 41 `<br>` in the source; 4 556 / 0 / 25; 315 / 0 / 10. The stored text welds a
heading to the sentence after it.

`white-space: pre-wrap` therefore renders nothing — it differs from `normal` only by preserving
whitespace runs and newlines, and there are none. The web layer cannot repair this: `<p>A.</p><p>B.
</p>` is already `A. B.` in the column, and splitting on incidental double spaces is not a fix.

So the parser emits `"\n"` at `<br>`, `</p>`, `</div>` and `</li>`, and the collected range is
re-collected through `babel refetch --from/--to`. The cost curve decides the timing: ~11 000 rows
now against 2.8M after the walk bottoms out, at 1 req/s.

Note the interaction with the sweep phase — open finding M1 in CLAUDE.md says `run_backfill` only
reaches `claim_retryable` once the cursor passes `stop_at`, so queued `stale` rows are not picked up
during the walk. The re-collection must therefore be driven deliberately (stop the crawler, run a
one-shot pass, restart), not queued and assumed.

### M2 must close before the site is public

CLAUDE.md rates M2 — no scheme or address filter on `article_images.source_url` — as Minor
explicitly because the fetch is "blind and read-only". Publishing `/img/{sha256}` removes the
blindness: an article containing `<img src="http://172.18.0.x:PORT/internal">` is crawled by a
worker running in gluetun's namespace with `FIREWALL_OUTBOUND_SUBNETS=172.16.0.0/12` open, and if
that endpoint answers with any `image/*` type the body is stored, marked `ok`, and rendered in the
gallery on a public page. The attacker reads their own article to retrieve the response.

`capture_image` gains a scheme allowlist (`http`, `https`) and a resolved-address filter rejecting
loopback, link-local, RFC1918 and CGNAT. Already-stored blobs whose `source_url` resolves to a
private address are re-checked before launch.

## Architecture

A fifth compose service, `web`, from the same image (`build: .`), command `babel serve`.

**It is not in gluetun's network namespace.** Every other service is, because they must egress
through the tunnel. This one needs *inbound* connections, which that namespace cannot accept, and
its only outbound dependency is Postgres on the bridge. A consequence worth having: the site stays
up when the VPN is down.

**The port binds to a LAN address, not loopback**, because `cloudflared` runs in a different LXC and
targets `<host-ip>:<port>`. Compose uses `${WEB_BIND:-127.0.0.1}:${WEB_PORT:-8080}:8080` — safe by
default, opened by `.env` on the host. `WEB_BIND` and `WEB_PORT` go into `.env.example` with a
comment that the value must equal the address in the Cloudflare ingress rule; `WEB_BIND` is a
specific LAN address and never `0.0.0.0`, because Docker's published-port rules install into the
DOCKER chain and bypass the host firewall.

```
gluetun ──┬── crawler   (egress to erepublik.com)
          └── images    (egress to image hosts)

bridge  ──┬── db        (Postgres)
          └── web       (FastAPI, :8080)  ◀── CF Tunnel ◀── <hostname>
                │
                └── /data/images  (bind mount, :ro)
```

### Read-only access is enforced server-side

The naive form does not work. asyncpg runs `RESET ALL` on every connection release
(`connection.py:1732-1759`, called from `pool.py:234-239`), which returns
`default_transaction_read_only` to the role default. Measured against `postgres:17` with this repo's
asyncpg: with `create_pool(init=...)`, acquire #1 reads `on` and `CREATE TABLE` raises
`ReadOnlySQLTransactionError`; after release, acquire #2 reads **`off`** and `CREATE TABLE`
succeeds. Only the first request served by each physical connection is protected.

So the control is the role, not the pool:

```sql
CREATE ROLE babel_web LOGIN PASSWORD '…';
GRANT CONNECT ON DATABASE babel TO babel_web;
GRANT USAGE ON SCHEMA public TO babel_web;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO babel_web;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO babel_web;
ALTER ROLE babel_web SET default_transaction_read_only = on;
ALTER ROLE babel_web SET statement_timeout = '10s';
```

`ALTER ROLE` is what `RESET ALL` resets *to*, so it survives. Both were verified on acquire #2. This
is an operator step rather than a migration because the role password does not belong in a public
repository. The `ALTER DEFAULT PRIVILEGES` line is not decoration: without it the first table a
later migration adds turns any route touching it into a 500.

If a client-side `SET` is ever kept as well, it uses asyncpg's `setup=` (per acquire) and never
`init=` (per physical connection). `idle_in_transaction_session_timeout` is not set — the handlers
open no explicit transactions.

`web_database_url` has no working default. If it is unset or equal to `database_url`, `babel serve`
refuses to start with a named error rather than silently running as the database owner.

**`babel serve` never calls `apply_migrations`.** Both existing long-running commands do
(`cli.py:159-163`, `cli.py:221-223`), and a third written by symmetry would crash-loop under the
read-only role — the same crash-loop CLAUDE.md already records from the first deployment. Instead it
reads the `schema_migrations` ledger at startup and fails fast, by name, if migration 005 is absent.

**Pool size**: `min_size=1, max_size=10`. Against stock `postgres:17` (`max_connections=100`, 3
superuser-reserved) the three services hold at most 8 (crawler) + 11 (images, `image_concurrency +
3`) + 10 (web) = 29 of 97, so no override is needed. An asyncpg pool is hard-bounded, so traffic
beyond 10 concurrent requests queues on the web side and cannot consume slots the crawler needs.
`cli.py:217-221` already records that `max_size=4` sat exactly on the crawler's limit and that one
more consumer "would block forever with no error" — that reasoning is why this number is written
down rather than defaulted.

### Build inputs

Two repository-level gaps surface the moment a third `build: .` service exists.

`.gitignore` line 39 is `*.html`, negated only for `tests/fixtures/**`. Verified:
`git check-ignore -v src/babel/web/templates/list.html` → `.gitignore:39:*.html`. `git add -A` would
exit 0 without mentioning the templates, the test suite would pass because `pythonpath = ["src"]`
reads the working tree, and the deploy host would raise `TemplateNotFound` on every route. The
commit that adds the web package also adds `!src/babel/**/*.html`. Templates keep the `.html`
extension; renaming them to `.jinja` is not the mitigation, it is a way to forget why.

There is no `.dockerignore`, and `pgdata/`, `data/images` and `gluetun/` all sit inside the build
context. One is added covering `pgdata/`, `data/`, `gluetun/`, `.git/`, `.venv/`, `.pytest_cache/`,
`.ruff_cache/`.

## Data access

### The cursor

The sort key is `(published_at, id)`. `published_at` alone is insufficient: it has second
granularity, ties occur, and a tie at a page boundary either drops a row or shows it twice.

```sql
WHERE (published_at, id) < ($ts, $id)
ORDER BY published_at DESC, id DESC
LIMIT $n + 1
```

`LIMIT n+1` answers "is there a next page" without counting anything. The backward page mirrors it
(`>`, ascending) and the rows are reversed in Python. `order=old` mirrors both. The cursor appears
in the URL as `?after=<epoch_us>-<id>` and is validated by regex.

### Filters compose in Python, not in SQL

The query text is built from the active filter set. `($1 IS NULL OR country = $1)` is forbidden: it
is opaque to the planner, and under a generic plan it degrades to a scan. This project has been
bitten by the same class of problem twice, and `repo.py` carries two separate comments about
Postgres only proving a partial index applicable from a `Const`. Four filter combinations, four
query texts; only values stay bound parameters.

### Indexes

Migration 005 creates four, and `articles_published_at_idx` and `articles_country_idx` become
redundant:

| Index | Serves |
|---|---|
| `(published_at DESC, id DESC)` | unfiltered paging |
| `(country, published_at DESC, id DESC)` | country filter |
| `(lower(author_name), published_at DESC, id DESC)` | author filter, case-insensitive |
| `(country, lower(author_name), published_at DESC, id DESC)` | both filters |

The fourth exists so the index set matches the four query texts. Measured at 2.8M rows: without it
the two-filter query still uses an index — the planner anchors on `lower(author_name)`, or
BitmapAnds the two — costing 10 ms warm, 58 ms cold, and 49.7 ms once the prepared statement flips
to a generic plan; with it, 0.064 ms, for ~157 MB of index. This is latency hygiene and not a
denial-of-service control: a garbage author value is the *fast* case (0.03 ms, empty index range),
not the slow one.

One incidental finding worth recording: with `plan_cache_mode=auto` Postgres adopts the generic plan
at the sixth execution of a prepared statement, and `conn.fetch()` prepares and caches. A
long-running web process will run generic plans in production, so plan quality must be acceptable
*generically*, not only on the first execution.

**`CREATE INDEX CONCURRENTLY` is unavailable here.** `apply_migrations` wraps every file in
`conn.transaction()` (`migrate.py:28`), and CIC inside a transaction block raises SQLSTATE 25001 —
verified with this repo's asyncpg driving that exact pattern. Even without the explicit transaction,
asyncpg's simple query protocol wraps a multi-statement file in an implicit block. If someone writes
such a migration the ledger row is never inserted and `restart: unless-stopped` retries the file
forever. When the keyword-search GIN index arrives, `apply_migrations` gains a per-file
`-- babel:no-transaction` marker (verified: a single CIC statement outside `conn.transaction()`
succeeds). That belongs with the feature that needs it.

**Migration 005 is an explicit operator step, not a side effect of `up -d`:**

```
docker compose stop crawler images
docker compose run --rm crawler babel migrate
docker compose up -d crawler images web
```

Both long-running commands apply migrations at startup, so `docker compose up -d images` alone —
which CLAUDE.md documents as safe — would run this DDL against a live walk. The file sets
`lock_timeout = '5s'` at the top, but that bounds only how long the migration itself waits to
*acquire* a lock — verified against a live Postgres: once acquired, the lock is held for the whole
build regardless of `lock_timeout`. Plain `CREATE INDEX` takes `ShareLock`, which blocks concurrent
writers (not readers) for the whole build; `ACCESS EXCLUSIVE` belongs to the two `ADD COLUMN`
statements below, and both are metadata-only (neither column has a default) and effectively
instant. What actually keeps this from blocking `save_article` indefinitely is the runbook — stop
`crawler` and `images` before migrating — not `lock_timeout`. `lock_timeout` only matters because
Postgres's lock queue is FIFO: once an `ACCESS EXCLUSIVE` request is waiting, every later reader and
writer queues behind it too, so failing fast to acquire is still worth doing.

The two `DROP INDEX` statements go in a **separate, later** migration, so a failed build cannot
leave `articles` with neither index set.

### The country list

A recursive skip-scan over the leading column of the country index — roughly 70 index probes, no
table scan, no cache:

```sql
WITH RECURSIVE t AS (
    (SELECT country FROM articles WHERE country IS NOT NULL ORDER BY country LIMIT 1)
    UNION ALL
    SELECT (SELECT country FROM articles
             WHERE country > t.country AND country IS NOT NULL
             ORDER BY country LIMIT 1)
      FROM t WHERE t.country IS NOT NULL
)
SELECT country FROM t WHERE country IS NOT NULL;
```

### Author suggestions

The author filter is an exact, case-insensitive text field; author names in list rows are links, and
that is the primary way to filter. When a typed name matches nothing, the page offers prefix
matches as links.

Those suggestions use a bounded skip-scan, not `LIKE`:

```sql
SELECT DISTINCT ON (lower(author_name)) author_name
  FROM articles
 WHERE lower(author_name) >= $1 AND lower(author_name) < $2
 ORDER BY lower(author_name), published_at DESC, id DESC
 LIMIT 8
```

The obvious form — `WHERE lower(author_name) LIKE 'x%' … DISTINCT … LIMIT 8` — is unbounded, because
`DISTINCT` is a blocking aggregate that `LIMIT` cannot push through, so cost scales with matching
*articles*. Measured on a 600k-row clone: `?author=a` aggregated 50 990 rows to yield 8, reading
12 139 buffers (95 MB); at 2.8M that is ~445 MB per public request. `?author=%` matched nearly
everything and became a 938 MB parallel sequential scan. The skip-scan form reads 127 buffers in
0.10 ms — 96× fewer buffers, and O(8) rather than O(matching rows). Because `LIKE` never appears on
the request path, `%` and `_` in user input need no escaping.

No `text_pattern_ops` index is created. It exists only to enable the plan being avoided.

### Counts and coverage

The archive-wide `count(*)` is cached for 5 minutes and shown in the header. Filtered results show
no count — a direct consequence of keyset paging, and more honest than "page 1 of ~4213".

The list also states coverage, because for the length of the backfill the archive is a recent slice
of a 19-year corpus and nothing else says so. Today that is ~11 000 of ~2.8M IDs: "oldest first"
returns 2024, not 2007; paging to the bottom hits a hard stop; a jump to any earlier date returns
nothing; and a country not yet reached is simply absent from the dropdown, indistinguishable from a
country that never published.

Rendered above the results, repeated verbatim as the end-of-list message when the `n+1` probe shows
no next page, and used as the body of an empty result:

> 10 093 articles so far, covering 8 Nov 2024 – 26 Jul 2026. Collection walks backwards through
> article IDs and has reached 2 785 850 of ~2 797 000; anything published earlier is not in the
> archive yet.

The span comes from `min(published_at)`/`max(published_at)` — measured at 2.8M rows with the new
index as two InitPlan limits over index-only scans, 8 buffers, 0.105 ms — and the frontier from
`crawl_cursor`, which the 404 handler already reads. `min` and `max` on the jump-to-date input are
set from the same two values; that is a client hint only, so the server still answers an
out-of-range date with the coverage line.

## Pages

| Route | Purpose |
|---|---|
| `GET /` | list: `?country=`, `?author=`, `?order=new\|old`, `?after=`/`?before=` |
| `GET /article/{id}` | article, comments, rescued images |
| `GET /img/{sha256}` | bytes from the content-addressed store |
| `GET /robots.txt` | see below |
| `GET /healthz` | for autoheal and monitoring |

A base template supplies a header linking to `/` and a footer carrying the archive's purpose and the
takedown contact (a config value, `BABEL_CONTACT`, never hardcoded). Every page uses it, including
404 and 503.

**List row**: date · title · author (link = filter by that author) · country (link = filter) ·
comment count. No thumbnails: originals average 84 KB at 6.2 per article, so 50 rows of previews is
megabytes. Thumbnail generation is separate work.

Controls: country dropdown, author field, newest/oldest toggle, `<input type="date">`, and
`← newer` / `older →`. A plain GET form, so every list state is a shareable link.

**Article page**: header (title, author, country, date, e-day), a `rel="nofollow noreferrer"` link
to the original, the body, the gallery, then comments indented by `depth` with `[removed]` where
`body IS NULL`. The header's author and country are the same filter links the list rows use.

### Dates render in game time

Every reader-facing date is `America/Los_Angeles`, never UTC.

`published_at` is a genuine UTC timestamp, and SPEC.md's "Date conversion" records that it disagrees
with the game's own reckoning for the last 7–8 hours of every game day. `article_with_comments.html`
in this repo is exactly that case: `datePublished` is 2026-07-21 05:53 GMT while the page says
"Published in Bulgaria on the 20th of July 2026" and the title says day 6 817. Rendered raw, a list
row reads 21 July and the article it links to reads day 6 817 — 20 July — one click later. That is
roughly a third of all rows, concentrated in the game's evening peak, and a reader can disprove it
against erepublik.com in one click.

Templates format `published_at.astimezone(GAME_TZ)`, reusing `parser.GAME_TZ`. `e_day` is shown
alongside when present but is never the source of the displayed date — it is nullable, coming from a
regex over `<title>`.

**The conversion happens in Python, never in the `WHERE` clause.** The date input's value is
interpreted as game-local midnight and converted to a UTC instant before the cursor is built.
Measured on 300k rows with `(published_at DESC, id DESC)`: the row comparison is an `Index Cond` at
0.062 ms, while `WHERE published_at AT TIME ZONE 'America/Los_Angeles' < $1` degrades to a `Filter`
that removes 296 641 rows.

### What was lost

`article_images.status` has four values and only `dead` means the image was gone when we looked. A
`total − ok` count would be wrong on precisely the newest articles: the drain runs at 1.35 img/s
against the ~5.3 img/s the walk produces (CLAUDE.md), so every article between the image worker's
front and the backfill cursor is majority-`pending`. A freshly ingested article has 6 `pending` rows
and 0 `ok`, and the naive line reads "6 of 6 images were already gone when we looked" above an empty
gallery — the exact inverse of the truth, on the project's central claim about itself.

Up to three independent facts, each omitted when its count is zero:

- the gallery: `status = 'ok'`, in `position` order;
- "N images were already gone when we looked" — `status = 'dead'` only;
- "N not captured yet" — `status IN ('pending','error')` below `MAX_IMAGE_ATTEMPTS`; rows at the
  ceiling read "N we could not retrieve", never wording that says gone.

Since migration 004 the key is `(article_id, source_url)` and duplicate-URL rows were removed, so
these counts are distinct image URLs, not `<img>` nodes. The denominator is never described as
"images in the article".

### The 404 says which kind

Consulting `fetch_log`: `missing` → already deleted when the crawler reached it; `error`/`stale` →
collection failed, will retry; no row → not collected yet, and the frontier from `crawl_cursor`.
Three different facts that a bare "404 Not Found" would flatten into one. A suppressed article
(below) falls through to the same handler.

### Caching

`/img/{sha256}` is content-addressed, but **not** `immutable`: `max-age=86400, must-revalidate`,
because suppression has to be able to reach it. Pages are `max-age=300`.

## Suppression

Takedown is a tombstone. Migration 005 adds `articles.hidden_at timestamptz`,
`images.withheld_at timestamptz`, and `CREATE INDEX article_images_sha256_idx ON article_images
(sha256)` — which does not exist today and is what answers "which other articles cite this blob".

`DELETE FROM articles` is not an option, and the reason was measured on a fresh database with all
four migrations: the delete cascades to `comments` and `article_images`, but the `images` row and
the file on disk survive (the FK runs `article_images.sha256 → images(sha256)`, not the reverse), and
`fetch_log` has no FK to `articles` at all, so its row stays `ok`. `babel refetch --from/--to` then
flips it to `stale`, the sweep re-collects, and the taken-down article comes back. **Deletion is
silently reversible by tooling this project already ships.** That is why the tombstone is not to be
simplified away later.

Deleting the blob instead is worse: content addressing means it is shared, so removing it blanks
that image in every other article citing the same bytes, with nothing recording which.

Every list and article query filters `articles.hidden_at IS NULL`. `/img/{sha256}` already probes
`images` by primary key to decide the serving type, so `AND withheld_at IS NULL` is free there.
`babel hide --article <id>` / `--image <sha256>` is the operator command.

## Security

**Escaping.** Bodies are plain text and render escaped inside `white-space: pre-wrap`. No `|safe`
anywhere. CSP on HTML pages: `default-src 'self'; script-src 'none'; img-src 'self'; frame-ancestors
'none'; base-uri 'none'; form-action 'self'`. Approach 4 ships no scripts, so `script-src 'none'`
breaks nothing and makes XSS impossible rather than unlikely.

**URL contexts need `|urlencode`, not `|e`.** Every value a template puts into a query string —
the `?author=` and `?country=` links in rows, in the suggestion block, in the pager — is
URL-encoded. HTML-escaping is the wrong control there. Measured against 2.15M real citizen names,
807 contain `&`, `#`, `%` or `+`: with `|e` alone an author named `#1PAKI` yields
`href="/?author=#1PAKI"`, the browser drops the fragment, the server sees `author=`, and the
archive's own "filter by this author" link returns the unfiltered list. The plain GET form needs
nothing — browsers form-encode input values.

**The image Content-Type is computed from the bytes, never echoed from the database.** `images.mime`
is the third party's header stored verbatim: `resolve_mime` returns the declared string unchanged,
and `tests/crawler/test_images.py:189` pins exactly that, so `image/jpg`, `image/x-png` and
`image/jpeg; charset=binary` are all in the column. Testing those against a literal allowlist fails
*genuine* JPEGs and PNGs — the gallery renders broken and clicking downloads a file. It also stops
nothing, because the string is chosen by the remote host: SVG bytes served as `image/png` store as
`image/png` and pass. And a stored type containing a non-latin-1 character cannot be re-emitted as
an HTTP header at all — measured, it raises `UnicodeEncodeError`, an unhandled 500 on every request
for that blob. `save_image_blob` is `ON CONFLICT DO NOTHING`, so a deduplicated blob keeps whatever
the *first* host declared, and one bad host poisons the type for every article citing those bytes.

So at serve time the first 16 bytes are read and passed to the existing `sniff_image_mime` (16, not
12: WebP is identified at offset 8–12). A canonical `image/png|jpeg|gif|webp|bmp|tiff` is served
inline; everything else — SVG, anything that only ever had a declared type, anything unrecognised —
is `application/octet-stream` with `Content-Disposition: attachment`. This repairs every existing
row with no migration and no re-collection, and it is the rule SPEC.md already states for ingest:
"Content-Type is a hint; the bytes are the evidence." Accepted consequence: **archived SVGs are
download links, not gallery images.**

`nosniff` and `Content-Security-Policy: default-src 'none'; sandbox` on `/img` responses are the
actual security controls; the type list is a serving decision, not a boundary. The sha256 is
validated as `^[0-9a-f]{64}$` and the path is built from the validated value, so traversal is
impossible.

**No outbound requests.** No fonts, no CDN, no hotlinked originals. A visitor never contacts imgur,
and imgur never learns the archive exists.

**No parameter increases request cost.** Page size is pinned at 50 and is not in the URL.

### robots.txt and noindex do not compound

A crawler blocked by `Disallow` never fetches the page and therefore never sees `X-Robots-Tag:
noindex` — the two controls cancel rather than reinforce. Since the goal is actually to stay out of
search results, crawlers must be allowed to fetch:

```
User-agent: *
Disallow: /?
Allow: /
```

Article pages are crawlable, so the `noindex` on them is seen and honoured. The parameterised list
space is refused, because filter combinations are a combinatorial crawl trap and carry no content
of their own.

`X-Robots-Tag: noindex, nofollow` is sent on every response, with a matching meta tag. Accepted
residual: a fetcher that ignores both can still list a URL.

## Errors

- Database unreachable → a 503 page and a log line, never a traceback.
- A row marked `ok` whose file is missing (`IMAGE_ROOT` moved, volume unmounted) → 404 plus a
  warning naming the sha. Never a 500.
- Malformed cursor, e.g. a truncated shared link → 302 to the same URL without it. Degrades gently
  and visibly.
- Unknown country → an empty list carrying the coverage line, not an error.

## Testing

The existing pytest suite, the existing `postgres:17` testcontainer, plus httpx's ASGI transport.

1. **Paging across tied `published_at` values**: every id exactly once, order preserved, backward
   symmetric, both `order=new` and `order=old`.
2. **Filters compose** — country and author together.
3. **Escaping, in both contexts.** `<script>alert(1)</script>` in title, body and a comment does not
   appear raw. And an author named ``a"><img src=x onerror=alert(1)>#&+`` produces a link whose href
   round-trips to exactly that author's rows, with no attribute break.
4. **`/img/`**: non-hex and `../` rejected; valid hex serves bytes; a missing file is 404, not 500.
5. **Serving type comes from bytes**: JPEG bytes stored as `image/jpg` serve as `image/jpeg` inline;
   SVG bytes stored as `image/png` serve as an attachment; a stored mime containing a non-latin-1
   character still builds a response and is an attachment, never a 500. Canonical fixtures alone
   would pass while all three are broken.
6. **Headers**: `X-Robots-Tag` on every route, `robots.txt` matches the policy above, CSP present.
7. **Three `fetch_log` states → three different 404 texts**, and a `hidden_at` article falls through
   to the same handler.
8. **Read-only survives connection reuse**: acquire, release, acquire again, *then* `INSERT` — it
   must fail on the second acquisition. A single-acquire test passes against a broken implementation
   and proves nothing.
9. **EXPLAIN**, taking the SQL from the builder function rather than a copy pasted into the test —
   and asserting on the `Index Cond:` line, not on the index name. The name appears in the plan even
   for the `IS NULL OR` form this design forbids, because an index scan with a filter still names
   its index. So: the country, `lower(author_name)` and `(published_at, id)` terms must appear as
   index conditions and must not appear in `Filter:`; the `IS NULL OR` variant is included as an
   explicit negative control; and enough rows are inserted (or `ANALYZE` run) that the plan is not
   chosen from a zero-page estimate. This is finding M3 in CLAUDE.md being *prevented from
   recurring in this new query path*, not closed outright: the pre-existing offenders — the two
   partial-index EXPLAIN tests in `tests/db/test_repo.py`, which still build their own copy of the
   SQL and EXPLAIN that instead of the code's query — are untouched, and `RETRYABLE_STATUSES` in
   `repo.py` is still unused. M3 stays open in CLAUDE.md until those are fixed too.

## Out of scope

Keyword search (the next phase-2 slice), translation, thumbnails, restoring in-text image placement,
authentication, RSS output, vote counts, author profile pages.

## Operator runbook

One-time:

1. Create the `babel_web` role with the grants above; put its DSN in `.env` as `WEB_DATABASE_URL`.
2. Set `WEB_BIND` to the host's LAN address and `WEB_PORT` in `.env`.
3. Add the public hostname in the Cloudflare Zero Trust dashboard, mapping it to
   `http://<WEB_BIND>:<WEB_PORT>`. The host needs a static or reserved address: if it changes, the
   tunnel 502s *and* Docker refuses to publish the port, so `web` crash-loops under
   `restart: unless-stopped`.

Deploy:

```bash
git pull
docker compose stop crawler images
docker compose run --rm crawler babel migrate
docker compose build web
docker compose up -d crawler images web
```
