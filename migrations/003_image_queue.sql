ALTER TABLE article_images ADD COLUMN attempts smallint NOT NULL DEFAULT 0;

-- The same-pass design marked rows this way when the disk was low, which took
-- them out of the queue for good. The worker now sleeps instead, so any such
-- row belongs back in the queue.
UPDATE article_images SET status = 'pending' WHERE status = 'skipped_no_space';

-- The drain order: newest article first, because that is where the images that
-- still resolve are. Partial, so the index stays small as rows leave the queue.
--
-- The predicate must list exactly the statuses claim_pending_images asks for,
-- and that query must spell them as a literal rather than a bound parameter.
-- Postgres proves a partial index applicable only from a Const; a narrower
-- predicate here, or a Param there, and the planner sequentially scans a table
-- holding millions of rows that are almost all 'ok' or 'dead'.
CREATE INDEX article_images_queue_idx
    ON article_images (article_id DESC, position)
    WHERE status IN ('pending', 'error');
