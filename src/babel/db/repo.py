"""Every write-path SQL statement in the project lives here.

The read path lives in browse.py. SQL still never appears outside this package.

Article saves are idempotent by design: a re-fetch replaces the row and its
children rather than erroring, which is what makes retrying a partially failed
crawl safe.
"""

from collections.abc import Sequence
from dataclasses import dataclass

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

# The host of a source_url, as SQL. Peels the authority off `scheme://…`, off a
# protocol-relative `//…` (common in older articles, and stored verbatim), then
# drops any `user@` and `:port` so the operator can name a host the plain way.
# Written once here because the claim, the requeue and the listing must all agree
# on what a host is — and must agree with `images.url_host`, which the worker's
# circuit breaker uses to decide which hosts to name.
_URL_HOST = """
    lower(split_part(
        regexp_replace(
            substring(source_url from '^(?:[A-Za-z][A-Za-z0-9+.-]*:)?//([^/?#]+)'),
            '^.*@', ''),
        ':', 1))
"""


class CommentsVanishedError(Exception):
    """The page said it has comments and the parse produced none.

    Raised rather than saving, because saving would be indistinguishable from an
    article that genuinely has no comments — and the difference is the archive.
    """


async def save_article(conn: asyncpg.Connection, article: Article) -> None:
    """Insert or replace an article together with its comments and image slots.

    Comments are additive: existing rows are updated, never deleted. Two reasons,
    and they are the same reason twice. A parse that stops finding comments —
    eRepublik renames `commentWrapper`, while `postBody` keeps working, so the
    article still parses and `fetch_article`'s gate still passes — used to delete
    every archived comment and insert nothing. Run over the range an operator is
    documented to sweep with `babel refetch --from/--to` after exactly that kind of
    breakage, it would have emptied the majority of the archive's text (comments
    carry 2054 chars against the article's 1357, per SPEC.md) with no exception, no
    log line, and no raw HTML to reparse. And a comment deleted upstream since the
    first fetch is precisely the thing this archive exists to still hold, so
    pruning it on a routine re-fetch would throw away text that no longer exists
    anywhere. The guard below catches the parser fault; not deleting covers the
    rest, including the parser faults nobody predicted.
    """
    if article.comment_count > 0 and not article.comments:
        raise CommentsVanishedError(
            f"article {article.id} reports {article.comment_count} comments, parsed 0"
        )

    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO articles (id, title, body, body_raw, author_id, author_name,
                                  country, published_at, e_day, comment_count, fetched_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10, now())
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title, body = EXCLUDED.body,
                body_raw = EXCLUDED.body_raw,
                author_id = EXCLUDED.author_id, author_name = EXCLUDED.author_name,
                country = EXCLUDED.country, published_at = EXCLUDED.published_at,
                e_day = EXCLUDED.e_day, comment_count = EXCLUDED.comment_count,
                fetched_at = now()
            """,
            article.id, article.title, article.body, article.body_raw,
            article.author_id, article.author_name, article.country,
            article.published_at, article.e_day, article.comment_count,
        )

        if article.comments:
            await conn.executemany(
                """INSERT INTO comments (id, article_id, position, depth, author_id,
                                         author_name, posted_at, body, body_raw)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                   ON CONFLICT (id) DO UPDATE SET
                       position = EXCLUDED.position, depth = EXCLUDED.depth,
                       author_id = EXCLUDED.author_id, author_name = EXCLUDED.author_name,
                       posted_at = EXCLUDED.posted_at, body = EXCLUDED.body,
                       body_raw = EXCLUDED.body_raw""",
                [
                    (c.id, article.id, c.position, c.depth, c.author_id,
                     c.author_name, c.posted_at, c.body, c.body_raw)
                    for c in article.comments
                ],
            )

        # Image rows start as 'pending'; the worker moves them to ok/dead/error.
        # Existing rows keep their status, so a re-parse does not discard the
        # knowledge that a link was already dead — and that is safe only because
        # the row is keyed on the URL. Keyed on position, as it was, a single
        # image added at the top shifted every later slot and handed each one a
        # new URL wearing the previous image's verdict and hash. See migration
        # 004. Position is now just where the image first appears, and orders the
        # drain. A row whose URL vanished from the article is left alone: the
        # blob is stored, and the archive keeps what the source no longer shows.
        if article.images:
            await conn.executemany(
                """INSERT INTO article_images (article_id, position, source_url, status)
                   VALUES ($1,$2,$3,'pending')
                   ON CONFLICT (article_id, source_url) DO UPDATE
                       SET position = LEAST(article_images.position, EXCLUDED.position)""",
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
        -- The status set is written as a literal, not a bound parameter, and
        -- must stay textually identical to fetch_log_retryable_idx's predicate.
        -- Postgres only proves a partial index applicable from a Const; with a
        -- Param it cannot, and a cached generic plan would then sequentially
        -- scan ~2.8M rows that are almost all 'ok'.
        WHERE status IN ('error', 'stale')
          AND attempts < $1
          AND updated_at <= now() - make_interval(secs => $2)
        ORDER BY article_id DESC
        LIMIT $3
        """,
        MAX_FETCH_ATTEMPTS, cooldown_sec, limit,
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
           ON CONFLICT (article_id, source_url) DO UPDATE SET
               sha256 = EXCLUDED.sha256, status = EXCLUDED.status, checked_at = now()""",
        article_id, position, source_url, sha256, status,
    )


# An image host that answers with a timeout rather than a 404 would otherwise sit
# in the queue forever. 'dead' is final and never recounted; only 'error' retries.
MAX_IMAGE_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class PendingImage:
    article_id: int
    position: int
    source_url: str
    attempts: int


async def claim_pending_images(
    conn: asyncpg.Connection,
    limit: int,
    cooldown_sec: float,
    excluded_hosts: Sequence[str] = (),
) -> list[PendingImage]:
    """Next images to fetch, newest article first.

    Ordering is not cosmetic: 66% of 2021-2026 images still resolve against 7%
    of 2007-2014, so draining oldest-first would spend the crawl on links that
    are already gone while the recoverable ones rot.

    An 'error' row returns to 'pending' via record_image_result only implicitly —
    it is re-offered here because its status is not terminal and its attempts are
    below the ceiling.

    `cooldown_sec` is the same guard `claim_retryable` applies to articles, and for
    the same reason — a host having a bad minute would otherwise burn all five of an
    image's attempts inside that minute and the image would be written off for good.
    The image side needs it more: it is the rate-limit-prone one, a failed row stays
    the newest row in the queue (the backfill only enqueues lower article ids) and so
    was re-claimed on the very next cycle. A never-attempted 'pending' row does not
    wait; a cooldown is for something that just failed.
    """
    rows = await conn.fetch(
        f"""
        -- The status set is written as a literal, not a bound parameter, and
        -- must stay textually identical to article_images_queue_idx's predicate.
        -- Postgres only proves a partial index applicable from a Const; with a
        -- Param it cannot, and the planner would sequentially scan a table
        -- holding millions of rows that are almost all 'ok' or 'dead'.
        -- The checked_at and host terms below are heap qualifiers and do not
        -- affect that.
        SELECT article_id, position, source_url, attempts
        FROM article_images
        WHERE status IN ('pending', 'error') AND attempts < $1
          AND (status = 'pending' OR checked_at <= now() - make_interval(secs => $2))
          AND ({_URL_HOST}) <> ALL($3::text[])
        ORDER BY article_id DESC, position
        LIMIT $4
        """,
        MAX_IMAGE_ATTEMPTS, cooldown_sec, list(excluded_hosts), limit,
    )
    return [PendingImage(**dict(r)) for r in rows]




async def requeue_images_by_host(conn: asyncpg.Connection, host: str) -> int:
    """Put one host's abandoned images back in the queue. Returns rows changed.

    'dead' is permanent by design, which is right when the host told us the truth
    and wrong when it did not. Every false 'dead' observed so far came as a batch
    from a single host — a rate limit, a landing page, a mislabelled Content-Type —
    so a host is the unit of recovery. Attempts reset too: an 'error' row sitting
    at the ceiling is every bit as abandoned as a 'dead' one.

    'ok' is never touched; requeueing a stored image would discard a good capture
    to fetch it again for nothing.
    """
    result = await conn.execute(
        f"""UPDATE article_images
            SET status = 'pending', attempts = 0, checked_at = now()
            WHERE status IN ('dead', 'error') AND {_URL_HOST} = lower($1)""",
        host,
    )
    return int(result.split()[-1])


async def stuck_image_hosts(conn: asyncpg.Connection, limit: int) -> list[tuple[str, int]]:
    """Hosts with abandoned images, worst first.

    Without this the requeue command is only usable by someone who already has a
    host name from a log line, which is exactly the person least likely to need it.
    """
    rows = await conn.fetch(
        f"""SELECT {_URL_HOST} AS host, count(*) AS n
            FROM article_images
            WHERE status IN ('dead', 'error')
            GROUP BY 1 ORDER BY n DESC, host LIMIT $1""",
        limit,
    )
    return [(r["host"], r["n"]) for r in rows]


async def record_image_result(
    conn: asyncpg.Connection,
    article_id: int,
    source_url: str,
    status: str,
    sha256: bytes | None = None,
) -> None:
    """Set an image's outcome and count the attempt.

    Addressed by URL, not position: the worker may be recording a result long
    after a re-parse moved that image within the article, and position is not
    identity. See migration 004.
    """
    await conn.execute(
        """UPDATE article_images
           SET status = $3, sha256 = $4, attempts = attempts + 1, checked_at = now()
           WHERE article_id = $1 AND source_url = $2""",
        article_id, source_url, status, sha256,
    )


async def hide_article(conn: asyncpg.Connection, article_id: int) -> int:
    """Suppress an article from the public site. Returns rows changed.

    A tombstone rather than a DELETE, and the difference is not stylistic.
    Measured on a fresh database: DELETE FROM articles cascades comments and
    article_images, but the images row and its blob survive (the FK runs
    article_images.sha256 -> images, not the reverse), and fetch_log has no FK to
    articles at all, so its row stays 'ok'. `babel refetch` then flips it to
    'stale', the sweep re-collects it, and the taken-down article comes back.
    Deletion is silently reversible by tooling this project already ships.

    Idempotent: an already-hidden row keeps its original timestamp, so re-running
    the command does not rewrite when the request arrived.
    """
    result = await conn.execute(
        "UPDATE articles SET hidden_at = now() WHERE id = $1 AND hidden_at IS NULL",
        article_id,
    )
    return int(result.split()[-1])


async def withhold_image(conn: asyncpg.Connection, digest: bytes) -> int:
    """Stop serving one blob. Returns how many articles cite it.

    The count is the point. Content addressing means a blob is shared — flags,
    avatars and recycled memes recur across thousands of articles — so
    withholding is never a single-article act, and the operator has to see the
    blast radius before deciding.
    """
    await conn.execute(
        "UPDATE images SET withheld_at = now() WHERE sha256 = $1 AND withheld_at IS NULL",
        digest,
    )
    return await conn.fetchval(
        "SELECT count(DISTINCT article_id) FROM article_images WHERE sha256 = $1", digest
    )
