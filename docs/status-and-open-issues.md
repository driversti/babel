# Status, history and open findings

Moved out of CLAUDE.md verbatim (as of 2026-11-21); figures are point-in-time snapshots.

## Status

Phase 1 — crawler. All ten original tasks are implemented: config, article/comment parser, DB
schema + migration runner, repository layer, rate limiter + fetcher, content-addressed image
store, end-to-end article ingest, RSS poller, newest-first backfill walker, and the CLI/service
that composes them (`src/babel/cli.py`). 535 tests, all passing (`uv run pytest`, needs Docker for
the `pgvector/pgvector:0.8.6-pg17-trixie` testcontainer); `uv run ruff check src tests` clean.

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

**The archive holds 162,618 articles and 2,985,540 comments as of 2026-07-30**, not the 10,093
articles and 161,155 comments recorded at the last handoff above — the crawler has kept walking and
polling in between. `lang` is NULL in every row; nothing populates it. That does not affect
embeddings — `bge-m3` needs no language label — but it means no "Serbian articles about X" filter
can be built on that column today.

**Phase 3 — cross-language semantic search — is on `feat/phase-3-embeddings`.** Design in
`docs/superpowers/specs/2026-07-30-embeddings-design.md`. A `bge-m3` embed service on a Jetson turns
article text into vectors; `babel embed` drains the corpus queue through it; `babel serve`'s
`/search` embeds the reader's query through the same service and ranks by cosine similarity via a
quantise-then-re-rank pgvector query. See the Commands section below for `babel embed` and
`/search`, and "Operating the live run" for the `embed` compose service — deliberately outside
gluetun's namespace, since it never talks to eRepublik, only to Postgres and the Jetson.

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

Update 2026-09-01: the walk finished (cursor 0, 1.54M articles). `article_images` at that point was
`pending` 3.07M / `ok` 719k / `dead` 270k / `error` 266k, and 98.6% of the `error` rows were already
at the five-attempt ceiling — abandoned but not labelled, and still counted by `stuck_image_hosts`.
Most of the `pending` mass is on hosts that stopped existing years ago (every `iNN.tinypic.com`,
`prikachi.com`, `www.dodaj.rs`, …), each still costing five fetches before it lands. `babel
image-hosts` (report + live probe) and `babel kill-image-host` (bulk `pending`/`error` → `dead` for
a named host, guarded by lifetime `ok`, reversible via `requeue-images`) exist to clear that —
see Commands. This does not touch re-accumulation from the poller; a host written off can be
re-enqueued by a later article citing it, which is rare enough to ignore for now.

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

