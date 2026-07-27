"""Every read-path SQL statement in the project.

Split from repo.py, which holds the write path. The two have opposite
requirements: repo.py's statements are fixed text with bound parameters, while
the list query's text varies with which filters are active — and it varies
deliberately. `($1 IS NULL OR country = $1)` is one statement instead of four,
and it is the wrong trade: Postgres proves a composite index applicable only
from a Const, so under a generic plan that form degrades to a scan over a table
heading for 2.8M rows. repo.py already carries two comments about the same
mechanism biting this project. Four combinations, four texts.
"""

import datetime
from dataclasses import dataclass

import asyncpg

PAGE_SIZE = 50


@dataclass(frozen=True, slots=True)
class ListFilters:
    country: str | None = None
    author: str | None = None


@dataclass(frozen=True, slots=True)
class Cursor:
    published_at: datetime.datetime
    article_id: int


@dataclass(frozen=True, slots=True)
class ArticleRow:
    id: int
    title: str
    author_name: str | None
    country: str | None
    published_at: datetime.datetime
    comment_count: int


@dataclass(frozen=True, slots=True)
class ListPage:
    rows: tuple[ArticleRow, ...]
    has_more: bool


_COLUMNS = "id, title, author_name, country, published_at, comment_count"


def build_list_query(
    filters: ListFilters, cursor: Cursor | None, *, descending: bool, limit: int
) -> tuple[str, list[object]]:
    """The list statement and its parameters, for one filter combination.

    Returned rather than executed so the EXPLAIN tests can plan the query the
    application actually sends. Asserting against a copy of the SQL pasted into a
    test proves only that Postgres can use an index from a Const — which is the
    mistake finding M3 records against the existing suite.
    """
    where: list[str] = ["hidden_at IS NULL"]
    params: list[object] = []

    if filters.country is not None:
        params.append(filters.country)
        where.append(f"country = ${len(params)}")
    if filters.author is not None:
        params.append(filters.author.lower())
        where.append(f"lower(author_name) = ${len(params)}")
    if cursor is not None:
        params.append(cursor.published_at)
        params.append(cursor.article_id)
        comparison = "<" if descending else ">"
        # Row-wise comparison, not `published_at < $n OR (published_at = $n AND
        # id < $m)`: only this form is an Index Cond against
        # (published_at DESC, id DESC). published_at has second granularity and
        # ties are common, so a boundary inside a tie is where a key of
        # published_at alone drops or repeats a row.
        where.append(f"(published_at, id) {comparison} (${len(params) - 1}, ${len(params)})")

    params.append(limit)
    order = "DESC" if descending else "ASC"
    sql = f"""
        SELECT {_COLUMNS}
        FROM articles
        WHERE {" AND ".join(where)}
        ORDER BY published_at {order}, id {order}
        LIMIT ${len(params)}
    """
    return sql, params


async def list_articles(
    conn: asyncpg.Connection,
    filters: ListFilters,
    *,
    order: str,
    cursor: Cursor | None,
    going: str,
    limit: int = PAGE_SIZE,
) -> ListPage:
    """One page of the list, already in display order.

    Two independent flips decide the SQL direction: `order` is what the reader
    asked for (new or old first) and `going` is which way they are paging. Going
    back is the same query run the other way round, so the rows come out
    reversed and are flipped back here rather than in the template.
    """
    forward = going == "next"
    descending = (order == "new") == forward

    sql, params = build_list_query(filters, cursor, descending=descending, limit=limit + 1)
    records = await conn.fetch(sql, *params)

    has_more = len(records) > limit
    rows = [ArticleRow(**dict(r)) for r in records[:limit]]
    if not forward:
        rows.reverse()
    return ListPage(rows=tuple(rows), has_more=has_more)
