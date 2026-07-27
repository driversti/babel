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
  10,000 IDs ask for confirmation (skip with `--yes`). The queued IDs are picked up by the backfill's
  sweep phase, which `run_backfill` only reaches once the walk bottoms out — finding M1 in CLAUDE.md
  — so during a walk that is ~32 days away. Nothing is lost by waiting (waiting burns no attempts),
  but a re-collection you need now has to be driven by hand: see "Re-collect the bodies before
  launch" below for the three-command form

## Public archive

A fifth compose service, `web` (command `babel serve`), serves the archive read-only over HTTP. It
is a separate FastAPI process from the crawler, deliberately outside gluetun's network namespace: it
needs *inbound* connections, which that namespace cannot accept, and its only outbound dependency is
Postgres on the bridge — so the site stays up when the VPN tunnel is down. `babel serve` never
applies migrations: `crawler` and `images` both do that at startup, and a third command written the
same way would connect as a SELECT-only role and crash-loop under `restart: unless-stopped`.
Applying the schema is an explicit operator step (below). It does check that the step was taken —
one throwaway connection reads `schema_migrations` before anything is served, and the process
refuses to start, naming `005_browse.sql`, if the browse migration is missing. A skipped migrate
step otherwise answers 503 on every page while `/healthz` and the compose healthcheck stay green.

`web` is also the one service that does not get `env_file: .env`. It is handed `WEB_DATABASE_URL`,
`IMAGE_ROOT`, `CONTACT` and `WEB_POOL_SIZE` and nothing else, because it is the only process here
that accepts connections from the internet and it reads none of the rest — see the comment on the
service in `docker-compose.yml` for what that costs and what carries the weight instead.

### One-time setup

1. Create a SELECT-only database role and put its DSN in `.env` as `WEB_DATABASE_URL` — `babel serve`
   refuses to start if this is unset or equal to `DATABASE_URL`, because that fallback would run the
   public site as the database owner, silently. The exact grants (including the two `ALTER ROLE`
   settings that are the actual enforcement — see `.env.example`) are documented there.
2. Set `WEB_BIND` to the host's LAN address and `WEB_PORT`, both in `.env`. `WEB_BIND` must never be
   `0.0.0.0`: Docker's published-port rules install into the `DOCKER` chain and bypass the host
   firewall, so binding to every interface defeats a host firewall that looks like it covers this
   port.
3. **Re-collect the article bodies** so they have paragraph breaks, and **audit the blobs captured
   before the address guard existed** — both below, both before the site is reachable, in either
   order.
4. Apply the schema and start the service — "Deploying migration 005" below, which is a
   stop/migrate/start and never a bare `up -d`.
5. Only then point whatever sits in front of it (reverse proxy, tunnel) at
   `http://<WEB_BIND>:<WEB_PORT>`.

### Re-collect the bodies before launch

The parser only started emitting `"\n"` at `<br>`, `</p>`, `</div>` and `</li>` on this branch, and
that changes new fetches only. Every article collected before it is one unbroken block of text in
the database, and publishing the site publishes that. Re-collection is a deliberate three-step pass,
not a queued job:

```bash
docker compose run --rm crawler babel refetch --from 1 --to 2797025 --yes
docker compose stop crawler
docker compose run --rm crawler babel run --no-poll   # runs the sweep; stop it when the queue drains
docker compose up -d crawler
```

The middle two steps are not optional. `run_backfill` only reaches its sweep phase once the walk
bottoms out (finding M1 in CLAUDE.md), which is ~32 days away, so the `stale` rows a running service
is holding are not picked up in the meantime. Nothing is lost by waiting — waiting burns no attempts
— but nothing happens either.

### Audit the pre-guard blobs before launch

Everything already in `article_images`/`images` was captured before `capture_image` had a scheme and
address filter, so nothing on disk was checked against it. Publishing `/img/{sha256}` is what turns
a blind fetch into a readable one, which is why this belongs before the hostname exists and not
after:

```sql
SELECT article_id, position, source_url, sha256
FROM article_images
WHERE status = 'ok'
  AND (source_url !~* '^https?://'
       OR source_url ~* '(://|@)(localhost|127\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|169\.254\.|0\.0\.0\.0)');
```

This is a heuristic over stored text, not a re-resolution of every hostname: a host that pointed at a
private address only when it was crawled will not match. Inspect anything it returns by hand and
`babel hide --image <sha256>` whatever turns out to be an internal endpoint rather than a real image
host.

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

**Neither command reaches a cache that already holds the page.** `/img/{sha256}` is served with
`Cache-Control: public, max-age=86400, must-revalidate`; `must-revalidate` governs what
a cache may do once the entry is *stale*, not before, so a cache holding that blob keeps serving it
for up to 24 hours after `babel hide --image`. Article and list pages carry `max-age=300`, so those
close within five minutes. The trade is deliberate — content-addressed blobs would justify
`immutable`, and this is already the shortened form — but it means a takedown is not complete when
the commands return: purge the blob's URL from whatever CDN or tunnel cache sits in front of the site
(for Cloudflare, a single-file purge of `https://<host>/img/<sha256>`), and say so if you are
answering someone who is counting hours.

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
