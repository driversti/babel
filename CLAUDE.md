# babel

Single source of truth for every coding agent: `AGENTS.md` and `GEMINI.md` are symlinks to this file.

Archive of every eRepublik article and its comments (Python 3.12, FastAPI, Postgres + pgvector, curl-cffi), eventually searchable across languages. Phase 1 crawler and the public read-only web archive are live; Phase 3 (bge-m3 embeddings on a Jetson, `/search`) is merged on `main`.

**Read [SPEC.md](SPEC.md) first**: design, measured facts about the source (feed limits, page structure, volume), phase boundaries. Those measurements were expensive; do not re-derive them, update them there if the site changes. [README.md](README.md) holds the operator runbooks.

## Build and test
- `uv sync` installs; `uv run ruff check src tests` lints (must be clean).
- `uv run pytest -q` runs the suite; it needs Docker (Postgres testcontainer `pgvector/pgvector:0.8.6-pg17-trixie`). `live`-marked tests need the real embed service and are skipped by default.
- Run locally: `docker compose up -d` (gluetun, db, crawler, images, embed, web) after copying `.env.example` to `.env`. The `sweep` service is behind a compose profile.
- CLI entry point is `babel` (`src/babel/cli.py`): `run`, `images`, `embed`, `serve`, `migrate`, `probe`, `refetch`, `requeue-images`, `image-hosts`, `kill-image-host`, `hide`. Full reference: [docs/commands.md](docs/commands.md).

## Deploy
- No release script or registry. The deploy host runs the compose stack straight from a checkout of `main` (address is not in this repo; the monorepo docs list `192.168.10.18` for projects not yet moved to jupiter LXCs, unconfirmed for babel). Anything pushed to `main` is one `git pull` away from the archive, so run the tests before pushing.
- Plain deploy of one service: `git pull && docker compose build <svc> && docker compose up -d <svc>` (`crawler`, `images`, `embed`, `web`). Restarting any of them is safe; the cursor is persisted.
- Migrations: build FIRST, then migrate, because `babel migrate` runs inside the old image and cannot see new files:
  `docker compose stop crawler images && docker compose build crawler images web && docker compose run --rm crawler babel migrate && docker compose up -d crawler images web`
- Never `git checkout` a feature branch on the deploy host, even for a read-only `docker compose run`: Compose reconciles every `depends_on` service (e.g. recreates `db`) against that branch's compose file.
- Never run `sweep` beside `crawler` (rate limiter is per process): stop `crawler` first. `web` and `embed` stay outside gluetun's network namespace on purpose.
- Details, health query and rollback notes: [docs/operations.md](docs/operations.md).

## Conventions and gotchas
- Egress: all eRepublik traffic goes through the gluetun VPN; the egress watchdog alerts and exits on an IP leak. Runs on an x86_64 host, not the Jetson (Tegra kernel lacks `ip rule`, gluetun cannot start); the Jetson only hosts the embed service (`jetson/`).
- Crawl politely: ~1 req/s. The RSS feed (5 pages, ~3 days) is for live polling only; history comes from walking article IDs downward. `GET /en/article/{id}/1/1000` needs no session.
- Image fetches must look like an `<img>` load and `dead` requires positive evidence; a false `dead` is never retried. Read the two SPEC.md entries before touching image fetching. `dead` is reversible via `babel requeue-images --host <h>`.
- Never place raw DB state in docs/commits: `data/`, `pgdata/`, `.env`, `gluetun/` are gitignored runtime state.
- `MAX_MARKUP_BYTES` (`src/babel/web/markup.py`, 32 KiB) is a plain size cap. Do not reintroduce a markup-aware render guard (five attempts were each bypassed). Never enable `debug` on `babel serve` (leaks tracebacks).
- `babel serve` needs `WEB_DATABASE_URL` set and different from `DATABASE_URL`; it checks but never applies migrations.
- When aborting a curl_cffi response call `response.quit_now.set()`, assigning the attribute is a no-op.
- Postgres partial indexes: keep the status literals inline in SQL (a bound parameter defeats the index; broken twice before).
- A stalled walk looks healthy (containers `Up`, quiet logs): check that `count(*) FROM articles` is rising.
- Open findings (I10 poller cooldown, M1 sweep reachability, M3 EXPLAIN tests) and history: [docs/status-and-open-issues.md](docs/status-and-open-issues.md). Read the review-2 findings plan in `docs/superpowers/plans/` before changing crawler behavior.
- Embedding: `babel embed` refuses to write a vector if the service's model id differs from `EMBED_MODEL`.
