import datetime

from babel.db.browse import (
    Cursor,
    ListFilters,
    build_list_query,
    list_articles,
)

UTC = datetime.UTC


async def _seed(conn, rows):
    """rows: (id, published_at, country, author_name)"""
    await conn.executemany(
        """INSERT INTO articles (id, title, body, author_name, country,
                                 published_at, comment_count)
           VALUES ($1::bigint, 'T' || $1::text, 'body', $2, $3, $4, 0)""",
        [(i, author, country, ts) for i, ts, country, author in rows],
    )


def _ts(day: int, second: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 1, day, 12, 0, second, tzinfo=UTC)


async def test_pages_cover_every_row_exactly_once_with_tied_timestamps(pool):
    # Ten articles sharing ONE timestamp. published_at alone cannot order them,
    # so a page boundary landing inside the tie is exactly where a keyset that
    # forgets `id` drops or repeats a row.
    tied = _ts(5)
    async with pool.acquire() as conn:
        await _seed(conn, [(100 + i, tied, "Poland", "ann") for i in range(10)])

        seen: list[int] = []
        cursor = None
        for _ in range(10):
            page = await list_articles(
                conn, ListFilters(), order="new", cursor=cursor, going="next", limit=3
            )
            seen.extend(r.id for r in page.rows)
            if not page.has_more:
                break
            last = page.rows[-1]
            cursor = Cursor(published_at=last.published_at, article_id=last.id)

    assert sorted(seen) == list(range(100, 110))
    assert len(seen) == len(set(seen))
    assert seen == sorted(seen, reverse=True)


async def test_prev_page_is_symmetric(pool):
    async with pool.acquire() as conn:
        await _seed(conn, [(200 + i, _ts(1 + i), "Poland", "ann") for i in range(6)])

        first = await list_articles(
            conn, ListFilters(), order="new", cursor=None, going="next", limit=2
        )
        second_cursor = Cursor(first.rows[-1].published_at, first.rows[-1].id)
        second = await list_articles(
            conn, ListFilters(), order="new", cursor=second_cursor, going="next", limit=2
        )
        back_cursor = Cursor(second.rows[0].published_at, second.rows[0].id)
        back = await list_articles(
            conn, ListFilters(), order="new", cursor=back_cursor, going="prev", limit=2
        )

    assert [r.id for r in back.rows] == [r.id for r in first.rows]


async def test_order_old_reverses(pool):
    async with pool.acquire() as conn:
        await _seed(conn, [(300 + i, _ts(1 + i), "Poland", "ann") for i in range(4)])
        page = await list_articles(
            conn, ListFilters(), order="old", cursor=None, going="next", limit=10
        )
    assert [r.id for r in page.rows] == [300, 301, 302, 303]


async def test_filters_compose(pool):
    async with pool.acquire() as conn:
        await _seed(
            conn,
            [
                (400, _ts(1), "Poland", "ann"),
                (401, _ts(2), "Poland", "bob"),
                (402, _ts(3), "Serbia", "ann"),
            ],
        )
        page = await list_articles(
            conn,
            ListFilters(country="Poland", author="ann"),
            order="new",
            cursor=None,
            going="next",
            limit=10,
        )
    assert [r.id for r in page.rows] == [400]


async def test_author_filter_ignores_case(pool):
    async with pool.acquire() as conn:
        await _seed(conn, [(500, _ts(1), "Poland", "AnnaBanana")])
        page = await list_articles(
            conn, ListFilters(author="annabanana"), order="new",
            cursor=None, going="next", limit=10,
        )
    assert [r.id for r in page.rows] == [500]


async def test_has_more_is_false_on_the_last_page(pool):
    async with pool.acquire() as conn:
        await _seed(conn, [(600 + i, _ts(1 + i), "Poland", "ann") for i in range(3)])
        page = await list_articles(
            conn, ListFilters(), order="new", cursor=None, going="next", limit=10
        )
    assert page.has_more is False
    assert len(page.rows) == 3


async def test_hidden_articles_never_appear(pool):
    async with pool.acquire() as conn:
        await _seed(conn, [(700, _ts(1), "Poland", "ann"), (701, _ts(2), "Poland", "ann")])
        await conn.execute("UPDATE articles SET hidden_at = now() WHERE id = 700")
        page = await list_articles(
            conn, ListFilters(), order="new", cursor=None, going="next", limit=10
        )
    assert [r.id for r in page.rows] == [701]


def test_builder_never_emits_the_is_null_or_form():
    for filters in (
        ListFilters(),
        ListFilters(country="Poland"),
        ListFilters(author="ann"),
        ListFilters(country="Poland", author="ann"),
    ):
        sql, _ = build_list_query(filters, None, descending=True, limit=50)
        assert "IS NULL OR" not in sql.upper()


def test_builder_emits_a_distinct_text_per_filter_combination():
    texts = {
        build_list_query(f, None, descending=True, limit=50)[0]
        for f in (
            ListFilters(),
            ListFilters(country="Poland"),
            ListFilters(author="ann"),
            ListFilters(country="Poland", author="ann"),
        )
    }
    assert len(texts) == 4
