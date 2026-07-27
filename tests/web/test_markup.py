"""What may leave this module, and what may not.

body_raw is written by anyone with an eRepublik account. These tests assert the
output alphabet, not the implementation: the invariant test at the bottom is
the one that catches a case nobody thought of.
"""

import pytest
from selectolax.parser import HTMLParser

from babel.web.markup import ALLOWED_ATTRIBUTES, EMITTED_TAGS, render_body


def html(raw: str) -> str:
    return str(render_body(raw, {}).html)


def test_emphasis_is_normalised_to_one_tag_each():
    assert "<strong>bold</strong>" in html("<p><b>bold</b></p>")
    assert "<em>it</em>" in html("<p><i>it</i></p>")
    assert "<strong>bold</strong>" in html("<p><strong>bold</strong></p>")


def test_underline_and_strike_survive():
    assert "<u>u</u>" in html("<p><u>u</u></p>")
    assert "<s>s</s>" in html("<p><strike>s</strike></p>")


def test_h1_becomes_h2_so_the_article_title_keeps_h1():
    assert "<h2>Head</h2>" in html("<h1>Head</h1>")


def test_an_unrecognised_tag_is_unwrapped_and_its_text_kept():
    assert "keep me" in html("<p><marquee>keep me</marquee></p>")
    assert "<marquee" not in html("<p><marquee>keep me</marquee></p>")


def test_the_emoji_element_unwraps_to_its_character():
    out = html('<p>100<q class="emoji emoji_1f635">\U0001f635</q></p>')
    assert "\U0001f635" in out
    assert "<q" not in out
    assert "emoji_1f635" not in out


@pytest.mark.parametrize("tag", ["script", "style", "iframe", "svg", "noscript", "template"])
def test_dangerous_elements_are_dropped_with_their_children(tag):
    out = html(f"<p>before<{tag}>SECRET</{tag}>after</p>")
    assert "SECRET" not in out
    assert "before" in out
    assert "after" in out


def test_text_is_escaped():
    out = html("<p>a &lt; b &amp; c \"d\"</p>")
    assert "&lt;" in out
    assert "<b " not in out


def test_a_literal_script_tag_in_a_text_node_cannot_escape():
    out = html("<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>")
    assert "<script>" not in out


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "java\tscript:alert(1)",
        "java&#9;script:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox",
        "//evil.example/x",
        "/relative",
        "",
    ],
)
def test_a_link_we_will_not_follow_becomes_plain_text(href):
    out = html(f'<p><a href="{href}">click</a></p>')
    assert "click" in out
    assert "<a " not in out


def test_an_http_link_survives_with_the_archive_rel():
    out = html('<p><a href="https://example.org/x">click</a></p>')
    assert 'href="https://example.org/x"' in out
    assert 'rel="nofollow noreferrer ugc"' in out
    assert 'target="_blank"' in out


def test_a_quote_in_an_href_cannot_break_out_of_the_attribute():
    out = html('<p><a href="https://e.org/&quot; onmouseover=&quot;alert(1)">x</a></p>')
    assert "onmouseover" not in out or "&#34;" in out
    assert 'onmouseover="alert(1)"' not in out


@pytest.mark.parametrize("attr", ["onclick", "style", "srcset", "formaction", "id", "class"])
def test_input_attributes_do_not_survive(attr):
    out = html(f'<p><strong {attr}="x">t</strong></p>')
    assert attr not in out


def test_depth_beyond_the_ceiling_flattens_to_text_rather_than_recursing():
    deep = "<p>" + "<span>" * 400 + "deep" + "</span>" * 400 + "</p>"
    out = html(deep)
    assert "deep" in out
    assert "<span" not in out


def test_truncated_markup_does_not_raise():
    assert "Alpha" in html('<div class="postBody"><p>Alpha<b>bo')


def test_empty_markup_renders_nothing():
    assert html("") == ""


HOSTILE = [
    '<p><img src="x" onerror="alert(1)"></p>',
    "<p><svg><script>alert(1)</script></svg></p>",
    '<p><a href="javascript:alert(1)">x</a></p>',
    '<p><form action="/x"><input name="a"></form></p>',
    '<p><meta http-equiv="refresh" content="0;url=http://evil"></p>',
    "<p><base href=\"http://evil/\"></p>",
    '<p><object data="http://evil/x"></object></p>',
    "<p><style>body{display:none}</style></p>",
    '<p><div style="position:fixed;inset:0">overlay</div></p>',
    "<p>" + "<b>" * 300 + "x" + "</b>" * 300 + "</p>",
]


@pytest.mark.parametrize("raw", HOSTILE)
def test_output_alphabet_holds_for_hostile_input(raw):
    """Parse the output back: every tag and attribute must be one of ours.

    This is the test that catches the case nobody anticipated. The two defects
    this project shipped and found live both looked correct to a test that
    asserted on the intended path only.
    """
    tree = HTMLParser(str(render_body(raw, {}).html))
    for node in tree.css("*"):
        if node.tag in ("html", "head", "body", "-text"):
            continue
        assert node.tag in EMITTED_TAGS, f"{node.tag} escaped the allowlist"
        for name in node.attributes:
            assert name in ALLOWED_ATTRIBUTES, f"{name} escaped the allowlist"
