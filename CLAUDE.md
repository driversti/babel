# CLAUDE.md

Archive of every eRepublik article and its comments, eventually searchable across languages.

**Read [SPEC.md](SPEC.md) first** — it holds the design, the measured facts about the source
(feed limits, page structure, volume, sizing) and the phase boundaries. Those measurements were
expensive to obtain; do not re-derive them, and update them there if the site changes.

## Status

Phase 1 — crawler. All ten original tasks are implemented: config, article/comment parser, DB
schema + migration runner, repository layer, rate limiter + fetcher, content-addressed image
store, end-to-end article ingest, RSS poller, newest-first backfill walker, and the CLI/service
that composes them (`src/babel/cli.py`). 175 tests, all passing (`uv run pytest`, needs Docker for
the `postgres:17` testcontainer); `uv run ruff check src tests` clean.

**The probe has passed.** 100 article IDs fetched through a VPN tunnel from the target host:
85 ok, 15 missing, zero Cloudflare challenges. Anonymous access from a VPN exit works.

**The crawler has now run for real**, on the target host, collecting articles and comments at
1 req/s from article 2797025 downward. It works: articles, authors, e-days, and full comment
threads in Persian, Serbian, Hungarian, Indonesian, Bulgarian, Polish, Spanish and English.

That run also found two image-capture defects that no test could have caught, because both were
about what real hosts do — see SPEC.md under "Image requests must be shaped like an `<img>` load"
and "`dead` requires positive evidence". 64% of images on same-day articles were recorded as
permanently gone while being perfectly alive. Both are fixed and pinned by tests. **If you touch
image fetching, read those two spec entries first** — the failure is silent, and the status it
writes is the one that never gets retried.

**A fresh deployment bootstraps itself.** On an empty database the backfill starts at the newest
article the RSS feed advertises; `--start-id` remains an explicit override, and a stored cursor
still beats both, so a restart resumes rather than jumping back to the top. Before this, `babel run`
(which passes no `--start-id`) raised on an empty `crawl_cursor` and the container crash-looped
under `restart: unless-stopped` until a row was inserted by hand — which is how the first real
deployment went.

**C1 and I4 from the whole-branch review are closed** — see
`docs/superpowers/plans/2026-07-26-final-review-findings.md`: the backfill now cycles through a
walk/sweep/idle loop instead of returning, `fetch_log` gained a `stale` status, and
`babel refetch --ids/--from/--to` lets an operator queue already-collected articles for
re-collection by hand. The service is expected to run indefinitely; reaching article 1 is not an
end state, it just means the loop spends more time sweeping and idling.

**Image capture is now its own service, and I1/I2/I6 are closed.** `article_images` is a
drainable queue (`pending`/`error` rows), not a side effect of ingest: `babel run` only records
image URLs, and `babel images` — a second long-running process, `docker compose`'s `images`
service — claims batches newest-first, retries transiently-failed rows up to a ceiling, and
pauses (with a throttled alert) rather than touching the queue when free disk drops below the
floor. That closes I1 (nothing is stranded by an unclean shutdown — an unfinished row is just
still `pending`), I2 (images now run at their own configurable rate, `image_requests_per_second`,
independent of the article crawl), and I6 (the streaming `_bytes_getter` in `cli.py` aborts a
download via `ImageTooLarge` as soon as it passes `max_bytes`, and rejects early on an
over-large `Content-Length`, so a hostile or oversized image can no longer be buffered whole in
memory). The egress watchdog (`_watch_egress`) now alerts through the same Telegram notifier
before it re-raises on `IpLeak`, in both `babel run` and `babel images` — the spec has called for
"log, alert and exit" since the first commit.

**Runs on the x86_64 host, not the Jetson.** The Tegra kernel lacks `CONFIG_IP_ADVANCED_ROUTER`,
so `ip rule` is unavailable and gluetun cannot start there at all. Details in `SPEC.md` under
"Target host". The Jetson keeps phase 3.

## Key facts

- Article pages are public: `GET /en/article/{id}/1/1000` needs no slug and no session, and
  returns the article plus all comments in one request.
- The RSS feed caps at 5 pages (~3 days). It is for live polling only; history comes from
  walking article IDs downward.
- Whole archive is ~2.8M articles, ~9.6 GB of text. Current publication rate is ~18/day.
- Crawl politely: start at 1 req/s.

## Commands

- `uv sync` — install
- `uv run pytest` — tests (needs Docker for testcontainers)
- `uv run ruff check src tests` — lint
- `docker compose up -d` — run the stack, including both long-running services, `crawler` and
  `images`
- `docker compose run --rm crawler babel migrate` — apply migrations
- `docker compose run --rm crawler babel probe --newest <id>` — check the exit node is not challenged
- `docker compose run --rm crawler babel refetch --ids 123,456` or
  `babel refetch --from 100 --to 200` — queue already-collected articles for re-collection after a
  parser fix or a markup change; the running service's sweep phase picks them up on its own
- `docker compose run --rm crawler babel requeue-images --host i.imgur.com` — put one image host's
  `dead`/`error` rows back to `pending` with attempts reset, after fixing whatever caused that host
  to be misjudged. Pass a host you don't recognise to get a ranked list of hosts with stuck images.
  `dead` is permanent by design, and every false `dead` so far arrived as a batch from one host —
  this is the way back.
- `babel images` — drain the `article_images` queue until stopped; runs as the separate `images`
  compose service, stoppable/restartable independently of `crawler` since ingest only enqueues
  image URLs and never fetches the bytes itself
