import datetime
import pathlib

import pytest
from selectolax.parser import HTMLParser

from babel.crawler.parser import _body_text, eday_to_date, parse_article

FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _body(fragment: str) -> str:
    node = HTMLParser(f"<div class='postBody'>{fragment}</div>").css_first("div.postBody")
    return _body_text(node)


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


def test_author_id_is_matched_by_name_not_by_first_commenter():
    """author_id is recovered by matching author_name against citizen-profile
    links anywhere in the document -- there is no id next to the byline
    itself. In this fixture the first citizen-profile link in the document
    belongs to K0rsakoff (the first commenter), not the author, NoFly11. If
    parse_article ever took the first link instead of matching by name, this
    would misattribute the article to the wrong citizen.
    """
    article = parse_article(load("article_with_comments.html"), 2796950)
    assert article is not None
    assert article.author_name == "NoFly11"
    assert article.author_id == 9017473


def test_author_id_stays_none_when_the_author_never_commented():
    """When the author never shows up as a commenter, no citizen-profile link
    matches their name, so author_id must stay None rather than being
    attached to whichever citizen link happens to be on the page.
    """
    html = """
    <html>
    <head>
    <title>Some Title - published by Ghostwriter on day 6,800 - page 1 of 1</title>
    </head>
    <body>
    <div class="postContent" itemType="https://schema.org/Article">
        <h2><a href="/en/article/-1-1/1/1">Some Title</a></h2>
        <meta itemprop="datePublished" content="Jul 21 2026 05:53:10 GMT" />
        <meta itemprop="author" content="Ghostwriter" />
        <div>
            <span>
                <em>5 days ago</em>
                <span>&bull;</span>
                <a href="/en/main/news/latest/all/Bulgaria/1">
                    Published in Bulgaria <img alt="Bulgaria" src="//example.com/Bulgaria.png" />
                </a>
                <span>&bull;</span>
                <em>by <q>Ghostwriter</q></em>
            </span>
        </div>
        <div class="postBody">Some article body text.</div>
    </div>
    <div class="commentsSection">
        <div class="authorWrapper">
            <a title="SomeoneElse" href="/en/citizen/profile/12345">SomeoneElse</a>
        </div>
    </div>
    </body>
    </html>
    """
    article = parse_article(html, 999)
    assert article is not None
    assert article.author_name == "Ghostwriter"
    assert article.author_id is None
    # Explicitly guard against misattributing the unrelated commenter's id.
    assert article.author_id != 12345


def test_paragraphs_become_separate_lines():
    assert _body("<p>First.</p><p>Second.</p>") == "First.\nSecond."


def test_br_breaks_a_line():
    assert _body("Alpha<br>Beta") == "Alpha\nBeta"


def test_double_br_keeps_one_blank_line():
    assert _body("Alpha<br><br>Beta") == "Alpha\n\nBeta"


def test_runs_of_blank_lines_collapse_to_one():
    assert _body("<p>A</p><br><br><br><p>B</p>") == "A\n\nB"


def test_list_items_are_separate_lines():
    assert _body("<ul><li>one</li><li>two</li></ul>") == "one\ntwo"


def test_heading_is_not_welded_to_the_next_sentence():
    text = _body("<h3>Mendirikan Perusahaan</h3><p>Langkah pertama.</p>")
    assert text == "Mendirikan Perusahaan\nLangkah pertama."


def test_inline_markup_does_not_break_a_line():
    assert _body("<p>A <b>bold</b> word.</p>") == "A bold word."


def test_real_fixture_gains_line_breaks():
    article = parse_article(load("article_indonesia.html"), 123)
    assert article is not None
    assert "\n" in article.body
    assert "<" not in article.body


def test_author_name_falls_back_to_the_title_byline_when_meta_author_is_missing():
    """meta[itemprop=author] is present on every fixture in this suite, so
    the <title> fallback in _parse_author_name never runs otherwise. This
    exercises that path directly by omitting the meta tag.
    """
    html = """
    <html>
    <head>
    <title>Some Title - published by Ghostwriter on day 6,800 - page 1 of 1</title>
    </head>
    <body>
    <div class="postContent" itemType="https://schema.org/Article">
        <h2><a href="/en/article/-1-1/1/1">Some Title</a></h2>
        <meta itemprop="datePublished" content="Jul 21 2026 05:53:10 GMT" />
        <div>
            <span>
                <em>5 days ago</em>
                <span>&bull;</span>
                <a href="/en/main/news/latest/all/Bulgaria/1">
                    Published in Bulgaria <img alt="Bulgaria" src="//example.com/Bulgaria.png" />
                </a>
                <span>&bull;</span>
                <em>by <q>Ghostwriter</q></em>
            </span>
        </div>
        <div class="postBody">Some article body text.</div>
    </div>
    </body>
    </html>
    """
    article = parse_article(html, 1000)
    assert article is not None
    assert article.author_name == "Ghostwriter"
