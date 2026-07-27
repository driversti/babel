"""Route handlers. No SQL here — everything comes from babel.db.browse."""

import datetime
import logging
import pathlib
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from babel.crawler.images import image_path
from babel.db import browse
from babel.web.blobs import HEAD_BYTES, parse_digest, serving_type
from babel.web.cursor import (
    decode_cursor,
    encode_cursor,
    game_date_to_utc,
    parse_game_date,
    to_game_time,
)

log = logging.getLogger(__name__)

_STATS_TTL_SEC = 300
# Far shorter than the success TTL, and deliberately so: this only has to outlive
# the single burst of waiters already queued on stats_lock when the scan fails —
# once they have all been served the cached error, nothing is gained by holding
# a stale 503 for anywhere near five minutes, and a genuinely transient cause
# (a statement_timeout hit under load, a momentary connection blip) should get a
# fresh attempt on the next request rather than wait out the success window.
_STATS_ERROR_TTL_SEC = 5

# `articles.id` is `bigint` (migrations/001_initial.sql), so this is the largest
# id the archive could ever hold — and the largest asyncpg will bind at all.
MAX_ARTICLE_ID = 2**63 - 1


class _ImageFileResponse(FileResponse):
    """A FileResponse scoped to the blob route.

    FileResponse re-stats the path itself right before it sends anything, and
    turns a file that has vanished by then into a bare RuntimeError — after
    parse_digest, the row lookup and this route's own existence check have
    all already passed, but still before any header or byte reaches the
    client (that stat is the first thing FileResponse.__call__ does; nothing
    is sent until after it succeeds). Every other "we don't have this blob"
    case on this route is a 404, so this one is too, converted by raising
    HTTPException from here rather than by returning it — a Response's
    __call__ runs inside Starlette's own wrap_app_handling_exceptions, which
    is what lets this still reach the ordinary 404 handler and its styled
    page instead of the traceback FileResponse would otherwise produce.
    """

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        except RuntimeError as exc:
            log.warning("blob vanished from %s before it could be served: %s", self.path, exc)
            raise HTTPException(status_code=404) from exc


def _missing_detail(status: str | None, frontier: int | None) -> str:
    """Why this article is not here — three different facts a bare 404 flattens into one."""
    if status == "missing":
        return "This article was already deleted when the crawler reached it."
    if status in ("error", "stale"):
        return "Collecting this article failed. The crawler will try again."
    reached = f" Collection has reached article {frontier:,}.".replace(",", " ") if frontier else ""
    return f"This article is not collected yet.{reached}"


def _cached_stats(app) -> browse.ArchiveStats | None:
    """The cached totals if they are still inside the TTL, else None."""
    cached: tuple[float, browse.ArchiveStats] | None = getattr(app.state, "stats_cache", None)
    if cached is not None and time.monotonic() - cached[0] < _STATS_TTL_SEC:
        return cached[1]
    return None


def _cached_stats_error(app) -> BaseException | None:
    """The cached failure if it is still inside its (short) TTL, else None."""
    cached: tuple[float, BaseException] | None = getattr(app.state, "stats_error", None)
    if cached is not None and time.monotonic() - cached[0] < _STATS_ERROR_TTL_SEC:
        return cached[1]
    return None


async def _stats(app, conn) -> browse.ArchiveStats:
    """Archive-wide totals, on a five-minute clock, computed once at a time.

    The count is the expensive part and there is no index that answers it:
    measured against postgres:17 with 300 000 seeded rows, warm cache, the
    statement is 3 714 buffers / 17.5 ms and 3 704 of those buffers are a
    Parallel Seq Scan on `articles` with `Filter: (hidden_at IS NULL)`, against
    the table the crawler writes to continuously. Hence the cache.

    Hence also the lock. The cache is written only after the query returns, so
    without it every request that arrives while the entry is stale starts its
    own scan — up to `web_pool_size` of them, each with its own parallel
    workers, from one burst of ordinary traffic. The waiters re-check the cache
    after acquiring, so they return the value the winner just computed instead
    of queueing up to repeat it. A request never waits on more than one scan.

    That has to hold when the scan raises too, not only when it succeeds — a
    success is cached by writing it after the query returns, and a raising
    query never reaches that line, so without a separate failure cache every
    waiter queued on the lock would wake to an empty cache and repeat the same
    doomed scan itself. Measured: 6 concurrent requests against a failing
    scan became 6 sequential scans and 1.82s wall time instead of 1 scan,
    each waiter holding a pool connection for its whole re-run. The error is
    therefore cached too, under `_STATS_ERROR_TTL_SEC` rather than the success
    TTL — see that constant for why the two durations differ.

    The cache lives on app.state, not in a module global. A module global
    outlives the app that filled it, and the tests build one app per test
    against a fresh database — the second test would read the first one's
    numbers. It is also simply the truthful scope: the cache belongs to a
    running service, not to an imported module.
    """
    fresh = _cached_stats(app)
    if fresh is not None:
        return fresh
    stale_error = _cached_stats_error(app)
    if stale_error is not None:
        raise stale_error
    async with app.state.stats_lock:
        fresh = _cached_stats(app)
        if fresh is not None:
            return fresh
        stale_error = _cached_stats_error(app)
        if stale_error is not None:
            raise stale_error
        try:
            stats = await browse.archive_stats(conn)
        except Exception as exc:
            app.state.stats_error = (time.monotonic(), exc)
            raise
        app.state.stats_cache = (time.monotonic(), stats)
        return stats


def register_routes(app: FastAPI) -> None:
    templates = app.state.templates

    @app.get("/", response_class=HTMLResponse)
    async def index(  # noqa: PLR0913 — these are the query parameters, not a signature to shrink
        request: Request,
        country: str | None = None,
        author: str | None = None,
        order: str = "new",
        after: str | None = None,
        before: str | None = None,
        on: str | None = None,
    ):
        if order not in ("new", "old"):
            order = "new"

        # A truncated link pasted from a chat is the normal way a bad cursor
        # arrives, so it redirects rather than showing the reader a 400 for
        # something they did not do.
        raw_cursor = after or before
        cursor = decode_cursor(raw_cursor)
        if raw_cursor and cursor is None:
            return RedirectResponse(request.url.remove_query_params(["after", "before"]), 302)

        jump = parse_game_date(on)
        if jump is not None:
            # The boundary a date jump lands on depends on which way the page
            # is sorted, not just on the date itself.
            #
            # Oldest-first fetches rows strictly *after* the cursor, so
            # anchoring at midnight of `jump` is already correct: the first
            # page starts right after that instant, which is the first
            # instant of the requested day.
            #
            # Newest-first fetches rows strictly *before* the cursor. Anchoring
            # at midnight of `jump` itself would put every article published
            # that day on the wrong side of "<" — the reported bug. The fix is
            # to anchor at midnight of the *next* day instead, so the whole of
            # `jump` satisfies "< next midnight" and only later days are
            # excluded.
            #
            # `jump + timedelta(days=1)` is calendar arithmetic on a `date`,
            # not instant arithmetic on a `datetime` — a game day is not
            # always 24 hours across a DST transition in America/Los_Angeles,
            # and `game_date_to_utc` re-resolves whichever calendar date
            # results to its own correct UTC offset.
            #
            # Both branches use article_id=0 as the cursor's row component,
            # for opposite reasons, both resting on ids always being positive:
            # - Oldest-first's ">" needs a tie (a row published at exactly
            #   midnight of `jump`) to be *included*: `id > 0` holds for every
            #   real id, so it is.
            # - Newest-first's "<" needs a tie (a row published at exactly
            #   midnight of the *next* day — i.e. actually the following day,
            #   not `jump`) to be *excluded*: `id < 0` holds for no real id,
            #   so it is.
            if order == "old":
                cursor = browse.Cursor(published_at=game_date_to_utc(jump), article_id=0)
            else:
                try:
                    next_day = jump + datetime.timedelta(days=1)
                except OverflowError:
                    # jump == date.max (9999-12-31): a perfectly valid ISO
                    # date — parse_game_date stays a pure parser and is not
                    # the place to reject it — but newest-first's boundary
                    # needs the day *after* it, which the calendar has no
                    # representation for. A reader who typed the calendar's
                    # own edge did not ask for a 500: leave `cursor` as it
                    # was (unset, absent an explicit after=/before=) so the
                    # response falls through to the ordinary unpositioned
                    # first page, the same treatment an unparseable date
                    # already gets.
                    pass
                else:
                    cursor = browse.Cursor(
                        published_at=game_date_to_utc(next_day), article_id=0
                    )

        going = "prev" if before else "next"
        filters = browse.ListFilters(country=country or None, author=author or None)

        async with app.state.pool.acquire() as conn:
            page = await browse.list_articles(
                conn, filters, order=order, cursor=cursor, going=going
            )
            countries = await browse.list_countries(conn)
            stats = await _stats(app, conn)
            suggestions: tuple[str, ...] = ()
            if filters.author and not page.rows:
                suggestions = await browse.suggest_authors(conn, filters.author)

        # `page.has_more` always describes the direction the page was *fetched*
        # in, so which of the two links it gates flips with `going`, and the
        # other link's condition is not its mirror.
        #
        # Forward: has_more means "more rows ahead", so it gates the forward
        # link. The backward link needs no such evidence — a cursor was
        # supplied, so the reader came from a page that is still there.
        #
        # Backward: has_more now means "more rows further back", so it gates
        # the *backward* link, and the forward link is unconditional, because
        # arriving on a backward page at all means there is a page ahead to
        # return to.
        #
        # Gating both on has_more was a guaranteed dead end rather than an edge
        # case: the top page always holds exactly PAGE_SIZE rows above page 2's
        # first row, so a backward fetch to it finds nothing beyond them and
        # reports has_more False every time. Page 1 lost its forward link, grew
        # a backward link onto an empty page, and — because list.html reads
        # `not next_cursor` — told the reader it held everything, above 51 more
        # articles.
        forward = going == "next"
        offer_next = page.has_more if forward else True
        offer_prev = cursor is not None if forward else page.has_more
        next_cursor = (
            encode_cursor(page.rows[-1].published_at, page.rows[-1].id)
            if page.rows and offer_next
            else None
        )
        prev_cursor = (
            encode_cursor(page.rows[0].published_at, page.rows[0].id)
            if page.rows and offer_prev
            else None
        )

        # The pager's words, not just its plumbing, have to follow `order`.
        # "next" (after=) always continues in the direction the reader is
        # already travelling, but which calendar direction that *is* flips
        # with the sort: newest-first's "next" moves to older articles,
        # oldest-first's "next" moves to newer ones. Chosen fix: flip the
        # words themselves rather than switch to direction-neutral labels
        # ("next page"/"previous page") — "older"/"newer" is the more useful
        # thing for a reader of a date-sorted archive to know, as long as it
        # is never wrong, so the label has to track `order` instead of being
        # hardcoded to what is only true for one of the two sort orders.
        next_label = "newer" if order == "old" else "older"
        prev_label = "older" if order == "old" else "newer"

        return templates.TemplateResponse(
            request=request,
            name="list.html",
            context={
                "rows": page.rows,
                "countries": countries,
                "stats": stats,
                "filters": filters,
                "order": order,
                "suggestions": suggestions,
                "next_cursor": next_cursor,
                "prev_cursor": prev_cursor,
                "next_label": next_label,
                "prev_label": prev_label,
                "game_time": to_game_time,
                "settings": app.state.settings,
            },
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.get("/article/{article_id}", response_class=HTMLResponse)
    async def article(request: Request, article_id: int):
        # An id outside `articles.id`'s own range is not a lookup that can miss,
        # it is one asyncpg refuses to bind: DataError, which is a PostgresError,
        # which is the database-down handler — so a scanner's
        # `/article/99999999999999999999999` made the site answer 503 "The
        # database is not answering" and log a traceback per request. Such an id
        # cannot name a row we hold, so it belongs in the same 404 as every other
        # article we do not have. Checked here rather than with `Path(le=...)`,
        # whose rejection is a raw FastAPI 422 JSON body instead of this
        # archive's own page.
        storable = 0 < article_id <= MAX_ARTICLE_ID
        async with app.state.pool.acquire() as conn:
            detail = await browse.get_article(conn, article_id) if storable else None
            if detail is None:
                status = await browse.fetch_log_status(conn, article_id) if storable else None
                stats = await _stats(app, conn)
                return templates.TemplateResponse(
                    request=request,
                    name="error.html",
                    context={
                        "heading": "Not in the archive",
                        "detail": _missing_detail(status, stats.frontier),
                        "settings": app.state.settings,
                    },
                    status_code=404,
                )
            comments = await browse.get_comments(conn, article_id)
            image_map = await browse.get_image_map(conn, article_id)
            counts = await browse.image_status_counts(conn, article_id)

        return templates.TemplateResponse(
            request=request,
            name="article.html",
            context={
                "article": detail,
                "comments": comments,
                "digests": [s.sha256.hex() for s in image_map.values()
                            if s.state == "ok" and s.sha256],
                "counts": counts,
                "game_time": to_game_time,
                "settings": app.state.settings,
            },
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.get("/img/{hex_digest}")
    async def image(hex_digest: str):
        digest = parse_digest(hex_digest)
        if digest is None:
            raise HTTPException(status_code=404)

        async with app.state.pool.acquire() as conn:
            blob = await browse.get_blob(conn, digest)
        if blob is None or blob.withheld_at is not None:
            raise HTTPException(status_code=404)

        path = image_path(pathlib.Path(app.state.settings.image_root), digest)
        try:
            # Only the leading bytes, never the whole blob: this is a public,
            # unauthenticated route, and a stored image can be up to
            # max_image_bytes (8 MiB by default). Buffering the full body per
            # request means memory scales with concurrent requests in flight —
            # measured at +218 MiB RSS for 100 concurrent 8 MiB GETs and +1.7 GiB
            # for 300 — for no benefit, since FileResponse below streams the
            # actual bytes from disk itself.
            with path.open("rb") as f:
                head = f.read(HEAD_BYTES)
        except OSError:
            # The row says we have it and the disk says otherwise — IMAGE_ROOT
            # moved, or the volume is not mounted. Name the blob in the log; the
            # reader gets a missing image, not a 500.
            log.warning("blob %s recorded but not readable at %s", hex_digest, path)
            raise HTTPException(status_code=404) from None

        content_type, inline = serving_type(head)
        headers = {
            # Not immutable: content addressing would justify it, but this is
            # other people's content and withholding has to be able to reach it.
            "Cache-Control": "public, max-age=86400, must-revalidate",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        }
        if not inline:
            headers["Content-Disposition"] = f'attachment; filename="{hex_digest}"'
        # media_type is passed explicitly so the response carries the type
        # decided above from the bytes — never FileResponse's own guess from the
        # path's extension (there isn't one; every blob's filename on disk is
        # its hex digest) and never the untrusted, unechoed images.mime.
        return _ImageFileResponse(path, media_type=content_type, headers=headers)
