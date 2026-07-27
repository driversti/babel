from babel.db.repo import hide_article, withhold_image


async def test_hide_article_sets_the_tombstone(pool):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO articles (id, title, body, published_at, comment_count)
               VALUES (3000, 'T', 'B', now(), 0) ON CONFLICT (id) DO NOTHING"""
        )
        assert await hide_article(conn, 3000) == 1
        assert await conn.fetchval("SELECT hidden_at FROM articles WHERE id = 3000") is not None


async def test_hiding_twice_is_idempotent(pool):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO articles (id, title, body, published_at, comment_count)
               VALUES (3001, 'T', 'B', now(), 0) ON CONFLICT (id) DO NOTHING"""
        )
        await hide_article(conn, 3001)
        first = await conn.fetchval("SELECT hidden_at FROM articles WHERE id = 3001")
        await hide_article(conn, 3001)
        assert await conn.fetchval("SELECT hidden_at FROM articles WHERE id = 3001") == first


async def test_withhold_image_reports_how_many_articles_cite_it(pool):
    digest = b"\x09" * 32
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO images (sha256, mime, bytes) VALUES ($1,'image/png',1) "
            "ON CONFLICT (sha256) DO NOTHING", digest,
        )
        for article_id in (3100, 3101):
            await conn.execute(
                """INSERT INTO articles (id, title, body, published_at, comment_count)
                   VALUES ($1, 'T', 'B', now(), 0) ON CONFLICT (id) DO NOTHING""",
                article_id,
            )
            await conn.execute(
                """INSERT INTO article_images (article_id, position, source_url, status, sha256)
                   VALUES ($1, 1, 'https://h/a.png', 'ok', $2)
                   ON CONFLICT (article_id, source_url) DO NOTHING""",
                article_id, digest,
            )
        citing = await withhold_image(conn, digest)
    assert citing == 2
