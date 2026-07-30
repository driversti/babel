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
  **Its operator-facing half is closed as of 2026-07-28: `babel run --sweep-only` skips the walk and
  goes straight to `claim_retryable`, and deliberately neither reads nor writes the cursor, so a
  sweep leaves the walk exactly where it was with nothing to restore afterwards.** What remains open
  is the running service, which still cannot interleave the phases on its own.
  `run_backfill` only reaches `claim_retryable` once the cursor passes `stop_at`. Nothing is lost
  (waiting does not burn attempts), but README.md and CLAUDE.md both said the running service
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

Found during Task 11's own review rounds (the markup render guard) rather than live, and now closed
by the whole-branch review's fix wave — see
`.superpowers/sdd/2026-07-27-article-markup/final-fix-report.md` for the full measurements and
mutation results:

- **The render cap's value is settled at 32 KiB, not 64.** `MAX_MARKUP_BYTES`
  (`src/babel/web/markup.py`) shipped at 64 KiB against a 354 ms figure that only searched
  five-byte-per-level shapes (`<div>`, `<dir>`); it missed that four-byte list elements (`<ul>`,
  `<ol>`, `<dd>`, `<dt>`, `<li>`) reach 16,384 levels in the same space instead of 13,107 and are not
  scope boundaries, so each additionally triggers HTML5's "have a p element in button scope" walk —
  and that `<a>`/`<nobr>` stack adoption-agency walks on top of that. The hill-climbed worst shape
  found this way, `"<ol><ol><dd><ol><li><ul><a><ol><nobr><ul>"`, measured **1,215 ms** at 64 KiB —
  over the 1 s budget the cap exists to enforce — and ~308 ms at the new 32 KiB, restoring the
  margin the old value was believed to have.
- **The cap's own test now covers that shape.** `tests/web/test_markup.py`'s timing test pinned the
  cap's value using `<div>` (356 ms, 2.8x headroom) while calling it "the single worst shape measured
  across every round" — it could not fail for an over-large cap, which is how 64 KiB survived to be
  committed. It now uses the hill-climbed shape above; killed by mutation, it fails at 64 KiB and
  passes at 32 KiB.
- **The cap is now per request, not just per body.** `render_body` still runs once per article body
  and once per comment inside a single `async def` route handler, with no `LIMIT` on `get_comments`
  — but `web/routes.py`'s `article` route now accumulates elapsed render time across the whole
  handler and stops calling `render_body` once `_RENDER_BUDGET_SEC` (1.0 s) is spent. The article
  renders first and unconditionally, so a hostile comment thread can only cost the *comments* their
  markup, never the article's own. Ten comments at the cap measured 3.4 s unbounded before this fix
  and ~1.0-1.3 s after, regardless of comment count.

- **Two gaps in what pins that budget, both still open.** The behaviour above is correct — the final
  review verified it directly — but neither half of it is defended by a test, on a module whose
  history is eight tests that passed for the wrong reason.

  First, "the article renders first and unconditionally" is asserted in a comment at
  `web/routes.py` and by nothing else: a mutation that moves the article render below the comment
  loop and gates it on the remaining budget — precisely what the comment forbids — survives all 190
  tests in `tests/web`. Every existing test uses a tiny article, so the budget is never spent before
  the article render in any of them.

  Second, the budget test's `0 < fell_back` half is coupled to machine speed. It needs ten renders
  to exceed 1.0 s, i.e. more than 0.1 s each against ~0.33 s today — about a 3.3x margin, so on
  hardware roughly four times faster all ten comments would render inside the budget and the
  assertion would pass vacuously. Its slow-machine half is fine.

  One deterministic test closes both, with no wall clock: monkeypatch `_RENDER_BUDGET_SEC` to `0.0`
  and assert that no comment renders while the article still does.

## Operating the live run

It runs as five compose services on the deploy host (see SPEC.md "Target host"; the address is not
in this repo), plus a sixth that only runs when an operator asks for it. `gluetun` is the tunnel,
`db` is Postgres, `crawler` walks and polls, `images` drains the image queue, and `web`
(`babel serve`) serves the public read-only archive. The sixth is `sweep`, behind a compose profile
so a bare `up -d` never starts it: it is `crawler`'s image running `--no-poll --sweep-only`, for
re-collection passes, and it is a service rather than a `compose run --rm` one-shot because that
one-shot carries `restart: no` and is deleted on exit — a reboot ended a 34-hour re-collection with
every other container coming back healthy around it. The profile exists because `RateLimiter` is
per process: a sweep beside the walk is two requests a second, so stop `crawler` first. `web` is
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

**Build before you migrate**, which is the opposite of what this file said until 2026-07-28. The
Dockerfile does `COPY migrations ./migrations` and nothing bind-mounts that directory, so
`babel migrate` run before the rebuild executes inside the *old* image and cannot see a migration
that arrived with `git pull`. It reports nothing to apply and exits 0, and then `web` refuses to
start naming a migration the operator just watched "succeed".

```bash
docker compose stop crawler images
docker compose build crawler images web
docker compose run --rm crawler babel migrate
docker compose up -d crawler images web
```

**Rolling the crawler back after migration 007 leaves `body_raw` stale beside a freshly-updated
`body`.** A rollback reverts the application image, not the schema — migration 007's `body_raw`
column stays on the table regardless. The pre-`feat/article-markup` `save_article` never mentions
that column, so its `ON CONFLICT ... DO UPDATE SET` has no `body_raw = EXCLUDED.body_raw` clause in
it: any article or comment the rolled-back crawler re-fetches (a re-poll, a sweep, a manual
`refetch`) gets `body` refreshed to the newer plain text while `body_raw` is left exactly as it was
at the moment of the rollback — increasingly out of step with the `body` sitting next to it, for as
long as the rollback lasts. Nothing crashes and nothing is lost; the two columns simply stop
agreeing until the crawler is rolled forward again.

**Two things gate the site being reachable, and both come before the hostname exists, not after.**
The runbook for each is in README.md under "One-time setup"; they are named here because CLAUDE.md
used to sequence the first one *after* deployment, which is the wrong way round.

1. **Audit the blobs captured before the address guard.** Everything already sitting in
   `article_images`/`images` predates the scheme/address guard (`a4ba9ca`) and the abort that
   actually severs an over-budget download (`e5461dc`), so neither protects rows already on disk —
   an internal address answering with an `image/*` type could have been stored and would still be
   `ok` today. Publishing `/img/{sha256}` is precisely what turns a blind fetch into a readable one,
   so the audit belongs before that happens. `source_url` is retained for every row, which makes the
   check one query; it is in README, and it is a heuristic over stored text, not a re-resolution.
2. **Re-collect the article bodies.** Every article and comment collected before migration 007 has
   no `body_raw`, so it renders through the plain-text fallback: no paragraphs for the oldest rows,
   and no emphasis, links or in-position images for any of them. Re-collection is what fills the
   column; it remains a deliberate stop/sweep/restart pass, not a queued job, because of M1 above: a
   running service does not reach its sweep phase until the walk bottoms out. Three commands, in
   README.

Neither is optional and neither is automatic; the site should not be pointed at a hostname until both
have been done.

One more thing worth knowing before anyone adds a debug flag to `babel serve`: the bare-`Exception`
handler `create_app` always registers (`web/app.py`) does **not** protect against one. Verified
directly against this project's FastAPI version: `ServerErrorMiddleware.__call__` checks
`self.debug` first and returns Starlette's own traceback page unconditionally when it is set,
before it ever looks at a registered Exception handler — `FastAPI(debug=True)` with this exact
handler still installed still serves the traceback, not this handler's sentence. So turning on
debug here would silently bypass this handler and leak schema and file-path detail on the public
site it exists to protect; it must stay off in production.

**The render guard (`MAX_MARKUP_BYTES`, `src/babel/web/markup.py`) is a size cap, not a cleverness —
do not reintroduce a markup-aware guard.** `babel serve` refuses to render markup for a body over the
cap and falls back to the stored plain text instead. That plainness is deliberate, paid for the hard
way: five successive attempts to bound the same risk by scanning the markup for how deeply it would
nest were each bypassed by a different HTML5 tokenizer behaviour — `<div/>` is not self-closing; a
close tag matching no open element is ignored; scope markers and RAWTEXT elements swallow close tags;
a quoted string is only an attribute value right after `=`; a close tag sitting inside a comment is
text, not markup — with measured stalls of 6 to 90 seconds each, and one round's fix opened four new
bypasses while closing the one it targeted. A byte count has no tokenizer state to get wrong, because
it never reads the markup at all: it is the one bound in this function's history that a cleverer input
shape cannot defeat. The full account is in `docs/superpowers/plans/2026-07-27-article-markup.md`'s
Task 11 section and the comment above `MAX_MARKUP_BYTES` itself.

The cap's byte value is settled at 32 KiB, not 64: a list-heavy shape (rather than plain nesting),
`"<ol><ol><dd><ol><li><ul><a><ol><nobr><ul>"`, renders in 1,215 ms at the old 64 KiB — over the 1 s
budget the guard exists to enforce — and ~308 ms at the current 32 KiB, which is why the constant
moved. Do not treat either figure as a proven worst case without re-measuring first: the 64 KiB
value was itself believed safe on the strength of a search that only tried five-byte-per-level
shapes, and missed this one. The cap is also now per request, not just per body:
`web/routes.py`'s `article` route accumulates render time across the article body and every comment
inside the same `async` handler and stops calling `render_body` once `_RENDER_BUDGET_SEC` (1.0 s) is
spent, so a page with many comments no longer stalls the event loop for the sum of all of them — see
`.superpowers/sdd/2026-07-27-article-markup/final-fix-report.md` for the measurements.

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
  parser fix or a markup change. The sweep phase that picks them up is only reached once the walk
  bottoms out (M1 above), so during a walk this queues work for ~32 days' time. To act on it now,
  `docker compose stop crawler` then `docker compose --profile sweep up -d sweep` until the queue
  drains (`--sweep-only`, which that service runs, is what makes the sweep reachable at all — see
  below) — README, "Re-collect the bodies before launch", has the exact commands. **A `--to` chosen
  as "the newest article right now" leaves a gap**: the poller keeps ingesting above that ceiling
  with the old image until the rebuild lands, and nothing revisits those IDs afterwards — the walk
  only descends and the sweep only sees what was queued. The first re-collection lost 27 articles
  that way. Query the backfilled column after the sweep drains rather than assuming coverage;
  README has the query and the follow-up range
- `docker compose --profile sweep up -d sweep` / `--profile sweep stop sweep` — the re-collection
  pass. The profile is what keeps a bare `up -d` from running it beside the walk at two requests a
  second, and `restart: unless-stopped` is what carries it across a host reboot; stop it by hand
  when `SELECT count(*) FROM fetch_log WHERE status = 'stale'` reaches zero, and that stop is what
  keeps it down afterwards. It idles rather than exiting when the queue drains, so nothing stops it
  for you
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
  see "Operating the live run" for why and for the deploy order. It does *check* them: one throwaway
  connection reads `schema_migrations` before anything is served and refuses, naming whichever of
  `005_browse.sql`/`007_body_markup.sql` is missing. Without that check a skipped migrate step
  answers 503 on every page — `UndefinedColumnError` is a `PostgresError`, so it lands in the
  database-down handler — while `/healthz` and the compose healthcheck stay green, which points the
  operator at Postgres instead of at the deploy
- `docker compose run --rm crawler babel hide --article <id>` or `babel hide --image <sha256>` —
  suppress an article, or stop serving one blob by digest. These are two separate commands on
  purpose: hiding an article does not withhold its images, because a blob is content-addressed and
  commonly shared across many articles. `--image` reports how many articles currently cite the
  digest so the blast radius is visible before deciding — a takedown request is not fully handled
  until both commands have been run for anything that needs to disappear entirely. Nor is it
  instantaneous: `/img/{sha256}` is served `max-age=86400`, so a cache that already holds the blob
  keeps serving it for up to a day (pages are `max-age=300`). Purge the CDN too — README, "Taking a
  page down"
