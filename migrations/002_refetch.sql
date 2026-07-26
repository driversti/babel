-- The sweep asks for rows that failed or were marked for re-collection and are
-- past their cooldown. A partial index keeps this cheap against a fetch_log
-- that will hold ~2.8M rows, almost all of them 'ok' and therefore irrelevant.
CREATE INDEX fetch_log_retryable_idx
    ON fetch_log (article_id DESC)
    WHERE status IN ('error', 'stale');
