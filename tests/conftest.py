import pathlib

import asyncpg
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
