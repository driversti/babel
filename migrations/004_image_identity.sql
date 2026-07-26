-- An image reference is identified by its URL, not by where it happens to sit.
--
-- article_images was keyed on (article_id, position), and position is purely
-- ordinal within the article body: `enumerate` over the img nodes. Adding,
-- removing or reordering a single image shifts every later slot. The upsert in
-- save_article updates source_url while deliberately preserving status, sha256
-- and attempts — correct while a slot means the same image, catastrophic once it
-- does not. A shifted slot got a new URL welded to the previous image's verdict:
-- 'ok' plus the sha256 of a different image for a URL never fetched, or 'dead'
-- for one never looked at. Neither is recoverable, because the queue excludes
-- both statuses, and articles.body is stored as markup-stripped text, so
-- source_url is the only surviving record of an article's image URLs — there is
-- nothing left to reconcile against.
--
-- Keying on the URL removes the failure rather than guarding it: a position
-- shift cannot remap anything, and a row keeps its captured blob forever.
--
-- Rows whose URL no longer appears in the article are deliberately NOT deleted.
-- An image the author removed is exactly what this archive exists to still hold,
-- and the blob is already stored; dropping the row would orphan it.

-- Duplicates first, or the primary key cannot be created. These are real and
-- common: newspaper-style articles repeat one divider image between sections,
-- measured at 2,628 duplicate groups across 12,720 rows — 43% of the queue at
-- the time — every one of them a separate fetch of bytes we already had.
-- Keep the most informative row: one that was actually captured, else the
-- earliest position.
DELETE FROM article_images a
      USING article_images b
      WHERE a.article_id = b.article_id
        AND a.source_url = b.source_url
        AND (
              (b.sha256 IS NOT NULL AND a.sha256 IS NULL)
           OR (((b.sha256 IS NULL) = (a.sha256 IS NULL)) AND b.position < a.position)
        );

ALTER TABLE article_images DROP CONSTRAINT article_images_pkey;
ALTER TABLE article_images ADD CONSTRAINT article_images_pkey
    PRIMARY KEY (article_id, source_url);

-- position stays, as the first place the image appears, and still orders the
-- drain. article_images_queue_idx is unchanged and still applies.
