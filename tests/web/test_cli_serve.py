import pytest

from babel.config import Settings
from babel.web.app import REQUIRED_MIGRATIONS, open_pool, verify_schema

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


def test_serve_checks_the_schema_before_it_creates_the_app():
    """The other half of the same rule: not applying migrations obliges it to
    check that somebody did. An unapplied 005 otherwise 503s every page while
    /healthz stays 200, so the compose healthcheck reports a healthy container
    serving nothing."""
    import inspect

    from babel import cli

    source = inspect.getsource(cli._serve)
    assert "verify_schema" in source
    assert source.index("verify_schema(conn)") < source.index("create_app(settings)")


async def test_verify_schema_passes_on_a_migrated_database(pg):
    await verify_schema(pg)  # every migration applied by the fixture


async def test_verify_schema_names_the_missing_migration(pg):
    await pg.execute("DELETE FROM schema_migrations WHERE name = $1", REQUIRED_MIGRATIONS[0])
    with pytest.raises(RuntimeError, match=REQUIRED_MIGRATIONS[0]):
        await verify_schema(pg)


async def test_verify_schema_names_the_markup_migration_too(pg):
    """A skipped migrate must be named, not answered as 503 forever.

    Without 007 the browse queries select body_raw, asyncpg raises
    UndefinedColumnError, which is a PostgresError, which lands in the
    database-down handler — so every page reports the database as not answering
    while /healthz and the compose healthcheck both stay green.
    """
    await pg.execute("DELETE FROM schema_migrations WHERE name = '007_body_markup.sql'")
    with pytest.raises(RuntimeError, match="007_body_markup.sql"):
        await verify_schema(pg)


async def test_verify_schema_treats_a_database_with_no_ledger_as_unmigrated(pg):
    """A database that has never been migrated has no `schema_migrations` at all.

    That must be the same named refusal, not an UndefinedTableError escaping as
    an unhandled startup crash that names a table instead of the missing step.
    """
    await pg.execute("DROP TABLE schema_migrations")
    with pytest.raises(RuntimeError, match=REQUIRED_MIGRATIONS[0]):
        await verify_schema(pg)


async def test_verify_schema_names_the_permission_gap_not_a_missing_migration(pg):
    """An unreadable ledger is a different fact from a missing migration, and the
    refusal must not conflate them.

    Reproduced against a role that was never granted SELECT on
    schema_migrations — an easy gap to leave, since `.env.example`'s
    `ALTER DEFAULT PRIVILEGES` line is what closes it, and a stricter,
    hand-rolled grant list can omit a deploy-internal ledger table without
    anyone intending to hide it. Before this fix, `InsufficientPrivilegeError`
    propagated straight out of `verify_schema` and crash-looped `babel serve`
    against a database whose schema was, in fact, fully applied.
    """
    await pg.execute("CREATE ROLE verify_schema_unpriv LOGIN")
    try:
        await pg.execute("SET ROLE verify_schema_unpriv")
        try:
            with pytest.raises(RuntimeError, match="no SELECT on schema_migrations") as exc_info:
                await verify_schema(pg)
        finally:
            await pg.execute("RESET ROLE")
    finally:
        await pg.execute("DROP ROLE verify_schema_unpriv")
    # The honest fact is "unreadable", not "missing" — the message must not
    # name a migration it never actually got to check.
    assert REQUIRED_MIGRATIONS[0] not in str(exc_info.value)


async def test_verify_schema_never_writes(pg):
    """It runs as the SELECT-only role, so it may only read.

    `SET default_transaction_read_only = on` is the session-level form of the
    `ALTER ROLE` the deployment runbook prescribes, and it makes any write raise
    ReadOnlySqlTransactionError. This is what stops the ledger read from being
    "fixed" one day by copying apply_migrations' `CREATE TABLE IF NOT EXISTS`
    bootstrap in front of it — harmless as the owner, fatal as this role.
    """
    await pg.execute("SET default_transaction_read_only = on")
    await verify_schema(pg)
