-- Separate from 005 on purpose: if the index build in 005 fails, its whole
-- transaction rolls back, and dropping the old indexes in the same file would
-- leave `articles` with neither set.
DROP INDEX IF EXISTS articles_published_at_idx;  -- superseded by articles_list_idx
DROP INDEX IF EXISTS articles_country_idx;       -- superseded by articles_country_list_idx
