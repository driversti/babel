-- Browse indexes, suppression tombstones, and the blob back-reference.
--
-- APPLY THIS DELIBERATELY, never as a side effect of `docker compose up -d`.
-- Both `babel run` and `babel images` call apply_migrations at startup, and
-- apply_migrations wraps a whole file in one transaction, so a per-service
-- restart would run this DDL against a live walk and hold ACCESS EXCLUSIVE on
-- `articles` for the length of the build. lock_timeout makes a contended run
-- fail fast instead of blocking save_article indefinitely. The runbook is in
-- README.md.
--
-- CREATE INDEX CONCURRENTLY is not available through this runner: it is illegal
-- inside a transaction block, and asyncpg wraps a multi-statement file in an
-- implicit one even without the explicit transaction. Index builds at scale are
-- an operator step through psql.
SET lock_timeout = '5s';

-- One index per filter combination the list can produce. The query text is
-- built per combination too, deliberately: `($1 IS NULL OR country = $1)` is
-- opaque to the planner and degrades to a scan under a generic plan.
CREATE INDEX IF NOT EXISTS articles_list_idx
    ON articles (published_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS articles_country_list_idx
    ON articles (country, published_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS articles_author_list_idx
    ON articles (lower(author_name), published_at DESC, id DESC);
-- Measured at 2.8M rows: without this the two-filter query still uses an index
-- (the planner anchors on the author predicate, ~150k distinct values against
-- 70 countries) at 10ms warm, 58ms cold, 49.7ms once the prepared statement
-- flips to a generic plan; with it, 0.064ms, for ~157MB.
CREATE INDEX IF NOT EXISTS articles_country_author_list_idx
    ON articles (country, lower(author_name), published_at DESC, id DESC);

-- Takedown is a tombstone, never a DELETE. Measured on a fresh database: a
-- DELETE cascades comments and article_images but leaves the images row and the
-- blob on disk, and fetch_log has no FK to articles, so its row survives as
-- 'ok' — `babel refetch` then flips it to 'stale', the sweep re-collects, and
-- the taken-down article comes back. Deletion is silently reversible by tooling
-- this project already ships. Do not "simplify" these columns away.
ALTER TABLE articles ADD COLUMN IF NOT EXISTS hidden_at   timestamptz;
ALTER TABLE images   ADD COLUMN IF NOT EXISTS withheld_at timestamptz;

-- Answers "which other articles cite this blob", which is what makes a
-- per-image takedown decision possible at all. Content-addressing means a blob
-- is shared, so withholding one is never a single-article act.
CREATE INDEX IF NOT EXISTS article_images_sha256_idx ON article_images (sha256);
