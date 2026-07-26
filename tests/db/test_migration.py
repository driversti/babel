async def test_creates_every_phase_one_table(pg):
    rows = await pg.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    names = {r["tablename"] for r in rows}
    assert {"articles", "comments", "images", "article_images", "fetch_log", "crawl_cursor"} <= names


async def test_migrations_are_idempotent(pg):
    import pathlib

    from babel.db.migrate import apply_migrations

    root = pathlib.Path(__file__).parent.parent.parent / "migrations"
    applied = await apply_migrations(pg, root)
    assert applied == []  # conftest already ran them


async def test_comments_cascade_when_an_article_is_deleted(pg):
    await pg.execute(
        """INSERT INTO articles (id, title, body, published_at)
           VALUES (1, 't', 'b', now())"""
    )
    await pg.execute(
        """INSERT INTO comments (id, article_id, position, depth)
           VALUES (10, 1, 0, 0)"""
    )
    await pg.execute("DELETE FROM articles WHERE id = 1")
    assert await pg.fetchval("SELECT count(*) FROM comments") == 0


async def test_article_images_records_dead_links_without_bytes(pg):
    await pg.execute(
        """INSERT INTO articles (id, title, body, published_at)
           VALUES (2, 't', 'b', now())"""
    )
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status)
           VALUES (2, 0, 'https://dead.example/x.png', 'dead')"""
    )
    row = await pg.fetchrow("SELECT sha256, status FROM article_images WHERE article_id = 2")
    assert row["sha256"] is None
    assert row["status"] == "dead"
