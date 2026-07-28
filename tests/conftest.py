import asyncio
import pathlib

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from babel.db.migrate import apply_migrations

MIGRATIONS = pathlib.Path(__file__).parent.parent / "migrations"

# Postgres does its own identifier quoting via format('%I'), and the aggregate
# is NULL when nothing is left, so this is a no-op on an already-empty schema
# rather than a syntax error on `DROP TABLE  CASCADE`.
_DROP_ALL_TABLES = """
DO $$
DECLARE victims text;
BEGIN
    SELECT string_agg(format('%I.%I', schemaname, tablename), ', ')
      INTO victims
      FROM pg_tables
     WHERE schemaname = 'public';
    IF victims IS NOT NULL THEN
        EXECUTE 'DROP TABLE ' || victims || ' CASCADE';
    END IF;
END $$;
"""


@pytest.fixture(scope="session")
def postgres_dsn():
    """One container for the whole run.

    Measured on this project: booting `postgres:17` costs 1.50 s, against 19 ms
    to connect, 23 ms to apply every migration and 19 ms to reset the schema. At
    171 database-backed tests, a container per test was ~263 s of the suite's
    ~315 s — the run was almost entirely Docker.
    """
    with PostgresContainer("postgres:17") as container:
        yield container.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture
async def pg(postgres_dsn):
    """A connection to a freshly-migrated, empty database.

    The schema is dropped and rebuilt *before* each test rather than cleaned up
    after, for two reasons. A test that dies mid-way cannot poison its
    successor, and when one fails its rows are still there to inspect.

    Dropping the tables rather than `TRUNCATE`, even though truncating is
    cheaper (3.2 ms against 18.7 ms, or 0.6 s against 3.2 s across the suite).
    Three tests in `tests/web/test_cli_serve.py` deliberately damage
    `schema_migrations` itself — two delete rows from it and one drops the table
    outright — to prove `verify_schema` refuses rather than crashing. Truncation
    restores none of that, so the cheaper reset would leave the next test
    running against a database missing a migration it believes is applied. Two
    and a half seconds is not worth an ordering-dependent suite.

    Dropping the tables rather than the schema, which is the obvious way to
    write this and is wrong. Measured: `initdb`'s `public` is owned by
    `pg_database_owner` with `{pg_database_owner=UC/…,=U/…}`, i.e. `USAGE` for
    `PUBLIC`; a schema you create yourself grants `PUBLIC` nothing. That single
    difference makes an unprivileged role unable to resolve `schema_migrations`
    at all, so
    `test_verify_schema_names_the_permission_gap_not_a_missing_migration` — which
    exists to prove "unreadable ledger" and "missing migration" stay distinct —
    got the missing-migration refusal instead and failed. Touching no schema
    object leaves nothing to reproduce. (`DROP OWNED BY CURRENT_USER` is not an
    option either: the role owns the database, so Postgres refuses.)

    A fresh connection per test, not a shared one: `SET ROLE`, prepared
    statements and session GUCs are connection state, and one of those same
    tests sets a role. 19 ms buys that isolation outright.
    """
    conn = await asyncpg.connect(postgres_dsn)
    try:
        await conn.execute(_DROP_ALL_TABLES)
        await apply_migrations(conn, MIGRATIONS)
        yield conn
    finally:
        await conn.close()


class FakePool:
    """asyncpg.Pool.acquire() is an async context manager; one connection suffices.

    Exclusive, like the thing it imitates. A real pool never hands the same
    connection to two borrowers at once, and asyncpg raises if two operations run
    on one connection concurrently. Without the lock this fixture handed the same
    connection to every caller, so it modelled a pool correctly only for code that
    never acquired concurrently — and silently dropped writes for code that did.
    """

    def __init__(self, conn):
        self._conn = conn
        self._lock = asyncio.Lock()

    def acquire(self):
        conn, lock = self._conn, self._lock

        class _Ctx:
            async def __aenter__(self):
                await lock.acquire()
                return conn

            async def __aexit__(self, *exc):
                lock.release()
                return False

        return _Ctx()


@pytest.fixture
def fake_pool():
    """asyncpg.Pool.acquire() is an async context manager; tests hand it one connection."""

    def build(conn):
        return FakePool(conn)

    return build


@pytest.fixture
def pool(pg, fake_pool):
    """A pool-shaped façade over the per-test connection.

    Application code takes an asyncpg.Pool and calls `async with pool.acquire()`.
    Handing it this keeps the tests' database access identical to production's
    without a second container per test.
    """
    return fake_pool(pg)
