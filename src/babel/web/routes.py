"""Route handlers. No SQL here — everything comes from babel.db.browse."""

import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from babel.db import browse
from babel.web.cursor import (
    decode_cursor,
    encode_cursor,
    game_date_to_utc,
    parse_game_date,
    to_game_time,
)

_STATS_TTL_SEC = 300


async def _stats(app, conn) -> browse.ArchiveStats:
    """Archive-wide totals, on a five-minute clock.

    The span is two index-only limits and costs nothing; the count is a heap
    scan at 2.8M rows, which is why this is cached rather than computed per
    request.

    The cache lives on app.state, not in a module global. A module global
    outlives the app that filled it, and the tests build one app per test
    against a fresh database — the second test would read the first one's
    numbers. It is also simply the truthful scope: the cache belongs to a
    running service, not to an imported module.
    """
    cached: tuple[float, browse.ArchiveStats] | None = getattr(app.state, "stats_cache", None)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _STATS_TTL_SEC:
        return cached[1]
    stats = await browse.archive_stats(conn)
    app.state.stats_cache = (now, stats)
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
            cursor = browse.Cursor(published_at=game_date_to_utc(jump), article_id=0)

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

        next_cursor = (
            encode_cursor(page.rows[-1].published_at, page.rows[-1].id)
            if page.rows and page.has_more
            else None
        )
        prev_cursor = (
            encode_cursor(page.rows[0].published_at, page.rows[0].id)
            if page.rows and (cursor is not None)
            else None
        )

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
                "game_time": to_game_time,
                "settings": app.state.settings,
            },
            headers={"Cache-Control": "public, max-age=300"},
        )
