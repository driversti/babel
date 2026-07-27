# babel

Archive of every eRepublik article and its comments, eventually searchable across languages.

Two producers feed one rate-limited fetcher: a 15-minute RSS poller picks up newly published
articles, and a backfill worker walks article IDs downward from the newest, recording where it
left off so an interrupted run resumes instead of restarting. Every article page is a single
request that returns the article, all of its comments, and the URLs of its images in one shot.
Ingest itself only records those image URLs as a `pending` queue; a second, independent service
(`babel images`) drains that queue at its own rate limit. The whole stack runs inside a VPN
container's network namespace — there is no network path that does not go through the tunnel —
and the egress IP is verified at startup and on an interval by both services, so a dropped or
misconfigured tunnel stops them rather than leaking traffic from the operator's own connection.

See [SPEC.md](SPEC.md) for the design: the measurements taken against the live site (feed
limits, page structure, article/image volume and sizing) and the phase boundaries. Those numbers
were expensive to gather; this file does not repeat them.

## Configuration

Copy `.env.example` to `.env` and fill in real values — `.env` is gitignored and must never be
committed:

```bash
cp .env.example .env
```

`.env` needs, at minimum:

- `DATABASE_URL` — must match `POSTGRES_PASSWORD` below
- `POSTGRES_PASSWORD` — required, no default; an unset value fails loudly rather than falling
  back to something guessable
- `HOME_COUNTRY` — the operator's own two-letter ISO country code, so a leaking tunnel is
  detectable
- `VPN_SERVICE_PROVIDER`, `VPN_TYPE`, `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESSES`,
  `SERVER_COUNTRIES` — Gluetun's WireGuard configuration for the VPN tunnel
- `BOT_TOKEN`, `CHAT_ID` — Telegram bot token and chat ID for alerts. Optional: leave both blank
  and both services log the two conditions they'd otherwise message about (disk full, egress IP
  leak) instead of notifying

## Running

```bash
uv sync                                              # install
docker compose up -d gluetun db                      # bring up the tunnel and the database
docker compose run --rm crawler babel migrate        # apply migrations
docker compose up -d crawler images                  # start both long-running services
docker compose logs -f crawler images
```

`babel run` starts both producers and the egress watchdog together, and exits — non-zero — the
moment any of them does, so a leaked tunnel or a crashed producer never runs unattended. The
service is expected to run indefinitely: reaching article 1 is not an end state. Once the backfill
walk bottoms out, it cycles into sweeping `fetch_log` for anything left `error` or `stale` and
retrying it, idling only when there is truly nothing to do, so the process keeps running rather
than exiting. Pass `--start-id <id>` on the very first run (there is no cursor yet); after that the
cursor in `crawl_cursor` picks up where the last run stopped. Use `--no-poll` or `--no-backfill` to
run only one producer, e.g. `babel run --start-id <id> --no-poll` for a backfill-only pass.

Image capture is a second, independent service. `babel run` only records each article's image
URLs as `pending` rows — it never fetches image bytes itself. `babel images` drains that queue on
its own schedule and its own rate limit, sharing the same VPN tunnel (`network_mode:
"service:gluetun"`) but nothing else in-process. Either service can be stopped, restarted or
redeployed without touching the other: stopping `images` simply lets the queue build up, and
`crawler` keeps ingesting articles normally in the meantime.

## Commands

- `babel probe --newest <id>` — fetch a sample of article pages through the tunnel and report
  what came back; use it to confirm the current exit node is not being challenged by Cloudflare
  before trusting it with a real run
- `babel migrate` — apply any pending SQL migrations
- `babel run [--start-id N] [--no-poll] [--no-backfill]` — run the crawler until stopped
- `babel images` — drain the image queue until stopped; a separate long-running service from
  `babel run`, stoppable and restartable independently
- `babel refetch --ids 123,456` or `babel refetch --from 100 --to 200` — queue already-collected
  articles for re-collection, e.g. after fixing a parser bug or when the site's markup has changed.
  Only `ok` and `error` rows are touched — a `missing` row is a fact about the article, not about
  our copy of it, and an ID never fetched will be reached by the walk anyway. Selections over
  10,000 IDs ask for confirmation (skip with `--yes`); the running service's backfill sweep phase
  picks the queued IDs up on its own, no restart required

## Public archive

A fifth compose service, `web` (command `babel serve`), serves the archive read-only over HTTP. It
is a separate FastAPI process from the crawler, deliberately outside gluetun's network namespace: it
needs *inbound* connections, which that namespace cannot accept, and its only outbound dependency is
Postgres on the bridge — so the site stays up when the VPN tunnel is down. `babel serve` never
applies migrations: `crawler` and `images` both do that at startup, and a third command written the
same way would connect as a SELECT-only role and crash-loop under `restart: unless-stopped`.
Applying the schema is an explicit operator step (below).

### One-time setup

1. Create a SELECT-only database role and put its DSN in `.env` as `WEB_DATABASE_URL` — `babel serve`
   refuses to start if this is unset or equal to `DATABASE_URL`, because that fallback would run the
   public site as the database owner, silently. The exact grants (including the two `ALTER ROLE`
   settings that are the actual enforcement — see `.env.example`) are documented there.
2. Set `WEB_BIND` to the host's LAN address and `WEB_PORT`, both in `.env`. `WEB_BIND` must never be
   `0.0.0.0`: Docker's published-port rules install into the `DOCKER` chain and bypass the host
   firewall, so binding to every interface defeats a host firewall that looks like it covers this
   port. Point whatever sits in front of it (reverse proxy, tunnel) at `http://<WEB_BIND>:<WEB_PORT>`.

### Deploying migration 005 (and any future browse migration)

Migration 005 adds the browse indexes and the suppression tombstones. **Never let it run as a side
effect of a bare `docker compose up -d`** — both `crawler` and `images` apply pending migrations at
startup, so starting either one first runs this DDL against a live walk. Plain `CREATE INDEX` (`CREATE
INDEX CONCURRENTLY` is unavailable through this project's transaction-wrapped migration runner) takes
`ShareLock` for the entire build, which blocks concurrent writers — not readers — for as long as the
build takes, not just while the lock is being acquired. Stop the writers first:

```bash
git pull
docker compose stop crawler images
docker compose run --rm crawler babel migrate
docker compose build web
docker compose up -d crawler images web
```

### Taking a page down

`babel hide --article <id>` sets a tombstone (`articles.hidden_at`) rather than deleting the row.
Measured on a fresh database: `DELETE FROM articles` cascades to `comments` and `article_images`, but
the `images` row and its blob on disk survive (the foreign key runs `article_images.sha256 ->
images(sha256)`, not the other way), and `fetch_log` has no foreign key to `articles` at all, so its
row stays `ok`. `babel refetch` would then flip that row to `stale`, the sweep would re-collect the
article, and the takedown would silently reverse itself. The tombstone is filtered out of every list
and article query instead, and hiding twice is a no-op — it does not overwrite when the first request
arrived.

**Hiding an article does not withhold its images.** Images are content-addressed and stored once, so
the same blob is very often cited by other articles too (flags, avatars, and recycled memes recur
across thousands of pages) — suppressing one article can never imply the image should stop being
served everywhere else it appears. `/img/{sha256}` stays reachable by digest until an operator
separately runs `babel hide --image <sha256>`, which reports how many articles currently cite that
blob so the blast radius is visible before deciding. **A takedown request is two steps, not one** —
handling only the article and believing the job done leaves the image itself still public.

## Development

```bash
uv run pytest             # full suite (needs Docker for testcontainers)
uv run ruff check src tests
docker compose build crawler images web
```
