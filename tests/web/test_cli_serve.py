import pytest

from babel.config import Settings
from babel.web.app import open_pool

DSN = "postgresql://babel:babel@db:5432/babel"


async def test_serve_refuses_to_run_as_the_owner_role():
    # No database needed: both refusals raise before any connection is opened.
    settings = Settings(database_url=DSN, web_database_url=DSN)
    with pytest.raises(RuntimeError, match="must not connect"):
        await open_pool(settings)


async def test_serve_refuses_without_a_web_dsn():
    settings = Settings(database_url=DSN, web_database_url=None)
    with pytest.raises(RuntimeError, match="WEB_DATABASE_URL is not set"):
        await open_pool(settings)


def test_serve_does_not_apply_migrations():
    """A read-only role cannot CREATE TABLE schema_migrations, and both existing
    long-running commands call apply_migrations at startup. A third written by
    symmetry would crash-loop under restart: unless-stopped."""
    import inspect

    from babel import cli

    source = inspect.getsource(cli._serve)
    assert "apply_migrations" not in source
