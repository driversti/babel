from babel.crawler.fetcher import FetchResult, fetch_article


async def test_200_with_a_body_is_ok():
    async def get(url):
        return 200, '<div class="postBody">hi</div>'

    result: FetchResult = await fetch_article(get, "http://x", max_attempts=3, backoff_sec=0)
    assert result.status == "ok"
    assert "postBody" in result.html


async def test_404_is_missing_and_is_not_retried():
    calls = {"n": 0}

    async def get(url):
        calls["n"] += 1
        return 404, "not found"

    result: FetchResult = await fetch_article(get, "http://x", max_attempts=3, backoff_sec=0)
    assert result.status == "missing"
    assert calls["n"] == 1


async def test_200_without_a_body_is_an_error_not_a_success():
    async def get(url):
        return 200, "<html>some interstitial</html>"

    result: FetchResult = await fetch_article(get, "http://x", max_attempts=2, backoff_sec=0)
    assert result.status == "error"


async def test_transient_failures_are_retried_then_succeed():
    calls = {"n": 0}

    async def get(url):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("slow")
        return 200, '<div class="postBody">hi</div>'

    result: FetchResult = await fetch_article(get, "http://x", max_attempts=5, backoff_sec=0)
    assert result.status == "ok"
    assert calls["n"] == 3


async def test_gives_up_after_max_attempts():
    async def get(url):
        raise TimeoutError("always slow")

    result: FetchResult = await fetch_article(get, "http://x", max_attempts=3, backoff_sec=0)
    assert result.status == "error"
    assert "TimeoutError" in result.error


async def test_cloudflare_challenge_is_reported_distinctly():
    async def get(url):
        return 403, "Just a moment... cf_chl"

    result: FetchResult = await fetch_article(get, "http://x", max_attempts=1, backoff_sec=0)
    assert result.status == "error"
    assert "cloudflare" in result.error.lower()
