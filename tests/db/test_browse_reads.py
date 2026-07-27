import datetime

from babel.db import browse
from babel.db.browse import (
    archive_stats,
    fetch_log_status,
    get_article,
    get_blob,
    get_comments,
    image_status_counts,
    list_countries,
    suggest_authors,
)

UTC = datetime.UTC


async def _article(conn, article_id, *, country="Poland", author="ann", hidden=False):
    await conn.execute(
        """INSERT INTO articles (id, title, body, author_name, country,
                                 published_at, e_day, comment_count, hidden_at)
           VALUES ($1, 'Title', 'Body text', $2, $3,
                   timestamptz '2026-01-05 12:00Z', 6800, 2, $4)""",
        article_id, author, country, datetime.datetime.now(UTC) if hidden else None,
    )


async def test_get_article_returns_none_for_a_hidden_row(pool):
    async with pool.acquire() as conn:
        await _article(conn, 900, hidden=True)
        assert await get_article(conn, 900) is None


async def test_get_article_returns_the_row(pool):
    async with pool.acquire() as conn:
        await _article(conn, 901)
        detail = await get_article(conn, 901)
    assert detail is not None
    assert detail.title == "Title"
    assert detail.e_day == 6800


async def test_comments_come_back_in_thread_order(pool):
    async with pool.acquire() as conn:
        await _article(conn, 902)
        await conn.executemany(
            """INSERT INTO comments (id, article_id, position, depth, author_name, body)
               VALUES ($1, 902, $2, $3, 'bob', $4)""",
            [(11, 1, 0, "first"), (12, 2, 1, "reply"), (13, 3, 0, None)],
        )
        rows = await get_comments(conn, 902)
    assert [r.position for r in rows] == [1, 2, 3]
    assert rows[1].depth == 1
    assert rows[2].body is None


async def test_image_counts_separate_dead_from_not_yet_captured(pool):
    async with pool.acquire() as conn:
        await _article(conn, 903)
        await conn.executemany(
            """INSERT INTO article_images (article_id, position, source_url, status, attempts)
               VALUES (903, $1, $2, $3, $4)""",
            [
                (1, "https://h/1.png", "ok", 1),
                (2, "https://h/2.png", "dead", 1),
                (3, "https://h/3.png", "pending", 0),
                (4, "https://h/4.png", "error", 2),
                (5, "https://h/5.png", "error", 5),
            ],
        )
        counts = await image_status_counts(conn, 903)
    assert counts["ok"] == 1
    assert counts["dead"] == 1
    assert counts["waiting"] == 2       # pending + error below the attempt ceiling
    assert counts["exhausted"] == 1     # error at the ceiling


async def test_image_map_entries_come_back_in_position_order(pool):
    async with pool.acquire() as conn:
        await _article(conn, 904)
        await conn.execute(
            "INSERT INTO images (sha256, mime, bytes) VALUES ($1,'image/png',1), ($2,'image/png',1)",
            b"\x01" * 32, b"\x02" * 32,
        )
        await conn.executemany(
            """INSERT INTO article_images (article_id, position, source_url, status, sha256)
               VALUES (904, $1, $2, 'ok', $3)""",
            [(2, "https://h/b.png", b"\x02" * 32), (1, "https://h/a.png", b"\x01" * 32)],
        )
        mapping = await browse.get_image_map(conn, 904)
    assert list(mapping) == ["https://h/a.png", "https://h/b.png"]
    assert mapping["https://h/a.png"].sha256 == b"\x01" * 32
    assert mapping["https://h/b.png"].sha256 == b"\x02" * 32


async def test_withheld_blob_is_reported(pool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO images (sha256, mime, bytes, withheld_at) VALUES ($1,'image/png',1,now())",
            b"\x03" * 32,
        )
        row = await get_blob(conn, b"\x03" * 32)
    assert row is not None
    assert row.withheld_at is not None


async def test_countries_are_distinct_and_sorted(pool):
    async with pool.acquire() as conn:
        await _article(conn, 905, country="Serbia")
        await _article(conn, 906, country="Poland")
        await _article(conn, 907, country="Poland")
        countries = await list_countries(conn)
    assert "Poland" in countries
    assert "Serbia" in countries
    assert len(countries) == len(set(countries))
    assert list(countries) == sorted(countries)


async def test_author_suggestions_are_prefix_matched_and_bounded(pool):
    async with pool.acquire() as conn:
        for i in range(20):
            await _article(conn, 1000 + i, author=f"annabel{i}")
        await _article(conn, 1100, author="bertram")
        names = await suggest_authors(conn, "anna", limit=8)
    assert len(names) == 8
    assert all(n.lower().startswith("anna") for n in names)
    assert "bertram" not in names


async def test_author_suggestions_treat_percent_as_a_literal(pool):
    # No LIKE on the request path, so wildcards are ordinary characters.
    async with pool.acquire() as conn:
        await _article(conn, 1200, author="plain")
        assert await suggest_authors(conn, "%", limit=8) == ()


async def test_archive_stats_reports_span_and_frontier(pool):
    async with pool.acquire() as conn:
        await _article(conn, 1300)
        await conn.execute(
            "INSERT INTO crawl_cursor (name, next_id) VALUES ('backfill', 2785850) "
            "ON CONFLICT (name) DO UPDATE SET next_id = 2785850"
        )
        stats = await archive_stats(conn)
    assert stats.articles >= 1
    assert stats.oldest is not None
    assert stats.newest is not None
    assert stats.frontier == 2785850


async def test_fetch_log_status_is_readable(pool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO fetch_log (article_id, status) VALUES (1400, 'missing') "
            "ON CONFLICT (article_id) DO UPDATE SET status = 'missing'"
        )
        assert await fetch_log_status(conn, 1400) == "missing"
        assert await fetch_log_status(conn, 1401) is None


# --- Fix round 1: a hidden article's comments, images and counts must not leak ---


async def test_get_comments_returns_nothing_for_a_hidden_article(pool):
    async with pool.acquire() as conn:
        await _article(conn, 910, hidden=True)
        await conn.execute(
            """INSERT INTO comments (id, article_id, position, depth, author_name, body)
               VALUES (20, 910, 1, 0, 'bob', 'hello')"""
        )
        assert await get_comments(conn, 910) == ()


async def test_get_image_map_returns_nothing_for_a_hidden_article(pool):
    async with pool.acquire() as conn:
        await _article(conn, 911, hidden=True)
        await conn.execute(
            "INSERT INTO images (sha256, mime, bytes) VALUES ($1,'image/png',1)",
            b"\x04" * 32,
        )
        await conn.execute(
            """INSERT INTO article_images (article_id, position, source_url, status, sha256)
               VALUES (911, 1, 'https://h/c.png', 'ok', $1)""",
            b"\x04" * 32,
        )
        assert await browse.get_image_map(conn, 911) == {}


async def test_image_status_counts_are_zero_for_a_hidden_article(pool):
    async with pool.acquire() as conn:
        await _article(conn, 912, hidden=True)
        await conn.execute(
            """INSERT INTO article_images (article_id, position, source_url, status, attempts)
               VALUES (912, 1, 'https://h/d.png', 'ok', 1)"""
        )
        counts = await image_status_counts(conn, 912)
    assert counts == {"ok": 0, "dead": 0, "waiting": 0, "exhausted": 0}


async def test_get_blob_is_unaffected_by_a_citing_articles_hidden_at(pool):
    """images.withheld_at is the only takedown signal get_blob honours.

    A blob is content-addressed and may be cited by many articles; hiding one
    citing article must not silently withhold a blob other, unhidden articles
    still legitimately display. Whether to withhold a shared blob is a
    separate, deliberate operator decision (see article_images_sha256_idx's
    "which other articles cite this blob"), never an automatic side effect of
    hiding one citing article.
    """
    async with pool.acquire() as conn:
        await _article(conn, 913, hidden=True)
        await conn.execute(
            "INSERT INTO images (sha256, mime, bytes) VALUES ($1,'image/png',1)",
            b"\x05" * 32,
        )
        await conn.execute(
            """INSERT INTO article_images (article_id, position, source_url, status, sha256)
               VALUES (913, 1, 'https://h/e.png', 'ok', $1)""",
            b"\x05" * 32,
        )
        row = await get_blob(conn, b"\x05" * 32)
    assert row is not None
    assert row.withheld_at is None


# --- Fix round 1: suggest_authors must not crash on unencodable input ---


async def test_author_suggestions_reject_a_prefix_ending_in_the_max_code_point(pool):
    async with pool.acquire() as conn:
        await _article(conn, 1500, author="ann")
        assert await suggest_authors(conn, "a" + chr(0x10FFFF), limit=8) == ()


async def test_author_suggestions_reject_a_lone_surrogate(pool):
    async with pool.acquire() as conn:
        await _article(conn, 1501, author="ann")
        assert await suggest_authors(conn, "a\ud800", limit=8) == ()


async def test_author_suggestions_handle_a_very_long_prefix_without_crashing(pool):
    async with pool.acquire() as conn:
        await _article(conn, 1502, author="ann")
        assert await suggest_authors(conn, "a" * 100_000, limit=8) == ()


async def test_author_suggestions_handle_an_unpaired_combining_character(pool):
    async with pool.acquire() as conn:
        await _article(conn, 1503, author="ann")
        assert await suggest_authors(conn, "́", limit=8) == ()


# --- Task 6: the raw markup and the image map the renderer needs ---


async def test_get_article_returns_the_raw_markup(pg):
    await pg.execute(
        """INSERT INTO articles (id, title, body, body_raw, published_at, comment_count)
           VALUES (7001, 't', 'text', '<p>A<br><br>B</p>', now(), 0)"""
    )
    detail = await browse.get_article(pg, 7001)
    assert detail.body_raw == "<p>A<br><br>B</p>"


async def test_get_comments_returns_the_raw_markup(pg):
    await pg.execute(
        """INSERT INTO articles (id, title, body, published_at, comment_count)
           VALUES (7002, 't', 'text', now(), 1)"""
    )
    await pg.execute(
        """INSERT INTO comments (id, article_id, position, depth, body, body_raw)
           VALUES (81, 7002, 0, 0, 'hi', '<p>hi<br>there</p>')"""
    )
    assert (await browse.get_comments(pg, 7002))[0].body_raw == "<p>hi<br>there</p>"


async def test_image_map_buckets_match_the_counts_the_note_shows(pg):
    await pg.execute(
        """INSERT INTO articles (id, title, body, published_at, comment_count)
           VALUES (7003, 't', 'text', now(), 0)"""
    )
    digest = bytes.fromhex("cd" * 32)
    await pg.execute("INSERT INTO images (sha256, bytes, mime) VALUES ($1, 3, 'image/png')",
                     digest)
    await pg.executemany(
        """INSERT INTO article_images (article_id, position, source_url, status,
                                       attempts, sha256)
           VALUES (7003, $1, $2, $3, $4, $5)""",
        [
            (0, "https://h/ok.png", "ok", 1, digest),
            (1, "https://h/dead.png", "dead", 1, None),
            (2, "https://h/wait.png", "pending", 0, None),
            (3, "https://h/gone.png", "error", 5, None),
        ],
    )
    mapping = await browse.get_image_map(pg, 7003)
    assert [s.state for s in mapping.values()] == ["ok", "dead", "waiting", "exhausted"]
    assert list(mapping) == ["https://h/ok.png", "https://h/dead.png",
                             "https://h/wait.png", "https://h/gone.png"]
    assert mapping["https://h/ok.png"].sha256 == digest


async def test_a_withheld_blob_reports_withheld_not_ok(pg):
    await pg.execute(
        """INSERT INTO articles (id, title, body, published_at, comment_count)
           VALUES (7004, 't', 'text', now(), 0)"""
    )
    digest = bytes.fromhex("ef" * 32)
    await pg.execute(
        """INSERT INTO images (sha256, bytes, mime, withheld_at)
           VALUES ($1, 3, 'image/png', now())""", digest,
    )
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status, sha256)
           VALUES (7004, 0, 'https://h/x.png', 'ok', $1)""", digest,
    )
    mapping = await browse.get_image_map(pg, 7004)
    assert mapping["https://h/x.png"].state == "withheld"


async def test_a_hidden_article_yields_an_empty_image_map(pg):
    await pg.execute(
        """INSERT INTO articles (id, title, body, published_at, comment_count, hidden_at)
           VALUES (7005, 't', 'text', now(), 0, now())"""
    )
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status)
           VALUES (7005, 0, 'https://h/x.png', 'pending')"""
    )
    assert await browse.get_image_map(pg, 7005) == {}
