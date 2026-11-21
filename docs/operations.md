# Operating the live run

Moved out of CLAUDE.md verbatim. Deploy host address is not in this repo (see SPEC.md "Target host").


It runs as six compose services on the deploy host (see SPEC.md "Target host"; the address is not
in this repo), plus a seventh that only runs when an operator asks for it. `gluetun` is the tunnel,
`db` is Postgres, `crawler` walks and polls, `images` drains the image queue, `embed` drains the
`article_embeddings` queue through the Jetson, and `web` (`babel serve`) serves the public read-only
archive, including `/search`. The seventh is `sweep`, behind a compose profile so a bare `up -d`
never starts it: it is `crawler`'s image running `--no-poll --sweep-only`, for re-collection passes,
and it is a service rather than a `compose run --rm` one-shot because that one-shot carries
`restart: no` and is deleted on exit — a reboot ended a 34-hour re-collection with every other
container coming back healthy around it. The profile exists because `RateLimiter` is per process: a
sweep beside the walk is two requests a second, so stop `crawler` first. `web` and `embed` are both
deliberately outside gluetun's namespace, for different reasons: `web` needs inbound connections,
which that namespace cannot accept; `embed` never talks to eRepublik at all, only to Postgres on the
bridge and the Jetson on the LAN, so routing it through the tunnel would only add
`FIREWALL_OUTBOUND_SUBNETS` exposure for nothing. Both mean the site and the embedding drain stay up
when the VPN tunnel is down. All state is in Postgres plus `data/images`; both are gitignored bind
mounts, so `git pull && docker compose build && docker compose up -d <service>` is the whole deploy
for `crawler`, `images`, `embed` and `web` individually. Restarting any one of them outside of a
migration is safe at any time — the cursor is persisted and a partial batch is re-walked without
HTTP; `embed`'s queue is a NULL-vector row, not a status, so a restart mid-batch just re-offers the
same rows.

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

**`docker compose run --rm <service>` is not confined to that service — it reconciles every
dependency against the compose file on the checked-out branch first, even for a one-off, read-only
command.** Measured directly on 2026-09-01: `main` had already picked up `db: pgvector/pgvector:...`
from the merged Phase-3 branch, while `.18`'s running `babel-db` was still the pre-cutover
`postgres:17` container — the intended cutover was still pending, on purpose (see the Phase 3 note
in "What is still open"). Running `docker compose run --rm crawler babel image-hosts` — a report
command that touches no schema and calls no migration — from a branch checked out on top of that
`main` recreated `babel-db` on the new image anyway, because Compose brings every `depends_on`
service in line with the current file before running the one-off, regardless of whether the command
itself would ever touch it. Data and schema survived intact (same volume, migration state unchanged
at `007_body_markup.sql`, no `vector` extension) and Postgres itself restarted cleanly, but
`babel-crawler`'s live connection was severed mid-query and the process crashed
(`asyncpg... connection has been released back to the pool`) — caught and restarted by
`restart: unless-stopped` within about a minute, no further recurrence. The lesson: a feature branch
must not be checked out on this host — even to run something read-only — while it carries an
image-tag change for a service the branch's own author did not intend to deploy yet. Test a new
command by copying the changed files into the checkout with `rsync`, or by pulling only the compose
services you are about to touch, never by `git checkout`-ing a whole branch that happens to sit
ahead of what is actually running.

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

