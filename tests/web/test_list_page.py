import datetime
import re
import urllib.parse

from babel.web.cursor import game_date_to_utc

UTC = datetime.UTC


async def _seed(pool, rows):
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO articles (id, title, body, author_name, country,
                                     published_at, comment_count)
               VALUES ($1, $2, 'Body', $3, $4, $5, 0)
               ON CONFLICT (id) DO NOTHING""",
            rows,
        )


async def test_list_shows_titles_newest_first(client, pool):
    await _seed(pool, [
        (10, "Older", "ann", "Poland", datetime.datetime(2026, 1, 1, tzinfo=UTC)),
        (11, "Newer", "ann", "Poland", datetime.datetime(2026, 1, 2, tzinfo=UTC)),
    ])
    body = (await client.get("/")).text
    assert body.index("Newer") < body.index("Older")


async def test_country_filter_narrows_the_list(client, pool):
    await _seed(pool, [
        (20, "PL", "ann", "Poland", datetime.datetime(2026, 2, 1, tzinfo=UTC)),
        (21, "RS", "ann", "Serbia", datetime.datetime(2026, 2, 2, tzinfo=UTC)),
    ])
    body = (await client.get("/", params={"country": "Poland"})).text
    assert "PL" in body
    assert "RS" not in body


async def test_dates_render_in_game_time(client, pool):
    # 2026-07-21 05:53 UTC is game day 20 July. A UTC render would say 21.
    await _seed(pool, [
        (30, "Evening", "ann", "Bulgaria", datetime.datetime(2026, 7, 21, 5, 53, tzinfo=UTC)),
    ])
    body = (await client.get("/")).text
    assert "2026-07-20" in body
    assert "2026-07-21" not in body


async def test_coverage_line_states_the_span_and_the_frontier(client, pool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crawl_cursor (name, next_id) VALUES ('backfill', 2785850) "
            "ON CONFLICT (name) DO UPDATE SET next_id = 2785850"
        )
    body = (await client.get("/")).text
    assert "2 785 850" in body or "2785850" in body
    assert "not in the archive yet" in body


async def test_author_link_round_trips_a_hostile_name(client, pool):
    hostile = 'a"><img src=x onerror=alert(1)>#&+ b'
    await _seed(pool, [
        (40, "Hostile", hostile, "Poland", datetime.datetime(2026, 3, 1, tzinfo=UTC)),
    ])
    page = (await client.get("/")).text
    assert '"><img src=x' not in page          # no attribute break
    assert "onerror=alert(1)" not in page or "&lt;img" in page

    # The generated link must actually filter to that author. |e alone would
    # truncate at the '#' and silently return the unfiltered list.
    expected = "/?author=" + urllib.parse.quote(hostile, safe="")
    assert expected in page.replace("&amp;", "&")
    filtered = (await client.get("/", params={"author": hostile})).text
    assert "Hostile" in filtered


async def test_unknown_author_offers_prefix_suggestions(client, pool):
    await _seed(pool, [
        (50, "A", "annabelle", "Poland", datetime.datetime(2026, 4, 1, tzinfo=UTC)),
    ])
    body = (await client.get("/", params={"author": "anna"})).text
    assert "annabelle" in body


async def test_malformed_cursor_redirects_rather_than_erroring(client):
    response = await client.get("/", params={"after": "not-a-cursor"}, follow_redirects=False)
    assert response.status_code == 302
    assert "after" not in response.headers["location"]


async def test_pager_link_appears_only_when_there_is_another_page(client, pool):
    await _seed(pool, [
        (60 + i, f"P{i}", "ann", "Poland",
         datetime.datetime(2026, 5, 1, tzinfo=UTC) + datetime.timedelta(seconds=i))
        for i in range(3)
    ])
    body = (await client.get("/")).text
    assert "after=" not in body  # three rows, page size 50


async def test_empty_result_explains_coverage_instead_of_showing_nothing(client):
    body = (await client.get("/", params={"country": "Nowhere"})).text
    assert "not in the archive yet" in body


async def test_date_jump_includes_the_whole_day_newest_first(client, pool):
    """A `?on=` jump must not drop the day it names.

    Regression for a direction mismatch: `game_date_to_utc(jump)` is the
    *start* of the day, but the descending page fetches rows strictly
    *before* the cursor, so anchoring there put every article on `jump`
    itself on the wrong side of "<" — the reader asked for this day and got
    the one before it instead.
    """
    day = datetime.date(2026, 8, 10)
    start = game_date_to_utc(day)
    next_start = game_date_to_utc(day + datetime.timedelta(days=1))
    await _seed(pool, [
        (400, "PrevDay", "ann", "Poland", start - datetime.timedelta(minutes=1)),
        (401, "FirstMinute", "ann", "Poland", start),
        (402, "LastMinute", "ann", "Poland", next_start - datetime.timedelta(minutes=1)),
        (403, "NextDay", "ann", "Poland", next_start),
    ])
    body = (await client.get("/", params={"on": "2026-08-10"})).text
    assert "FirstMinute" in body
    assert "LastMinute" in body
    assert "NextDay" not in body
    # Newest-first: the requested day leads, most recent first, then earlier
    # days continue below it.
    assert body.index("LastMinute") < body.index("FirstMinute") < body.index("PrevDay")


async def test_date_jump_includes_the_whole_day_oldest_first(client, pool):
    """The same jump, sorted the other way — already correct, guarded here too."""
    day = datetime.date(2026, 8, 10)
    start = game_date_to_utc(day)
    next_start = game_date_to_utc(day + datetime.timedelta(days=1))
    await _seed(pool, [
        (410, "PrevDay", "ann", "Poland", start - datetime.timedelta(minutes=1)),
        (411, "FirstMinute", "ann", "Poland", start),
        (412, "LastMinute", "ann", "Poland", next_start - datetime.timedelta(minutes=1)),
        (413, "NextDay", "ann", "Poland", next_start),
    ])
    body = (await client.get("/", params={"on": "2026-08-10", "order": "old"})).text
    assert "FirstMinute" in body
    assert "LastMinute" in body
    assert "PrevDay" not in body
    # Oldest-first: the requested day leads, earliest first, then later days
    # continue below it.
    assert body.index("FirstMinute") < body.index("LastMinute") < body.index("NextDay")


async def test_pager_labels_track_the_chosen_order(client, pool):
    """`order=old` continues towards newer articles, and the label must say so.

    Previously hardcoded to "older →" / "← newer", which is only true
    under `order=new` — under `order=old` the labels pointed the reader the
    wrong way.
    """
    await _seed(pool, [
        (500 + i, f"P{i}", "ann", "Poland",
         datetime.datetime(2026, 9, 1, tzinfo=UTC) + datetime.timedelta(seconds=i))
        for i in range(55)
    ])

    first = (await client.get("/", params={"order": "old"})).text
    assert "P0" in first
    assert "P54" not in first
    assert "newer →" in first
    assert "older →" not in first

    match = re.search(r'href="(/\?after=[^"]+)"', first)
    assert match, "expected a next-page link on the first oldest-first page"
    second = (await client.get(match.group(1))).text
    assert "P54" in second
    assert "P0" not in second
    assert "← older" in second


async def test_date_jump_at_the_end_of_the_calendar_does_not_500(client, pool):
    """`jump + timedelta(days=1)` overflows past `date.max`; that must not reach
    the reader as a crash.

    Reproduced live by the coordinator: `?on=9999-12-31` under `order=new`
    raised an unhandled `OverflowError`, caught only by the app's catch-all
    500 handler. Before the newest-first date-jump fix, this exact input did
    no arithmetic on `jump` at all and was harmless — a regression introduced
    by that fix, not a pre-existing gap. A reader who types the calendar's
    own edge into the date-jump field should get the list, not an error
    page, under either sort order.
    """
    await _seed(pool, [
        (600, "Ordinary", "ann", "Poland", datetime.datetime(2026, 1, 1, tzinfo=UTC)),
    ])
    for order in ("new", "old"):
        response = await client.get("/", params={"on": "9999-12-31", "order": order})
        assert response.status_code == 200
        assert "<html" in response.text.lower()
        assert "Traceback" not in response.text


_PAGER_RE = re.compile(r'<nav class="pager">(.*?)</nav>', re.S)
_LINK_RE = re.compile(r'href="(/\?(after|before)=[^"]*)"')
_TITLE_RE = re.compile(r'<a class="title" href="/article/(\d+)">')

END_MESSAGE = "That is everything collected so far."


def _pager(body: str) -> dict[str, str]:
    """The pager's two links by direction, `after` (forward) and `before` (back).

    Read out of the rendered page rather than asserted on the handler's locals,
    because the defect this guards was invisible at the row level: the pages
    held exactly the right rows, and only the links between them were wrong.
    """
    nav = _PAGER_RE.search(body)
    assert nav is not None, "the page has no pager at all"
    return {m[2]: m[1].replace("&amp;", "&") for m in _LINK_RE.finditer(nav[1])}


def _ids(body: str) -> list[str]:
    return _TITLE_RE.findall(body)


async def _seed_three_pages(pool, base_id, tag):
    """101 rows under their own country tag: two full pages of 50, then one.

    101 rather than 51 so the walk can reach a genuine end of list and the
    "everything collected" message can be checked where it is true as well as
    where it is not. The tag is what every test here filters on, so each one
    pages through only its own rows whatever else the database holds.
    """
    await _seed(pool, [
        (base_id + i, f"P{i:03d}", "ann", tag,
         datetime.datetime(2026, 10, 1, tzinfo=UTC) + datetime.timedelta(seconds=i))
        for i in range(101)
    ])


async def test_paging_back_keeps_the_way_forward_newest_first(client, pool):
    """Forward one page, back one page — the way forward must still be there.

    `page.has_more` describes the direction the query was *fetched* in. Gating
    the forward link on it is right going forward and wrong coming back, where
    it means "more rows further back". The top page always holds exactly
    PAGE_SIZE rows above page 2's first row, so on the way back has_more is
    always False there: every backward navigation lost the "older →" link,
    gained a "← newer" link onto an empty page, and printed the end-of-list
    message above 51 further articles.
    """
    await _seed_three_pages(pool, 700, "PageNew")

    first = (await client.get("/", params={"country": "PageNew"})).text
    links = _pager(first)
    assert "after" in links
    assert "before" not in links, "the top page has nothing newer than itself"
    assert END_MESSAGE not in first

    second = (await client.get(links["after"])).text
    assert _ids(second) != _ids(first)
    second_links = _pager(second)
    assert "after" in second_links
    assert "before" in second_links

    back = (await client.get(second_links["before"])).text
    assert _ids(back) == _ids(first), "paging back must land on the rows it came from"
    back_links = _pager(back)
    assert "after" in back_links, "paging back must not destroy the forward link"
    assert "before" not in back_links, "there is still nothing newer than the top page"
    assert END_MESSAGE not in back

    # And the restored link is not merely present — it goes where it says.
    forward_again = (await client.get(back_links["after"])).text
    assert _ids(forward_again) == _ids(second)


async def test_paging_back_keeps_the_way_forward_oldest_first(client, pool):
    """The same walk under `order=old`, the direction combination with no test."""
    await _seed_three_pages(pool, 900, "PageOld")

    first = (await client.get("/", params={"country": "PageOld", "order": "old"})).text
    links = _pager(first)
    assert "after" in links
    assert "before" not in links
    assert END_MESSAGE not in first

    second = (await client.get(links["after"])).text
    assert _ids(second) != _ids(first)
    back = (await client.get(_pager(second)["before"])).text
    assert _ids(back) == _ids(first)
    back_links = _pager(back)
    assert "after" in back_links, "paging back must not destroy the forward link"
    assert "before" not in back_links
    assert END_MESSAGE not in back

    forward_again = (await client.get(back_links["after"])).text
    assert _ids(forward_again) == _ids(second)


async def test_the_end_of_the_list_is_claimed_only_at_the_end(client, pool):
    """Walk to the real last page, then back off it.

    The end-of-list message is gated on the forward link being absent, so it
    inherits whatever that gate gets wrong. Three pages of 101 rows: it belongs
    on the third and nowhere else, in either sort order.
    """
    for order, base_id, tag in (("new", 1100, "EndNew"), ("old", 1300, "EndOld")):
        await _seed_three_pages(pool, base_id, tag)
        params = {"country": tag, "order": order}

        pages = [(await client.get("/", params=params)).text]
        while "after" in _pager(pages[-1]):
            pages.append((await client.get(_pager(pages[-1])["after"])).text)

        assert len(pages) == 3, f"{order}: expected 101 rows to page as 50/50/1"
        assert [len(_ids(p)) for p in pages] == [50, 50, 1]
        assert [END_MESSAGE in p for p in pages] == [False, False, True]

        # Now walk all the way back. No page reached by a `before=` link is the
        # end of the archive — the reader is standing on the page that follows
        # it — and every one of them must offer the way forward again.
        walked_back = [pages[-1]]
        while "before" in _pager(walked_back[-1]):
            walked_back.append((await client.get(_pager(walked_back[-1])["before"])).text)
        for step, page in enumerate(walked_back[1:], start=1):
            assert END_MESSAGE not in page, f"{order}: step {step} back is not the end"
            assert "after" in _pager(page), f"{order}: step {step} back lost the way forward"

        # Every row seen once on the way down, once on the way back.
        assert sorted(i for p in pages for i in _ids(p)) == sorted(
            i for p in walked_back for i in _ids(p)
        )


async def test_date_jump_at_the_start_of_the_calendar_does_not_500(client, pool):
    """The other end of the representable range.

    `date.min + timedelta(days=1)` does not overflow, so this end was never
    actually broken — included so a future change to the day-jump arithmetic
    can't quietly regress it without a test noticing.
    """
    await _seed(pool, [
        (601, "Ordinary", "ann", "Poland", datetime.datetime(2026, 1, 1, tzinfo=UTC)),
    ])
    for order in ("new", "old"):
        response = await client.get("/", params={"on": "0001-01-01", "order": order})
        assert response.status_code == 200
        assert "<html" in response.text.lower()
        assert "Traceback" not in response.text
