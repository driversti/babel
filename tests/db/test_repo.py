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
    """A re-parse must not undo a 'dead' verdict on the same image.

    `save_article`'s ON CONFLICT clause on `article_images` deliberately omits
    `status` from the DO UPDATE SET list, so a row a worker already marked 'dead'
    (with sha256 cleared) stays 'dead' when the article is saved again. Once we
    know a link is dead, a later re-save has no way to tell whether it came back,
    and overwriting the verdict to 'pending' would throw that knowledge away.

    This is safe only because the row is keyed on `source_url`. The earlier
    version of this test re-saved the article with a DIFFERENT url at the same
    position and asserted the 'dead' verdict carried over — which was not a
    guarantee worth having but I8 itself, written down as the contract: a URL
    that had never been fetched, wearing another image's verdict. A different URL
    is a different image and now gets its own row, fresh.
    """
    article = make_article()
    await repo.save_article(pg, article)
    await repo.record_image(pg, article.id, 0, article.images[0].source_url, "dead", sha256=None)

    row = await pg.fetchrow(
        "SELECT status, sha256 FROM article_images WHERE article_id = $1 AND source_url = $2",
        article.id, article.images[0].source_url,
    )
    assert row["status"] == "dead"
    assert row["sha256"] is None

    # Re-saving the same article, unchanged, must not disturb the verdict.
    await repo.save_article(pg, make_article(title="Corrected"))
    assert await pg.fetchval(
        "SELECT status FROM article_images WHERE article_id = $1 AND source_url = $2",
        article.id, article.images[0].source_url,
    ) == "dead", "status must survive a re-save, or a dead link looks pending again"

    # A different URL is a different image: its own row, its own fresh verdict.
    await repo.save_article(
        pg, make_article(images=(ImageRef(position=0, source_url="https://x.example/b.png"),))
    )
    assert await pg.fetchval(
        "SELECT status FROM article_images WHERE article_id = $1 AND source_url = $2",
        article.id, "https://x.example/b.png",
    ) == "pending"


async def test_duplicate_comment_ids_in_one_save_are_silently_deduped(pg):
    """Documents current behaviour, not a desired guarantee.

    If a parser bug ever produces two `Comment` objects with the same `id` in
    one `Article.comments` tuple, `save_article`'s upsert collapses them to one
    row rather than raising. This test pins that today's behaviour is "one row,
    no exception" -- it does not assert this is the right outcome, and whether a
    duplicate should instead surface as a parser-level error is still open.

    Which copy survives changed when the insert became `ON CONFLICT DO UPDATE`
    (so a re-parse can correct a comment body) instead of `DO NOTHING`: last
    write wins now, first did before. Neither is meaningfully more correct for
    an input that should not exist, so the assertion below follows the mechanism
    rather than claiming a guarantee.
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
    assert rows[0]["author_name"] == "other"
    assert rows[0]["body"] == "second copy"


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
    claimed = await repo.claim_pending_images(pg, limit=10, cooldown_sec=0)
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
               VALUES (1, $1, $2, $3)""",
            position, f"https://x.example/{position}.png", status,
        )
    claimed = await repo.claim_pending_images(pg, limit=10, cooldown_sec=0)
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
    claimed = await repo.claim_pending_images(pg, limit=10, cooldown_sec=0)
    assert [c.position for c in claimed] == [1]


async def test_claim_respects_the_limit(pg):
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    for position in range(5):
        await pg.execute(
            """INSERT INTO article_images (article_id, position, source_url, status)
               VALUES (1, $1, $2, 'pending')""",
            position, f"https://x.example/{position}.png",
        )
    assert len(await repo.claim_pending_images(pg, limit=2, cooldown_sec=0)) == 2


async def test_record_result_sets_status_and_bumps_attempts(pg):
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status)
           VALUES (1, 0, 'https://x.example/a.png', 'pending')"""
    )
    await repo.record_image_result(pg, 1, "https://x.example/a.png", "error")
    row = await pg.fetchrow("SELECT status, attempts FROM article_images WHERE article_id = 1")
    assert row["status"] == "error"
    assert row["attempts"] == 1

    await repo.record_image_result(pg, 1, "https://x.example/a.png", "error")
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
    await repo.record_image_result(pg, 1, "https://x.example/a.png", "error")
    assert [c.position for c in await repo.claim_pending_images(pg, limit=10, cooldown_sec=0)] == [0]


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
    await repo.record_image_result(pg, 1, "https://x.example/a.png", "dead")
    assert await repo.claim_pending_images(pg, limit=10, cooldown_sec=0) == []


async def test_a_just_failed_image_waits_out_the_cooldown(pg):
    """`claim_retryable` guards the article side against a host having a bad minute.
    The image side had no such guard, on the column that was already being written:
    `record_image_result` maintains `checked_at` on every attempt, and nothing read it."""
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status)
           VALUES (1, 0, 'https://x.example/a.png', 'pending')"""
    )
    await repo.record_image_result(pg, 1, "https://x.example/a.png", "error")

    assert await repo.claim_pending_images(pg, limit=10, cooldown_sec=3600) == []
    assert [c.position for c in await repo.claim_pending_images(pg, limit=10, cooldown_sec=0)] == [0]


async def test_a_never_attempted_image_does_not_wait(pg):
    """A cooldown is for something that just failed. A fresh row must go straight out,
    or every image would sit idle for an hour after ingest enqueued it."""
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status)
           VALUES (1, 0, 'https://x.example/a.png', 'pending')"""
    )
    claimed = await repo.claim_pending_images(pg, limit=10, cooldown_sec=86400)
    assert [c.position for c in claimed] == [0]


async def test_a_parse_that_lost_the_comments_cannot_empty_the_thread(pg):
    """The archive's own recovery command was its worst threat.

    `save_article` deleted every comment unconditionally and inserted only if the
    parse produced some. eRepublik renames `commentWrapper`; articles still parse
    (the body gate is `postBody`), so the walk keeps writing 'ok' rows with empty
    threads. The operator fixes the article side, misses the comment selector, and
    runs the documented `babel refetch --from --to` over 500k ids — each save then
    deletes comments that WERE collected correctly and inserts nothing. Comments
    are the majority of the archive's text (2054 vs 1357 chars per SPEC.md), and
    the raw-HTML window was dropped precisely because refetch exists.
    """
    await repo.save_article(pg, make_article())
    assert await pg.fetchval("SELECT count(*) FROM comments WHERE article_id = 1") == 2

    # comment_count still says 2; the parse found none. That is a parser fault,
    # not an article whose comments were all deleted upstream.
    blinded = make_article(comments=(), comment_count=2)
    try:
        await repo.save_article(pg, blinded)
    except repo.CommentsVanishedError:
        pass
    else:
        raise AssertionError("expected save_article to refuse a comment_count/comments mismatch")

    assert await pg.fetchval("SELECT count(*) FROM comments WHERE article_id = 1") == 2, (
        "the archived thread must survive a parse that could not see it"
    )


async def test_an_article_that_genuinely_has_no_comments_saves_fine(pg):
    """Most articles have none. Refusing those would stop the crawl dead."""
    await repo.save_article(pg, make_article(comments=(), comment_count=0))
    assert await pg.fetchval("SELECT count(*) FROM articles WHERE id = 1") == 1
    assert await pg.fetchval("SELECT count(*) FROM comments WHERE article_id = 1") == 0


async def test_comments_deleted_upstream_are_kept_not_pruned(pg):
    """A shrinking thread is a real thing — moderators delete comments. But the
    archive exists to hold what the game no longer will, so a re-fetch that sees
    fewer comments must not discard the ones it can no longer see."""
    await repo.save_article(pg, make_article())
    shrunk = make_article(
        comments=(Comment(1, 0, 0, 42, "someone", None, "first"),), comment_count=1
    )
    await repo.save_article(pg, shrunk)
    assert await pg.fetchval("SELECT count(*) FROM comments WHERE article_id = 1") == 2, (
        "comment 2 no longer appears upstream; that is exactly why it is archived"
    )


async def test_a_shifted_position_cannot_inherit_another_images_verdict(pg):
    """I8. Positions are ordinal within the body, so inserting one image at the
    top shifts every later slot by one.

    While the row was keyed on (article_id, position), that shift handed each
    slot a new URL welded to the previous image's status and sha256 — 'ok' with
    the hash of a different image, for a URL that was never fetched. Nothing can
    detect it afterwards: the queue excludes 'ok' and 'dead', and `articles.body`
    is markup-stripped, so source_url is the only record of the URLs there were.
    """
    first = make_article(images=(ImageRef(position=0, source_url="https://x.example/a.png"),))
    await repo.save_article(pg, first)
    await repo.save_image_blob(pg, b"\x02" * 32, "image/png", 10)
    await repo.record_image_result(pg, first.id, "https://x.example/a.png", "ok", b"\x02" * 32)

    # The author adds a banner above the existing image; a.png is now position 1.
    shifted = make_article(
        images=(
            ImageRef(position=0, source_url="https://x.example/banner.png"),
            ImageRef(position=1, source_url="https://x.example/a.png"),
        )
    )
    await repo.save_article(pg, shifted)

    rows = {
        r["source_url"]: r
        for r in await pg.fetch(
            "SELECT source_url, status, sha256 FROM article_images WHERE article_id = $1", first.id
        )
    }
    assert rows["https://x.example/banner.png"]["status"] == "pending", (
        "a URL never fetched must not inherit a verdict"
    )
    assert rows["https://x.example/banner.png"]["sha256"] is None, (
        "and must certainly not inherit another image's bytes"
    )
    assert rows["https://x.example/a.png"]["status"] == "ok", "the captured image keeps its blob"
    assert rows["https://x.example/a.png"]["sha256"] == b"\x02" * 32


async def test_one_image_used_many_times_in_an_article_is_fetched_once(pg):
    """Newspaper-style articles repeat a divider between sections. Keyed on
    position that was one queue row and one fetch per occurrence; measured at
    12,720 redundant rows, 43% of the live queue."""
    divider = "https://x.example/divider.png"
    article = make_article(
        images=tuple(ImageRef(position=i, source_url=divider) for i in range(7))
    )
    await repo.save_article(pg, article)
    assert await pg.fetchval(
        "SELECT count(*) FROM article_images WHERE article_id = $1", article.id
    ) == 1


async def test_an_image_the_author_removed_keeps_its_captured_bytes(pg):
    """Same rule as comments: the archive holds what the source no longer does."""
    article = make_article(images=(ImageRef(position=0, source_url="https://x.example/gone.png"),))
    await repo.save_article(pg, article)
    await repo.save_image_blob(pg, b"\x03" * 32, "image/png", 10)
    await repo.record_image_result(pg, article.id, "https://x.example/gone.png", "ok", b"\x03" * 32)

    await repo.save_article(
        pg, make_article(images=(ImageRef(position=0, source_url="https://x.example/new.png"),))
    )

    row = await pg.fetchrow(
        "SELECT status, sha256 FROM article_images WHERE article_id = $1 AND source_url = $2",
        article.id, "https://x.example/gone.png",
    )
    assert row is not None, "the row must survive, or the stored blob is orphaned"
    assert row["sha256"] == b"\x03" * 32


async def test_claim_can_leave_a_whole_host_out(pg):
    """Skipping inside the worker would not be enough. The drain is ordered
    newest-article-first and a bad host clusters at the head, so the same rows
    would come back every cycle and the worker would spin on them — or sleep,
    while thousands of other hosts' images waited behind. The claim has to look
    past the host entirely."""
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    for position, url in enumerate([
        "https://bad.example/1.png",
        "https://bad.example/2.png",
        "https://good.example/1.png",
    ]):
        await pg.execute(
            """INSERT INTO article_images (article_id, position, source_url, status)
               VALUES (1, $1, $2, 'pending')""",
            position, url,
        )

    claimed = await repo.claim_pending_images(
        pg, limit=10, cooldown_sec=0, excluded_hosts=["bad.example"]
    )
    assert [c.source_url for c in claimed] == ["https://good.example/1.png"]


async def test_excluding_nothing_claims_everything(pg):
    """The common case: no host is in trouble, and the filter must be a no-op."""
    await pg.execute("INSERT INTO articles (id, title, body, published_at) VALUES (1,'t','b',now())")
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status)
           VALUES (1, 0, 'https://a.example/1.png', 'pending')"""
    )
    assert len(await repo.claim_pending_images(pg, limit=10, cooldown_sec=0, excluded_hosts=[])) == 1


async def test_save_article_round_trips_the_raw_markup(pg):
    article = make_article(
        article_id=9101, title="T", body="text only",
        body_raw='<div class="postBody"><p>A<br><br><b>B</b></p></div>',
        author_id=None, author_name="ann", country="Poland",
        published_at=datetime.datetime(2026, 7, 21, 5, 53, tzinfo=datetime.UTC),
        e_day=6817, comment_count=1,
        comments=(Comment(id=55, position=0, depth=0, author_id=None,
                          author_name="bob", posted_at=None,
                          body="hi", body_raw="<p>hi<br>there</p>"),),
    )
    await repo.save_article(pg, article)

    row = await pg.fetchrow("SELECT body, body_raw FROM articles WHERE id = 9101")
    assert row["body"] == "text only"
    assert row["body_raw"] == '<div class="postBody"><p>A<br><br><b>B</b></p></div>'
    comment = await pg.fetchrow("SELECT body_raw FROM comments WHERE id = 55")
    assert comment["body_raw"] == "<p>hi<br>there</p>"


async def test_a_refetch_replaces_the_raw_markup(pg):
    """The update branch is a separate SQL path and has been wrong before."""
    def build(raw):
        return make_article(
            article_id=9102, title="T", body="text", body_raw=raw,
            author_id=None, author_name="ann", country="Poland",
            published_at=datetime.datetime(2026, 7, 21, 5, 53, tzinfo=datetime.UTC),
            e_day=6817, comment_count=1,
            comments=(Comment(id=56, position=0, depth=0, author_id=None,
                              author_name="bob", posted_at=None,
                              body="hi", body_raw=raw),),
        )

    await repo.save_article(pg, build("<p>first</p>"))
    await repo.save_article(pg, build("<p>second</p>"))

    assert await pg.fetchval("SELECT body_raw FROM articles WHERE id = 9102") == "<p>second</p>"
    assert await pg.fetchval("SELECT body_raw FROM comments WHERE id = 56") == "<p>second</p>"
