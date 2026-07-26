# CLAUDE.md

Archive of every eRepublik article and its comments, eventually searchable across languages.

**Read [SPEC.md](SPEC.md) first** — it holds the design, the measured facts about the source
(feed limits, page structure, volume, sizing) and the phase boundaries. Those measurements were
expensive to obtain; do not re-derive them, and update them there if the site changes.

## Status

Phase 1 — crawler. All ten tasks are implemented: config, article/comment parser, DB schema +
migration runner, repository layer, rate limiter + fetcher, content-addressed image store,
end-to-end article ingest, RSS poller, newest-first backfill walker, and the CLI/service that
composes them (`src/babel/cli.py`). 93 tests, all passing (`uv run pytest`, needs Docker for the
`postgres:17` testcontainer); `uv run ruff check src tests` clean.

**The probe has passed.** 100 article IDs fetched through a VPN tunnel from the target host:
85 ok, 15 missing, zero Cloudflare challenges. Anonymous access from a VPN exit works.

**The crawler itself has still never run.** The probe validates reachability, nothing more. No
article has been stored, and one Critical plus four Important findings from the whole-branch review
are open — see `docs/superpowers/plans/2026-07-26-final-review-findings.md`. The largest is C1:
there is no way to re-visit an article once recorded, so a transient failure loses it permanently.

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
- `docker compose up -d` — run the stack
- `docker compose run --rm crawler babel migrate` — apply migrations
- `docker compose run --rm crawler babel probe --newest <id>` — check the exit node is not challenged
