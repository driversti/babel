-- The markup the game served, kept so rendering can be fixed without a re-crawl.
--
-- SPEC.md's "Raw HTML is not archived" is deliberately narrowed here, not
-- broken: that decision rejected the whole page at 118 GB. This is postBody and
-- the comment <p> only — about 1.4x the text already stored, so roughly 13 GB
-- across the full archive. What it buys is the thing the archive kept paying
-- for: a rendering or parsing mistake becomes `docker compose up -d web`
-- instead of another 32-day walk. migration 004's own comment records the cost
-- of not having it.
--
-- NAMED body_raw, NOT body_html. It is untrusted bytes from a third-party
-- server. `body_html` would read as "already sanitised" and would invite
-- `|safe` in a template. Nothing may render this column except
-- babel.web.markup.render_body.
--
-- Nullable on purpose: NULL means "collected before this change" and renders
-- through the old plain-text path, so `web` can be deployed before any
-- re-collection has run. Unlike 005 this migration is metadata-only — ADD
-- COLUMN with no default takes ACCESS EXCLUSIVE but rewrites nothing.

ALTER TABLE articles ADD COLUMN body_raw text;
ALTER TABLE comments ADD COLUMN body_raw text;
