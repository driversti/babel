# babel — eRepublik article archive

Design spec. Written 2026-07-26.

## Problem

eRepublik has no article search. If you did not bookmark an article, it is gone. Articles are
also written in ~10 languages, so most players cannot read most of what is published.

babel collects every article ever published, stores it with its comments, and eventually makes
it searchable regardless of the reader's language.

## Phases

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Crawler: live polling + backfill → Postgres, including images | **current** |
| 2 | Public web archive (browse + keyword search) | later |
| 3 | Embeddings + cross-language semantic search | later |
| 4 | Telegram digest / sentiment | maybe |

Only phase 1 is specified below. Later phases are sketched at the end so phase 1 does not
paint them into a corner.

## Measurements

Taken 2026-07-26 against the live site. These drive every sizing decision here, so they are
recorded rather than re-derived.

### Feed

- `GET /en/main/news/latest/all/all/{page}/rss` — public, no session, no Cloudflare challenge.
- Sorting: `latest` or `rated`. Country and category segments must be `all` or the endpoint
  returns HTML instead of RSS.
- **Pagination caps at page 5** — 50 items, roughly 3 days. Deeper pages return an empty feed.
  The feed is for live collection only; history is not reachable through it.
- `<description>` is truncated at 200 characters, so the article page must be fetched anyway.
- Feed completeness: 50 of the 53 article IDs in the sampled range appeared, i.e. ~94%. The
  missing ones are deleted articles.

### Cloudflare, from behind the VPN

Ran 2026-07-26 from the target host through a WireGuard tunnel exiting in France: 100 random
article IDs at 1 req/s returned **85 `ok`, 15 `missing`, and zero Cloudflare challenges**. The
missing ones are deleted articles and gaps in the ID sequence, consistent with the ~94% feed
coverage measured separately.

This was the assumption the whole design rested on, and it is the one thing that could not be
established by reading code. It is now measured rather than reasoned.

### Article pages

- `GET /en/article/{id}` works with **no slug and no session**. The slug in real URLs is
  decorative. `/en/article/{id}/1/1000` returns the article and all its comments in one request.
- Missing/deleted IDs return HTTP 404.
- Body lives in `div.postBody`, inside `div.postContent[itemType="schema.org/Article"]`.
- Metadata available logged-out: title, author name, country, `itemprop="datePublished"`, eRepublik
  day, comment count (in the `<meta name="description">` text).
- **The author's citizen ID is not on the page.** The header carries only the name, in
  `itemprop="author"` and the `<title>` byline. Citizen IDs appear solely inside the comment thread,
  so an author's ID is recoverable only when they also commented on their own article — matching the
  byline name against a commenter's profile link. Otherwise `author_id` is NULL and `author_name` is
  all there is. Resolving the rest would mean a `citizen-search?name=` lookup per unique author,
  cacheable and cheap since authors repeat heavily, but that is phase-2 work and not worth blocking
  ingestion for. Do not guess: attaching the first citizen link on the page attributes articles to
  whoever commented first.
- Vote counts are **not** rendered logged-out. Ranking by votes would need an `erpk` cookie.
  Comment count is a free substitute and is what "most discussed" should use.

### Comments

One `<div id="comment{id}" class="commentWrapper">` per comment. Inside: author link
`/en/citizen/profile/{id}`, display name, `<span>Day 6,819, 21:34</span>`, and the text in a `<p>`.
Nesting depth is encoded as `padding-left:{30*depth}px` on a wrapper div. Deleted comments render
as `<i>[removed]</i>`.

Comment IDs are globally sequential (~44.8M as of 2026-07).

### Images

Articles average **6.2 images**, embedded from wherever the author happened to host them. Of 344
image URLs sampled across all eras and actually downloaded:

| Era | Still resolving |
|-----|----------------:|
| 2007–2014 | **7%** |
| 2015–2020 | 34% |
| 2021–2026 | **66%** |

Top hosts are a roll-call of dead and dying image services: imageshack, photobucket, prntscr,
`content.foto.my.mail.ru`, Dropbox public links, personal domains. A live image is 55 KB at the
median, 84 KB at the mean, 221 KB at p90.

The conclusion is the opposite of the intuitive one. Old images are not worth rushing for — 93% of
2007–2014 is already gone and no crawl will bring it back. **Recent images are the ones actively
being lost**: a third of 2021–2026 has already rotted, and the rest is decaying now. Every month of
delay permanently costs part of the only layer still recoverable.

So image capture belongs in phase 1, and it must work newest-first, for the same reason the
backfill does. What matters is the delay, measured in minutes or hours — a sweep run months later
recovers strictly less. It does not have to happen in the same pass as the article, and an earlier
draft of this document over-specified that; see "Image capture runs as its own worker" below.

| Depth | Images surviving | Bytes |
|-------|-----------------:|------:|
| 5 years | ~155 000 | ~13 GB |
| 10 years | ~430 000 | ~36 GB |
| Everything | ~4 900 000 | ~410 GB |

Content-addressed storage deduplicates this substantially — flags, avatars, unit logos and recycled
memes recur across thousands of articles — realistically landing near 250–300 GB for the full set.
Still roughly 30× the text.

### Volume

Publication rate, from sequential article IDs sampled across eras:

| Year | Articles/day |
|------|-------------:|
| 2009–2010 | ~1 630 |
| 2011–2012 | ~690 |
| 2013–2014 | ~350 |
| 2016–2017 | ~109 |
| 2019–2021 | ~52 |
| 2022–2024 | ~34 |
| 2026 | **~18** |

**93% of all articles predate 2016.** Current max article ID is ~2 797 000, so the whole archive
is ~2.8M articles.

Per article: body averages 1 357 characters, comments average 2 054 characters across ~21 comments
(median 13). Comments hold more text than articles do.

| Depth | Articles | Stored text | HTML transferred | Crawl @1 req/s |
|-------|---------:|------------:|-----------------:|---------------:|
| 5 years | 59 000 | 0.2 GB | 2 GB | ~16 h |
| 10 years | 191 000 | 0.7 GB | 8 GB | ~2 days |
| Everything | 2 800 000 | 9.6 GB | 118 GB | ~32 days |

## Phase 1 scope

A service that ends up holding every eRepublik article, its comments and its still-living images in
Postgres, and stays current from then on. No AI, no web UI, no digests.

### Components

**Live poller.** Reads RSS pages 1–5 every 15 minutes, extracts article IDs, enqueues unseen ones.
The ~3-day feed window means a multi-hour outage loses nothing.

**Backfill worker.** Walks IDs downward from a persisted cursor, enqueueing as it goes. Starts at
the current maximum and runs indefinitely. There is no target depth — the archive simply gets
deeper, and the most useful years arrive first.

**Fetcher.** Single work queue shared by both producers, drained by a pool of workers behind one
global rate limiter. Fetches `/en/article/{id}/1/1000`, parses, writes.

**Parser.** Pure functions over HTML strings, tested against saved fixtures. No network, no DB.

**Image worker.** A separate process draining `article_images WHERE status = 'pending'`,
newest-article-first, behind its own rate limit. Sleeps rather than consuming the queue when disk
space runs low. See "Image capture runs as its own worker" below.

**Notifier.** Telegram, for the two events where a human has to know and a log line will not reach
them: the disk filling, and an egress IP in the home country. Deliberately narrow — this is not a
digest, and adding routine notifications here would teach the operator to ignore it. Credentials
live in `.env`.

### Network egress

**All outbound traffic must leave through a VPN.** The operator's own IP is never to appear in
eRepublik's logs, and a crawl that runs for a month gives a leak plenty of opportunities.

Periodic IP checks are not sufficient, because they only notice a leak after it has happened.
Instead the crawler runs with no network stack of its own:

```yaml
crawler:
  network_mode: "service:gluetun"
  depends_on:
    gluetun:
      condition: service_healthy
```

Gluetun holds the only network namespace. If the tunnel drops or the VPN container dies, the
crawler loses connectivity entirely rather than falling back to the host route. A leak is not
merely detected, it is impossible.

On top of that, belt and braces:

- Verify the exit IP at startup and every 15 minutes. If the reported country equals
  `HOME_COUNTRY`, log, alert and exit immediately.
- Gluetun's HTTP control server allows stop/start, so the exit node can be rotated on a schedule
  or in response to 429/403.
- Provider, credentials, exit countries and `HOME_COUNTRY` live in `.env`, which is gitignored.
  None of them belong in this repository.

Constraints worth remembering: an LXC container cannot run a VPN, since it has no `/dev/net/tun` —
this needs bare metal or a VM, which the Jetson satisfies. Gluetun's DNS blocks some IP-lookup
services, so pick one known to work and pin it.

### Rate limiting

Start at **1 request/second** globally and watch for 429/403. The monorepo notes a ~3000 req/hr
limit for authenticated API use; anonymous page fetches may be more generous, but there is no
reason to find the ceiling the hard way. Raise only after a clean multi-day run.

### Resumability

Every attempted ID gets a `fetch_log` row recording one of four statuses: `ok` and `missing` are
final answers about the article itself; `error` and `stale` are unfinished business — a fetch that
failed, or a row deliberately queued for re-collection by `babel refetch` (see below). The backfill
cursor is persisted after each batch. This matters: the full backfill is a month long and will be
interrupted, repeatedly, over its lifetime.

`run_backfill` never returns; it cycles through three phases indefinitely:

1. **Walk.** While the cursor is above `stop_at`, fetch a batch of unseen IDs downward from it and
   advance the cursor. This is the original newest-first sweep down toward article 1.
2. **Sweep.** Once the walk bottoms out, claim IDs whose `fetch_log` row is `error` or `stale`,
   past a cooldown (`retry_cooldown_sec`) and under the attempt ceiling, and retry them. This is
   also where a transient failure inside a completed walk batch gets another chance — the cursor
   has already moved past it, so only the sweep will ever see it again.
3. **Idle.** When neither phase has work — the walk is done and nothing is retryable — sleep
   (`backfill_idle_sleep_sec`) and check again. Reaching article 1 no longer ends the process; it
   just means the loop spends most of its time in this phase, still watching for new `stale`/`error`
   rows.

A restart resumes from the persisted cursor and re-enters whichever phase applies; it never
re-fetches an `ok` row on its own. `babel refetch --ids/--from/--to` is the operator's way to force
one back into rotation: it flips `ok`/`error` rows to `stale`, which the sweep phase then picks up.

### Schema

```sql
CREATE TABLE articles (
    id            bigint PRIMARY KEY,           -- eRepublik article ID
    title         text        NOT NULL,
    body          text        NOT NULL,
    author_id     bigint,
    author_name   text,
    country       text,
    published_at  timestamptz NOT NULL,
    e_day         int,
    lang          char(3),                      -- detected, ISO 639-3
    comment_count int         NOT NULL DEFAULT 0,
    fetched_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE comments (
    id          bigint PRIMARY KEY,             -- eRepublik comment ID
    article_id  bigint      NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    position    int         NOT NULL,           -- order within the thread
    depth       smallint    NOT NULL DEFAULT 0,
    author_id   bigint,
    author_name text,
    posted_at   timestamptz,
    body        text                            -- NULL when [removed]
);

CREATE TABLE images (
    sha256      bytea PRIMARY KEY,              -- content address; also the path on disk
    mime        text,
    bytes       int         NOT NULL,
    width       int,
    height      int,
    stored_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE article_images (
    article_id bigint      NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    position   int         NOT NULL,            -- order within the article body
    source_url text        NOT NULL,            -- as written by the author
    sha256     bytea       REFERENCES images(sha256),   -- NULL when not retrieved
    status     text        NOT NULL,            -- pending | ok | dead | error
    attempts   smallint    NOT NULL DEFAULT 0,
    checked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, position)
);

CREATE TABLE fetch_log (
    article_id bigint PRIMARY KEY,
    status     text        NOT NULL,            -- ok | missing | error | stale
    attempts   smallint    NOT NULL DEFAULT 1,
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE crawl_cursor (
    name       text PRIMARY KEY,                -- 'backfill'
    next_id    bigint      NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
```

Indexes on `articles(published_at)`, `articles(country)`, `articles(author_id)`,
`comments(article_id, position)`, `fetch_log(status)`.

### Date conversion

Comments carry an eRepublik day number rather than a date. Day 1 is 2007-11-21:

```
date = 2007-11-21 + (eday - 1) days, in America/Los_Angeles
```

Verified against six articles spanning 2009 to 2026, comparing the day number in `<title>` with the
game-local date in the `<meta name="description">` text. Dropping the `- 1` puts every date one day
late, so it is worth a test.

Note that the `itemprop="datePublished"` meta tag is a genuine UTC timestamp and will disagree with
the game-local date whenever publication falls after 16:00 PST. The day number and the description
date are the game's own reckoning; `datePublished` is not. Use the former for `e_day`, the latter
for `published_at`.

(The monorepo's shared notes give this as `(now - epoch).days` with a 2007-11-21 epoch, which
yields day 0 on launch day and is one behind what the game displays. Unverified whether other
projects here depend on that convention.)

### Image storage

Binary data does not go in Postgres. Files are written to a content-addressed tree —
`{IMAGE_ROOT}/ab/cd/abcdef...` keyed by SHA-256 — which deduplicates for free and makes the whole
store relocatable with `rsync`. Postgres keeps only the metadata above.

`IMAGE_ROOT` is configuration, never a constant. The archive will outgrow the crawler's host, and
moving it must not require touching code.

`article_images` records every image reference whether or not the bytes were retrieved, so a dead
link is a recorded fact rather than an absence. `status = dead` is permanent knowledge: it says
this image was already gone when we looked, which is worth keeping.

**A free-space floor is mandatory.** On the intended host the image tree shares a filesystem with
both Postgres and the OS root, so filling it takes down the machine and not merely the crawl. The
image worker checks free space before each batch and, below the threshold, **sleeps with the queue
untouched** and sends one notification. It does not mark the rows.

Marking them would be the tempting move and it is wrong: a marked row has left the queue, and when
space is freed — or `IMAGE_ROOT` is moved to a larger disk — nothing would go back for it. Leaving
them `pending` makes recovery automatic. Text ingestion is unaffected either way, since it runs in
a different process.

### Target host

Phase 1 runs on the x86_64 box, not the Jetson: 16 cores, 30 GiB RAM, a 1.9 TB NVMe with ~1.7 TB
free, bare metal, Docker 29.4. Roomy enough that the disk stops being a design constraint at all —
the full unreduced image set is under a fifth of what is free.

**The Jetson cannot run this.** Its Tegra kernel (5.15.148-tegra) is built without
`CONFIG_IP_ADVANCED_ROUTER`, so policy routing is unavailable — `ip rule` fails on the host and in
every container regardless of capabilities. Gluetun sets up routing rules unconditionally and has
no option to skip it, so it exits at startup there. Verified directly, including with a minimal
gluetun carrying none of this project's settings. WireGuard itself is fine: `/dev/net/tun` exists
and the userspace implementation works; only the routing blocks it.

That reshapes where the Jetson fits rather than removing it. Its value was always the GPU and
always-on operation, not proximity to eRepublik. Phase 3 embeddings still belong there, reading
from Postgres over the LAN.

Notes that follow:

- x86_64 means the standard amd64 build applies. The ARM64 build path this document previously
  called out as friction does not exist for phase 1; it returns only if phase 3 containerises the
  embedding work on the Jetson.
- Text, metadata and vectors total roughly 15 GB, images 250-300 GB after deduplication. Against
  1.7 TB free, the free-space floor is a safety net rather than an operating constraint — but it
  stays, because the image tree still shares a filesystem with Postgres and the OS root.
- The host already runs eleven unrelated containers. Nothing conflicts: no VPN container, and
  port 5432 was free.

### Decisions

**Raw HTML is not archived, including the 30-day window this document originally specified.** 118
GB for something we can re-fetch. The implementation plan dropped that window entirely — not just
the full-scale archive — on the grounds that articles "remain re-fetchable, and `fetch_log` records
which IDs would need it."

That justification was false when it was written: no refetch path existed, `filter_unseen` treated
`ok` as final forever, and a cursor that had already advanced meant an ID that failed mid-batch was
gone for good. The whole-branch review caught this before merge (finding C1, in
`docs/superpowers/plans/2026-07-26-final-review-findings.md`) and flagged it as the reason this
document's own headline risk — "the parser will break" — would have been unrecoverable: a markup
change on day 20 would have written millions of degraded rows with no raw HTML left to reparse and
no way to even identify which rows needed it.

It is true now. `fetch_log` carries a status per attempted ID (`ok | missing | error | stale`), the
backfill's sweep phase retries `error` and `stale` rows on a cooldown, and `babel refetch
--ids/--from/--to` lets an operator queue a known-bad range for re-collection by hand after a fix
ships. The 30-day window stays dropped — deliberately now, with the capability that makes dropping
it safe actually built, rather than assumed.

**Comments come free.** They arrive in the same request as the article, so there is never a reason
to fetch an article without them.

**Backfill runs newest-first.** The archive is useful from day one and no depth decision has to be
made up front. Stopping at any point still leaves the most relevant years collected. Images make
this ordering matter more, not less: the newest are both the most numerous survivors and the ones
still actively disappearing.

**Image capture runs as its own worker, draining a queue.** `save_article` writes each image
reference as a `pending` row; a separate process — its own container, sharing the same VPN
namespace — drains them newest-article-first. Three things follow, and each fixes a defect the
original same-pass design had:

- A kill mid-download strands nothing. `pending` is a queue state, not a lost row.
- Images stop sharing eRepublik's politeness budget. That budget exists for erepublik.com; imgur
  neither needs nor notices it. At 6.2 images per article, one shared 1 req/s limiter turns a
  32-day crawl into roughly 234 days. Separate limits put article collection back at ~32 days.
- Image capture can be paused without stopping article collection — which the free-space floor
  above makes a certainty, not a hypothetical.

The survival curve still governs the *order*: newest first, because that is where the living images
are. What it never actually required was doing the work in the same pass.

**Image fetches are rate-limited separately and politely per host.** A global limit distinct from
eRepublik's, plus at most one concurrent request per hostname. Articles cite CDNs and someone's
personal server side by side; the CDN will not notice either way, and the personal server should
not be handed CDN-shaped load.

**Image requests must be shaped like an `<img>` load, not like navigation.** Measured on the first
live run: 64% of the images on *same-day* articles were recorded as gone. None of them were.
curl_cffi's `impersonate="chrome"` sends the header set a browser uses when a human navigates to a
URL, and the large image hosts content-negotiate on it — `media.giphy.com` and `i.postimg.cc`
answered `200 text/html` with a landing page, `i.imgur.com` answered `429`. Sending
`Sec-Fetch-Dest: image` (with `Sec-Fetch-Mode: no-cors`, `Sec-Fetch-Site: cross-site` and an
`Accept: image/*` list) returns the real bytes from all three. A `Referer` also satisfies postimg
and is deliberately not sent: it would disclose our crawling to every author-chosen third-party
host, and `Sec-Fetch-Dest` achieves the same without telling anyone anything.

**`dead` requires positive evidence; everything else is retryable.** `dead` is permanent and never
reclaimed, so only **404** and **410** may produce it — codes that state the image is not coming
back. A 429, a 5xx, a 403 or an empty 200 says something about the host's mood, not the image's
existence, and must be recorded as `error` so the attempt ceiling gets another look at it. The
first implementation inferred `dead` from any non-200, which is how the rate-limited imgur images
above were discarded permanently. This archive exists *because* these links die: a wasted retry
costs one request, a false `dead` costs the image forever. A 200 carrying a non-image body remains
`dead` — the host answered with a page, which is what a removal notice looks like — but it is
logged per host, because the two cases above prove that inference can be wrong at scale.

**Content-Type is a hint; the bytes are the evidence.** That per-host logging immediately earned
itself: `content.screencast.com` serves live 2014-era Jing PNGs — twelve years old, exactly what
this archive exists to rescue — labelled `application/octet-stream`, and 45 of them were recorded
as gone in the first batch after the fix above. So the type is resolved from the leading bytes
(PNG, JPEG, GIF, WebP, BMP, TIFF signatures) whenever the declaration is generic or absent. A
declared `image/*` is still trusted as-is even when no signature matches, because SVG has none and
neither will the next format: sniffing may only ever widen what is accepted, never narrow it.
Expect more of this. Every false `dead` so far has arrived as a batch from a single host, which is
why the warning names the host rather than the URL.

**Nothing is translated at ingest time.** Store originals; translation is a phase 3 concern and
belongs at query time, on the handful of documents actually retrieved.

### Out of scope

Web UI, embeddings, search, translation, summarisation, digests, vote counts, newspaper metadata,
author profiles. Telegram is in scope only for the two alerts named under Components — nothing
routine, nothing scheduled.

## Later phases, in brief

**Phase 2 — public archive.** Browse and keyword search over the corpus. Postgres full-text search
is enough to start and will visibly fail across languages, which is the motivation for phase 3.
The public site should not be hosted on the Jetson at home; the Jetson holds the crawler, the
database and (later) the index.

**Phase 3 — semantic search.** `bge-m3` embeddings computed locally on the Jetson: an encoder model,
which is what that hardware is genuinely good at, and free to re-run when the model changes.
Storage as `halfvec(1024)` with a `bit(1024)` binary-quantised HNSW index — about 400 MB for the
full 2.8M archive, so 8 GB of unified memory is not a constraint. Retrieval reranks the binary
candidates against the full vectors.

Postgres stays the only datastore. The queries this archive exists to answer are hybrid — a
metadata predicate and a semantic one together, "articles from Serbia in March about X" — and in
Postgres that is one statement with a `WHERE` clause. A separate vector database would mean either
mirroring all metadata into it or intersecting two result sets by hand, and a second database to
run on a 7 GB machine. At 2.8M vectors there is nothing pgvector cannot handle; the case for a
dedicated store starts an order of magnitude further up.

**pgvector 0.8 or newer is required.** Earlier versions apply the `WHERE` filter after traversing
the HNSW graph, so a filtered vector query silently returns too few rows or poor recall. Iterative
index scans, added in 0.8, fix exactly that — and filtered vector search is the primary query
shape here, not an edge case.

Prerequisite: the NVIDIA container runtime must be installed and configured on the host first.

**Phase 4 — digest / sentiment.** A daily Telegram digest is one cron job and one model call once
the crawler exists. Sentiment analysis is the weakest idea here: it produces confident output that
cannot be validated. If built, anchor it to countable things (comment volume by country, topic tags)
rather than a mood score.

## Risks

**The parser will break.** eRepublik will change its markup eventually. Mitigated by fixture-based
tests and, since the raw-HTML window was dropped rather than kept (see "Decisions"), by
`babel refetch`: a fixed parser can be pointed back at any already-collected range instead of
needing the original bytes on hand.

**A month-long crawl invites trouble** — restarts, IP throttling, transient 5xx. Handled by the
resumability design above; correctness here is worth more than speed.

**Cloudflare may treat VPN exit nodes differently from residential addresses.** Every measurement
in this document was taken from a residential connection; none of it has been confirmed from
behind a tunnel. An earlier crawler in this monorepo did hit Cloudflare blocks. This is the
cheapest risk to retire and the most expensive to discover late, so the first task of phase 1 is
to fetch a hundred articles through the VPN and confirm the responses are clean — before any
other code is written.

**Sharing a network namespace complicates reaching Postgres.** A container with
`network_mode: "service:gluetun"` has no bridge interface, so the database cannot simply be
addressed by service name. Either the database joins the same namespace and is reached on
localhost, or Gluetun is configured to route local subnets outside the tunnel. To be settled in
the implementation plan.

**ARM64.** The rest of the monorepo builds amd64 images for Proxmox. Jetson needs arm64 builds on
an `nvcr.io/nvidia/l4t-*` base plus the NVIDIA container runtime. Not hard, but it is a separate
release path, and it only becomes relevant in phase 3.

**Publishing a mirror of other players' content** is a deliberate decision to make before phase 2
goes live, not after.
