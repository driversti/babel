import datetime
import time

from babel.web.markup import MAX_MARKUP_BYTES

UTC = datetime.UTC

# Same hill-climbed worst shape as test_markup.py's
# test_the_worst_known_shape_at_the_cap_renders_well_under_a_second: at
# MAX_MARKUP_BYTES (32 KiB) it costs ~0.3s per render on this machine, real
# enough to exercise B1's per-request budget cheaply without needing an
# implausibly large comment count.
_EXPENSIVE_UNIT = "<ol><ol><dd><ol><li><ul><a><ol><nobr><ul>"


def _body_at_cap() -> str:
    n = MAX_MARKUP_BYTES // len(_EXPENSIVE_UNIT)
    return _EXPENSIVE_UNIT * n


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
    assert "missing-image" in body
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


async def test_the_gallery_subtracts_per_url_not_page_wide(client, pool):
    """A mutant that makes the subtraction page-wide -- `gallery = [...] if
    not shown else []` -- passes both test_a_captured_image_renders_inline_
    and_not_in_the_gallery (image A alone: shown is non-empty, so the whole
    gallery collapses to [] regardless of the per-URL check, and A's count
    assertion still holds since it is only ever emitted inline) and
    test_an_image_dropped_from_the_article_still_shows_below (image B
    alone: shown is empty, so the whole-gallery-or-nothing branch still
    yields the full gallery). Neither test has an article citing one image
    while carrying a leftover second one, so neither can tell "subtract
    this URL" from "subtract everything, once, based on whether anything at
    all is shown". This is that article.
    """
    await _article(pool, 2108)
    digest_a = bytes.fromhex("5e" * 32)
    digest_b = bytes.fromhex("6f" * 32)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE articles SET body_raw = $1 WHERE id = 2108",
            '<p>see<br><br><img src="https://h/cited.png"></p>',
        )
        await conn.execute(
            "INSERT INTO images (sha256, bytes, mime) VALUES ($1, 3, 'image/png'), "
            "($2, 3, 'image/png')", digest_a, digest_b,
        )
        await conn.execute(
            """INSERT INTO article_images (article_id, position, source_url, status, sha256)
               VALUES (2108, 0, 'https://h/cited.png', 'ok', $1),
                      (2108, 1, 'https://h/dropped.png', 'ok', $2)""",
            digest_a, digest_b,
        )
    body = (await client.get("/article/2108")).text
    body_start = body.index('<div class="body">')
    gallery_start = body.index('<div class="gallery">')
    images_note_start = body.index('<p class="images-note">')
    body_slice = body[body_start:gallery_start]
    gallery_slice = body[gallery_start:images_note_start]
    assert f'/img/{digest_a.hex()}' in body_slice
    assert f'/img/{digest_a.hex()}' not in gallery_slice
    assert f'/img/{digest_b.hex()}' in gallery_slice
    assert f'/img/{digest_b.hex()}' not in body_slice


async def test_the_same_blob_under_two_urls_does_not_render_twice(client, pool):
    """An author re-uploading byte-identical media under a new URL -- the
    edit pattern migration 004 exists to protect -- gives article_images two
    rows for one digest. Only one of the two URLs is still cited in
    body_raw; the other is a stale duplicate of an image the reader can
    already see. Without deduping by digest, the leftover URL's row still
    clears the per-URL `not in shown` check (its own URL was never cited)
    and lands in the gallery, showing the same blob a second time under a
    caption -- "no longer in the article's text" -- that is false for it.
    """
    await _article(pool, 2109)
    digest = bytes.fromhex("3c" * 32)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE articles SET body_raw = $1 WHERE id = 2109",
            '<p><img src="https://h/live.png"></p>',
        )
        await conn.execute(
            "INSERT INTO images (sha256, bytes, mime) VALUES ($1, 3, 'image/png')", digest,
        )
        await conn.execute(
            """INSERT INTO article_images (article_id, position, source_url, status, sha256)
               VALUES (2109, 0, 'https://h/live.png', 'ok', $1),
                      (2109, 1, 'https://h/old-copy.png', 'ok', $1)""",
            digest,
        )
    body = (await client.get("/article/2109")).text
    assert body.count(f'/img/{digest.hex()}') == 1
    assert "no longer in the article" not in body


async def test_an_empty_rendered_body_does_not_falsely_flag_the_gallery(client, pool):
    """body_raw = "<p>   </p>" parses to a real (non-None) RenderedBody
    whose .html is empty -- _paragraphs drops whitespace-only paragraphs --
    so `rendered is not None` is true even though there is no rendered
    markup on the page at all. The template's own body branch already
    falls back to plain text correctly, because {% if body_html %} tests
    the *content*, not whether `rendered` exists -- but gallery_is_leftover
    used to test existence, not content, so the page showed the plain-text
    body and a gallery captioned "no longer in the article's text" for an
    image the (empty) rendered body was never actually compared against.
    """
    await _article(pool, 2110, body="Alpha Beta")
    digest = bytes.fromhex("4d" * 32)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE articles SET body_raw = $1 WHERE id = 2110", "<p>   </p>",
        )
        await conn.execute(
            "INSERT INTO images (sha256, bytes, mime) VALUES ($1, 3, 'image/png')", digest,
        )
        await conn.execute(
            """INSERT INTO article_images (article_id, position, source_url, status, sha256)
               VALUES (2110, 0, 'https://h/x.png', 'ok', $1)""", digest,
        )
    body = (await client.get("/article/2110")).text
    assert 'class="body-text"' in body
    assert "no longer in the article" not in body


async def test_a_comment_over_the_markup_cap_falls_back_to_plain_text(client, pool):
    """render_body returns None for a body over MAX_MARKUP_BYTES (Task 11).

    The route used to build comment_html unconditionally --
    `render_body(c.body_raw, {}).html` -- so a single oversized COMMENT
    body raised AttributeError on the None and 500'd the whole article page,
    not just that one comment. article.html's `comment_html.get(c.id)`
    already falls back to the comment's own plain-text `body` when the id is
    missing from the dict -- the same path a comment with no body_raw at all
    takes (see test_comments_render_with_depth_and_removed_markers) -- so the
    fix only has to omit a None render from the dict rather than store one;
    no template change was needed. The article's own body is untouched by
    this fixture (plain default text, no body_raw), isolating this to the
    comment path the bug was in.

    The oversized body is plain, ordinary markup, not a deeply nested one:
    round 6 replaced the depth-scanning guard (which this test originally
    exercised via MAX_NESTING) with a byte-size cap alone, so any body over
    MAX_MARKUP_BYTES is refused regardless of its shape.
    """
    await _article(pool, 2111, comment_count=1)
    oversized = "<p>" + ("z" * (MAX_MARKUP_BYTES + 1)) + "</p>"
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO comments (id, article_id, position, depth, author_name,
                                     body, body_raw)
               VALUES (92, 2111, 0, 0, 'bob', 'plain fallback text', $1)""",
            oversized,
        )
    response = await client.get("/article/2111")
    assert response.status_code == 200
    assert "plain fallback text" in response.text


async def test_the_render_budget_bounds_a_page_with_many_expensive_comments(client, pool):
    """B1: render_body is synchronous CPU with no `await`, called once for
    the article and once per comment inside a single `async def` route
    handler, and get_comments has no LIMIT -- so before this fix, the whole
    `web` process (every concurrent reader, plus /healthz) froze for the sum
    of every render on the page. Ten comments at MAX_MARKUP_BYTES, each
    costing ~0.3s to render (see _EXPENSIVE_UNIT above), measured 3.40s
    unbounded on this machine for the article plus all ten -- and climbing
    linearly with comment count, not bounded by anything -- before the fix
    below existed. The budget stops calling render_body once 1.0s of render
    time has been spent on the page, so wall time stays close to the budget
    regardless of how many comments there are, and it is measured end to
    end here (HTTP request in, response out), not just at the render_body
    call site, so a fix that only bounds *some* of the renders would still
    be caught.

    Rather than asserting exactly which comment index the cutoff lands on --
    fragile across machines, since it depends on exact render timing -- this
    checks the two invariants the budget actually promises: not every
    comment can have rendered (the wall-time bound proves that on its own),
    and at least the first must have (the article's own render leaves most
    of the 1.0s budget unspent, so the very first comment is never skipped).
    """
    await _article(pool, 2112, body="fallback body text")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE articles SET body_raw = $1 WHERE id = 2112", "<p>short</p>")
        await conn.executemany(
            """INSERT INTO comments (id, article_id, position, depth, author_name,
                                     body, body_raw)
               VALUES ($1, 2112, $2, 0, 'bob', $3, $4)""",
            [
                (300 + i, i, f"plain fallback {i}", _body_at_cap())
                for i in range(10)
            ],
        )
    start = time.perf_counter()
    response = await client.get("/article/2112")
    elapsed = time.perf_counter() - start
    assert response.status_code == 200
    # Bounded: the 1.0s budget plus at most one render's worth of overshoot
    # (~0.3s at the cap, since the budget is only checked *before* a render,
    # not during one) plus generous HTTP/DB/test-harness overhead -- nowhere
    # near the ~3.4s ten unbounded renders cost on this machine.
    assert elapsed < 2.5, f"took {elapsed:.2f}s -- render budget may not be enforced"
    body = response.text
    fell_back = sum(1 for i in range(10) if f"plain fallback {i}" in body)
    # Some, but not all, comments fell back to plain text: proof both that
    # rendering still happens at all (the first one always does -- the
    # article's own render is short and leaves most of the budget unspent)
    # and that the budget actually stopped later ones rather than silently
    # doing nothing.
    assert 0 < fell_back < 10, f"{fell_back} of 10 comments fell back to plain text"


async def test_a_withheld_image_cited_in_a_comment_is_not_linked_around(client, pool):
    """N1: comments render with `images={}` (before this fix), so a withheld
    blob's URL cited in a comment never resolves to the 'withheld' state the
    article's own body correctly special-cases -- it looks up the empty map,
    gets None, and falls to the ordinary "not archived" placeholder, which
    (unlike 'withheld') gets a link to the original source. `babel hide
    --image` is a takedown, and a comment can cite the very URL the article
    hid to route straight around it. No blob or digest is exposed by this --
    the bug is only that the link and caption are wrong, not that content
    leaks -- but the takedown mechanism should not be bypassable through the
    comment thread it does not otherwise touch.

    Both the article body and a comment cite the identical URL here so the
    two can be compared directly: the article's own placeholder already gets
    this right (test_browse_reads.py covers get_image_map's 'withheld'
    state), so this test only has to show the comment now matches it.
    """
    await _article(pool, 2113, body="see the image")
    digest = bytes.fromhex("7a" * 32)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE articles SET body_raw = $1 WHERE id = 2113",
            '<p><img src="https://h/withheld.png"></p>',
        )
        await conn.execute(
            "INSERT INTO images (sha256, bytes, mime, withheld_at) "
            "VALUES ($1, 3, 'image/png', now())",
            digest,
        )
        await conn.execute(
            """INSERT INTO article_images (article_id, position, source_url, status, sha256)
               VALUES (2113, 0, 'https://h/withheld.png', 'ok', $1)""",
            digest,
        )
        await conn.execute(
            """INSERT INTO comments (id, article_id, position, depth, author_name,
                                     body, body_raw)
               VALUES (301, 2113, 0, 0, 'bob', 'plain fallback',
                       '<p><img src="https://h/withheld.png"></p>')"""
        )
    body = (await client.get("/article/2113")).text
    assert body.count("Image not available") == 2  # article body + comment
    assert 'href="https://h/withheld.png"' not in body
