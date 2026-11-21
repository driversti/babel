# Key facts and command reference

Moved out of CLAUDE.md verbatim. README.md "Commands" has the user-facing list.

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
- `docker compose up -d` — run the stack, including all four long-running services, `crawler`,
  `images`, `embed` and `web`
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
- `docker compose run --rm crawler babel image-hosts [--min-rows 500] [--no-probe]` — read-only
  report of image hosts by queue depth (`pending` + `error`), with lifetime `ok`/`dead` beside each
  and a one-request-per-host live `probe` (`nxdomain` / `blocked` / `unreachable` / `gone` (404/410)
  / `http-error` (403/429/5xx — *not* auto-safe) / `not-image` / `alive`). After the walk the queue
  is millions of `pending` rows, a large share on hosts gone for years (tinypic since 2019); this is
  how you find them. Ends with a ready-to-paste `kill-image-host` line for the hosts whose probe
  says gone and which have never returned an `ok`.
- `docker compose run --rm crawler babel kill-image-host --host i49.tinypic.com,prikachi.com` —
  the exact reverse of `requeue-images`: moves a dead host's `pending`/`error` rows to `dead` so the
  worker stops spending five attempts on each. Refuses a host with any stored (`ok`) image unless
  `--force`; `attempts` is left as a record. Undone by `requeue-images --host <h>`. Pass `--host ?`
  for the stuck-hosts list.
- `babel images` — drain the `article_images` queue until stopped; runs as the separate `images`
  compose service, stoppable/restartable independently of `crawler` since ingest only enqueues
  image URLs and never fetches the bytes itself
- `babel embed` — drain the `article_embeddings` queue until stopped; runs as the separate `embed`
  compose service. Claims articles with no vector newest-first, batches them to the Jetson's
  `POST /embed`, and refuses to write a vector if the service reports a model id different from
  `EMBED_MODEL` — the one silent-degradation failure this whole design exists to name and catch.
  No status/attempts column like `article_images` has: an embedding failure is always transient (the
  service is down, the batch timed out), so a restart mid-batch just re-offers the same rows
- `babel serve` — run the public read-only web archive; the `web` compose service. Refuses to start
  if `WEB_DATABASE_URL` is unset or equal to `DATABASE_URL`, and never applies migrations itself —
  see "Operating the live run" for why and for the deploy order. It does *check* them: one throwaway
  connection reads `schema_migrations` before anything is served and refuses, naming whichever of
  `005_browse.sql`/`007_body_markup.sql`/`008_embeddings.sql` is missing. Without that check a
  skipped migrate step answers 503 on every page — `UndefinedColumnError` is a `PostgresError`, so
  it lands in the database-down handler — while `/healthz` and the compose healthcheck stay green,
  which points the operator at Postgres instead of at the deploy
- `GET /search?q=…` — part of `babel serve`, not a separate command. Embeds the query string through
  the same Jetson service `babel embed` uses, then runs the two-stage pgvector query
  (`src/babel/db/search.py`) and renders results through the same templates as browse. Degrades
  honestly: a slow or unreachable embed service renders a "semantic search is unavailable right now"
  notice rather than a keyword fallback, because keyword search does not exist in this codebase.
  `noindex`, and disallowed in `robots.txt`, like the rest of the filtered browse space
- `docker compose run --rm crawler babel hide --article <id>` or `babel hide --image <sha256>` —
  suppress an article, or stop serving one blob by digest. These are two separate commands on
  purpose: hiding an article does not withhold its images, because a blob is content-addressed and
  commonly shared across many articles. `--image` reports how many articles currently cite the
  digest so the blast radius is visible before deciding — a takedown request is not fully handled
  until both commands have been run for anything that needs to disappear entirely. Nor is it
  instantaneous: `/img/{sha256}` is served `max-age=86400`, so a cache that already holds the blob
  keeps serving it for up to a day (pages are `max-age=300`). Purge the CDN too — README, "Taking a
  page down"
