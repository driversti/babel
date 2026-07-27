import datetime

from babel.db.browse import Cursor, ListFilters, build_list_query

UTC = datetime.UTC
CURSOR = Cursor(datetime.datetime(2026, 1, 5, 12, 0, tzinfo=UTC), 500)


async def _plan(conn, sql: str, params: list[object]) -> str:
    rows = await conn.fetch(f"EXPLAIN {sql}", *params)
    return "\n".join(r[0] for r in rows)


def _index_conds(plan: str) -> str:
    return "\n".join(line for line in plan.splitlines() if "Index Cond:" in line)


def _filters(plan: str) -> str:
    return "\n".join(line for line in plan.splitlines() if "Filter:" in line)


async def _populated(pool):
    """Enough rows, and ANALYZEd, so the planner is not choosing off a zero-page
    estimate. On an empty table every plan costs about the same and the choice
    carries no information."""
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO articles (id, title, body, author_name, country,
                                     published_at, comment_count)
               SELECT g,
                      'T' || g,
                      'body',
                      'author' || (g % 500),
                      (ARRAY['Poland','Serbia','Hungary'])[1 + g % 3],
                      timestamptz '2026-01-01' + (g || ' seconds')::interval,
                      0
                 FROM generate_series(1, 20000) g
               ON CONFLICT (id) DO NOTHING"""
        )
        await conn.execute("ANALYZE articles")
    return pool


async def test_cursor_is_an_index_condition_not_a_filter(pool):
    await _populated(pool)
    sql, params = build_list_query(ListFilters(), CURSOR, descending=True, limit=50)
    async with pool.acquire() as conn:
        plan = await _plan(conn, sql, params)
    assert "articles_list_idx" in plan
    assert "published_at" in _index_conds(plan)
    assert "published_at" not in _filters(plan)


async def test_country_is_an_index_condition(pool):
    await _populated(pool)
    sql, params = build_list_query(ListFilters(country="Poland"), CURSOR, descending=True, limit=50)
    async with pool.acquire() as conn:
        plan = await _plan(conn, sql, params)
    assert "country" in _index_conds(plan)
    assert "country" not in _filters(plan)


async def test_author_is_an_index_condition(pool):
    await _populated(pool)
    sql, params = build_list_query(ListFilters(author="author7"), CURSOR, descending=True, limit=50)
    async with pool.acquire() as conn:
        plan = await _plan(conn, sql, params)
    assert "lower(author_name)" in _index_conds(plan) or "lower((author_name" in _index_conds(plan)
    assert "author_name" not in _filters(plan)


async def test_both_filters_use_the_composite_index(pool):
    await _populated(pool)
    sql, params = build_list_query(
        ListFilters(country="Poland", author="author7"), CURSOR, descending=True, limit=50
    )
    async with pool.acquire() as conn:
        plan = await _plan(conn, sql, params)
    assert "articles_country_author_list_idx" in plan
    conds = _index_conds(plan)
    assert "country" in conds
    assert "author_name" in conds
    # Not `_filters(plan) == ""`: hidden_at IS NULL is part of every one of
    # these queries and no browse index covers hidden_at, so it always shows
    # up as a Filter regardless of whether country/author land correctly in
    # Index Cond. Assert on those two predicates specifically, the same way
    # the three tests above do.
    filters = _filters(plan)
    assert "country" not in filters
    assert "author_name" not in filters


async def test_negative_control_the_is_null_or_form_degrades(pool):
    """The shape build_list_query exists to avoid.

    It still names an index in its plan — which is why asserting on the index
    name proves nothing — but the predicate lands in Filter: rather than
    Index Cond: once Postgres has to plan it *generically* (parameter value
    unknown), which is exactly the situation the module docstring warns
    about: "under a generic plan that form degrades to a scan".

    A single EXPLAIN issued the way the four tests above do it — conn.fetch
    with the parameter already bound — does not exhibit this: asyncpg's bind
    protocol hands Postgres the actual value up front, so it plans as if
    `$1::text IS NULL` were a known-false Const and folds the whole OR down
    to a plain `country = 'Poland'`, indistinguishable from the good form.
    That was verified directly: swapping this test's execution back to
    `conn.fetch(f"EXPLAIN {sql}", ["Poland"])` makes it use
    articles_country_list_idx with country correctly in Index Cond — i.e. it
    stops discriminating at all. Reaching the degraded plan this test exists
    to name requires forcing a plan Postgres cannot specialise for any one
    value: a SQL-level PREPARE, `plan_cache_mode = force_generic_plan`, and
    an EXPLAIN EXECUTE with the value spelled out as a literal rather than
    bound as a parameter.
    """
    await _populated(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            """PREPARE degraded_form(text) AS
               SELECT id FROM articles
               WHERE hidden_at IS NULL AND ($1::text IS NULL OR country = $1::text)
               ORDER BY published_at DESC, id DESC
               LIMIT 50"""
        )
        await conn.execute("SET plan_cache_mode = force_generic_plan")
        rows = await conn.fetch("EXPLAIN EXECUTE degraded_form('Poland')")
        plan = "\n".join(r[0] for r in rows)
    assert "country" in _filters(plan)
    assert "country" not in _index_conds(plan)
