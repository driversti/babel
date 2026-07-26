# babel

Archive of every eRepublik article and its comments, eventually searchable across languages.

Two producers feed one rate-limited fetcher: a 15-minute RSS poller picks up newly published
articles, and a backfill worker walks article IDs downward from the newest, recording where it
left off so an interrupted run resumes instead of restarting. Every article page is a single
request that returns the article, all of its comments, and the URLs of its images in one shot.
The whole stack runs inside a VPN container's network namespace — there is no network path that
does not go through the tunnel — and the egress IP is verified at startup and on an interval, so
a dropped or misconfigured tunnel stops the crawler rather than leaking traffic from the
operator's own connection.

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

## Running

```bash
uv sync                                              # install
docker compose up -d gluetun db                      # bring up the tunnel and the database
docker compose run --rm crawler babel migrate        # apply migrations
docker compose up -d crawler                         # start the long-running service
docker compose logs -f crawler
```

`babel run` starts both producers and the egress watchdog together, and exits — non-zero — the
moment any of them does, so a leaked tunnel or a crashed producer never runs unattended. The one
exception is the backfill reaching article 1: that is a completed archive, not a failure, and the
process exits cleanly. Pass `--start-id <id>` on the very first run (there is no cursor yet);
after that the cursor in `crawl_cursor` picks up where the last run stopped. Use `--no-poll` or
`--no-backfill` to run only one producer, e.g. `babel run --start-id <id> --no-poll` for a
backfill-only pass.

## Commands

- `babel probe --newest <id>` — fetch a sample of article pages through the tunnel and report
  what came back; use it to confirm the current exit node is not being challenged by Cloudflare
  before trusting it with a real run
- `babel migrate` — apply any pending SQL migrations
- `babel run [--start-id N] [--no-poll] [--no-backfill]` — run the crawler until stopped

## Development

```bash
uv run pytest             # full suite (needs Docker for testcontainers)
uv run ruff check src tests
docker compose build crawler
```
