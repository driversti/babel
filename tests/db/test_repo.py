import datetime

from babel.db import repo
from babel.models import Article, Comment, ImageRef


def make_article(article_id: int = 1, **kw) -> Article:
    defaults = dict(
        id=article_id,
        title="Title",
        body="Body text",
        author_id=42,
        author_name="someone",
        country="Serbia",
        published_at=datetime.datetime(2026, 7, 25, 12, 0, tzinfo=datetime.UTC),
        e_day=6822,
        comment_count=2,
        images=(ImageRef(position=0, source_url="https://x.example/a.png"),),
        comments=(
            Comment(1, 0, 0, 42, "someone", None, "first"),
            Comment(2, 1, 1, 43, "other", None, None),
        ),
    )
    return Article(**{**defaults, **kw})


async def test_saves_article_with_comments_and_image_rows(pg):
    await repo.save_article(pg, make_article())
    assert await pg.fetchval("SELECT count(*) FROM articles") == 1
    assert await pg.fetchval("SELECT count(*) FROM comments WHERE article_id = 1") == 2
    assert await pg.fetchval("SELECT count(*) FROM article_images WHERE article_id = 1") == 1
    assert await pg.fetchval("SELECT status FROM article_images WHERE article_id = 1") == "pending"


async def test_saving_twice_replaces_rather_than_duplicating(pg):
    await repo.save_article(pg, make_article())
    await repo.save_article(pg, make_article(title="Corrected"))
    assert await pg.fetchval("SELECT count(*) FROM articles") == 1
    assert await pg.fetchval("SELECT title FROM articles WHERE id = 1") == "Corrected"
    assert await pg.fetchval("SELECT count(*) FROM comments WHERE article_id = 1") == 2


async def test_removed_comment_body_is_null(pg):
    await repo.save_article(pg, make_article())
    assert await pg.fetchval("SELECT body FROM comments WHERE id = 2") is None


async def test_fetch_log_counts_attempts(pg):
    await repo.record_fetch(pg, 7, "error", "timeout")
    await repo.record_fetch(pg, 7, "error", "timeout again")
    row = await pg.fetchrow("SELECT status, attempts, last_error FROM fetch_log WHERE article_id = 7")
    assert row["attempts"] == 2
    assert row["last_error"] == "timeout again"


async def test_cursor_round_trips(pg):
    assert await repo.get_cursor(pg, "backfill") is None
    await repo.set_cursor(pg, "backfill", 2_797_000)
    assert await repo.get_cursor(pg, "backfill") == 2_797_000
    await repo.set_cursor(pg, "backfill", 2_796_999)
    assert await repo.get_cursor(pg, "backfill") == 2_796_999


async def test_filter_unseen_skips_anything_already_logged(pg):
    await repo.record_fetch(pg, 100, "ok")
    await repo.record_fetch(pg, 101, "missing")
    assert await repo.filter_unseen(pg, [100, 101, 102]) == [102]


async def test_error_rows_are_offered_again(pg):
    await repo.record_fetch(pg, 200, "error", "boom")
    assert await repo.filter_unseen(pg, [200], retry_errors=True) == [200]
    assert await repo.filter_unseen(pg, [200], retry_errors=False) == []


async def test_errored_row_below_attempt_ceiling_is_reoffered(pg):
    await repo.record_fetch(pg, 210, "error", "boom")  # attempts = 1
    assert await repo.filter_unseen(pg, [210], retry_errors=True, max_attempts=3) == [210]


async def test_errored_row_at_attempt_ceiling_is_not_reoffered(pg):
    for _ in range(3):
        await repo.record_fetch(pg, 211, "error", "boom")  # attempts = 3
    assert await repo.filter_unseen(pg, [211], retry_errors=True, max_attempts=3) == []


async def test_ok_and_missing_rows_are_never_reoffered(pg):
    # Bump attempts well past any plausible ceiling to prove ok/missing rows
    # are excluded on status alone, not because their attempts count happens
    # to be low.
    for _ in range(10):
        await repo.record_fetch(pg, 212, "ok")
    await repo.record_fetch(pg, 213, "missing")

    assert await repo.filter_unseen(pg, [212, 213], retry_errors=True, max_attempts=1) == []
    assert await repo.filter_unseen(pg, [212, 213], retry_errors=False, max_attempts=1) == []


async def test_image_blob_is_deduplicated_by_hash(pg):
    await repo.save_article(pg, make_article())
    digest = b"\x01" * 32
    await repo.save_image_blob(pg, digest, "image/png", 1234)
    await repo.save_image_blob(pg, digest, "image/png", 1234)
    assert await pg.fetchval("SELECT count(*) FROM images") == 1
    await repo.record_image(pg, 1, 0, "https://x.example/a.png", "ok", digest)
    assert await pg.fetchval("SELECT status FROM article_images WHERE article_id = 1") == "ok"


async def test_resaving_article_does_not_resurrect_dead_image_status(pg):
    """A re-parse of an article must not undo a 'dead' verdict on its images.

    `save_article`'s ON CONFLICT clause on `article_images` deliberately omits
    `status` from the DO UPDATE SET list, so a slot that a worker already marked
    'dead' (with sha256 cleared) stays 'dead' when the article is saved again.
    This is intentional and irreversible-by-design: once we know a link is dead,
    a later re-save has no way to tell whether the image came back, and
    overwriting the verdict back to 'pending' would silently throw that
    knowledge away, sending the crawler to re-check a link that already proved
    dead. A regression that adds `status = EXCLUDED.status` to the upsert would
    make this test fail while leaving every other test in this suite green.

    `source_url`, by contrast, IS in the DO UPDATE SET list on purpose, so this
    test also pins that a changed URL for the same slot does get applied on
    re-save -- confirming the upsert still does its job for the column it is
    supposed to touch, not just the one it must leave alone.
    """
    article = make_article()
    await repo.save_article(pg, article)

    # Move the slot off 'pending' the way the image worker would after
    # discovering the source link is gone. sha256=None models "we looked and
    # there is nothing to hash" -- the case whose loss is unrecoverable.
    await repo.record_image(pg, article.id, 0, article.images[0].source_url, "dead", sha256=None)
    row = await pg.fetchrow(
        "SELECT status, sha256 FROM article_images WHERE article_id = $1 AND position = 0",
        article.id,
    )
    assert row["status"] == "dead"
    assert row["sha256"] is None

    # Re-save the same article (as a re-crawl/re-parse would), but with the
    # image's source_url changed, to prove the upsert still updates the column
    # it is meant to update.
    updated = make_article(images=(ImageRef(position=0, source_url="https://x.example/b.png"),))
    await repo.save_article(pg, updated)

    row = await pg.fetchrow(
        "SELECT status, sha256, source_url FROM article_images WHERE article_id = $1 AND position = 0",
        article.id,
    )
    assert row["status"] == "dead", "status must survive a re-save, or a dead link looks pending again"
    assert row["sha256"] is None
    assert row["source_url"] == "https://x.example/b.png"


async def test_duplicate_comment_ids_in_one_save_are_silently_deduped(pg):
    """Documents current behaviour, not a desired guarantee.

    If a parser bug ever produces two `Comment` objects with the same `id` in
    one `Article.comments` tuple, `save_article`'s `ON CONFLICT (id) DO NOTHING`
    insert means the second row is dropped rather than raising. This test
    pins that today's behaviour is "keep the first, ignore the rest, no
    exception" -- it does not assert this is the right outcome. See the report
    for this task for a note on whether silently dropping is desirable, versus
    surfacing the duplicate as a parser-level error.
    """
    dup_id = 99
    article = make_article(
        comments=(
            Comment(dup_id, 0, 0, 42, "someone", None, "first copy"),
            Comment(dup_id, 1, 0, 43, "other", None, "second copy"),
        ),
    )
    await repo.save_article(pg, article)

    rows = await pg.fetch(
        "SELECT id, author_name, body FROM comments WHERE article_id = $1", article.id
    )
    assert len(rows) == 1
    assert rows[0]["id"] == dup_id
    assert rows[0]["author_name"] == "someone"
    assert rows[0]["body"] == "first copy"


async def test_claim_retryable_returns_errors_and_stale_newest_first(pg):
    await repo.record_fetch(pg, 100, "error", "boom")
    await repo.record_fetch(pg, 300, "ok")
    await pg.execute("UPDATE fetch_log SET status = 'stale' WHERE article_id = 300")
    await repo.record_fetch(pg, 200, "error", "boom")
    assert await repo.claim_retryable(pg, limit=10, cooldown_sec=0) == [300, 200, 100]


async def test_claim_retryable_ignores_ok_and_missing(pg):
    await repo.record_fetch(pg, 1, "ok")
    await repo.record_fetch(pg, 2, "missing")
    assert await repo.claim_retryable(pg, limit=10, cooldown_sec=0) == []


async def test_claim_retryable_respects_the_attempt_ceiling(pg):
    for _ in range(repo.MAX_FETCH_ATTEMPTS):
        await repo.record_fetch(pg, 1, "error", "boom")
    await repo.record_fetch(pg, 2, "error", "boom")
    assert await repo.claim_retryable(pg, limit=10, cooldown_sec=0) == [2]


async def test_claim_retryable_honours_the_cooldown(pg):
    await repo.record_fetch(pg, 1, "error", "boom")
    # Just written, so updated_at is now: a one-hour cooldown must exclude it.
    assert await repo.claim_retryable(pg, limit=10, cooldown_sec=3600) == []
    await pg.execute(
        "UPDATE fetch_log SET updated_at = now() - interval '2 hours' WHERE article_id = 1"
    )
    assert await repo.claim_retryable(pg, limit=10, cooldown_sec=3600) == [1]


async def test_claim_retryable_respects_the_limit(pg):
    for article_id in range(1, 6):
        await repo.record_fetch(pg, article_id, "error", "boom")
    assert len(await repo.claim_retryable(pg, limit=2, cooldown_sec=0)) == 2


async def test_mark_stale_resets_status_and_attempts(pg):
    await repo.record_fetch(pg, 1, "ok")
    await repo.record_fetch(pg, 1, "ok")  # attempts now 2
    assert await repo.mark_stale(pg, [1]) == 1
    row = await pg.fetchrow("SELECT status, attempts FROM fetch_log WHERE article_id = 1")
    assert row["status"] == "stale"
    assert row["attempts"] == 0


async def test_mark_stale_makes_a_collected_article_retryable_again(pg):
    await repo.record_fetch(pg, 1, "ok")
    assert await repo.claim_retryable(pg, limit=10, cooldown_sec=0) == []
    await repo.mark_stale(pg, [1])
    assert await repo.claim_retryable(pg, limit=10, cooldown_sec=0) == [1]


async def test_mark_stale_ignores_ids_that_were_never_fetched(pg):
    # Nothing to re-collect: the walk will reach them on its own.
    assert await repo.mark_stale(pg, [999]) == 0


async def test_mark_stale_leaves_missing_rows_alone(pg):
    # A 404 is a fact about the article, not about our collection of it.
    await repo.record_fetch(pg, 1, "missing")
    assert await repo.mark_stale(pg, [1]) == 0
    assert await pg.fetchval("SELECT status FROM fetch_log WHERE article_id = 1") == "missing"


async def test_the_sweep_can_actually_use_the_partial_index(pg):
    # claim_retryable's status set must stay a literal, textually matching
    # fetch_log_retryable_idx's predicate: Postgres only proves a partial index
    # applicable from a Const, never from a bound parameter. Disabling seqscan
    # makes the planner take the index if -- and only if -- it can prove it
    # applies, so this tests provability rather than cost preference.
    await repo.record_fetch(pg, 1, "error", "boom")
    await pg.execute("SET enable_seqscan = off")
    plan = "\n".join(
        r["QUERY PLAN"]
        for r in await pg.fetch(
            """EXPLAIN SELECT article_id FROM fetch_log
               WHERE status IN ('error', 'stale') AND attempts < 5
                 AND updated_at <= now() - make_interval(secs => 0)
               ORDER BY article_id DESC LIMIT 50"""
        )
    )
    # Control: a predicate the index does not cover must NOT reach it, or the
    # assertion above would pass for any query at all.
    wider = "\n".join(
        r["QUERY PLAN"]
        for r in await pg.fetch(
            """EXPLAIN SELECT article_id FROM fetch_log
               WHERE status IN ('error', 'stale', 'ok') ORDER BY article_id DESC LIMIT 50"""
        )
    )
    await pg.execute("SET enable_seqscan = on")
    assert "fetch_log_retryable_idx" in plan, plan
    assert "fetch_log_retryable_idx" not in wider, wider


async def test_claim_returns_newest_articles_first(pg):
    for article_id in (100, 300, 200):
        await pg.execute(
            "INSERT INTO articles (id, title, body, published_at) VALUES ($1, 't', 'b', now())",
            article_id,
        )
        await pg.execute(
            """INSERT INTO article_images (article_id, position, source_url, status)
               VALUES ($1, 0, 'https://x.example/a.png', 'pending')""",
            article_id,
        )
    claimed = await repo.claim_pending_images(pg, limit=10)
    assert [c.article_id for c in claimed] == [300, 200, 100]


async def test_claim_excludes_terminal_statuses(pg):
    # 'ok' and 'dead' are answers and must never be reclaimed; 'pending' and
    # 'error' are unfinished business and both belong in the claim -- see
    # test_an_errored_row_is_reclaimed_below_the_ceiling for why 'error' alone
    # is retried while 'dead' alone is not.
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    for position, status in enumerate(["pending", "ok", "dead", "error"]):
        await pg.execute(
            """INSERT INTO article_images (article_id, position, source_url, status)
               VALUES (1, $1, 'https://x.example/a.png', $2)""",
            position, status,
        )
    claimed = await repo.claim_pending_images(pg, limit=10)
    assert [c.position for c in claimed] == [0, 3]


async def test_claim_skips_rows_at_the_attempt_ceiling(pg):
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status, attempts)
           VALUES (1, 0, 'https://x.example/a.png', 'pending', $1)""",
        repo.MAX_IMAGE_ATTEMPTS,
    )
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status, attempts)
           VALUES (1, 1, 'https://x.example/b.png', 'pending', $1)""",
        repo.MAX_IMAGE_ATTEMPTS - 1,
    )
    claimed = await repo.claim_pending_images(pg, limit=10)
    assert [c.position for c in claimed] == [1]


async def test_claim_respects_the_limit(pg):
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    for position in range(5):
        await pg.execute(
            """INSERT INTO article_images (article_id, position, source_url, status)
               VALUES (1, $1, 'https://x.example/a.png', 'pending')""",
            position,
        )
    assert len(await repo.claim_pending_images(pg, limit=2)) == 2


async def test_record_result_sets_status_and_bumps_attempts(pg):
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status)
           VALUES (1, 0, 'https://x.example/a.png', 'pending')"""
    )
    await repo.record_image_result(pg, 1, 0, "error")
    row = await pg.fetchrow("SELECT status, attempts FROM article_images WHERE article_id = 1")
    assert row["status"] == "error"
    assert row["attempts"] == 1

    await repo.record_image_result(pg, 1, 0, "error")
    row = await pg.fetchrow("SELECT status, attempts FROM article_images WHERE article_id = 1")
    assert row["attempts"] == 2


async def test_an_errored_row_is_reclaimed_below_the_ceiling(pg):
    # 'error' means we could not tell whether the image is there. Unlike 'dead',
    # it must come back around until the attempt ceiling is reached.
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status)
           VALUES (1, 0, 'https://x.example/a.png', 'pending')"""
    )
    await repo.record_image_result(pg, 1, 0, "error")
    assert [c.position for c in await repo.claim_pending_images(pg, limit=10)] == [0]


async def test_the_queue_can_actually_use_the_partial_index(pg):
    # article_images_queue_idx's predicate and this query's status list must stay
    # textually identical: Postgres proves a partial index applicable only from a
    # Const. Disabling seqscan makes the planner take the index if -- and only if
    # -- it can prove it applies, so this tests provability, not cost preference.
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status)
           VALUES (1, 0, 'https://x.example/a.png', 'pending')"""
    )
    await pg.execute("SET enable_seqscan = off")
    plan = "\n".join(
        r["QUERY PLAN"]
        for r in await pg.fetch(
            """EXPLAIN SELECT article_id, position, source_url, attempts FROM article_images
               WHERE status IN ('pending', 'error') AND attempts < 5
               ORDER BY article_id DESC, position LIMIT 50"""
        )
    )
    # Control: a predicate the index does not cover must NOT reach it, or the
    # assertion below would pass for any query at all.
    wider = "\n".join(
        r["QUERY PLAN"]
        for r in await pg.fetch(
            """EXPLAIN SELECT article_id FROM article_images
               WHERE status IN ('pending', 'error', 'ok')
               ORDER BY article_id DESC LIMIT 50"""
        )
    )
    await pg.execute("SET enable_seqscan = on")
    assert "article_images_queue_idx" in plan, plan
    assert "article_images_queue_idx" not in wider, wider


async def test_a_dead_row_is_never_reclaimed(pg):
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status)
           VALUES (1, 0, 'https://x.example/a.png', 'pending')"""
    )
    await repo.record_image_result(pg, 1, 0, "dead")
    assert await repo.claim_pending_images(pg, limit=10) == []
