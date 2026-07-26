"""Every SQL statement in the project lives here.

Article saves are idempotent by design: a re-fetch replaces the row and its
children rather than erroring, which is what makes retrying a partially failed
crawl safe.
"""

from collections.abc import Sequence

import asyncpg

from babel.models import Article


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
    conn: asyncpg.Connection, ids: Sequence[int], *, retry_errors: bool = False
) -> list[int]:
    """Drop IDs already fetched. With retry_errors, previous failures come back."""
    if not ids:
        return []
    predicate = "status <> 'error'" if retry_errors else "TRUE"
    rows = await conn.fetch(
        f"SELECT article_id FROM fetch_log WHERE article_id = ANY($1::bigint[]) AND {predicate}",
        list(ids),
    )
    seen = {r["article_id"] for r in rows}
    return [i for i in ids if i not in seen]


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
