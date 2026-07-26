from babel.crawler.poller import newest_article_id, parse_rss_ids, poll_once
from babel.db import repo


def _rss(article_ids: list[int]) -> str:
    items = "".join(
        f"<item><link>https://www.erepublik.com/en/article/slug-{i}/1/20</link></item>"
        for i in article_ids
    )
    return f"<rss><channel><link>https://erepublik.com/rss</link>{items}</channel></rss>"

RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
  <title><![CDATA[eRepublik News]]></title>
  <link>https://erepublik.com/en/main/news/latest/all/all/1/rss</link>
  <item>
    <title><![CDATA[One]]></title>
    <link>https://www.erepublik.com/en/article/one-2797019/1/20</link>
    <pubDate>Sun, 26 Jul 2026 01:00:33 -0700</pubDate>
  </item>
  <item>
    <title><![CDATA[Two]]></title>
    <link>https://www.erepublik.com/en/article/two-2797018/1/20</link>
    <pubDate>Sat, 25 Jul 2026 17:19:48 -0700</pubDate>
  </item>
</channel></rss>
"""


def test_extracts_article_ids_from_item_links_only():
    # The <channel><link> is not an article and must not be picked up.
    assert parse_rss_ids(RSS) == [2797019, 2797018]


def test_ignores_a_malformed_feed():
    assert parse_rss_ids("<rss><channel></channel></rss>") == []


async def test_poll_ingests_only_unseen_ids(pg):
    await repo.record_fetch(pg, 2797019, "ok")
    seen: list[int] = []

    async def ingest(article_id: int) -> str:
        seen.append(article_id)
        return "ok"

    async def fetch_rss(page: int) -> str:
        return RSS if page == 1 else "<rss><channel></channel></rss>"

    ingested = await poll_once(pg, ingest, fetch_rss, pages=2)
    assert seen == [2797018]
    assert ingested == [2797018]


async def test_ingest_failure_does_not_stop_other_ids(pg):
    seen: list[int] = []

    async def ingest(article_id: int) -> str:
        if article_id == 2797019:
            raise RuntimeError("boom")
        seen.append(article_id)
        return "ok"

    async def fetch_rss(page: int) -> str:
        return RSS if page == 1 else "<rss><channel></channel></rss>"

    ingested = await poll_once(pg, ingest, fetch_rss, pages=2)

    assert seen == [2797018]
    assert ingested == [2797018]
    assert await pg.fetchval("SELECT status FROM fetch_log WHERE article_id = 2797019") == "error"


async def test_errored_id_is_retried_on_a_later_poll(pg):
    # Successes must record 'ok' the way the real Ingestor does (poll_once
    # itself only writes fetch_log on failure), or the second poll would treat
    # every ID as never-seen instead of exercising the retry-only-errors path.
    async def failing_ingest(article_id: int) -> str:
        if article_id == 2797019:
            raise RuntimeError("boom")
        await repo.record_fetch(pg, article_id, "ok")
        return "ok"

    async def fetch_rss(page: int) -> str:
        return RSS if page == 1 else "<rss><channel></channel></rss>"

    await poll_once(pg, failing_ingest, fetch_rss, pages=2)
    assert await pg.fetchval("SELECT status FROM fetch_log WHERE article_id = 2797019") == "error"

    seen: list[int] = []

    async def ingest(article_id: int) -> str:
        seen.append(article_id)
        return "ok"

    # A later poll cycle over the same feed page must bring the previously
    # errored article back; 2797018 already succeeded and must not repeat.
    ingested = await poll_once(pg, ingest, fetch_rss, pages=2)
    assert seen == [2797019]
    assert ingested == [2797019]


async def test_poll_deduplicates_ids_appearing_on_several_pages(pg):
    seen: list[int] = []

    async def ingest(article_id: int) -> str:
        seen.append(article_id)
        return "ok"

    async def fetch_rss(page: int) -> str:
        return RSS

    await poll_once(pg, ingest, fetch_rss, pages=3)
    assert sorted(seen) == [2797018, 2797019]


async def test_newest_article_id_is_the_highest_in_the_feed():
    """The feed is ordered newest-first in practice, but the backfill's starting
    point must not depend on that — an out-of-order feed would silently skip
    every article above the first entry."""

    async def fetch_rss(page: int) -> str:
        assert page == 1, "only the first page is needed to find the newest article"
        return _rss([2797020, 2797025, 2797011])

    assert await newest_article_id(fetch_rss) == 2797025


async def test_newest_article_id_refuses_an_empty_feed():
    """Returning 0 or None here would seed the cursor below every article and the
    walk would idle forever having collected nothing."""

    async def fetch_rss(page: int) -> str:
        return "<rss><channel></channel></rss>"

    try:
        await newest_article_id(fetch_rss)
    except ValueError as e:
        assert "no article" in str(e).lower()
    else:
        raise AssertionError("expected ValueError")
