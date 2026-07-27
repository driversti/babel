import datetime

from babel.db.browse import (
    archive_stats,
    fetch_log_status,
    get_article,
    get_blob,
    get_comments,
    get_ok_image_digests,
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


async def test_ok_digests_come_back_in_position_order(pool):
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
        digests = await get_ok_image_digests(conn, 904)
    assert digests == (b"\x01" * 32, b"\x02" * 32)


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
