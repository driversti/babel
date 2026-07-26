from babel.config import Settings


def test_defaults_are_conservative():
    s = Settings(_env_file=None)
    assert s.requests_per_second == 1.0
    assert s.home_country == "XX"
    assert s.comments_per_page == 1000
    assert s.min_free_bytes > 0


def test_article_url_requests_all_comments():
    s = Settings(_env_file=None)
    assert s.article_url(2797019) == "https://www.erepublik.com/en/article/2797019/1/1000"


def test_rss_url_uses_latest_sorting():
    s = Settings(_env_file=None)
    assert s.rss_url(3) == "https://www.erepublik.com/en/main/news/latest/all/all/3/rss"
