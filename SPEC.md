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
| 1 | Crawler: live polling + backfill → Postgres | **current** |
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
- Metadata available logged-out: title, author name + citizen ID, country, `itemprop="datePublished"`,
  eRepublik day, comment count (in the `<meta name="description">` text).
- Vote counts are **not** rendered logged-out. Ranking by votes would need an `erpk` cookie.
  Comment count is a free substitute and is what "most discussed" should use.

### Comments

One `<div id="comment{id}" class="commentWrapper">` per comment. Inside: author link
`/en/citizen/profile/{id}`, display name, `<span>Day 6,819, 21:34</span>`, and the text in a `<p>`.
Nesting depth is encoded as `padding-left:{30*depth}px` on a wrapper div. Deleted comments render
as `<i>[removed]</i>`.

Comment IDs are globally sequential (~44.8M as of 2026-07).

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

Comments carry an eRepublik day number rather than a date. Verified against live data:

```
date = 2007-11-21 + eday days, in America/Los_Angeles
```

Cross-check: eDay 6 819 → 2026-07-23, which matches the article page.

### Decisions

**Raw HTML is not archived at full scale.** 118 GB for something we can re-fetch. Keep raw HTML
for the last 30 days only, as insurance against a parser bug or a markup change.

**Comments come free.** They arrive in the same request as the article, so there is never a reason
to fetch an article without them.

**Backfill runs newest-first.** The archive is useful from day one and no depth decision has to be
made up front. Stopping at any point still leaves the most relevant years collected.

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

**Phase 4 — digest / sentiment.** A daily Telegram digest is one cron job and one model call once
the crawler exists. Sentiment analysis is the weakest idea here: it produces confident output that
cannot be validated. If built, anchor it to countable things (comment volume by country, topic tags)
rather than a mood score.

## Risks

**The parser will break.** eRepublik will change its markup eventually. Mitigated by fixture-based
tests and the 30-day raw HTML window.

**A month-long crawl invites trouble** — restarts, IP throttling, transient 5xx. Handled by the
resumability design above; correctness here is worth more than speed.

**ARM64.** The rest of the monorepo builds amd64 images for Proxmox. Jetson needs arm64 builds on
an `nvcr.io/nvidia/l4t-*` base plus the NVIDIA container runtime. Not hard, but it is a separate
release path, and it only becomes relevant in phase 3.

**Publishing a mirror of other players' content** is a deliberate decision to make before phase 2
goes live, not after.
