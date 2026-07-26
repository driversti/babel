"""Every SQL statement in the project lives here.

Article saves are idempotent by design: a re-fetch replaces the row and its
children rather than erroring, which is what makes retrying a partially failed
crawl safe.
"""

from collections.abc import Sequence

import asyncpg

from babel.models import Article

# A row stuck at 'error' forever would mean a single transient failure (a
# timeout, a flaky 5xx) permanently drops an article from the archive. But
# retrying forever is just as wrong: a genuinely broken ID (malformed page,
# permanently gone article) would be re-offered on every backfill pass and
# the month-long walk would never converge. Five attempts gives transient
# failures several chances across independent crawl passes while still
# letting a truly bad ID fall out of rotation.
MAX_FETCH_ATTEMPTS = 5

# 'ok' and 'missing' are answers. 'error' and 'stale' are unfinished business.
RETRYABLE_STATUSES = ("error", "stale")


async def save_article(conn: asyncpg.Connection, article: Article) -> None:
    """Insert or replace an article together with its comments and image slots."""
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO articles (id, title, body, author_id, author_name, country,
                                  published_at, e_day, comment_count, fetched_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9, now())
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title, body = EXCLUDED.body,
                author_id = EXCLUDED.author_id, author_name = EXCLUDED.author_name,
                country = EXCLUDED.country, published_at = EXCLUDED.published_at,
                e_day = EXCLUDED.e_day, comment_count = EXCLUDED.comment_count,
                fetched_at = now()
            """,
            article.id, article.title, article.body, article.author_id, article.author_name,
            article.country, article.published_at, article.e_day, article.comment_count,
        )

        await conn.execute("DELETE FROM comments WHERE article_id = $1", article.id)
        if article.comments:
            await conn.executemany(
                """INSERT INTO comments (id, article_id, position, depth, author_id,
                                         author_name, posted_at, body)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                   ON CONFLICT (id) DO NOTHING""",
                [
                    (c.id, article.id, c.position, c.depth, c.author_id,
                     c.author_name, c.posted_at, c.body)
                    for c in article.comments
                ],
            )

        # Image slots start as 'pending'; the image worker moves them to ok/dead/
        # skipped_no_space. Existing rows keep their status so a re-parse does not
        # discard the knowledge that a link was already dead.
        if article.images:
            await conn.executemany(
                """INSERT INTO article_images (article_id, position, source_url, status)
                   VALUES ($1,$2,$3,'pending')
                   ON CONFLICT (article_id, position) DO UPDATE
                       SET source_url = EXCLUDED.source_url""",
                [(article.id, i.position, i.source_url) for i in article.images],
            )


async def record_fetch(
    conn: asyncpg.Connection, article_id: int, status: str, error: str | None = None
) -> None:
    await conn.execute(
        """
        INSERT INTO fetch_log (article_id, status, attempts, last_error, updated_at)
        VALUES ($1, $2, 1, $3, now())
        ON CONFLICT (article_id) DO UPDATE SET
            status = EXCLUDED.status,
            attempts = fetch_log.attempts + 1,
            last_error = EXCLUDED.last_error,
            updated_at = now()
        """,
        article_id, status, error,
    )


async def get_cursor(conn: asyncpg.Connection, name: str) -> int | None:
    return await conn.fetchval("SELECT next_id FROM crawl_cursor WHERE name = $1", name)


async def set_cursor(conn: asyncpg.Connection, name: str, next_id: int) -> None:
    await conn.execute(
        """INSERT INTO crawl_cursor (name, next_id, updated_at) VALUES ($1, $2, now())
           ON CONFLICT (name) DO UPDATE SET next_id = EXCLUDED.next_id, updated_at = now()""",
        name, next_id,
    )


async def filter_unseen(
    conn: asyncpg.Connection,
    ids: Sequence[int],
    *,
    retry_errors: bool = False,
    max_attempts: int = MAX_FETCH_ATTEMPTS,
) -> list[int]:
    """Drop IDs already fetched.

    With retry_errors, rows logged as 'error' are offered again, but only
    while their attempts count stays below max_attempts -- once a row hits
    the ceiling it is treated as seen, same as 'ok' or 'missing'. Those two
    statuses are final answers and are never re-offered regardless of the
    flag.
    """
    if not ids:
        return []
    rows = await conn.fetch(
        """
        SELECT article_id FROM fetch_log
        WHERE article_id = ANY($1::bigint[])
          AND ($2::boolean IS FALSE OR NOT (status = 'error' AND attempts < $3::smallint))
        """,
        list(ids), retry_errors, max_attempts,
    )
    seen = {r["article_id"] for r in rows}
    return [i for i in ids if i not in seen]


async def claim_retryable(conn: asyncpg.Connection, limit: int, cooldown_sec: int) -> list[int]:
    """Article IDs worth another attempt, newest first.

    The cooldown matters more than it looks. Without it, a host having a bad
    minute burns all five of an article's attempts inside that minute and the
    article is then written off permanently.
    """
    rows = await conn.fetch(
        """
        SELECT article_id FROM fetch_log
        WHERE status = ANY($1::text[])
          AND attempts < $2
          AND updated_at <= now() - make_interval(secs => $3)
        ORDER BY article_id DESC
        LIMIT $4
        """,
        list(RETRYABLE_STATUSES), MAX_FETCH_ATTEMPTS, cooldown_sec, limit,
    )
    return [r["article_id"] for r in rows]


async def mark_stale(conn: asyncpg.Connection, article_ids: Sequence[int]) -> int:
    """Queue already-collected articles for re-collection. Returns rows changed.

    Only touches 'ok' and 'error' rows. A 'missing' row records a fact about the
    article rather than about our copy of it, and an ID with no row at all will
    be reached by the walk anyway.
    """
    if not article_ids:
        return 0
    result = await conn.execute(
        """UPDATE fetch_log SET status = 'stale', attempts = 0, updated_at = now()
           WHERE article_id = ANY($1::bigint[]) AND status IN ('ok', 'error')""",
        list(article_ids),
    )
    return int(result.split()[-1])


async def save_image_blob(
    conn: asyncpg.Connection, sha256: bytes, mime: str | None, size: int
) -> None:
    await conn.execute(
        """INSERT INTO images (sha256, mime, bytes) VALUES ($1, $2, $3)
           ON CONFLICT (sha256) DO NOTHING""",
        sha256, mime, size,
    )


async def record_image(
    conn: asyncpg.Connection,
    article_id: int,
    position: int,
    source_url: str,
    status: str,
    sha256: bytes | None = None,
) -> None:
    await conn.execute(
        """INSERT INTO article_images (article_id, position, source_url, sha256, status, checked_at)
           VALUES ($1,$2,$3,$4,$5, now())
           ON CONFLICT (article_id, position) DO UPDATE SET
               sha256 = EXCLUDED.sha256, status = EXCLUDED.status, checked_at = now()""",
        article_id, position, source_url, sha256, status,
    )
