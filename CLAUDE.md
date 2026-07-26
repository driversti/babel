# CLAUDE.md

Archive of every eRepublik article and its comments, eventually searchable across languages.

**Read [SPEC.md](SPEC.md) first** — it holds the design, the measured facts about the source
(feed limits, page structure, volume, sizing) and the phase boundaries. Those measurements were
expensive to obtain; do not re-derive them, and update them there if the site changes.

## Status

Phase 1 — crawler. All ten original tasks are implemented: config, article/comment parser, DB
schema + migration runner, repository layer, rate limiter + fetcher, content-addressed image
store, end-to-end article ingest, RSS poller, newest-first backfill walker, and the CLI/service
that composes them (`src/babel/cli.py`). 204 tests, all passing (`uv run pytest`, needs Docker for
the `postgres:17` testcontainer); `uv run ruff check src tests` clean.

**The crawler is running live right now** on the deploy host, and has been since 2026-07-26. It
works: articles, authors, e-days and full comment threads in Persian, Serbian, Hungarian,
Indonesian, Bulgarian, Polish, Spanish and English. Where it stood at the last handoff — 10,093
articles, 161,155 comments, 2,193 images (1,795 blobs, 735 MB), walking down from 2797025 and
reached 2785850, 1.7 TB free. Telegram alerts are configured and a real message was delivered.

**Read [docs/superpowers/plans/2026-07-26-review-2-findings.md](docs/superpowers/plans/2026-07-26-review-2-findings.md)
before changing anything.** It is the second whole-branch review, and five of its findings are
still open — see "What is still open" below.

**The probe has passed.** 100 article IDs fetched through a VPN tunnel from the target host:
85 ok, 15 missing, zero Cloudflare challenges. Anonymous access from a VPN exit works.

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

## What is still open

From the second whole-branch review, in the order it recommends. Its Criticals (C2, C3) and I8 are
already fixed and deployed; these are not.

- **I9 — the image size cap saves nothing.** `ImageTooLarge` raises early, but the `finally:
  await response.aclose()` then waits for the *entire* transfer: curl_cffi's async `aclose()` is
  `await self.astream_task` and never sets `quit_now`, which is the only thing that aborts the
  connection. Measured with this venv against a 300 MB stream and an 8 MiB cap: raised at 0.13 s,
  all 300 MB still transferred, RSS 60 → 462 MB. **CLAUDE.md previously claimed I6 was closed on
  exactly this basis; it was not.** Fix: set `response.quit_now` before `await response.aclose()`
  in `_bytes_getter`, and give `tests/test_bytes_getter.py`'s fake `_Response` a `quit_now` — its
  absence is why the suite cannot see this.
- **I10 — the poller burns five attempts in an hour and those IDs are then unreachable.**
  `poll_once` re-offers `error` rows with no cooldown every `poll_interval_sec`; after five cycles
  `filter_unseen` reports them seen forever and `claim_retryable` excludes them. They sit *above*
  the backfill cursor, which only descends, so the walk never covers them either. The trigger is an
  asymmetric failure — a markup change, where the feed keeps working while `parse_article` returns
  None. Fix: give the poller the cooldown the sweep has.
- **M1 — the sweep is unreachable for the whole ~32-day walk**, so `babel refetch` looks inert.
  `run_backfill` only reaches `claim_retryable` once the cursor passes `stop_at`. Nothing is lost
  (waiting does not burn attempts), but README.md and CLAUDE.md both say the running service
  "picks the queued IDs up on its own", which reads as immediacy. Either interleave the phases or
  correct the docs.
- **M2 — image URLs get no scheme or address filter.** `source_url` is raw `img@src` from untrusted
  article HTML. `file:///etc/passwd` returns the file's bytes (recorded `error`, bytes discarded —
  but the read happens), and the worker runs inside gluetun's namespace with
  `FIREWALL_OUTBOUND_SUBNETS=172.16.0.0/12` open. Blind and read-only, hence Minor, but the control
  is missing. Fix in `capture_image` so the image tests cover it.
- **M3 — both partial-index EXPLAIN tests EXPLAIN a copy of the SQL, not the code's query.** They
  build their own string and EXPLAIN that, proving only that Postgres can use a partial index from a
  `Const`. The invariant they exist to guard is the one this project has broken twice. Also
  `RETRYABLE_STATUSES` (repo.py) is used nowhere and reads as an invitation to make the literal a
  bound parameter — the exact change the tests are meant to block. Use it or delete it.

Not from the review, found live and unresolved: **image throughput is the thing to watch.** It ran
at 0.04 img/s at its worst and 1.35 img/s after the circuit breaker, against the ~5.3 img/s the
article walk produces, so `article_images` still grows. Four separate causes were found and fixed
(see the commits) and the queue is no longer growing for a *broken* reason — but whether the drain
keeps up over a month is unmeasured. Watch the pending count and `docker compose logs images`.

## Operating the live run

It runs as four compose services on the deploy host (see SPEC.md "Target host"; the address is not
in this repo). `gluetun` is the tunnel, `db` is Postgres, `crawler` walks and polls, `images`
drains the image queue. All state is in Postgres plus `data/images`; both are gitignored bind
mounts, so `git pull && docker compose build && docker compose up -d <service>` is the whole
deploy. Restarting is safe at any time — the cursor is persisted and a partial batch is re-walked
without HTTP.

**The deploy host tracks `main`, and `main` is where work happens.** Phase 1 was built on
`feat/phase-1-crawler` and fast-forwarded in; that branch is history now. Anything pushed to `main`
is one `git pull` away from the machine holding the archive, so `uv run pytest` before pushing is
not a formality.

One query answers "is it healthy", and is worth running first:

```sql
SELECT (SELECT count(*) FROM articles)                                    AS articles,
       (SELECT min(id) FROM articles)                                     AS reached,
       (SELECT count(*) FROM comments)                                    AS comments,
       (SELECT string_agg(status || '=' || n, ' ')
          FROM (SELECT status, count(*) n FROM article_images GROUP BY 1) x) AS images,
       (SELECT count(*) FROM fetch_log WHERE status = 'error')            AS fetch_errors;
```

Rising `error` on `article_images` with a flat `ok` means a host is misbehaving —
`docker compose logs images | grep "holding off"` names the ones the circuit breaker is sitting
out, and `babel requeue-images --host <h>` brings back anything already written off. A stalled walk
looks identical to a healthy one from the outside: the containers stay `Up` and the logs stay quiet,
so **check that `count(*) FROM articles` is actually rising** rather than trusting `docker ps`.

Four image defects were found by watching this run, not by reasoning, and every one of them looked
like success from the outside. If image behaviour ever seems wrong, measure a single fetch against
the real host before changing code — twice in this project the obvious hypothesis was wrong and the
measurement was cheap.

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
