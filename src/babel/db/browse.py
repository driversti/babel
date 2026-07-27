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

from babel.db.repo import MAX_IMAGE_ATTEMPTS

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


@dataclass(frozen=True, slots=True)
class ArticleDetail:
    id: int
    title: str
    body: str
    body_raw: str | None
    author_name: str | None
    author_id: int | None
    country: str | None
    published_at: datetime.datetime
    e_day: int | None
    comment_count: int


@dataclass(frozen=True, slots=True)
class CommentRow:
    id: int
    position: int
    depth: int
    author_id: int | None
    author_name: str | None
    posted_at: datetime.datetime | None
    body: str | None
    body_raw: str | None


@dataclass(frozen=True, slots=True)
class ArchiveStats:
    articles: int
    oldest: datetime.datetime | None
    newest: datetime.datetime | None
    frontier: int | None


@dataclass(frozen=True, slots=True)
class BlobRow:
    sha256: bytes
    withheld_at: datetime.datetime | None


@dataclass(frozen=True, slots=True)
class ImageState:
    """One image slot as the page needs to render it.

    `state` collapses status, attempts and the blob's tombstone into the single
    fact the renderer acts on, in the same four buckets image_status_counts
    uses plus 'withheld'. Keeping the bucket definition in SQL means the note
    under the article and the placeholder in it cannot drift apart.
    """

    state: str  # ok | dead | waiting | exhausted | withheld
    sha256: bytes | None


async def get_article(conn: asyncpg.Connection, article_id: int) -> ArticleDetail | None:
    row = await conn.fetchrow(
        """SELECT id, title, body, body_raw, author_name, author_id, country,
                  published_at, e_day, comment_count
             FROM articles
            WHERE id = $1 AND hidden_at IS NULL""",
        article_id,
    )
    return ArticleDetail(**dict(row)) if row else None


async def get_comments(conn: asyncpg.Connection, article_id: int) -> tuple[CommentRow, ...]:
    # EXISTS, not a JOIN: the check is against the fixed $1 (not a per-row
    # column of comments), so Postgres hoists it into a single InitPlan
    # evaluated once, rather than adding a second table's columns to a plan
    # that otherwise selects only from comments. A hidden article must yield
    # no comments — hidden_at is the takedown mechanism, and comments have no
    # tombstone of their own to check.
    rows = await conn.fetch(
        """SELECT id, position, depth, author_id, author_name, posted_at, body, body_raw
             FROM comments
            WHERE article_id = $1
              AND EXISTS (SELECT 1 FROM articles WHERE id = $1 AND hidden_at IS NULL)
            ORDER BY position""",
        article_id,
    )
    return tuple(CommentRow(**dict(r)) for r in rows)


async def get_image_map(conn: asyncpg.Connection, article_id: int) -> dict[str, ImageState]:
    """Every image slot of an article, keyed by the URL the article cited.

    Keyed on source_url because that is what the renderer has in hand: the
    `src` in body_raw is the same string `_parse_images` recorded at ingest, and
    migration 004 already made (article_id, source_url) the primary key.

    The buckets mirror image_status_counts exactly, plus 'withheld' for a blob
    `babel hide --image` has taken down — which must not be served and must not
    be linked round.

    The same EXISTS-on-$1 check as get_comments: a hidden article's images must
    not surface here even though each row's own status is fine, because the
    article-level tombstone has to dominate.
    """
    rows = await conn.fetch(
        """SELECT ai.source_url,
                  ai.sha256,
                  CASE
                    WHEN i.withheld_at IS NOT NULL                  THEN 'withheld'
                    WHEN ai.status = 'ok' AND ai.sha256 IS NOT NULL THEN 'ok'
                    WHEN ai.status = 'dead'                         THEN 'dead'
                    WHEN ai.status = 'error' AND ai.attempts >= $2  THEN 'exhausted'
                    ELSE 'waiting'
                  END AS state
             FROM article_images ai
             LEFT JOIN images i ON i.sha256 = ai.sha256
            WHERE ai.article_id = $1
              AND EXISTS (SELECT 1 FROM articles WHERE id = $1 AND hidden_at IS NULL)
            ORDER BY ai.position""",
        article_id, MAX_IMAGE_ATTEMPTS,
    )
    return {r["source_url"]: ImageState(state=r["state"], sha256=r["sha256"]) for r in rows}


async def image_status_counts(conn: asyncpg.Connection, article_id: int) -> dict[str, int]:
    """Four buckets, because 'not captured yet' is not 'gone'.

    The drain runs slower than the walk produces work, so `pending` is the normal
    state for a recently crawled article, not an edge case. Counting
    `total - ok` would render "6 of 6 images were already gone when we looked"
    above an empty gallery on the newest articles — the inverse of the truth, on
    the project's central claim about itself.

    The same EXISTS-on-$1 check as get_comments and get_image_map: a hidden
    article reports every bucket as zero rather than the queue's real counts,
    because "how many images does this article have" is itself something a
    takedown must stop answering.
    """
    row = await conn.fetchrow(
        """SELECT
             count(*) FILTER (WHERE status = 'ok')                              AS ok,
             count(*) FILTER (WHERE status = 'dead')                            AS dead,
             count(*) FILTER (WHERE status = 'pending'
                                 OR (status = 'error' AND attempts < $2))       AS waiting,
             count(*) FILTER (WHERE status = 'error' AND attempts >= $2)        AS exhausted
           FROM article_images
          WHERE article_id = $1
            AND EXISTS (SELECT 1 FROM articles WHERE id = $1 AND hidden_at IS NULL)""",
        article_id, MAX_IMAGE_ATTEMPTS,
    )
    return {k: int(v) for k, v in dict(row).items()}


# A loose index scan: ~70 probes into articles_country_list_idx rather than a
# scan of the table. Postgres has no native skip scan, so the recursion is how
# you ask for one.
_COUNTRIES_SQL = """
WITH RECURSIVE t AS (
    (SELECT country FROM articles
      WHERE country IS NOT NULL AND hidden_at IS NULL
      ORDER BY country LIMIT 1)
    UNION ALL
    SELECT (SELECT country FROM articles
             WHERE country > t.country AND country IS NOT NULL AND hidden_at IS NULL
             ORDER BY country LIMIT 1)
      FROM t WHERE t.country IS NOT NULL
)
SELECT country FROM t WHERE country IS NOT NULL
"""


async def list_countries(conn: asyncpg.Connection) -> tuple[str, ...]:
    rows = await conn.fetch(_COUNTRIES_SQL)
    return tuple(r["country"] for r in rows)


def _prefix_upper_bound(prefix: str) -> str:
    """The smallest string greater than every string starting with `prefix`.

    Requires `prefix` to be non-empty and to not end in U+10FFFF, the maximum
    Unicode code point — there is no next character to compute for that one,
    and `chr()` raises rather than answer. `suggest_authors` is this
    function's only caller and screens both cases out first; see its
    docstring for why that is done there instead of with a try/except here.
    """
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


async def suggest_authors(
    conn: asyncpg.Connection, prefix: str, limit: int = 8
) -> tuple[str, ...]:
    """Up to `limit` author names starting with `prefix`, case-insensitively.

    A range scan, not `LIKE` with `DISTINCT`. DISTINCT is a blocking aggregate
    that LIMIT cannot push through, so that form costs O(matching articles):
    measured on 600k rows, `?author=a` aggregated 50,990 rows to return 8, reading
    95MB, and `?author=%` became a 938MB sequential scan. This form reads 127
    buffers in 0.10ms and is O(limit). It also means no LIKE ever sees user
    input, so `%` and `_` need no escaping — they are ordinary characters.

    `prefix` is public, untrusted input (a query parameter), and two shapes of
    it cannot name any stored author — rejected up front, before either
    `_prefix_upper_bound` or the query runs, rather than left to raise and
    surface as a 500 on a public page:

    - A prefix ending in U+10FFFF, the maximum code point: `_prefix_upper_bound`
      has no next character to compute (`chr(0x110000)` is out of range).
    - Any lone surrogate (U+D800-U+DFFF) anywhere in the prefix: it cannot be
      represented in UTF-8 at all, so asyncpg raises `DataError` trying to
      encode it as a bound parameter. This is checked separately because it
      defeats the first guard — a lone surrogate not in the last position
      would sail past a check that only looks at the final character, and
      only fails later, encoding the whole string.

    Both are the same answer, not two special cases: a string that cannot be
    represented, or that names no valid successor, cannot match a stored
    author name either, so an empty tuple is correct, not a fallback.
    """
    lowered = prefix.lower()
    if not lowered:
        return ()
    if ord(lowered[-1]) >= 0x10FFFF:
        return ()
    try:
        lowered.encode("utf-8")
    except UnicodeEncodeError:
        return ()
    rows = await conn.fetch(
        """SELECT DISTINCT ON (lower(author_name)) author_name
             FROM articles
            WHERE lower(author_name) >= $1 AND lower(author_name) < $2
              AND hidden_at IS NULL
            ORDER BY lower(author_name), published_at DESC, id DESC
            LIMIT $3""",
        lowered, _prefix_upper_bound(lowered), limit,
    )
    return tuple(r["author_name"] for r in rows)


async def archive_stats(conn: asyncpg.Connection) -> ArchiveStats:
    """What the archive holds and how far collection has reached.

    The count is the expensive part and nothing here can make it cheap: no index
    answers `count(*)`. Measured against postgres:17, 300 000 seeded rows
    (29 MB heap), ANALYZEd, warm cache — the whole statement is 3 714 buffers /
    17.5 ms, of which 3 704 are the count alone, a Parallel Seq Scan on
    `articles` with `Filter: (hidden_at IS NULL)` and two workers launched. It
    reads the whole table, so its cost tracks the heap: these seeded rows carry
    a four-character body and the real ones average roughly 3.4 KB (SPEC.md:
    ~9.6 GB of text across ~2.8M articles), so the real archive's scan is larger
    than the figure above by that ratio.

    The span is not the expensive part: 8 buffers, 0.034 ms, two InitPlan Limits
    over articles_list_idx. This docstring used to say "8 buffers, 0.105 ms" and
    call them Index Only Scans — that was measured against the same statement
    without `hidden_at IS NULL`, which is not the statement this function sends.
    Measured side by side in one session: with the predicate they are Index Scan
    + `Filter: (hidden_at IS NULL)`, without it Index Only Scan with
    `Heap Fetches: 1`, and 8 buffers either way. The conclusion survived; the
    sentence did not.

    The caller caches this whole result and single-flights the refresh — see
    `routes._stats` for why the cache alone was not enough.
    """
    row = await conn.fetchrow(
        """SELECT (SELECT count(*) FROM articles WHERE hidden_at IS NULL)      AS articles,
                  (SELECT min(published_at) FROM articles WHERE hidden_at IS NULL) AS oldest,
                  (SELECT max(published_at) FROM articles WHERE hidden_at IS NULL) AS newest,
                  (SELECT next_id FROM crawl_cursor WHERE name = 'backfill')   AS frontier"""
    )
    return ArchiveStats(**dict(row))


async def get_blob(conn: asyncpg.Connection, digest: bytes) -> BlobRow | None:
    row = await conn.fetchrow(
        "SELECT sha256, withheld_at FROM images WHERE sha256 = $1", digest
    )
    return BlobRow(**dict(row)) if row else None


async def fetch_log_status(conn: asyncpg.Connection, article_id: int) -> str | None:
    return await conn.fetchval("SELECT status FROM fetch_log WHERE article_id = $1", article_id)
