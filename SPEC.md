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

So image capture belongs in phase 1, in the same pass as the article, in the same newest-first
order. A later pass over 2024's articles will find strictly less than a pass today.

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

A service that ends up holding every eRepublik article and its comments in Postgres, and stays
current from then on. No AI, no web UI, no Telegram.

### Components

**Live poller.** Reads RSS pages 1–5 every 15 minutes, extracts article IDs, enqueues unseen ones.
The ~3-day feed window means a multi-hour outage loses nothing.

**Backfill worker.** Walks IDs downward from a persisted cursor, enqueueing as it goes. Starts at
the current maximum and runs indefinitely. There is no target depth — the archive simply gets
deeper, and the most useful years arrive first.

**Fetcher.** Single work queue shared by both producers, drained by a pool of workers behind one
global rate limiter. Fetches `/en/article/{id}/1/1000`, parses, writes.

**Parser.** Pure functions over HTML strings, tested against saved fixtures. No network, no DB.

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

Every attempted ID gets a row recording the outcome. The backfill cursor is persisted after each
batch. A restart resumes from the cursor and retries only IDs marked as errors — never re-fetches
successes, never re-walks completed ranges. This matters: the full backfill is a month long and
will be interrupted.

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
    status     text        NOT NULL,            -- ok | dead | skipped_no_space | error
    checked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, position)
);

CREATE TABLE fetch_log (
    article_id bigint PRIMARY KEY,
    status     text        NOT NULL,            -- ok | missing | error
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

**A free-space floor is mandatory.** Before each batch the crawler checks available space and
stops fetching images below the threshold, recording `skipped_no_space` and continuing with text.
On the intended host the image tree shares a filesystem with both Postgres and the OS root, so
filling it takes down the machine and not merely the crawl. Text ingestion must never be blocked
by image storage.

### Target host

An NVIDIA Jetson Orin Nano Super dev kit: 6 cores, 7.4 GiB of unified memory, a 465 GB NVMe with
~388 GB free, JetPack 6 (L4T R36.4.7), Docker 29.6. Bare metal, so `/dev/net/tun` is available for
the VPN.

Notes that follow from this:

- Text, metadata and vectors total roughly 15 GB. The disk is not a constraint for them.
- Images are, eventually. 388 GB does not comfortably hold the full ~410 GB unreduced set on a
  partition it shares with the OS, hence the free-space floor and the relocatable `IMAGE_ROOT`.
- Memory is unified between CPU and GPU; the 3.7 GiB of zram swap is compressed RAM and adds no
  real capacity, so it should not be counted toward an index budget.
- The NVIDIA container runtime is **not** currently configured — `docker info` reports only `runc`.
  Irrelevant to phase 1, a prerequisite for phase 3.

### Decisions

**Raw HTML is not archived at full scale.** 118 GB for something we can re-fetch. Keep raw HTML
for the last 30 days only, as insurance against a parser bug or a markup change.

**Comments come free.** They arrive in the same request as the article, so there is never a reason
to fetch an article without them.

**Backfill runs newest-first.** The archive is useful from day one and no depth decision has to be
made up front. Stopping at any point still leaves the most relevant years collected. Images make
this ordering matter more, not less: the newest are both the most numerous survivors and the ones
still actively disappearing.

**Images are fetched in the same pass as the article.** Not a later sweep. The survival curve means
a deferred pass recovers strictly less, and the loss is permanent.

**Nothing is translated at ingest time.** Store originals; translation is a phase 3 concern and
belongs at query time, on the handful of documents actually retrieved.

### Out of scope

Web UI, embeddings, search, translation, summarisation, Telegram, vote counts, newspaper metadata,
author profiles.

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
tests and the 30-day raw HTML window.

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
