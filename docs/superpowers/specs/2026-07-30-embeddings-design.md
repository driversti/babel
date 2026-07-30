# babel phase 3, first slice — cross-language semantic search

Design spec. Written 2026-07-30.

Read [SPEC.md](../../../SPEC.md) first, in particular "Later phases, in brief". This document
takes that sketch — `bge-m3`, `halfvec(1024)`, binary-quantised HNSW, Postgres as the only
datastore — and turns it into something buildable. Where it contradicts the sketch, it says so and
gives the measurement.

## Problem

The archive holds 162,618 articles in at least eight languages and there is no way to find one by
what it is *about*. Browse filters by country and author; that is metadata, not content. Phase 2's
other half, keyword search, was never built — and SPEC.md already anticipated that it "will visibly
fail across languages", because comparing words cannot match a Ukrainian query to a Hungarian
article.

This slice delivers the thing that can: an embedding per article, stored beside it in Postgres, and
a search box on the public site that finds articles by meaning regardless of the language either
side is written in.

## Decisions

Taken with the operator before design, and not open in implementation:

1. **The search is public**, part of `babel serve`, not a private CLI. This is the constraint that
   shapes everything else: a user-supplied string has to be turned into a vector on the request
   path.
2. **Articles only, not comments.** There are 2,985,540 comments against 162,618 articles — an 18x
   corpus for a different product. Out of scope.
3. **One encoder, on the Jetson, for both the corpus and the queries.** See "Why one encoder".
4. **One vector per article**, not per chunk. See "Why one vector".
5. **`bge-m3`**, as SPEC.md already chose. See "Why bge-m3, and what it costs to change".
6. **The Jetson holds no state and no credentials.** It serves `POST /embed` and nothing else; the
   database work stays on the x86 box. This reverses the sketch in SPEC.md ("reading from Postgres
   over the LAN") — see "Why the Jetson does not touch Postgres".
7. **Degradation is honest, not silent.** When the Jetson is unreachable the search box says so.
   There is no keyword fallback, because keyword search does not exist.

## What was measured

Taken 2026-07-30 by reading the two hosts directly. Everything in this section is measured; the
"Still unmeasured" section at the end is the list of what is not.

**The Jetson** (`jetson@<jetson-host>`):

| | |
|---|---|
| Board | Jetson Orin Nano **Super** Engineering Reference Developer Kit |
| Power mode | `MAXN_SUPER` (the fast one) |
| L4T / JetPack | R36.4.7 → JetPack 6.2, Ubuntu 22.04.5, kernel 5.15.148-tegra |
| CPU / RAM | 6 cores, 7.4 GiB total — 6.2 GiB available, plus 3.7 GiB swap |
| Disk | 456 GB NVMe, 387 GB free |
| CUDA | `nvidia-l4t-cuda` 36.4.7 present; `libcuda.so` under `/usr/lib/aarch64-linux-gnu/tegra/` |
| CUDA toolkit | **absent** — no `/usr/local/cuda`, no `nvcc` |
| Docker | 29.6.2, runtimes `runc` only, **no NVIDIA runtime**, no `/etc/docker/daemon.json` |
| Already running | `ytdlp-telegram-bot`, `ytdlp-file-server`, `ytdlp-pot-server`, `jupyter` — ~1 GB RAM |

The existing `jupyter` container runs `torch 2.8.0+cpu` with `torch.cuda.is_available() == False`.
Nothing on this machine currently uses the GPU. The missing CUDA toolkit is not a problem for the
container path: an L4T base image ships the CUDA userspace, and the driver comes from the host
through the NVIDIA container runtime. It would be a problem for a bare-metal build.

**The corpus** (`erepublik@<deploy-host>`, live):

| | |
|---|---|
| Articles | 162,618 (CLAUDE.md still says 10,093 — stale since the last handoff) |
| With `body_raw` | 162,617 — the re-collection sweep has effectively finished |
| Comments | 2,985,540 |
| `body` length | mean 1,850 chars; p50 **926**; p90 4,626; p99 13,959; max 65,541 |
| `body` bytes | mean 2,159 |
| `lang` | **NULL in every row** — the column is never populated |
| Postgres | 17.10, `17.10-1.pgdg13+1` — a **trixie** base |

Two of these change the design. The length distribution is what settles "one vector per article".
The Debian version is what settles which pgvector image may be used.

`lang` being empty is noted and deliberately not fixed here. It does not affect embeddings —
`bge-m3` needs no language label — but it means a "Serbian articles about X" filter cannot be
built on that column. Separate work.

**pgvector images.** `pgvector/pgvector` publishes `0.8.6-pg17-trixie`, which is both new enough
for SPEC.md's hard requirement (≥ 0.8, for iterative index scans) and the same Debian generation as
the running server.

## Why one encoder

If the corpus is encoded by one model and the query by another, the vectors stop being comparable
and **nothing reports it**. No exception, no log line, no failed healthcheck — the search box
simply starts returning irrelevant articles, and the only detector is a human noticing that the
results are bad. This project has been bitten twice by exactly that shape of failure: image rows
recorded `dead` while the images were alive (64% of same-day images, found only by measuring), and
a render cap that passed its own test for the wrong reason. Both were silent.

So: one model, one runtime, one process. The Jetson encodes the corpus and the queries.

The cost is that public search depends on a machine that public traffic does not otherwise touch.
That cost is small here — both hosts are in the same house behind the same uplink, so the failure
modes that take the Jetson out mostly take the site out anyway — and it is bounded by decision 7:
when `/embed` does not answer, the search box says search is unavailable and browse keeps working.

A CPU encoder on the x86 box, as a second path, is deliberately left for later. It is a pure
addition: it changes neither the schema nor a single stored vector, so nothing about this design
has to be revisited to add it. What it *would* require is a measurement this slice does not make —
that the same weights at a different precision on a different device produce vectors close enough
to the stored ones to rank identically.

## Why one vector

The median article is 926 characters. Even at the worst tokens-per-character ratio in this corpus
(Persian and Cyrillic script, roughly 0.4 tokens/char against ~0.25 for Latin), that is under 400
tokens — comfortably inside a single forward pass. p90 is 4,626 characters. Chunking exists to stop
a long document's topic from being diluted across one averaged vector; at this length distribution
there is nothing to dilute.

The tail is real but thin: p99 is 13,959 characters and the longest is 65,541. Those get truncated
at the token cap and represented by their opening, which is where an article states its subject.

Chunking remains available later at the cost of a re-run, not a redesign: adding a `chunk_index`
to the primary key and re-embedding is days of GPU time this machine has spare.

## Why bge-m3, and what it costs to change

`bge-m3` is a 568M-parameter XLM-RoBERTa-large encoder covering 100+ languages with an 8,192-token
context, producing 1,024-dimension dense vectors. It needs **no instruction prefix** — unlike the
`multilingual-e5` family, where forgetting `query:` / `passage:` is another silent-degradation
failure. It is the model SPEC.md already picked and the storage math already assumes.

`Qwen3-Embedding-0.6B` is newer and scores better on multilingual retrieval benchmarks. It also
emits 1,024 dimensions. **The dimension is what the schema commits to**, so switching models later
costs a re-run of the corpus and nothing else: no migration, no index redefinition, no change to
the query. That is why the model choice is not worth agonising over now, and why the `model` column
below exists.

## Architecture

```
Jetson <host>                    x86 box <host>
┌────────────────────────┐              ┌───────────────────────────────┐
│ babel-embed-service    │              │ db  (pgvector/pgvector:pg17)  │
│  arm64, NVIDIA runtime │              │   articles                    │
│  bge-m3 fp16 on GPU    │              │   article_embeddings          │
│                        │              └───────────────────────────────┘
│  POST /embed           │◀───batches───┐         ▲              ▲
│  GET  /healthz         │              │         │              │
└────────────────────────┘              │  ┌──────┴───────┐  ┌───┴────────┐
              ▲                         └──│ embed worker │  │ web        │
              │                            │ `babel embed`│  │ /search    │
              └──────one query per search──┼──────────────┼──┤            │
                                           └──────────────┘  └────────────┘
```

Three moving parts:

1. **`babel-embed-service`** on the Jetson. Stateless. Takes a list of strings, returns a list of
   vectors and the model id it used. Knows nothing about eRepublik, articles or Postgres. Built and
   run on the Jetson itself — no cross-compilation, no registry push, which retires SPEC.md's
   "ARM64" risk rather than paying it.
2. **`babel embed`**, a new long-running service on the x86 box, alongside `crawler`, `images` and
   `web`. It claims articles with no vector, newest-first, batches their text to `/embed`, and
   writes what comes back. Same shape as `imageworker.py`: a drainable queue, retries with a
   ceiling, a circuit breaker, and no state outside Postgres.
3. **`/search`** in `babel serve`. One `/embed` call for the query string, then one SQL statement.

### Why the Jetson does not touch Postgres

SPEC.md's sketch has the Jetson "reading from Postgres over the LAN". That is worse than it looks,
for a reason that only surfaces when you check the compose file: `db` publishes on
`127.0.0.1:5432`, deliberately, "purely so the operator can run `psql` from the host". Letting the
Jetson connect means republishing Postgres on the LAN address, adding a `pg_hba` rule, creating a
role with write access, and putting that role's password on a third machine — a permanent widening
of the database's exposure, in service of a process that only needs to multiply matrices.

Inverting it costs nothing. The x86 box already runs every other worker in this project, already
holds the credentials, and already has the batching/retry/circuit-breaker patterns written down in
`imageworker.py`. The bulk traffic this adds to the LAN is trivial: 2.8M articles at ~2.2 KB of
text out and 2 KB of vector back is under 12 GB, once, over gigabit.

The result is a Jetson service with no secrets, no database driver, no migrations, and a test that
is one HTTP call.

## Schema

Migration `008_embeddings.sql`:

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE article_embeddings (
    article_id bigint PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    embedding  halfvec(1024),
    model      text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- The queue. Shrinks to nothing as the corpus drains, and never covers a row
-- that has a vector.
CREATE INDEX article_embeddings_pending_idx
    ON article_embeddings (article_id DESC) WHERE embedding IS NULL;

-- Seed the 162,618 articles already collected.
INSERT INTO article_embeddings (article_id) SELECT id FROM articles
ON CONFLICT DO NOTHING;
```

`halfvec` is two bytes per dimension instead of four: 2,048 bytes per article, so ~333 MB at
today's 162,618 rows and ~5.7 GB across the full 2.8M archive. That matches SPEC.md's "text,
metadata and vectors total roughly 15 GB".

**A row exists from ingest with a NULL embedding, exactly like `article_images`.** `save_article`
gains one statement; migration 008 seeds what is already collected.

The obvious alternative — no row until there is a vector, and an anti-join for the queue — was
designed first and rejected on its steady-state cost. `articles LEFT JOIN article_embeddings WHERE
ae.article_id IS NULL ORDER BY a.id DESC LIMIT 32` reads the `articles` primary key backwards and
probes for each row, which is cheap only while the unembedded rows are near the top. They are not:
the crawler adds ~18 articles a day at the top through the poller and ~1 a second at the *bottom*
through the descending walk, so in steady state the queue lives at the walk frontier and every
claim scans past the entire embedded corpus to reach it — a cost that grows to 2.8M index entries
and is paid again every couple of seconds, forever. A partial index on `embedding IS NULL` makes
the same claim O(queue), which is the shape `claim_pending_images` already has and for the same
reason.

**No status and no attempts column**, unlike `article_images`. That part of the comparison holds:
`article_images` needs statuses because a remote host can permanently refuse a URL and that verdict
has to be remembered. Embedding has no such verdict — a failure is always transient (the service is
down, the batch timed out) and the answer is always to try again. Retry accounting stays in the
worker process, where a restart resets it, which is right for a transient-only failure.

**The seeding creates a gap the same shape as the one `refetch --to` left**, and it closes the same
way. Migration 008 seeds the articles that exist when it runs; `save_article` queues the ones
ingested afterwards. An article ingested *between* the two — by a crawler still running the old
image — gets neither. The documented deploy order already prevents it (stop the writers, build,
migrate, start), and README carries a reconcile statement for when it happens anyway:
`INSERT INTO article_embeddings (article_id) SELECT id FROM articles ON CONFLICT DO NOTHING`.

`model` records which encoder produced the row. Two uses, both load-bearing:

- `babel embed` refuses to write a vector when `/embed` reports a model id different from the one
  it is configured for. A model swapped underneath the service is the silent failure from "Why one
  encoder", arriving through the back door.
- Switching models becomes a query: `DELETE FROM article_embeddings WHERE model <> $1` re-opens the
  whole corpus for re-embedding, and a mixed table is visible rather than merely wrong.

The HNSW index is **not** in the migration file. Migration 005's comment already establishes why:
`CREATE INDEX CONCURRENTLY` is illegal inside a transaction and this project's runner wraps every
file in one, so index builds at scale are an operator step through `psql`. It goes in README's
runbook with `maintenance_work_mem` raised for the build:

```sql
SET maintenance_work_mem = '4GB';
CREATE INDEX article_embeddings_bin_idx ON article_embeddings
    USING hnsw ((binary_quantize(embedding)::bit(1024)) bit_hamming_ops)
    WHERE embedding IS NOT NULL;
```

Partial, because the same table now holds the queue: rows waiting to be embedded have a NULL
vector and belong in no similarity index. The predicate is repeated in the search query so the
planner can match it.

Sizing: the quantised vectors are 128 bytes each — 358 MB at 2.8M rows, which is the "about 400 MB"
figure in SPEC.md. The HNSW graph's neighbour lists sit on top of that and are not free; expect
closer to 1 GB in total. To be measured, not assumed.

**Build the index now, at 162k rows, not later at 2.8M.** At the current size an exact scan over
the 21 MB of quantised vectors would be fast enough and the index unnecessary — which is precisely
the argument for building it anyway. The search path that runs today should be the search path that
runs at 2.8M, so that its behaviour is observed early rather than discovered when the corpus makes
it mandatory. Incremental insert cost is irrelevant at a write rate of single-digit rows per
second.

## The retrieval query

Two stages in one statement: find candidates by Hamming distance over the 128-byte quantised
vectors, then re-rank those candidates against the full `halfvec`.

```sql
BEGIN;
SET LOCAL hnsw.ef_search = 500;
SET LOCAL hnsw.iterative_scan = relaxed_order;

SELECT a.id, a.title, a.author_name, a.country, a.published_at,
       1 - (c.embedding <=> $1::halfvec(1024)) AS score
FROM (
    SELECT ae.article_id, ae.embedding
    FROM article_embeddings ae
    JOIN articles ar ON ar.id = ae.article_id AND ar.hidden_at IS NULL
    WHERE ae.embedding IS NOT NULL
    ORDER BY binary_quantize(ae.embedding)::bit(1024)
             <~> binary_quantize($1::halfvec(1024))::bit(1024)
    LIMIT 500
) c
JOIN articles a ON a.id = c.article_id
ORDER BY c.embedding <=> $1::halfvec(1024)
LIMIT 20;
COMMIT;
```

**The `BEGIN` is not decoration.** `SET LOCAL` applies to the surrounding transaction, and outside
one it does nothing at all — Postgres emits `WARNING: SET LOCAL can only be used in transaction
blocks` and carries on, which asyncpg does not raise. Since asyncpg runs each `execute` in its own
implicit transaction, issuing the two `SET LOCAL`s as separate calls on a pooled connection would
leave both settings at their defaults and no error anywhere. The alternative — plain `SET` — is
worse on a pool, because it persists on that connection for whichever unrelated request picks it up
next. So the search runs inside an explicit transaction, and that is a property to pin with a test,
not a line of style.

Three settings carry real weight, and each is a silent failure when wrong:

- **`hnsw.ef_search` must be at least the inner `LIMIT`.** The default is 40. Left at the default,
  the inner query asks for 500 candidates and the index returns 40, so 460 of them silently do not
  exist and recall collapses. Nothing errors.
- **`hnsw.iterative_scan`** is why SPEC.md requires pgvector ≥ 0.8. Without it, a filter — the
  `hidden_at IS NULL` above, and any country or date predicate added later — is applied *after* the
  graph walk, so a selective filter returns far fewer rows than asked for. `relaxed_order` is the
  right mode here because the outer re-rank re-sorts anyway.
- **The corpus and the query must be encoded identically — same model, same output head.** `bge-m3`
  emits three different representations (dense, lexical/sparse, and a multi-vector ColBERT head).
  Only the dense one belongs in this column, and choosing the wrong one yields perfectly valid
  1,024-dimension vectors that retrieve badly.

  What this bullet is **not** is a normalisation requirement, which an earlier draft of this
  document asserted and which is false. Measured directly against `pgvector/pgvector:0.8.6-pg17-trixie`:
  `binary_quantize` reads only the sign of each dimension, and `<=>` normalises internally, so
  scaling a vector by 1000x changes neither the bits (`[0.5,-0.5,0.5,-0.5]` and
  `[500,-500,500,-500]` both quantise to `1010`) nor the cosine distance (0.292893218813 in both
  cases). Only `<#>`, inner product, is magnitude-sensitive — it returns -1 against -1000 for that
  same pair — and this design does not use it. `bge-m3` normalises by default and that stays on;
  the worker asserts the norm as a *change detector* on the encoder, not as a correctness guard.
  If a future revision switches the re-rank to `<#>` for speed, this becomes load-bearing and the
  assertion is what makes the switch safe.

The over-fetch ratio (500 → 20) is the recall knob. 25x is a starting value, to be checked against
the evaluation set below, not a measured optimum.

## The embed service (Jetson)

`POST /embed`, request `{"texts": ["…"]}`, response `{"model": "BAAI/bge-m3", "vectors": [[…]]}`.
`GET /healthz` returns the model id and whether CUDA is live. That is the entire contract.

Deliberate constraints:

- **Bind to the LAN interface, and cap input length hard.** Every query string reaching this
  service originated with an anonymous internet user typing into a public search box. Transformer
  attention is quadratic in sequence length, so an unbounded query is a way to stall the GPU from
  the outside — the same class of attack as the one `MAX_MARKUP_BYTES` exists to stop, arriving at
  a different door. The cap is applied twice, at `web` before the call and at the service before
  the tokenizer, because the service is reachable from the LAN independently of `web`.
- **Batch size and token cap are configuration, not constants.** Corpus batches and query batches
  are different workloads; a query is one short string that must return in milliseconds, a corpus
  batch is 32 long ones that may take a second.
- **The model is pre-downloaded to a mounted directory, not fetched at start.** A restart must not
  need the internet, and a 2+ GB download must not sit inside a container start.
- **PyTorch fp16 first; TensorRT only if measurement demands it.** Start with the simplest thing
  that runs on the GPU, measure, and escalate only against a number. The model at fp16 is roughly
  1.1 GB of weights against 6.2 GB available.

Prerequisite, and the one change this design makes to a machine that is doing other work:
`nvidia-container-toolkit` must be installed and Docker's default runtime pointed at it, which
**restarts the Docker daemon** and therefore the four containers already running on the Jetson.
Their restart policies are to be checked first.

## The corpus worker (x86)

`babel embed`, a new compose service using the existing babel image. On the bridge network, not in
gluetun's namespace: it talks to Postgres and to a LAN address, and has no reason to egress through
the tunnel — the same reasoning that already keeps `web` outside it.

The loop mirrors `imageworker.py`:

1. Claim a batch of articles with no `article_embeddings` row, newest-first.
2. Build the input as `title + "\n\n" + body` — plain `body`, never `body_raw`, which is untrusted
   markup and would spend the token budget on tag names.
3. `POST /embed`.
4. Verify: the returned model id matches configuration, the vector count matches the batch, and
   each vector's L2 norm is ≈ 1.
5. Write the rows.
6. On failure, back off and retry; on repeated failure against the same endpoint, open the circuit
   breaker and alert through the existing Telegram notifier.
7. When the queue is empty, idle and re-check — the same walk/idle shape `run_backfill` already
   uses. The service is expected to run indefinitely, because the crawler keeps adding articles.

Newest-first, matching the crawler and the image worker, so the most-read part of the archive
becomes searchable first.

## Search on the public site

`GET /search?q=…`, rendered by the same Jinja templates as the browse list, so a result is the row
the reader already recognises.

- Query length capped before the `/embed` call.
- Timeout on `/embed` (order of 2 s). On timeout or connection failure: the page renders with an
  honest "semantic search is unavailable right now" notice and a link back to browse. **Not a
  keyword fallback** — keyword search does not exist in this codebase, and pretending otherwise in
  the degradation path would be a second thing to build under a name that suggests it is already
  there.
- The `/embed` endpoint address is configuration on `web`, and `web`'s environment stays the
  narrow, deliberately-not-`env_file` set it is today.
- Results are `noindex` like the rest of the filtered space, and `robots.txt` disallows `/search`
  for the reason the browse design already gives about crawlable filter spaces.

## Failure modes this design names

Each of these is silent by default. Each gets a test or an assertion, not a comment.

1. **Corpus and query encoded by different models.** → `model` column, worker refuses a mismatch,
   `/healthz` reports the id.
2. **The wrong output head, or corpus and query encoded differently.** → one configured model id
   and one code path shared by the worker and `/search`; the worker asserts dimension and norm as a
   change detector on the encoder. Note what this is *not*: vector magnitude does not affect either
   operator this design uses, per the measurement under "The retrieval query".
3. **`ef_search` below the inner `LIMIT`.** → the search helper sets it from the same constant that
   sets the `LIMIT`; a test asserts they cannot diverge.
4. **Filter applied after the graph walk.** → `iterative_scan` set explicitly; an EXPLAIN test.
5. **`SET LOCAL` issued outside a transaction, so both settings above quietly do nothing.** → the
   search runs in an explicit transaction; a test reads back `current_setting('hnsw.ef_search')`
   inside the same statement path rather than trusting that the `SET` took.
6. **Query-length DoS.** → capped at `web` and again at the service.
7. **A collation change from swapping the Postgres image.** → the pgvector image must be the
   `-trixie` variant. A different Debian generation means a different glibc, which means different
   collation, which means silently wrong text index ordering across the existing archive — the one
   step in this design that can damage data already collected.

   **Measured 2026-07-30, and it is clean:** the running `db` container reports
   `Debian GLIBC 2.41-12+deb13u3`, codename `trixie`; `pgvector/pgvector:0.8.6-pg17-trixie` reports
   the identical `Debian GLIBC 2.41-12+deb13u3`, `trixie`. Not merely the same generation — the same
   package revision. The rule stays written down because it governs the *next* image bump too, and
   the equality is what has to be re-checked then, not assumed from this one.

## Testing

- **The test container changes.** `postgres:17` becomes `pgvector/pgvector:0.8.6-pg17-trixie`
  across the suite, which currently shares one container for the whole run.
- **A fake `/embed`** for worker and web tests: deterministic vectors from a hash of the input, so
  cross-language behaviour is not being asserted by a fake that cannot have it.
- **An EXPLAIN test on the search query**, taking its SQL from the builder itself and asserting on
  `Index Cond:` rather than an index name — following `tests/db/test_browse_plans.py`, not the
  older pattern that finding M3 criticises.
- **A real-model test, opt-in and marked**, not in the default run: the development machine is an
  arm64 Mac with no CUDA and the CI path has no GPU.
- **A cross-language evaluation set** — of the order of 20 hand-built query/article pairs where a
  query in one language must retrieve a known article in another, drawn from the languages actually
  in the archive (Persian, Serbian, Hungarian, Indonesian, Bulgarian, Polish, Spanish, English).
  Recall@20 on this set is the acceptance criterion for the slice. Without it, "semantic search
  works" is an assertion nobody has checked, on a system whose failure mode is plausible-looking
  wrong answers.

## Deployment

Two hosts, and the x86 half is subject to the build-before-migrate rule README already carries —
migrations are baked into the image.

On the x86 box, the `db` image change is the risky step and is a stop/swap/start, never a bare
`up -d`. The PGDATA directory is compatible (`pgvector/pgvector` is the stock `postgres` image plus
the extension), so this is an image swap, not a dump and restore — provided the Debian generation
matches, per failure mode 6.

On the Jetson, `nvidia-container-toolkit`, then a local `docker compose build`, then the service.
The model directory is a bind mount, gitignored, populated once.

## Out of scope

Comments. Translation of results at query time. A cross-encoder re-ranker. Hybrid keyword+vector
fusion. Summarisation and digests (SPEC.md's phase 4). Populating `lang`. A CPU encoder on the x86
box as a second path.

## Still unmeasured

Named here so that the implementation plan starts by measuring them rather than by trusting this
document:

1. **Throughput of `bge-m3` on this Jetson.** The estimate behind the sizing above is 5–15
   articles/second, which would put today's 162,618 articles at 3–9 hours and the full 2.8M archive
   at 2–6 days. If that holds, the GPU is not the bottleneck — the 32-day crawl is. Nothing in this
   design depends on the number being right, but the plan's first task is to get it.
2. **The right token cap.** 1,024 tokens covers roughly p90 of this corpus; 2,048 covers past p97
   at meaningfully more compute. Measure both at the same batch size and pick. Changing it later
   costs a re-run, not a migration.
3. **HNSW index size and build time** at 2.8M rows.
4. **Recall of the quantise-then-re-rank pattern** at a 25x over-fetch, against the evaluation set.
5. **Whether `nvidia-container-toolkit` installs cleanly on JetPack 6.2** and what the four running
   containers do when the daemon restarts.
