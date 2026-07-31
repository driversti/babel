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

## Semantic search

Two processes on two machines. `babel embed` on the x86 box drains the queue of
unembedded articles through the Jetson's `POST /embed`; `babel serve` calls the
same endpoint for each reader's query. They must use the same model — vectors
from two models are not comparable, and nothing reports it.

### One-time setup on the Jetson

```bash
ssh jetson@<jetson-address>
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
docker run --rm --runtime nvidia nvcr.io/nvidia/l4t-jetpack:r36.4.0 nvidia-smi   # must print a table
```

The daemon restart bounces every container on that host. Check their restart
policies first.

**Download the model once.** It lands in a bind mount that is read-only afterwards, so a
restart needs no internet:

```bash
mkdir -p ~/babel-embed/models
docker run --rm -v ~/babel-embed/models:/models python:3.12-slim sh -c \
  "pip install -q huggingface_hub && python -c '
from huggingface_hub import snapshot_download
snapshot_download(\"BAAI/bge-m3\", local_dir=\"/models/bge-m3\",
    allow_patterns=[\"*.json\", \"*.bin\", \"*.model\", \"tokenizer*\"],
    ignore_patterns=[\"onnx/*\"])'"
```

`*.bin`, not `*.safetensors` — `BAAI/bge-m3`'s upstream repository ships no safetensors
variant at all, so the pattern that looks more modern silently downloads ~43 MB of JSON and
tokenizer files with no weights. `ignore_patterns=["onnx/*"]` skips a duplicate ONNX export the
repo also carries, which this service never uses. Expect ~2.3 GB under `~/babel-embed/models/bge-m3`.

**Clone this branch onto the Jetson itself** (the compose file below lives in it, so nothing
resolves before this step) **and build the image:**

```bash
git clone -b feat/phase-3-embeddings <repo-url> ~/babel-embed/src
cd ~/babel-embed/src/jetson
docker compose build
```

**Convert the weights before starting the service — this step is not optional.**
`jetson/embed_service/encoder.py` loads the model with `AutoModel.from_pretrained`, and this
image's `transformers` refuses to `torch.load` a pickle checkpoint — which is all
`pytorch_model.bin` is — unless torch ≥ 2.6 (CVE-2025-32434). This image's torch, from the
`dustynv/l4t-pytorch:r36.4.0` base, is 2.4.0. Skip this and the container starts, the healthcheck
never turns healthy, and the logs show `AutoModel.from_pretrained` raising that `ValueError`, not a
crash at startup. Convert once, using the image just built and the script this repo tracks for
exactly this (`jetson/convert_to_safetensors.py` has the full reasoning in its docstring):

```bash
docker run --rm -v ~/babel-embed/models:/models \
    -v ~/babel-embed/src/jetson/convert_to_safetensors.py:/app/convert.py \
    babel-embed python3 /app/convert.py
sudo mv ~/babel-embed/models/bge-m3/pytorch_model.bin \
    ~/babel-embed/models/pytorch_model.bin.bak
```

The `sudo` is not decoration: `pytorch_model.bin` is root-owned because `snapshot_download` above
ran as root inside its own container. Moving it out removes any ambiguity about which weights file
loads — `transformers` prefers safetensors when both are present, but only one should exist here.

**Set `EMBED_BIND` before bringing the service up.** `jetson/docker-compose.yml` binds to
`127.0.0.1` by default, deliberately: that address is unreachable from the x86 box, so a deployment
that forgets this setting fails loudly (connection refused, not a silent LAN exposure). Copy the
template and fill in this machine's own LAN address:

```bash
cp .env.example .env
# edit .env (still inside ~/babel-embed/src/jetson): EMBED_BIND=<this Jetson's LAN address>
docker compose up -d
curl http://<jetson-address>:8081/healthz     # {"model":"BAAI/bge-m3","dim":1024,"cuda":true}
```

### Pointing the x86 side at it

Everything above ran on the Jetson, over the `ssh` opened at the top of this section, inside
`~/babel-embed/src/jetson`. The rest of this file runs back on the x86 box, in this repo's own
checkout root — not `~/babel-embed/src`, which does not exist there.

`EMBED_SERVICE_URL` defaults to `http://localhost:8081`, which is not the Jetson — it resolves
inside whichever container reads it — so a `.env` that skips this step still starts `babel embed`
and `babel serve` without error. Both look healthy: `babel embed` backs off against a closed local
port forever, logging and alerting exactly as it would for a real outage, and `/search` answers
"unavailable" on every query. Neither process crashes or refuses to start, so nothing short of
trying a search names the missing step.

If `.env` does not already exist in this checkout (per "Configuration" above), `cp .env.example
.env` first. Either way, edit the `EMBED_*`/`SEARCH_*` block `.env.example` already carries, at
minimum:

```bash
# in this checkout's root. embed reads this .env via env_file:; web does not
# get env_file: .env (see its own service comment for why) but reads the same
# two values through docker-compose.yml's ${EMBED_SERVICE_URL}/${EMBED_MODEL}
# interpolation, so one edit here reaches both.
EMBED_SERVICE_URL=http://<jetson-address>:8081   # the address the curl above just confirmed, not localhost
EMBED_MODEL=BAAI/bge-m3                          # must match the Jetson's MODEL_ID
```

`EMBED_BATCH_SIZE`, `EMBED_MAX_CHARS`, `SEARCH_MAX_QUERY_CHARS` and `SEARCH_TIMEOUT_SEC` ship
working defaults in `.env.example` and don't need editing to get search running. This only edits
the file; the next section is what actually brings `embed` up.

### Deploying the database change

`db` moves from `postgres:17` to `pgvector/pgvector:0.8.6-pg17-trixie`. Same
upstream image plus the extension, same PGDATA — an image swap, not a dump and
restore. **The `-trixie` tag is not optional.** Prod runs `17.10-1.pgdg13+1`
and both images report `Debian GLIBC 2.41-12+deb13u3`; a bookworm image would
change text collation under every index already built on this data.

Build before migrating, as always — migrations are baked into the image:

```bash
docker compose stop crawler images embed
docker compose build crawler images embed web
docker compose up -d db
docker compose run --rm crawler babel migrate
docker compose up -d crawler images embed web
```

### Building the similarity index

Not in the migration: `CREATE INDEX CONCURRENTLY` is illegal inside a
transaction and this project's runner wraps every file in one. Do it through
`psql` once there are vectors to index.

```sql
SET maintenance_work_mem = '4GB';
CREATE INDEX CONCURRENTLY article_embeddings_bin_idx ON article_embeddings
    USING hnsw ((binary_quantize(embedding)::bit(1024)) bit_hamming_ops)
    WHERE embedding IS NOT NULL;
```

Two things this design deliberately does not have, named here so an operator meets them in the
runbook rather than in production. `/search` has **no rate limit and no query-vector cache**: about
60 bytes of request buys a `bge-m3` forward pass on the Jetson, and `search_timeout_sec` bounds the
*page*, not the queue behind it. Neither is hard to add — a cache keyed on the normalised query
string would absorb the common case — but both were out of scope for this slice. If the site takes
real traffic, watch the Jetson's load before assuming it is fine.

### Watching the drain

```sql
SELECT count(*) FILTER (WHERE embedding IS NULL)     AS pending,
       count(*) FILTER (WHERE embedding IS NOT NULL) AS done,
       count(DISTINCT model)                         AS models
FROM article_embeddings;
```

`models` must be 1. More than one means the corpus is half-encoded by something
else, and search quality is already degraded.

### If the queue is missing rows

Migration 008 seeds the articles that existed when it ran, and `save_article`
queues the ones ingested afterwards. An article written *between* the two — by
a crawler still running the old image — gets neither. The deploy order above
prevents it; this closes it when it happens anyway:

```sql
INSERT INTO article_embeddings (article_id) SELECT id FROM articles
ON CONFLICT DO NOTHING;
```

### Changing the model

The dimension is the only thing the schema commits to, so a different
1024-dimension encoder costs a re-run and no migration:

```sql
UPDATE article_embeddings SET embedding = NULL, model = NULL WHERE model <> 'new/model';
```

Then point `EMBED_MODEL` and the Jetson's `MODEL_ID`/`MODEL_DIR` at it and
restart both. Search degrades while the queue drains — rows with a NULL vector
are not searchable.

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

A sixth compose service, `web` (command `babel serve`), serves the archive read-only over HTTP. It
is a separate FastAPI process from the crawler, deliberately outside gluetun's network namespace: it
needs *inbound* connections, which that namespace cannot accept, and its only outbound dependency is
Postgres on the bridge — so the site stays up when the VPN tunnel is down. `babel serve` never
applies migrations: `crawler` and `images` both do that at startup, and a third command written the
same way would connect as a SELECT-only role and crash-loop under `restart: unless-stopped`.
Applying the schema is an explicit operator step (below). It does check that the step was taken —
one throwaway connection reads `schema_migrations` before anything is served, and the process
refuses to start, naming whichever of `005_browse.sql`/`007_body_markup.sql`/`008_embeddings.sql`
is missing. A skipped migrate step otherwise answers 503 on every page while `/healthz` and the
compose healthcheck stay green.

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

Every article and comment collected before migration 007 has no `body_raw`, so it renders through
the plain-text fallback: no paragraphs for the oldest rows, and no emphasis, links or in-position
images for any of them. Re-collection is what fills the column, and it is a deliberate three-step
pass, not a queued job.

**`--sweep-only` is not optional here.** Without it the one-shot walks instead of sweeping: the walk
runs while `cursor >= stop_at`, `babel run` hardcodes `stop_at=1`, and a live cursor is in the
millions — so the sweep is a month away and the pass silently collects nothing it was asked to.
The flag skips the walk and, deliberately, neither reads nor writes the cursor, so the walk keeps
its place with nothing to restore afterwards.

**Expect an hour of apparent nothing first.** `refetch` stamps `updated_at = now()` and the sweep
only claims rows older than `retry_cooldown_sec` (an hour by default), so the first hour logs
nothing at all. After that it logs `sweeping 50 article(s)` roughly once a minute. Judge progress
by `SELECT count(*) FROM articles WHERE body_raw IS NOT NULL`, not by the logs, and note the loop
idles rather than exiting when the queue drains — stopping it is the operator's job.

**Run it as the `sweep` service, not as `compose run --rm`.** A one-shot container carries
`restart: no` and is deleted when it exits, so a host reboot ends the pass and nothing brings it
back — while `db`, `images` and `web` all return healthy around it, which is what makes the failure
invisible. The `sweep` service is the same command with `restart: unless-stopped`, so it survives a
reboot; stopping it by hand is what keeps it stopped afterwards.

It sits behind a compose profile so that a bare `docker compose up -d` cannot start it beside the
walk: `RateLimiter` is constructed per process, so two crawling processes make two requests a second
against a source this project decided to ask once a second.

```bash
docker compose run --rm crawler babel refetch --from 1 --to 2797025 --yes
docker compose stop crawler
docker compose --profile sweep up -d sweep        # runs until you stop it, reboot or no reboot
docker compose --profile sweep stop sweep         # once the queue drains
docker compose up -d crawler
```

Stopping `crawler` first is not optional, and neither is `--sweep-only`. `run_backfill` only reaches
its sweep phase once the walk bottoms out (finding M1 in CLAUDE.md), which is ~32 days away, so the
`stale` rows a running service is holding are not picked up in the meantime. Nothing is lost by
waiting — waiting burns no attempts — but nothing happens either.

Track it with the queue itself rather than the container list, since a stopped sweep and a working
one look identical from outside:

```sql
SELECT count(*) FROM fetch_log WHERE status = 'stale';
```

**Then close the gap above `--to`, because the poller kept working while you were not looking.**
`--to` is chosen as the newest article that exists when the range is picked, but the poller goes on
ingesting above that ceiling for as long as it takes to `git pull`, rebuild and restart — and until
the restart it is doing so with the *old* image, which does not write the column being backfilled.
Those articles are too new for the refetch and too old for the new code, and nothing ever revisits
them: the walk only descends, and the sweep only sees rows something has queued.

The first real re-collection lost 27 articles this way, IDs 2797026-2797053 against a `--to` of
2797025 — 0.02% of the archive, found only by querying the column afterwards. Every one of them was
from the last three days, i.e. exactly the articles a reader is most likely to open.

So after the sweep drains, ask what is still NULL rather than assuming the pass covered it:

```sql
SELECT count(*), min(id), max(id) FROM articles WHERE body_raw IS NULL;
```

A contiguous block starting one above the old `--to` is this gap. Queue that range and sweep it
again — with `RETRY_COOLDOWN_SEC=0`, since there is no reason to wait an hour for two dozen rows,
and `timeout` because the loop idles instead of exiting:

```bash
docker compose run --rm crawler babel refetch --from 2797026 --to 2797053 --yes
docker compose stop crawler
timeout 150 docker compose run --rm -e RETRY_COOLDOWN_SEC=0 crawler babel run --no-poll --sweep-only
docker compose up -d crawler
```

**A few rows will stay NULL, and that is the archive working.** After the gap above was closed one
article remained: 2797032, whose re-collection came back `missing` because it had been deleted from
the site in the days since. Its plain text survives from the first collection and its markup never
will. The same is true of comments — 6 kept text with no markup because they were deleted between
the two visits. Judge the pass by whether the NULLs are a contiguous recent block (a gap, fixable)
or scattered singletons whose `fetch_log` says `missing` (deleted upstream, nothing to fix).

Do not read a large NULL count on `comments` as any of this. Around 2% of comments have no
`body_raw` *and* no `body`, which is deliberate: `parse_comments` stores a removed comment as its
slot and nothing else, so that the renderer has no body to render for a comment whose whole point is
that there is no body. Only a comment with text and no markup is worth looking at.

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

**Build before you migrate.** The Dockerfile does `COPY migrations ./migrations` and nothing
bind-mounts that directory (the crawler's only volumes are `data/images` and `tests/fixtures`), so a
`babel migrate` run before the rebuild executes inside the *old* image and cannot see a migration
that arrived with `git pull`. It reports nothing to apply and exits 0 — and then `web` refuses to
start, naming a migration the operator just watched "succeed". Verified against this repository's
own Dockerfile.

```bash
git pull
docker compose stop crawler images
docker compose build crawler images web
docker compose run --rm crawler babel migrate
docker compose up -d crawler images web
```

Migration 007 adds two nullable columns and rewrites nothing, so it does not carry 005's `ShareLock`
problem. It still goes through stop/migrate/start, because `web` refuses to serve without it (see
`REQUIRED_MIGRATIONS` in `src/babel/web/app.py`) and all three images are being rebuilt anyway:

```bash
docker compose stop crawler images
docker compose build crawler images web
docker compose run --rm crawler babel migrate
docker compose up -d web
```

`web` is safe to bring up straight away, before any re-collection: rows without `body_raw` render
exactly as they do today, through the plain-text fallback. Then run the re-collection pass below, and
finally `docker compose up -d crawler images`.

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
