import datetime
import pathlib

import pytest
from selectolax.parser import HTMLParser

from babel.crawler.parser import MAX_BODY_RAW_CHARS, _body_text, eday_to_date, parse_article, parse_comments

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


def test_br_with_an_attribute_still_breaks_a_line():
    """Nineteen years of hand-written and WYSIWYG-generated markup includes
    <br> tags carrying attributes, e.g. <br clear="all"> to clear a float.
    A regex anchored on an immediate '>' misses these and silently welds
    the surrounding text back together -- the exact defect this function
    exists to fix, just on a shape none of the three fixtures happen to
    contain.
    """
    assert _body('Alpha<br clear="all">Beta') == "Alpha\nBeta"


def test_self_closing_br_with_an_attribute_still_breaks_a_line():
    assert _body('Alpha<br class="clear" />Beta') == "Alpha\nBeta"


def test_br_attribute_containing_a_gt_does_not_end_the_match_early():
    """A naive '[^>]*' body for the attribute run stops at the first '>' it
    sees, including one inside a quoted attribute value -- so
    <br title="a>b"> would be cut into <br title="a> (matched, turned into a
    newline) followed by the literal text b">, leaking raw markup into the
    stored body. Because raw HTML is never archived, a body corrupted this
    way cannot be repaired except by re-fetching the article. The pattern
    must treat a quoted value as one unit and consume it whole.
    """
    result = _body('Alpha<br title="a>b">Beta')
    assert result == "Alpha\nBeta"
    assert "<" not in result
    assert ">" not in result


def test_br_is_matched_case_insensitively():
    assert _body("Alpha<BR>Beta") == "Alpha\nBeta"


def test_a_tag_merely_starting_with_br_is_not_treated_as_a_break():
    """<br\\b...> must not match <brand> or <broken> -- only a real <br>
    tag, bare or with attributes, counts as a line break.
    """
    assert _body("Alpha<brand>Beta</brand>") == "Alpha Beta"
    assert _body("Alpha<broken>Beta</broken>") == "Alpha Beta"


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


def test_body_raw_keeps_the_markup_the_game_served():
    article = parse_article(load("article_with_images.html"), 2797005)
    assert "<b>1000 Q7</b>" in article.body_raw
    assert "<u>30 ve" in article.body_raw
    assert "<br>" in article.body_raw
    assert 'src="https://resmim.net/cdn/2026/07/25/ECBQFR.png"' in article.body_raw


def test_body_raw_is_the_outer_node_so_no_string_surgery_is_needed():
    article = parse_article(load("article_with_images.html"), 2797005)
    assert article.body_raw.startswith('<div class="postBody"')


def test_body_text_is_still_stripped_when_body_raw_is_captured():
    article = parse_article(load("article_with_images.html"), 2797005)
    assert "<" not in article.body
    assert "1000 Q7" in article.body


def test_body_raw_is_truncated_at_the_ceiling(caplog):
    filler = "x" * (MAX_BODY_RAW_CHARS + 500)
    html = load("article_with_images.html").replace(
        '<div class="postBody">', f'<div class="postBody">{filler}', 1
    )
    article = parse_article(html, 2797005)
    assert len(article.body_raw) == MAX_BODY_RAW_CHARS
    assert "truncated" in caplog.text


def test_comment_body_raw_keeps_the_link_markup():
    comments = parse_comments(load("article_with_images.html"))
    first = comments[0]
    assert "<br>" in first.body_raw
    assert 'href="https://www.erepublik.com/tr/article/2797005"' in first.body_raw


def test_a_removed_comment_has_no_body_raw():
    html = """
    <div id="comment1" class="commentWrapper"><div><div style="padding-left:0px;">
      <div class="details"><span>Day 6,819, 21:34</span><p>[removed]</p></div>
    </div></div></div>
    """
    comment = parse_comments(html)[0]
    assert comment.body is None
    assert comment.body_raw is None
