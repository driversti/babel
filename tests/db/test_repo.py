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


async def test_image_blob_is_deduplicated_by_hash(pg):
    await repo.save_article(pg, make_article())
    digest = b"\x01" * 32
    await repo.save_image_blob(pg, digest, "image/png", 1234)
    await repo.save_image_blob(pg, digest, "image/png", 1234)
    assert await pg.fetchval("SELECT count(*) FROM images") == 1
    await repo.record_image(pg, 1, 0, "https://x.example/a.png", "ok", digest)
    assert await pg.fetchval("SELECT status FROM article_images WHERE article_id = 1") == "ok"
