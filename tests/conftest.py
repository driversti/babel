import asyncio
import pathlib

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from babel.db.migrate import apply_migrations

MIGRATIONS = pathlib.Path(__file__).parent.parent / "migrations"


@pytest_asyncio.fixture
async def pg():
    with PostgresContainer("postgres:17") as container:
        dsn = container.get_connection_url().replace("postgresql+psycopg2", "postgresql")
        conn = await asyncpg.connect(dsn)
        await apply_migrations(conn, MIGRATIONS)
        try:
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
