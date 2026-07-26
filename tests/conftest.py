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
    """asyncpg.Pool.acquire() is an async context manager; one connection suffices."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.fixture
def fake_pool():
    """asyncpg.Pool.acquire() is an async context manager; tests hand it one connection."""

    def build(conn):
        return FakePool(conn)

    return build
