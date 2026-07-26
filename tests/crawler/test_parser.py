import datetime
import pathlib

import pytest

from babel.crawler.parser import eday_to_date, parse_article

FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("eday", "expected"),
    [
        (1, datetime.date(2007, 11, 21)),
        (705, datetime.date(2009, 10, 25)),
        (3576, datetime.date(2017, 9, 4)),
        (6819, datetime.date(2026, 7, 22)),
        (6822, datetime.date(2026, 7, 25)),
    ],
)
def test_eday_to_date(eday, expected):
    assert eday_to_date(eday) == expected


def test_parses_core_fields():
    article = parse_article(load("article_indonesia.html"), 2797015)
    assert article is not None
    assert article.id == 2797015
    assert article.title.startswith("Strategi Menjadi Pengusaha Food Q2")
    assert article.country == "Indonesia"
    assert article.author_id and article.author_id > 0
    assert article.author_name
    assert article.e_day > 6000
    assert article.published_at.tzinfo is not None


def test_body_is_plain_text_without_markup():
    article = parse_article(load("article_indonesia.html"), 2797015)
    assert "Dalam dunia eRepublik" in article.body
    assert "<" not in article.body
    assert len(article.body) > 500


def test_collects_image_references_in_order():
    article = parse_article(load("article_with_images.html"), 2797005)
    assert len(article.images) == 2
    assert [i.position for i in article.images] == [0, 1]
    assert all(i.source_url.startswith(("http://", "https://", "//")) for i in article.images)


def test_an_article_without_images_yields_an_empty_tuple():
    article = parse_article(load("article_indonesia.html"), 2797015)
    assert article.images == ()


def test_comment_count_comes_from_the_meta_description():
    article = parse_article(load("article_with_images.html"), 2797005)
    assert article.comment_count == 15


def test_returns_none_for_a_404_page():
    assert parse_article("<html><title>404 - Not Found | eRepublik</title></html>", 1) is None
