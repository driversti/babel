"""The public read-only site.

Deliberately not in gluetun's network namespace: it needs inbound connections,
which that namespace cannot accept, and its only outbound dependency is
Postgres on the bridge. The site therefore stays up when the tunnel is down.
"""

import asyncio
import contextlib
import logging
import pathlib
from collections.abc import AsyncIterator

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from babel.config import Settings
from babel.db.migrate import applied_migrations

log = logging.getLogger(__name__)

TEMPLATE_DIR = pathlib.Path(__file__).parent / "templates"
STATIC_DIR = pathlib.Path(__file__).parent / "static"

# Article pages are crawlable so the noindex on them is actually seen: a crawler
# blocked by robots.txt never fetches the page and therefore never reads the
# header, which leaves the URL indexable as a bare entry that noindex can never
# remove. The parameterised list space is refused because filter combinations
# are a crawl trap carrying no content of their own.
ROBOTS_TXT = "User-agent: *\nDisallow: /?\nAllow: /\n"

CSP = (
    "default-src 'self'; script-src 'none'; img-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


# The migrations the read path cannot work without — not "every file in
# migrations/". One image carries one migrations/ directory for all four
# services, so an all-or-nothing check would stop the public site from booting
# over a crawler-only migration it never reads, during exactly the window the
# documented deploy order creates (stop the writers, migrate, start everything).
# 006 is not listed for the same reason: it only drops indexes 005 made
# redundant, and no browse query names one of them. A future browse migration
# belongs in this tuple, added by the commit that adds the migration.
REQUIRED_MIGRATIONS = ("005_browse.sql",)


def web_dsn(settings: Settings) -> str:
    """The SELECT-only DSN, or a named refusal.

    Split out of `open_pool` because the startup schema check needs the same
    two refusals to have run before it dials anything.
    """
    dsn = settings.web_database_url
    if not dsn:
        raise RuntimeError(
            "WEB_DATABASE_URL is not set. The public site connects as a SELECT-only "
            "role; falling back to DATABASE_URL would run it as the database owner."
        )
    if dsn == settings.database_url:
        raise RuntimeError(
            "WEB_DATABASE_URL equals DATABASE_URL. The public site must not connect "
            "as the crawler's own role. See README.md for the role setup."
        )
    return dsn


async def verify_schema(conn: asyncpg.Connection) -> None:
    """Refuse to serve against a database the browse schema was never applied to.

    Reads the ledger; writes nothing. That is a requirement, not an
    observation — this runs as the SELECT-only role, so the
    `CREATE TABLE IF NOT EXISTS schema_migrations` that `apply_migrations` opens
    with would raise here rather than be a no-op.

    Without this the failure is silent and misattributed. Measured with
    `hidden_at` dropped from `articles`: every page answered 503 "The database
    is not answering" — `asyncpg.UndefinedColumnError` is a `PostgresError`, so
    it lands in the database-down handler — while `/healthz` stayed 200 and the
    compose healthcheck stayed green. An operator would spend that outage
    looking at Postgres. Crash-looping with this message instead is the same
    answer the two crawler commands already give, and what
    `restart: unless-stopped` is for.
    """
    applied = await applied_migrations(conn)
    missing = [name for name in REQUIRED_MIGRATIONS if name not in applied]
    if missing:
        raise RuntimeError(
            f"the browse schema is not applied: {', '.join(missing)} is not in "
            "schema_migrations. Run `docker compose run --rm crawler babel migrate` "
            "first (stop crawler and images before you do — see README.md). Serving "
            "without it answers 503 on every page as though the database were down."
        )


async def open_pool(settings: Settings) -> asyncpg.Pool:
    """The read-only pool.

    The read-only guarantee is a property of the ROLE, not of this call. asyncpg
    runs `RESET ALL` on every connection release, which returns
    default_transaction_read_only to the role default — measured against
    postgres:17, a pool built with `init=` had acquire #1 blocked and acquire #2
    writing successfully. So `ALTER ROLE babel_web SET
    default_transaction_read_only = on` is the control, and it is an operator
    step because the role's password does not belong in this repository. See
    README.md.
    """
    return await asyncpg.create_pool(
        web_dsn(settings), min_size=1, max_size=settings.web_pool_size
    )


def create_app(settings: Settings, pool: object | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Built here rather than in create_app because a lock belongs to the
        # loop the app actually runs on, and this is the first place that loop
        # is running. It guards the archive_stats refresh; see routes._stats.
        app.state.stats_lock = asyncio.Lock()
        # An injected pool belongs to the caller: used as-is, never closed here.
        # That is the seam the tests drive the app through, and it leaves the
        # production path — pool omitted, open_pool runs — exactly as strict.
        if pool is not None:
            app.state.pool = pool
            yield
            return
        app.state.pool = await open_pool(settings)
        try:
            yield
        finally:
            await app.state.pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault("Content-Security-Policy", CSP)
        return response

    @app.get("/robots.txt", response_class=PlainTextResponse)
    async def robots() -> str:
        return ROBOTS_TXT

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok"

    def render_error(request: Request, status: int, heading: str, detail: str) -> HTMLResponse:
        response = app.state.templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"heading": heading, "detail": detail, "settings": settings},
            status_code=status,
        )
        # Normally security_headers adds these after call_next returns. But a
        # handler registered for the bare Exception class runs inside
        # ServerErrorMiddleware, which sits *outside* security_headers — its
        # response never passes back through that middleware's post-call_next
        # code. Setting them here too means every error page carries them
        # regardless of which layer built it. setdefault for the CSP so a
        # caller — task 12's image route — can still ask for something
        # stricter than the site-wide default.
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault("Content-Security-Policy", CSP)
        return response

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Exception) -> HTMLResponse:
        return render_error(request, 404, "Not here", "There is no such page in this archive.")

    @app.exception_handler(asyncpg.PostgresError)
    @app.exception_handler(OSError)
    async def database_down(request: Request, exc: Exception) -> HTMLResponse:
        # A traceback on a public page tells a stranger about the schema. The log
        # gets the detail; the reader gets a sentence.
        log.exception("database error serving %s", request.url.path)
        return render_error(
            request, 503, "The archive is unavailable",
            "The database is not answering. This is usually brief.",
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> HTMLResponse:
        # Anything not caught above is a bug, not an expected failure mode.
        # FastAPI/Starlette pull a handler registered for the bare Exception
        # class out of the usual per-route dict and hand it to
        # ServerErrorMiddleware itself (Starlette.build_middleware_stack), so
        # this one runs at the outermost layer instead of being skipped —
        # without it, this exact case reaches Starlette's own fallback, which
        # is a bare "Internal Server Error" with none of the headers below.
        # Worth knowing before anyone adds a debug flag: this handler does NOT
        # protect against one. Starlette's ServerErrorMiddleware.__call__ checks
        # `self.debug` before it ever looks at a registered Exception handler —
        # `if self.debug: return debug_response(...); elif self.handler is
        # None: ...; else: call self.handler`. Verified directly against this
        # FastAPI version: FastAPI(debug=True) with this exact handler still
        # registered returns Starlette's own traceback page, not this one's
        # sentence. Turning on debug here would bypass this handler entirely
        # and leak schema and file-path detail on the public route it exists
        # to protect — it must stay off in production.
        log.exception("unhandled error serving %s", request.url.path)
        return render_error(
            request, 500, "Something went wrong",
            "An unexpected error occurred. This is usually brief.",
        )

    from babel.web.routes import register_routes  # noqa: PLC0415 — avoids a cycle

    register_routes(app)
    return app
