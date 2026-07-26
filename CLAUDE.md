# CLAUDE.md

Archive of every eRepublik article and its comments, eventually searchable across languages.

**Read [SPEC.md](SPEC.md) first** — it holds the design, the measured facts about the source
(feed limits, page structure, volume, sizing) and the phase boundaries. Those measurements were
expensive to obtain; do not re-derive them, and update them there if the site changes.

## Status

Phase 1 — crawler. All ten tasks are implemented: config, article/comment parser, DB schema +
migration runner, repository layer, rate limiter + fetcher, content-addressed image store,
end-to-end article ingest, RSS poller, newest-first backfill walker, and the CLI/service that
composes them (`src/babel/cli.py`). The suite is 90 tests, all passing (`uv run pytest`, needs
Docker for the `postgres:17` testcontainer) and `uv run ruff check src tests` is clean.

**Not yet run against the live site.** Every measurement in SPEC.md was taken from a residential
connection, and the crawler has never been exercised through the VPN. Task 1 Step 12 — fetch a
hundred articles through the tunnel and confirm Cloudflare isn't challenging the exit node — is
still blocked on VPN credentials and has not happened. Do not assume the crawler works end to end
against the real site until that probe has run clean.

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
- `docker compose up -d` — run the stack
- `docker compose run --rm crawler babel migrate` — apply migrations
- `docker compose run --rm crawler babel probe --newest <id>` — check the exit node is not challenged
