# Second whole-branch review — findings

Branch `feat/phase-1-crawler`, the 29 commits since
[the first whole-branch review](2026-07-26-final-review-findings.md) — ~4 000 changed lines across
34 files, reviewed against the assembled diff (commit list, stat summary, full diff at 10 lines of
context) plus the working tree. Five independent lenses were run over it — data integrity,
longevity, privacy/security, tests, correctness — their findings deduplicated, and every non-minor
finding then put through one adversarial refutation pass against the real code, the real
`postgres:17` fixture and the real `curl_cffi` in `.venv`. Severities below are the post-refutation
severities: four findings were raised as critical and two of those were argued down, one important
was argued down twice. Nothing was refuted outright.

**The crawler is running live** and has collected ~3 200 articles, ~37 000 comments and ~480 image
blobs. So correctness *in the small* is now observed rather than reasoned — which is a real change
from the last review, where nothing had run. What is **not** observed, and what nothing in this diff
demonstrates, is month-scale behaviour: whether the service survives ~32 days unattended, and
whether it is quietly losing or corrupting data while looking healthy. Every finding below is about
that gap. Two of them are firing on the live run right now.

Also not verified: no lens exercised the running deployment, gluetun's actual routing, or Postgres
plans against a table of realistic size — the two partial-index EXPLAIN checks were run against an
empty test table, which is exactly why M3 matters. Behaviour under a genuine eRepublik markup change
is reasoned from mangled fixtures, not from a real change.

## Critical

### C2 — The image queue has no retry cooldown, so one bad minute from a host burns all five attempts

`claim_pending_images` (repo.py:215) filters on `status IN ('pending', 'error') AND attempts < $1`
and nothing else. There is no `checked_at` predicate and no `cooldown_sec` parameter, and
`run_image_worker` sleeps only when the batch comes back *empty* — a non-empty batch falls straight
through the `for` loop and re-enters the `while`. Because the ordering is `article_id DESC, position`
and the backfill only ever enqueues *lower* article ids, a row that just failed is still the newest
row in the queue and is re-claimed on the very next cycle. At the default
`image_requests_per_second = 5.0` and `image_batch_size = 50` a cycle is ~10 s, so five cycles —
under a minute — drive every image in that batch to `attempts = 5`, a state
`claim_pending_images` excludes forever. The only way back is an operator noticing and running
`requeue-images --host`, and there is no signal to notice: `_capture_one` logs only on a raised
exception, and a clean 429 raises nothing.

`claim_retryable` (repo.py:132), the `fetch_log` equivalent, guards the identical failure with
`cooldown_sec`, and its docstring names this exact hazard verbatim: *"Without it, a host having a bad
minute burns all five of an article's attempts inside that minute and the article is then written off
permanently."* That reasoning was never carried across to the image path — the side that is actually
rate-limit-prone, and where SPEC.md records a measured 429 from `i.imgur.com`. `record_image_result`
even maintains `checked_at` on every attempt (repo.py:304); nothing reads it. The column a cooldown
needs exists, is written, and is unused.

This is the fifth instance of the defect family the project has already been bitten by four times,
and the test suite encodes it as correct behaviour rather than catching it:
`tests/crawler/test_imageworker.py:108` drives ten back-to-back cycles with `noop_sleep` and a
getter that always fails, then asserts `attempts == MAX_IMAGE_ATTEMPTS` **and**
`calls['n'] == MAX_IMAGE_ATTEMPTS` — five attempts at zero elapsed time, asserted as the contract.
`tests/db/test_repo.py:363` asserts the same for the query. The specific imgur 429 that motivated
the `Sec-Fetch-Dest: image` fix is partly addressed, but the mechanism is header-independent: any
429, 403, 5xx, timeout, or brief VPN blip does it, and SPEC records that such failures arrive
batched from a single host. A one-hour egress outage — with `_watch_egress` polling every 900 s and
tolerating five consecutive lookup failures, so up to ~75 minutes blind — permanently abandons
thousands of living images across many hosts, which also makes the per-host recovery path awkward
rather than merely unprompted.

Fix: give `claim_pending_images` the cooldown it is missing —
`AND (status = 'pending' OR checked_at <= now() - make_interval(secs => $N))`, with a settable
`image_retry_cooldown_sec`. Keep the status set a textual literal (see M3) since the extra
`checked_at` term is a heap qualifier and does not affect index applicability. Add a test that a
permanently-failing getter reaches the ceiling only across cooldown boundaries, and log a per-host
warning when a row hits the ceiling so a 429 storm is visible without querying the database.

### C3 — `save_article` deletes every comment first, so a parse that finds none destroys the thread

`save_article` runs `DELETE FROM comments WHERE article_id = $1` unconditionally (repo.py:47) and
then inserts only `if article.comments`. `parse_article` returns a valid `Article` on the strength of
`div.postContent`, `div.postBody` and `datePublished` alone; `parse_comments` is a separate,
unguarded pass over `div.commentWrapper` / `id="comment{n}"`, and its result is assigned with no
check and no relation to the independently parsed `comment_count` (which comes from
`meta[name=description]`). Verified against the real 41-comment fixture: renaming `commentWrapper` to
`commentBox`, or the comment id prefix to `c-`, leaves the article parsing perfectly — 4 556 chars of
body, `comment_count = 41`, `comments = 0`. `fetch_article`'s only body gate is `"postBody" in body`,
so it passes too. Then `Ingestor.ingest` calls `save_article` and immediately `record_fetch(..., "ok")`
with nothing in between. Reproduced against the testcontainer: save an article with two comments,
re-save it with `comments=()`, and `count(*) FROM comments` is 0 while `articles.comment_count`
still reads 2. No exception, no log line, no marker, and nothing anywhere cross-checks
`comment_count` against `len(comments)`.

The failure that matters is the recovery path eating the archive. eRepublik renames
`commentWrapper` on day 20; articles still parse, so the walk keeps writing `ok` rows with empty
threads. The operator notices, fixes the article side but not the wrapper selector (or fixes the
wrapper while the id format is what changed), and does the documented thing:
`babel refetch --from 2300000 --to 2800000`. The sweep re-ingests 500 000 articles; each
`save_article` deletes the comments that *were* collected correctly before day 20 and inserts
nothing, then seals each row with `record_fetch('ok')`. SPEC.md states plainly that the 30-day raw
HTML window was dropped *because* refetch exists, so there are no bytes to reparse and no record of
which rows were emptied. Per SPEC.md comments carry more text than articles do (2 054 vs 1 357
chars), so this is the bulk of the archive.

Even with no precursor parser defect at all, the same unconditional DELETE erases any comment
deleted upstream since the first fetch, on every refetch, over ranges the operator is documented to
sweep 500k ids at a time — archived text that no longer exists anywhere. `repo.py` documents
"a re-fetch replaces the row and its children" as deliberate but gives no reason to permit
shrink-to-zero, and the image upsert one block below applies precisely the opposite reasoning so a
re-parse cannot discard a known-dead verdict. The asymmetry rules out reading this as a considered
trade.

Fix: refuse to write on the strength of an empty parse — if `not article.comments and
article.comment_count > 0`, record `error` instead of saving, the same rule `fetcher.py:33-38`
already applies to a missing `postBody` ("recording it as a successful empty article would be an
unrecoverable data loss"). Then stop using DELETE-then-insert: upsert on `(id)` and delete only ids
absent from a *non-empty* parsed set, and store `len(article.comments)` beside `comment_count` so a
divergence is queryable rather than invisible.

## Important

**I8 — A re-parse remaps `source_url` onto a slot that keeps the old image's terminal status and
hash.** `article_images` is keyed on `(article_id, position)` and positions are purely ordinal within
`div.postBody` (`enumerate` over `body_node.css("img")`), so any image added, removed or reordered
shifts every later slot. The upsert updates `source_url` and deliberately preserves `status`,
`sha256` and `attempts`, which means each shifted slot gets a *new* URL welded to the *old* image's
verdict: `('ok', sha256_of_a_different_image)` for a URL that was never fetched, or `dead` for one
never looked at, and possibly already at the retry ceiling. Neither is recoverable —
`claim_pending_images` excludes `ok` and `dead`, and `requeue_images_by_host` documents that "'ok' is
never touched". Worse, `articles.body` is stored as markup-stripped text, so `source_url` is the
*only* surviving record of image URLs: there is no second source to reconcile against and no query
that can tell a remapped row from a correct one. `tests/db/test_repo.py:140-153` pins the behaviour
and in doing so demonstrates the bug — it re-saves with `b.png` and asserts `status == "dead"` for a
URL that has never been fetched. The docstring's justification ("a later re-save has no way to tell
whether the image came back") is sound for the same-URL case and simply inapplicable once the row's
URL has been replaced. The previous review listed this as a Minor with *"Only reachable once C1's
refetch path exists — fix them together"*, then said "Promote". C1's refetch path shipped in this
diff; this did not, so C1 was closed by shipping the trigger for an unfixed corruption. It stays
Important rather than Critical only because no autonomous path produces it: firing needs an
operator-initiated refetch (or the narrow crash window between `save_article` and `record_fetch`)
*and* the parsed list actually shifting — but when it fires it fires across the whole refetched
range at once, silently. Fix: make the carried-over status conditional —
`status = CASE WHEN article_images.source_url = EXCLUDED.source_url THEN article_images.status ELSE
'pending' END`, with the same CASE zeroing `sha256` and `attempts`. Better, key the slot on
`(article_id, source_url)` so a position shift cannot remap anything, and delete slots whose URL is
gone.

**I9 — I6 is not closed: `ImageTooLarge` raises early but the whole body is still buffered.**
CLAUDE.md:52-55 states I6 is closed because the getter "aborts a download via `ImageTooLarge` as soon
as it passes `max_bytes` … so a hostile or oversized image can no longer be buffered whole in
memory". That is factually wrong in both of its clauses. `_bytes_getter` raises inside the
`aiter_content` loop, and the `finally: await response.aclose()` then *waits for the entire transfer
to finish*: curl_cffi's async `aclose()` is `if self.astream_task: await self.astream_task` and never
sets `quit_now`, which is the only thing that makes the write callback return `CURL_WRITEFUNC_ERROR`
and abort the connection — unlike the sync `_finalize_stream()`, which does set it. The queue behind
it is an unbounded `asyncio.Queue`. Measured with the project's own venv against a local server
streaming 300 MB with `max_bytes` at the production default of 8 MiB: `ImageTooLarge` raised after
0.13 s, the server logged that it had nonetheless sent all 300 MB, and peak RSS went 60 MB → 462 MB.
Setting `quit_now` before `aclose()` in an otherwise identical loop made the server fail at 12 MB
with a `BrokenPipeError` and held RSS at 46 MB, which pins the cause exactly. The early
`Content-Length` rejection behaves the same way — raised at 0.11 s, 300 MB still transferred, RSS
463 MB. And there is no wall-clock bound to fall back on: for a scalar timeout with `stream=True`
curl_cffi sets `LOW_SPEED_LIMIT=1`/`LOW_SPEED_TIME=20` and deliberately does *not* set `TIMEOUT_MS`,
so a trickling server held `get_bytes` for a measured 35 s, and any host sending above 1 byte/s
indefinitely stalls the strictly serial image worker for as long as it likes. `capture_image` maps
`ImageTooLarge` to `error`, so the same body is drained up to five times, through the VPN, on a host
also running Postgres. Not Critical: no terminal status is written, nothing is lost or corrupted, the
deploy host is now the 30 GiB x86_64 box rather than the 7.4 GiB Jetson that set I6's original
severity, and the blast radius is the separate `babel-images` container. But the cap currently saves
neither memory, bandwidth nor time. Fix: in the `finally`, set `response.quit_now` before
`await response.aclose()` (and update `tests/test_bytes_getter.py`, whose fake `_Response` stubs
`aclose()` as `pass` and has no `quit_now` — which is why the suite cannot see this); separately pass
a `(connect, read)` tuple or an explicit total cap so a stream cannot outlive
`request_timeout_sec`. And correct CLAUDE.md.

**I10 — The poller burns all five fetch attempts in an hour, and nothing can ever recover those
ids.** `poll_once` re-offers `error` rows through `filter_unseen(retry_errors=True)` with no cooldown,
once per `poll_interval_sec` (900 s). Five cycles take an hour and leave every affected id at
`attempts = 5`, at which point `filter_unseen`'s `NOT (status = 'error' AND attempts < 5)` reports
them as seen forever, and `claim_retryable`'s `attempts < MAX_FETCH_ATTEMPTS` excludes them from the
sweep SPEC.md advertises as the safety net. Those ids sit *above* the backfill cursor, which only
descends, so the walk never covers them either. Reproduced on the `postgres:17` fixture: five polls
against a working feed with a failing ingest leave both ids at `attempts = 5`, the sixth poll
ingests nothing, `claim_retryable(cooldown_sec=0)` returns `[]`, and only `mark_stale` puts them
back. `claim_retryable`'s docstring names this hazard and fixes it for the sweep only; the poller has
no equivalent, no test covers the ceiling on this path, and SPEC.md asserts the opposite
("a multi-hour outage loses nothing"). Two corrections to how this was first framed, neither fatal:
a plain site outage is largely self-limiting, because the RSS feed is the same host and a failed feed
records nothing — the reachable trigger is an *asymmetric* failure, most obviously a markup change
(SPEC's own headline risk), where the feed keeps working while `parse_article` returns None and
ingest writes `error` with `'unparseable page'`; and in steady state a 75-minute window exposes only
the ~1 article published in it, so "~54 lost" is a three-day parser break, not an hour of downtime.
It is also quieter than it looks — the `error` is recorded inside `ingest`, so `poll_once` logs
nothing at all. Fix: give the poller the cooldown the sweep has (a `cooldown_sec` on
`filter_unseen`'s retry branch, or simply stop re-offering an id already attempted in this window),
and as belt-and-braces let `claim_retryable` reconsider ceiling rows once after a long cooldown, so
the safety net stops excluding exactly the rows a burst failure creates. Recovery today is
`babel refetch` over the affected range — which is itself gated on M1.

## Minor

**M1 — The sweep is unreachable for the whole ~32-day walk, so `babel refetch` appears inert.**
`run_backfill`'s walk branch `continue`s on every cycle while `cursor >= stop_at`, and cli.py:343
passes `stop_at=1`, so `claim_retryable` at backfill.py:93 is not reached until the cursor has
descended past ~2.8M ids. During that window nothing drains `stale` rows at all —
`filter_unseen` hardcodes `status = 'error'`, so `stale` counts as seen for both the walk and the
poller. Verified with throwaway tests against the fixture: with `cursor = 2_200_000`, a `mark_stale`d
id at 2_500_000 is never fetched across five cycles while `claim_retryable(50, 0)` returns it, so the
queue is populated and simply not drained. Raised as Important and argued down to Minor: no data is
lost, because waiting does not burn attempts and nothing prunes `fetch_log`, so every starved row is
still claimable when the walk bottoms out — C1 was about permanent loss and that part is genuinely
closed, the work here is deferred rather than dropped. SPEC.md documents the phase ordering as
implemented ("Once the walk bottoms out"); what it omits is any reason for strict priority, and any
mention of the month-scale latency. Note `cooldown_sec` is a per-row `updated_at` filter in the SQL,
not a loop sleep, so once reachable the sweep runs cycles back to back. The residue is a priority
choice plus two doc lines that overpromise: README.md:80 and CLAUDE.md:83 both say the running
service's sweep "picks the queued IDs up on its own, no restart required", which reads as immediacy,
and a restart would not help since the cursor resumes mid-walk. Cheap fix: check the sweep first each
cycle and fall through to the walk when it is empty, or sweep every N walk cycles — otherwise state
the latency, and consider a `babel sweep` escape hatch.

**M2 — Image URLs get no scheme or address filter, so `file://`, loopback and the Docker bridge are
reachable.** `source_url` is the raw `img@src` from untrusted article HTML, stored verbatim and handed
to `session.get()` with nothing between it and libcurl but `normalise_url`, which only prefixes
`https:` onto a `//` URL. `ImageRef` validates nothing. Confirmed with the production getter that
`file:///etc/passwd` returns the file's bytes (status 0, so the row records `error` and the bytes are
discarded — but the read happens and burns five attempts). Redirects are followed by session default,
and because the worker runs inside gluetun's namespace with `FIREWALL_OUTBOUND_SUBNETS=172.16.0.0/12`
deliberately opened, loopback and the whole bridge are reachable, including gluetun's own control
server on `:8000` with `auth: none` and the eleven unrelated containers SPEC notes on the host. The
serial worker plus deterministic `ORDER BY article_id DESC, position` gives a clean timing oracle —
an article can interleave attacker-hosted images around an internal address and read refused-vs-hung
from its own logs against a ~0.4 s baseline, i.e. a working blind port scanner. Raised as Important
and argued down to Minor because it is strictly blind and strictly read-only: a non-image body
becomes `dead` with the bytes dropped, a non-200 becomes `error` and dropped, an image-sniffing body
lands in a content-addressed store the attacker cannot read, nothing logs a response body, and every
mutating gluetun endpoint requires a PUT with a JSON body — so the tunnel cannot be dropped through
this path. No wrong terminal status is written and no traffic escapes the namespace. Still a missing
control with a cheap fix: in `capture_image` (so the image tests cover it, not just the CLI wiring),
reject any scheme that is not http/https, resolve the host and reject loopback, link-local and
RFC1918, re-checking after each redirect or following redirects manually; record such rows `dead`
with a distinct reason so they leave the queue instead of retrying five times.

The two below were surfaced by a single lens each and **were not put through the adversarial
refutation pass**; treat the reasoning as unconfirmed.

**M3 (unverified) — Both partial-index EXPLAIN tests EXPLAIN a copy of the SQL, not the code's
query.** The two tests that exist specifically to enforce the invariant that has bitten this project
twice each build their own SQL string inside the test and EXPLAIN *that*, never calling
`repo.claim_retryable` or `repo.claim_pending_images`. Their comments state the invariant correctly
("the status set must stay a literal, textually matching the index predicate") while the assertion
covers a literal the test itself authored — proving only that Postgres can prove a partial index
applicable from a `Const`, which is a fact about Postgres. Compounding it, this diff adds
`RETRYABLE_STATUSES = ("error", "stale")` at repo.py:25, which grep confirms is used nowhere in `src`
or `tests`, and which reads as an invitation to refactor the literal into a bound parameter — the
exact change the tests are meant to block. Fix: EXPLAIN the string the production function actually
executes (extract it to a module constant and reference it from both), and either use or delete
`RETRYABLE_STATUSES`. Also worth noting the EXPLAIN runs against an empty table, so it is testing
plan derivability rather than plan choice.

**M4 (unverified) — The image worker is strictly serial, so the configured rate is unreachable and
the queue may grow without bound.** `run_image_worker` awaits each capture in turn, so there is never
more than one image request in flight: `image_requests_per_second` (default 5) is a ceiling the
worker cannot approach — the real rate is `1/(0.2s + latency)` — and `HostLimiter` can never contend,
making the per-host politeness SPEC calls for inert code. With the defaults the arithmetic does not
close: ingest enqueues ~6.2 image rows per article at 1 article/s against a drain of 1-2/s, so the
pending queue grows monotonically for the whole month. That turns SPEC's "delay measured in minutes
or hours" into weeks for every backfilled article, and SPEC's own measurement is that the delay is
what costs images permanently. Fix: run a small number of concurrent captures (which also makes
`HostLimiter` meaningful), and add a logged queue-depth gauge so a diverging drain is visible.

## Raised and refuted

Nothing was refuted this round. Every non-minor finding above survived its refutation pass; the pass
changed three severities (I8 and I9 down from Critical, M1 and M2 down from Important) and corrected
several failure scenarios, which are recorded inline above so a future reviewer does not re-argue
the overstated versions. In particular: the imgur 429 that motivated C2 is *partly* fixed by the
`Sec-Fetch-Dest: image` header, so do not re-derive C2 from that trigger alone; a plain eRepublik
outage does *not* trigger I10, because the feed fails with it; and the common refetch, where the
markup has not changed, rewrites each `source_url` to an identical value and is harmless under I8.

## Status of the findings this diff claims to close

C1 is **closed with a caveat**: the walk/sweep/idle loop and `babel refetch` both exist and the
permanent-loss mechanism is gone, but the sweep is unreachable for the duration of the walk (M1), and
shipping the refetch path activated the trigger for I8, which the previous review explicitly said to
fix alongside it.

I1 is **closed** — `article_images` is now a drainable queue, so an unfinished row is just still
`pending`. I2 is **closed** — images have their own limiter and rate, though M4 argues the configured
rate is not achievable. I3, I5 and I7 remain closed; the egress watchdog now tolerates
`MAX_CONSECUTIVE_LOOKUP_FAILURES` and alerts before re-raising, and no country code, hostname or
credential appears in any file this diff touches (`.env.example` holds empty placeholders,
`gluetun`'s runtime bind mount is untracked, the default image root is gitignored). I4 is **closed** —
the backfill no longer returns, so `FIRST_COMPLETED` cannot take the poller down at the bottom of the
range.

**I6 is not closed** (I9), and CLAUDE.md asserts that it is, in terms that measurement contradicts.
Of the previous review's Minors, the pool width and the fresh-deployment bootstrap are fixed; the
`article_images` remap Minor was promoted and is now I8; observability is improved but still thin —
there is no status distribution, no `skipped_no_space` rate, no queue depth, and, per C2, no log line
at all when an image row hits its ceiling.

## Recommended order

C3 first, and before any mass refetch: it is a few lines (refuse to write, or record `error`/`stale`,
when `comment_count > 0 and not comments`) and it protects the majority of the archive's text from
the very command an operator reaches for in an emergency. C2 second, and it should be deployed to the
live run rather than merely merged — it is one SQL predicate plus a settings field, on a column
already being written, and it is losing living images now. I8 third, because it must land before the
first large refetch and it is the same one-CASE-expression size. Then I10 and I9, both small and both
mechanical.

M1 needs a decision, not a fix — either interleave the phases or correct README.md and CLAUDE.md,
which currently describe behaviour the code does not have. M3 is worth doing while nearby: it is the
guard for the one invariant this project has broken twice, and it is not currently guarding
anything. M4 needs a measurement from the live run (queue depth over a day) before deciding on
concurrency. M2 can follow.

Nothing here is validated at month scale. The three findings that would show up first on the running
deployment are C2 (silent permanent image loss, happening now), M4 (a queue that only grows), and
I10 (the newest articles, which SPEC calls the most valuable, quietly missing after any asymmetric
failure). A single query — status distribution over `fetch_log` and `article_images`, plus queue
depth and the count of rows at the attempt ceiling — would turn all three from reasoning into
observation, and does not exist yet.
