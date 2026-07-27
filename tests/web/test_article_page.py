import datetime

UTC = datetime.UTC


async def _article(pool, article_id, **kw):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO articles (id, title, body, author_name, country,
                                     published_at, e_day, comment_count)
               VALUES ($1, $2, $3, 'ann', 'Poland',
                       timestamptz '2026-07-21 05:53Z', 6817, $4)
               ON CONFLICT (id) DO NOTHING""",
            article_id, kw.get("title", "A title"), kw.get("body", "Line one.\nLine two."),
            kw.get("comment_count", 0),
        )


async def test_article_renders_title_body_and_game_date(client, pool):
    await _article(pool, 2000)
    body = (await client.get("/article/2000")).text
    assert "A title" in body
    assert "Line one." in body
    assert "2026-07-20" in body          # game day, not the UTC 21st
    assert "6,817" in body or "6817" in body


async def test_script_in_stored_text_is_escaped(client, pool):
    await _article(pool, 2001, title="<script>alert(1)</script>", body="<script>alert(2)</script>")
    body = (await client.get("/article/2001")).text
    assert "<script>alert(1)</script>" not in body
    assert "<script>alert(2)</script>" not in body
    assert "&lt;script&gt;" in body


async def test_comments_render_with_depth_and_removed_markers(client, pool):
    await _article(pool, 2002, comment_count=2)
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO comments (id, article_id, position, depth, author_name, body)
               VALUES ($1, 2002, $2, $3, 'bob', $4)""",
            [(31, 1, 0, "hello"), (32, 2, 1, None)],
        )
    body = (await client.get("/article/2002")).text
    assert "hello" in body
    assert "[removed]" in body


async def test_only_dead_images_are_reported_as_gone(client, pool):
    await _article(pool, 2003)
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO article_images (article_id, position, source_url, status, attempts)
               VALUES (2003, $1, $2, $3, $4)""",
            [(1, "https://h/1.png", "pending", 0),
             (2, "https://h/2.png", "pending", 0),
             (3, "https://h/3.png", "dead", 1)],
        )
    body = (await client.get("/article/2003")).text
    assert "1 image was already gone" in body
    assert "2 not captured yet" in body
    assert "3 of 3" not in body


async def test_a_fresh_article_is_never_called_lost(client, pool):
    await _article(pool, 2004)
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO article_images (article_id, position, source_url, status, attempts)
               VALUES (2004, $1, $2, 'pending', 0)""",
            [(i, f"https://h/{i}.png") for i in range(1, 7)],
        )
    body = (await client.get("/article/2004")).text
    assert "already gone" not in body
    assert "6 not captured yet" in body


async def test_404_says_deleted_upstream(client, pool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO fetch_log (article_id, status) VALUES (2100, 'missing') "
            "ON CONFLICT (article_id) DO UPDATE SET status = 'missing'"
        )
    response = await client.get("/article/2100")
    assert response.status_code == 404
    assert "already deleted" in response.text


async def test_404_says_collection_failed(client, pool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO fetch_log (article_id, status) VALUES (2101, 'error') "
            "ON CONFLICT (article_id) DO UPDATE SET status = 'error'"
        )
    assert "will try again" in (await client.get("/article/2101")).text


async def test_404_says_not_collected_yet_and_names_the_frontier(client, pool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crawl_cursor (name, next_id) VALUES ('backfill', 2785850) "
            "ON CONFLICT (name) DO UPDATE SET next_id = 2785850"
        )
    text = (await client.get("/article/2102")).text
    assert "not collected yet" in text
    assert "2 785 850" in text or "2785850" in text


async def test_hidden_article_falls_through_to_the_404(client, pool):
    await _article(pool, 2103)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE articles SET hidden_at = now() WHERE id = 2103")
    assert (await client.get("/article/2103")).status_code == 404


async def test_an_id_too_big_for_the_column_is_a_404_not_an_outage(client):
    """`articles.id` is `bigint`, so a larger id cannot name a row we hold.

    Binding one anyway makes asyncpg raise `DataError`, which is a
    `PostgresError` — so it landed in the app's database-down handler and the
    site answered 503 "The database is not answering", logging a traceback per
    request. Any scanner could make a public archive report itself unavailable
    on demand. Both ends of the range, because a sufficiently negative id
    overflows the same way a positive one does.
    """
    for path in (
        "/article/9223372036854775808",           # bigint max + 1
        "/article/99999999999999999999999",
        "/article/-9223372036854775809",          # bigint min - 1
    ):
        response = await client.get(path)
        assert response.status_code == 404, path
        assert "<html" in response.text.lower()
        assert "not answering" not in response.text
        assert "Traceback" not in response.text


async def test_markup_is_rendered_when_body_raw_is_present(client, pool):
    await _article(pool, 2100, body="Alpha Beta")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE articles SET body_raw = $1 WHERE id = 2100",
            "<p>Alpha<br><br><b>Beta</b></p>",
        )
    body = (await client.get("/article/2100")).text
    assert "<p>Alpha</p>" in body
    assert "<strong>Beta</strong>" in body


async def test_a_row_without_body_raw_still_renders_the_plain_text(client, pool):
    await _article(pool, 2101, body="Line one.\nLine two.")
    body = (await client.get("/article/2101")).text
    assert "body-text" in body
    assert "Line one." in body


async def test_a_script_in_body_raw_cannot_reach_the_page(client, pool):
    await _article(pool, 2102)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE articles SET body_raw = $1 WHERE id = 2102",
            '<p>ok<script>alert(1)</script><a href="javascript:alert(2)">x</a></p>',
        )
    body = (await client.get("/article/2102")).text
    assert "<script>alert(1)</script>" not in body
    assert "javascript:alert(2)" not in body
    assert "ok" in body


async def test_a_captured_image_renders_inline_and_not_in_the_gallery(client, pool):
    await _article(pool, 2103)
    digest = bytes.fromhex("1a" * 32)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE articles SET body_raw = $1 WHERE id = 2103",
            '<p>see<br><br><img src="https://h/a.png"></p>',
        )
        await conn.execute(
            "INSERT INTO images (sha256, bytes, mime) VALUES ($1, 3, 'image/png')", digest,
        )
        await conn.execute(
            """INSERT INTO article_images (article_id, position, source_url, status, sha256)
               VALUES (2103, 0, 'https://h/a.png', 'ok', $1)""", digest,
        )
    body = (await client.get("/article/2103")).text
    assert f'/img/{digest.hex()}' in body
    assert body.count(f'/img/{digest.hex()}') == 1  # inline only, not also below


async def test_an_image_dropped_from_the_article_still_shows_below(client, pool):
    await _article(pool, 2104)
    digest = bytes.fromhex("2b" * 32)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE articles SET body_raw = $1 WHERE id = 2104", "<p>no images now</p>",
        )
        await conn.execute(
            "INSERT INTO images (sha256, bytes, mime) VALUES ($1, 3, 'image/png')", digest,
        )
        await conn.execute(
            """INSERT INTO article_images (article_id, position, source_url, status, sha256)
               VALUES (2104, 0, 'https://h/old.png', 'ok', $1)""", digest,
        )
    body = (await client.get("/article/2104")).text
    assert f'/img/{digest.hex()}' in body
    assert "no longer in the article" in body


async def test_a_missing_image_renders_a_placeholder_with_a_link(client, pool):
    await _article(pool, 2105)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE articles SET body_raw = $1 WHERE id = 2105",
            '<p><img src="https://h/gone.png"></p>',
        )
        await conn.execute(
            """INSERT INTO article_images (article_id, position, source_url, status)
               VALUES (2105, 0, 'https://h/gone.png', 'dead')"""
        )
    body = (await client.get("/article/2105")).text
    assert "already gone" in body
    assert 'href="https://h/gone.png"' in body


async def test_comment_markup_is_rendered(client, pool):
    await _article(pool, 2106, comment_count=1)
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO comments (id, article_id, position, depth, author_name,
                                     body, body_raw)
               VALUES (91, 2106, 0, 0, 'bob', 'one two',
                       '<p>one<br><br><a href="https://e.org/">two</a></p>')"""
        )
    body = (await client.get("/article/2106")).text
    assert 'href="https://e.org/"' in body
    assert "<p>one</p>" in body


async def test_the_plain_text_fallback_does_not_also_appear_beside_markup(client, pool):
    """Found by mutation testing: removing the {% if body_html %}/{% else %}
    split so `article.body` always renders alongside `body_html` left the
    brief's own test_markup_is_rendered_when_body_raw_is_present green, since
    that test only asserts the rendered markup is present, never that the raw
    plain-text fallback is absent. `class="body-text"` is the fallback's own
    marker and appears nowhere else on a comment-free article.
    """
    await _article(pool, 2107, body="Alpha Beta")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE articles SET body_raw = $1 WHERE id = 2107",
            "<p>Alpha Beta</p>",
        )
    body = (await client.get("/article/2107")).text
    assert 'class="body-text"' not in body
