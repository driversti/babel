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
