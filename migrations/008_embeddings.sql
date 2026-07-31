-- Vectors, and the queue that fills them.
--
-- APPLY THIS DELIBERATELY, and only after `docker compose build`. Migrations
-- are baked into the image (the Dockerfile COPYs migrations/ and nothing
-- bind-mounts it), so migrating before the rebuild runs the *old* file, reports
-- nothing to do, and exits 0. README has the order.
--
-- Requires the pgvector extension, which stock postgres:17 does not carry. The
-- db service image must be pgvector/pgvector:0.8.6-pg17-trixie — the -trixie
-- variant specifically, matching the 17.10-1.pgdg13+1 already running. Both
-- ship Debian GLIBC 2.41-12+deb13u3; a bookworm image would change collation
-- under every existing text index.
CREATE EXTENSION IF NOT EXISTS vector;

-- A row per article, created at ingest with a NULL vector. This mirrors
-- article_images, and for the same reason: the alternative — no row until
-- there is a vector, and an anti-join for the queue — reads the articles
-- primary key backwards and probes for each row, which is cheap only while the
-- unembedded rows are near the top. They are not. The poller adds ~18 a day at
-- the top and the descending walk adds ~1 a second at the *bottom*, so in
-- steady state the queue lives at the walk frontier and every claim scans past
-- the whole embedded corpus to reach it — growing to 2.8M index entries, paid
-- again every couple of seconds, forever.
--
-- NO status and NO attempts column, unlike article_images. That table needs
-- them because a remote host can permanently refuse a URL and the verdict has
-- to be remembered. Embedding has no such verdict: a failure is always
-- transient — the service is down, the batch timed out — and the answer is
-- always to try again. Retry accounting lives in the worker process, where a
-- restart resets it, which is right for a transient-only failure.
--
-- The FK is a plain, non-deferred REFERENCES, same as every other one in this
-- schema — deliberately. save_article needs to read the pre-overwrite body
-- before the articles upsert runs (see the comment there), which at first
-- looked like it needed the *row creation* to happen early too, in one
-- combined statement — and that combined statement, tried directly, fails
-- with ForeignKeyViolationError ("is not present in table articles") for any
-- article never seen before, because a plain FK checks at the end of the
-- statement that violates it, not at commit, and articles.id has no matching
-- row yet at that point in the transaction. The fix was not to defer the
-- constraint — that would change when Postgres reports *every* future
-- violation of this FK, not just this one call's — but to split the
-- statement: the UPDATE that clears a changed body's vector touches only a
-- row that already exists, so no FK check arises from it at all, and the
-- INSERT that creates a new article's queue row runs after the upsert, where
-- the parent row already exists and the ordinary immediate check just passes.
CREATE TABLE article_embeddings (
    article_id bigint PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    embedding  halfvec(1024),
    model      text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- The queue. Partial, so it shrinks to nothing as the corpus drains and never
-- covers a row that already has a vector.
CREATE INDEX article_embeddings_pending_idx
    ON article_embeddings (article_id DESC) WHERE embedding IS NULL;

-- Everything already collected. ~162,618 rows at the time of writing, all of
-- them narrow, so this is fast — unlike 005's index builds, which is why this
-- one may live in the migration at all.
INSERT INTO article_embeddings (article_id) SELECT id FROM articles
ON CONFLICT DO NOTHING;

-- The HNSW similarity index is deliberately NOT here. CREATE INDEX
-- CONCURRENTLY is illegal inside a transaction and this runner wraps a whole
-- file in one, so an index build at scale is an operator step through psql.
-- README carries the statement and the maintenance_work_mem it needs.
