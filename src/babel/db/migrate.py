"""Apply .sql files in filename order, once each.

Deliberately not Alembic: the schema is small, the migrations are plain SQL, and
a crawler that runs for a month benefits more from something with no moving
parts than from a framework.
"""

import pathlib

import asyncpg

_LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


async def apply_migrations(conn: asyncpg.Connection, directory: pathlib.Path) -> list[str]:
    """Run any migration not yet recorded. Returns the names applied this call."""
    await conn.execute(_LEDGER)
    done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
    applied: list[str] = []
    for path in sorted(directory.glob("*.sql")):  # noqa: ASYNC240 — local read at startup, not hot-path I/O
        if path.name in done:
            continue
        async with conn.transaction():
            await conn.execute(path.read_text(encoding="utf-8"))
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
        applied.append(path.name)
    return applied
