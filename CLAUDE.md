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
before changing anything.** It is the second whole-branch review. It originally left five findings
open; I9 and M2 have since closed, leaving three — see "What is still open" below.

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

**I9 is now closed, and the remedy this file used to prescribe for it was not what closed it.**
`_bytes_getter` (`cli.py`) now calls `response.quit_now.set()` before `await response.aclose()` on
every path that abandons a response mid-flight — a redirect, a declared-`Content-Length` rejection,
and a mid-stream over-budget abort — closed by commit `e5461dc`, then extended by `1bd5e9c` to a
fourth path found in a later round: a timeout cancelling the fetch. The wording this entry
previously gave as the fix, "set `response.quit_now` before `await response.aclose()`", is itself a
no-op: `quit_now` is an `asyncio.Event` captured by reference inside curl_cffi's write-callback
closure, so *assigning* to the attribute only rebinds the name in local scope and never reaches the
object the closure holds — only calling `.set()` on the existing object does. Measured directly:
applying that exact wording and nothing else, against a 300 MB stream with an 8 MiB cap, still
transferred 100% of the body. `tests/test_bytes_getter.py`'s fake responses now carry a `quit_now`
that records whether and when `.set()` was called, which is what makes the two remedies
distinguishable to the suite at all — a no-op `aclose()` on the previous fake could not.

**M2 is closed too, found while verifying the above.** `classify_url` (`crawler/images.py`, commit
`a4ba9ca`) rejects any image URL whose scheme is not `http`/`https`, or whose resolved address is
not globally routable — loopback, link-local, RFC1918, CGNAT, multicast and unspecified are all
refused before the fetch is ever dialled, and `_bytes_getter` applies it to every hop of a redirect
chain, not just the URL an article wrote, so a public host cannot 302 its way around the guard
either. A blocked address is permanent and records `dead`; a resolver hiccup is `error` and stays
retryable, the same distinction this project draws everywhere else between "the image is gone" and
"we could not check." This file's "What is still open" list still named it as missing; it was not.

**Runs on the x86_64 host, not the Jetson.** The Tegra kernel lacks `CONFIG_IP_ADVANCED_ROUTER`,
so `ip rule` is unavailable and gluetun cannot start there at all. Details in `SPEC.md` under
"Target host". The Jetson keeps phase 3.

## What is still open

From the second whole-branch review, in the order it recommends. Its Criticals (C2, C3), I8, I9 and
M2 are already fixed and deployed; these are not.

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
- **M3 — still open for the crawler's own queries; prevented from recurring in the newer browse
  path, not closed.** `tests/db/test_repo.py`'s two partial-index EXPLAIN tests (covering
  `claim_retryable` and `claim_pending_images`) still build their own copy of the SQL and EXPLAIN
  that, proving only that Postgres can use a partial index from a `Const` — the invariant they exist
  to guard is the one this project has broken twice, and it is still unguarded here.
  `RETRYABLE_STATUSES` (repo.py) is still used nowhere and still reads as an invitation to make the
  literal a bound parameter — the exact change the tests are meant to block. The article-browser
  work added a different EXPLAIN suite, `tests/db/test_browse_plans.py`, that does take its SQL from
  `build_list_query` itself and asserts on `Index Cond:` rather than the index name — but that
  guards the four new browse queries, not the two above. Use `RETRYABLE_STATUSES` or delete it;
  either closes the half of this finding the browse work did not touch.

Not from the review, found live and unresolved: **image throughput is the thing to watch.** It ran
at 0.04 img/s at its worst and 1.35 img/s after the circuit breaker, against the ~5.3 img/s the
article walk produces, so `article_images` still grows. Four separate causes were found and fixed
(see the commits) and the queue is no longer growing for a *broken* reason — but whether the drain
keeps up over a month is unmeasured. Watch the pending count and `docker compose logs images`.

## Operating the live run

It runs as five compose services on the deploy host (see SPEC.md "Target host"; the address is not
in this repo). `gluetun` is the tunnel, `db` is Postgres, `crawler` walks and polls, `images`
drains the image queue, and `web` (`babel serve`) serves the public read-only archive. `web` is
deliberately outside gluetun's namespace: it needs inbound connections, which that namespace cannot
accept, and its only outbound dependency is Postgres on the bridge, so the site stays up when the
tunnel is down. All state is in Postgres plus `data/images`; both are gitignored bind mounts, so
`git pull && docker compose build && docker compose up -d <service>` is the whole deploy for
`crawler`, `images` and `web` individually. Restarting any one of them outside of a migration is
safe at any time — the cursor is persisted and a partial batch is re-walked without HTTP.

**That one-liner is not safe for a migration.** Both `crawler` and `images` apply pending
migrations at startup, so bringing up either one first runs a pending migration file against a live
walk. Migration 005 (the browse indexes and the suppression tombstones) is an explicit
stop/migrate/start, never a bare `up -d`, because plain `CREATE INDEX` (`CONCURRENTLY` is
unavailable through this project's transaction-wrapped migration runner) takes `ShareLock` for the
whole build, which blocks concurrent writers — not readers — for as long as the build takes:

```bash
docker compose stop crawler images
docker compose run --rm crawler babel migrate
docker compose build web
docker compose up -d crawler images web
```

**A one-time audit is worth running once `web` and this branch's image-capture fixes are both
deployed.** Everything already sitting in `article_images`/`images` was captured before the
scheme/address guard (`a4ba9ca`) and before the abort that actually severs an over-budget download
(`e5461dc`) existed, so neither protects rows already on disk — an internal address answering with
an `image/*` type could have been stored and would still be `ok` today. `source_url` is retained for
every row, which makes the check one query:

```sql
SELECT article_id, position, source_url, sha256
FROM article_images
WHERE status = 'ok'
  AND (source_url !~* '^https?://'
       OR source_url ~* '(://|@)(localhost|127\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|169\.254\.|0\.0\.0\.0)');
```

This is a heuristic over stored text, not a re-resolution of every hostname at audit time — a host
that only pointed at a private address when it was first crawled will not match. Anything the query
does return is worth inspecting by hand and withholding with `babel hide --image` if it turns out to
be internal rather than a real image host.

One more thing worth knowing before anyone adds a debug flag to `babel serve`: the bare-`Exception`
handler `create_app` always registers (`web/app.py`) does **not** protect against one. Verified
directly against this project's FastAPI version: `ServerErrorMiddleware.__call__` checks
`self.debug` first and returns Starlette's own traceback page unconditionally when it is set,
before it ever looks at a registered Exception handler — `FastAPI(debug=True)` with this exact
handler still installed still serves the traceback, not this handler's sentence. So turning on
debug here would silently bypass this handler and leak schema and file-path detail on the public
site it exists to protect; it must stay off in production.

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
- `docker compose up -d` — run the stack, including all three long-running services, `crawler`,
  `images` and `web`
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
- `babel serve` — run the public read-only web archive; the `web` compose service. Refuses to start
  if `WEB_DATABASE_URL` is unset or equal to `DATABASE_URL`, and never applies migrations itself —
  see "Operating the live run" for why and for the deploy order
- `docker compose run --rm crawler babel hide --article <id>` or `babel hide --image <sha256>` —
  suppress an article, or stop serving one blob by digest. These are two separate commands on
  purpose: hiding an article does not withhold its images, because a blob is content-addressed and
  commonly shared across many articles. `--image` reports how many articles currently cite the
  digest so the blast radius is visible before deciding — a takedown request is not fully handled
  until both commands have been run for anything that needs to disappear entirely
