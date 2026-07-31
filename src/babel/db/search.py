"""The read path for semantic search.

Two stages in one statement. Stage one ranks by Hamming distance over
binary-quantised vectors — 128 bytes each instead of 2,048 — and takes the top
`candidates`. Stage two re-ranks exactly those against the full halfvec. The
quantised form loses precision; the over-fetch is what buys it back.

The index and the query must quantise the same way or the index cannot serve
the ORDER BY, and the symptom is a sequential scan over the whole table rather
than an error. `quantised()` is the single source both take it from.
"""

import contextlib
import datetime
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

import asyncpg

from babel.db.repo import EMBED_DIM, vector_literal

# 25x over-fetch. A starting value, not a measured optimum — the acceptance
# test in the plan's task 8 is what checks it against real recall.
CANDIDATES = 500
RESULTS = 20


def quantised(expression: str) -> str:
    return f"binary_quantize({expression})::bit({EMBED_DIM})"


HNSW_INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS article_embeddings_bin_idx ON article_embeddings
    USING hnsw (({quantised("embedding")}) bit_hamming_ops)
    WHERE embedding IS NOT NULL
"""


@dataclass(frozen=True, slots=True)
class SearchRow:
    id: int
    title: str
    author_name: str | None
    country: str | None
    published_at: datetime.datetime
    score: float


def build_search_query() -> str:
    """The two-stage statement. Its parameters are ($1 vector text, $2 candidates, $3 limit).

    Returned rather than executed so the EXPLAIN test plans the query the
    application actually sends — the mistake finding M3 records against the
    older suite is asserting on a copy pasted into a test.

    Takes no arguments: candidates and limit are bound at execution, and the
    statement text does not vary with them. That is the difference from
    build_list_query, whose text genuinely varies per filter combination.
    """
    # article_embeddings carries no alias in the subquery, deliberately: the
    # index expression is built from the bare column name "embedding"
    # (HNSW_INDEX_SQL, via quantised("embedding")), and an aliased reference
    # like "ae.embedding" is a textually different expression even though it
    # names the same column — exactly the kind of divergence this module's
    # docstring warns turns the ORDER BY into a sequential scan. Unqualified
    # "embedding" and "article_id" are unambiguous here: articles has neither
    # column, so there is nothing for them to collide with.
    return f"""
        SELECT a.id, a.title, a.author_name, a.country, a.published_at,
               1 - (c.embedding <=> $1::halfvec({EMBED_DIM})) AS score
        FROM (
            SELECT article_id, embedding
            FROM article_embeddings
            JOIN articles ar ON ar.id = article_embeddings.article_id AND ar.hidden_at IS NULL
            WHERE embedding IS NOT NULL
            ORDER BY {quantised("embedding")}
                     <~> {quantised(f"$1::halfvec({EMBED_DIM})")}
            LIMIT $2
        ) c
        JOIN articles a ON a.id = c.article_id
        ORDER BY c.embedding <=> $1::halfvec({EMBED_DIM})
        LIMIT $3
    """


async def _apply_settings(conn: asyncpg.Connection, candidates: int) -> None:
    """Both settings, inside whatever transaction the caller opened.

    ef_search comes from `candidates` and not from a constant of its own. Left
    at its default of 40 while the inner query asks for 500, the index returns
    40 candidates and the other 460 silently do not exist — no error, just a
    worse answer. Deriving it here is what makes the two impossible to diverge.

    iterative_scan is why pgvector 0.8 is a hard requirement: without it the
    hidden_at and embedding-IS-NOT-NULL predicates are applied *after* the graph
    walk, so a filter that excludes anything returns fewer rows than asked for.
    relaxed_order is correct here because the outer stage re-sorts anyway.

    Interpolated rather than bound: SET takes no parameters. `candidates` is
    coerced to int at the call site for that reason.
    """
    await conn.execute(f"SET LOCAL hnsw.ef_search = {int(candidates)}")
    await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")


@contextlib.asynccontextmanager
async def _tuned(conn: asyncpg.Connection, candidates: int) -> AsyncIterator[None]:
    """The transaction and the settings, together, for both callers.

    Together on purpose. `search_articles` and
    `search_articles_with_settings_probe` used to each open their own
    `async with conn.transaction():` block around a shared `_apply_settings`
    call — which meant the test that proves SET LOCAL took effect exercised
    only the probe's copy of the transaction. The production function's own
    transaction could be deleted entirely and the whole suite stayed green,
    because nothing else in the suite runs `search_articles` against a settings
    change that depends on it. A single helper neither caller can own a private
    copy of closes that gap: there is now exactly one transaction to delete,
    and deleting it breaks the probe's test regardless of which caller the
    reviewer imagines exercising it.
    """
    async with conn.transaction():
        await _apply_settings(conn, candidates)
        yield


async def search_articles(
    conn: asyncpg.Connection,
    vector: Sequence[float],
    *,
    candidates: int = CANDIDATES,
    limit: int = RESULTS,
) -> tuple[SearchRow, ...]:
    literal = vector_literal(vector)
    sql = build_search_query()
    async with _tuned(conn, candidates):
        rows = await conn.fetch(sql, literal, candidates, limit)
    return tuple(
        SearchRow(
            id=r["id"], title=r["title"], author_name=r["author_name"],
            country=r["country"], published_at=r["published_at"], score=float(r["score"]),
        )
        for r in rows
    )


async def search_articles_with_settings_probe(
    conn: asyncpg.Connection, vector: Sequence[float], *, candidates: int, limit: int
) -> str:
    """Run a search and report what ef_search was actually set to.

    Exists only for the test that proves SET LOCAL took effect. Reading the
    setting back is the only way to tell a working SET LOCAL from a no-op one,
    because the no-op raises nothing. A thin wrapper over `_tuned`, the same
    helper `search_articles` uses — not a second implementation — so this is
    honest production surface, not a stand-in that could drift from what
    actually runs.
    """
    literal = vector_literal(vector)
    sql = build_search_query()
    async with _tuned(conn, candidates):
        await conn.fetch(sql, literal, candidates, limit)
        return await conn.fetchval("SELECT current_setting('hnsw.ef_search')")
