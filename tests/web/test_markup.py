"""What may leave this module, and what may not.

body_raw is written by anyone with an eRepublik account. These tests assert the
output alphabet, not the implementation: the invariant test at the bottom is
the one that catches a case nobody thought of.
"""

import pathlib
import time

import pytest
from selectolax.parser import HTMLParser

from babel.web.markup import ALLOWED_ATTRIBUTES, EMITTED_TAGS, Image, _convert, render_body

FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures"


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
    """Regression guard for a test that used to pass with no ceiling at all.

    `<span>` is unwrapped unconditionally (it's not in KEPT), so 400 levels
    of it stays inert -- both assertions held even with MAX_DEPTH set to
    10**9, because unwrapping never nests output and 400 plain recursive
    calls doesn't come close to exhausting the stack. `<em>` is a KEPT tag,
    so it recurses through Inline objects that really do nest; with the
    ceiling removed, 400 of them raises RecursionError, which is what makes
    this test capable of failing. Asserting the exact count, not just
    presence, pins MAX_DEPTH's value rather than merely its existence: 99,
    because the <p> is processed at depth 0 and each nested <em> descends one
    level, so the 100th <em> (depth 100) is the one the ceiling flattens.
    """
    deep = "<p>" + "<em>" * 400 + "deep" + "</em>" * 400 + "</p>"
    out = html(deep)
    assert "deep" in out
    assert out.count("<em>") == 99


def test_depth_ceiling_flattening_does_not_resurrect_dropped_content():
    """A DROPPED tag sitting past MAX_DEPTH must not leak through node.text().

    The ceiling's flatten path calls node.text(), which walks every
    descendant text node in C -- including ones inside a <script> or
    <style> that would normally never be reached, because the DROPPED check
    in _convert only fires while walking node-by-node and flattening skips
    that walk. render_body now decomposes every DROPPED subtree once, up
    front, so by the time flattening runs there is nothing left under the
    too-deep node for node.text() to resurrect. 120 nested <div>s (unwrapped,
    not KEPT or DROPPED, so they don't hit the per-node DROPPED check
    themselves) puts the payload past MAX_DEPTH=100.
    """
    payload = "<div>" * 120 + "<script>alert('XSSMARKER')</script>VISIBLE" + "</div>" * 120
    out = html(payload)
    assert "XSSMARKER" not in out
    assert "VISIBLE" in out


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
    # The five below reach emissions the first ten never did: an accepted
    # https:// link (the only place author bytes enter an attribute value),
    # a heading, a list, a run of <br>, and a link buried past the depth
    # ceiling. Without these, the invariant sweep below observed only <p>
    # and <strong> across all ten cases and zero attributes, so its own
    # `assert name in ALLOWED_ATTRIBUTES` never executed even once.
    '<p><a href="https://example.org/x?y=&quot; onmouseover=&quot;alert(2)">click</a></p>',
    "<h3><script>alert(1)</script>Heading</h3>",
    '<ul><li onclick="alert(1)">Item</li></ul>',
    '<p>Line1<br onclick="alert(1)">Line2<br><br></p>',
    "<p>" + "<b>" * 150 + '<a href="https://evil.example/">deep link</a>' + "</b>" * 150 + "</p>",
    # These four round out coverage of every EMITTED_TAGS member reachable in
    # this task (em, u, s, blockquote, ol, h2, h4, h5, h6 -- img and span
    # stay unreached until Task 5 actually emits them). Before these, the
    # sweep below reached 7 of 18 EMITTED_TAGS; a tag-alphabet regression in
    # any of these nine could have shipped without the invariant test ever
    # touching it.
    '<p><em onclick="alert(1)">em</em><u style="color:red">u</u>'
    '<s onmouseover="alert(1)">s</s></p>',
    '<blockquote onclick="alert(1)">quote<script>alert(1)</script></blockquote>',
    '<ol><li formaction="/x">item</li></ol>',
    '<h2 onclick="alert(1)">H2</h2><h4 onclick="alert(1)">H4</h4>'
    '<h5 onclick="alert(1)">H5</h5><h6 onclick="alert(1)">H6</h6>',
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


def test_hostile_input_sweep_actually_exercises_attributes():
    """Regression guard: the invariant test above must not vacuously pass.

    Before the five https/heading/list/br/depth-ceiling entries were added,
    the HOSTILE sweep produced only <p> and <strong> tags and zero
    attributes across all ten original cases, so
    `assert name in ALLOWED_ATTRIBUTES` in the test above ran zero times in
    the whole suite -- it could not have caught a hostile byte breaking out
    of the one attribute an author's bytes actually reach, an href.

    The tag check is pinned to every EMITTED_TAGS member this task can
    actually reach -- everything except "img" and "span", which stay
    unreachable until Task 5 renders a real <img> and its own
    "missing-image" <span>. Asserting the full reachable set, not a handful
    of representative tags, is what turns "the sweep observes most of
    EMITTED_TAGS" from a one-time review finding into something a later
    change can't quietly regress.
    """
    seen_tags: set[str] = set()
    seen_attrs: set[str] = set()
    for raw in HOSTILE:
        tree = HTMLParser(str(render_body(raw, {}).html))
        for node in tree.css("*"):
            if node.tag in ("html", "head", "body", "-text"):
                continue
            seen_tags.add(node.tag)
            seen_attrs.update(node.attributes)
    assert seen_attrs, "no HOSTILE payload emitted any attribute"
    assert (EMITTED_TAGS - {"img", "span"}) <= seen_tags


def test_an_image_with_a_disallowed_scheme_is_dropped_before_task_5_sees_it():
    """Image.source_url must be a checked URL, the same way Inline("a").href is.

    render_body can't observe this yet -- _emit still renders every Image as
    Markup("") regardless of validity, because image rendering is Task 5 --
    so this asserts directly on _convert's output, which is part of this
    module's documented interface.
    """
    body = HTMLParser('<p><img src="javascript:alert(1)"></p>').body
    p_node = next(c for c in body.iter(include_text=True) if c.tag == "p")
    img_node = next(c for c in p_node.iter(include_text=True) if c.tag == "img")
    assert _convert(img_node, 0) == ()


def test_an_image_with_an_allowed_scheme_still_produces_an_image_node():
    body = HTMLParser('<p><img src="https://example.org/x.jpg"></p>').body
    p_node = next(c for c in body.iter(include_text=True) if c.tag == "p")
    img_node = next(c for c in p_node.iter(include_text=True) if c.tag == "img")
    assert _convert(img_node, 0) == (Image("https://example.org/x.jpg"),)


def test_a_protocol_relative_image_source_survives_as_the_raw_string():
    """"//host/path" sources are common in older articles (crawler/images.py's
    normalise_url rewrites them to "https://..." for the same reason before
    fetching) and article_images already holds rows keyed on that exact raw
    string. _convert must validate a normalised copy rather than reject the
    raw string for lacking a scheme -- and Image.source_url must stay the
    RAW "//..." form, because Task 5 looks images up by exactly what the
    article wrote.
    """
    body = HTMLParser('<p><img src="//www.erepublik.net/images/x/Turkey.png"></p>').body
    p_node = next(c for c in body.iter(include_text=True) if c.tag == "p")
    img_node = next(c for c in p_node.iter(include_text=True) if c.tag == "img")
    assert _convert(img_node, 0) == (Image("//www.erepublik.net/images/x/Turkey.png"),)


def test_an_image_source_survives_verbatim_including_incidental_whitespace():
    """Image.source_url must match what crawler/parser.py actually stored.

    crawler/parser.py writes img.attributes.get("src") to article_images
    with no .strip() -- so a src carrying surrounding whitespace is stored
    with that whitespace intact. _convert validates a stripped-and-
    normalised COPY (scheme checking tolerates incidental whitespace fine)
    but must store the raw, unstripped attribute value, or Task 5's
    `images.get(item.source_url)` misses a row that's actually on disk.
    """
    body = HTMLParser('<p><img src=" //host/x.png "></p>').body
    p_node = next(c for c in body.iter(include_text=True) if c.tag == "p")
    img_node = next(c for c in p_node.iter(include_text=True) if c.tag == "img")
    assert _convert(img_node, 0) == (Image(" //host/x.png "),)


def test_thousands_of_dropped_elements_render_well_under_a_second():
    """Cost guard, not just correctness -- a correctness-only test would have
    passed on the quadratic version too.

    An earlier version of render_body removed DROPPED subtrees with
    `while (n := root.css_first(sel)) is not None: n.decompose()`, which
    rescans the whole tree on every css_first() call and so costs O(n^2) in
    the number of dropped elements: measured at 25.3s for 8,000 of them,
    long enough to stall the `web` process's async event loop (and its
    /healthz) for every other in-flight request. render_body now uses
    HTMLParser.strip_tags(), which runs in selectolax's C layer and is
    linear. body_raw is capped at 1,000,000 characters (crawler/parser.py),
    so a stored body can hold several times the 4,000-element payload here;
    "well under a second" is a generous ceiling against a ~5ms measurement,
    chosen to be robust to slower CI hardware while still catching a
    regression back to quadratic behaviour.
    """
    raw = "<p>" + "".join(f"<script>x{i}</script>" for i in range(4000)) + "</p>"
    started = time.perf_counter()
    out = html(raw)
    elapsed = time.perf_counter() - started
    assert "x0" not in out and "x3999" not in out
    assert elapsed < 1.0, f"took {elapsed:.2f}s -- DROPPED removal may have regressed to O(n^2)"


def test_a_double_br_starts_a_new_paragraph():
    out = html("<p>Alpha<br><br>Beta</p>")
    assert out.count("<p>") == 2
    assert "<p>Alpha</p>" in out
    assert "<p>Beta</p>" in out


def test_a_single_br_stays_a_line_break_inside_one_paragraph():
    out = html("<p>Alpha<br>Beta</p>")
    assert out.count("<p>") == 1
    assert "Alpha<br>Beta" in out


def test_a_longer_run_of_br_is_still_one_paragraph_break():
    out = html("<p>Alpha<br><br><br><br>Beta</p>")
    assert out.count("<p>") == 2


def test_leading_and_trailing_breaks_produce_no_empty_paragraphs():
    out = html("<p><br><br>Alpha<br><br></p>")
    assert out == "<p>Alpha</p>"


def test_whitespace_only_paragraphs_are_dropped():
    out = html("<p>Alpha<br><br>   <br><br>Beta</p>")
    assert out.count("<p>") == 2


def test_emphasis_survives_the_paragraph_split():
    out = html("<p><b>A</b><br><br>B</p>")
    assert "<p><strong>A</strong></p>" in out


def test_a_real_block_element_ends_the_paragraph():
    out = html("<p>Alpha</p><ul><li>one</li><li>two</li></ul><p>Beta</p>")
    assert "<ul><li>one</li><li>two</li></ul>" in out
    assert "<p>Alpha</p>" in out
    assert "<p>Beta</p>" in out


def test_the_real_fixture_gains_paragraphs():
    """Read the actual fixture, not a hand-inlined stand-in for it.

    The game writes "<br>" newline newline "<br>", not "<br><br>" --
    selectolax parses that newline-newline gap into its own whitespace Text
    node sitting *between* the two Break items. A hand-inlined "<br><br>"
    skips exactly the shape that defeats a naive run-of-Breaks count, so this
    test used to pass against an implementation that could not split a real
    article at all -- it asserted `out.count("<p>") == 4` against input that
    happened to already avoid the bug.

    `body_node.html` (selectolax's outer-HTML property, div wrapper
    included) mirrors `_capture_raw` in crawler/parser.py exactly:
    `body_raw = node.html`. So this is the same string a real `body_raw`
    column value would be.
    """
    fixture_html = (FIXTURES / "article_with_images.html").read_text(encoding="utf-8")
    body_node = HTMLParser(fixture_html).css_first("div.postBody")
    out = html(body_node.html)
    # Six paragraphs by eye: greeting, list announcement, the <u>30 ve
    # üstü</u> offer, and one each for the two image links and the sign-off.
    assert out.count("<p>") == 6
    assert "<u>30 ve üstü</u>" in out


def test_a_nested_block_gets_the_same_paragraph_treatment():
    """Regression guard: replacing _has_paragraph_break's body with `return
    False` leaves the whole 67-test suite green, because nothing else calls
    it and nothing else exercises a non-p, non-li block with a real
    paragraph boundary in its own children. Uses the real "<br>\\n\\n<br>"
    shape, not an adjacent "<br><br>", so this also can't pass by accident
    the way test_the_real_fixture_gains_paragraphs used to.
    """
    out = html("<blockquote>A<br>\n\n<br>B</blockquote>")
    assert out == "<blockquote><p>A</p><p>B</p></blockquote>"


def test_li_is_excluded_from_paragraph_grouping():
    """Regression guard: deleting the `if kept == "li"` branch in _convert
    leaves the whole suite green while a list item with a double <br> in it
    gains `<li><p>A</p><p>B</p></li>` -- the exact spacing change the task's
    own constraint forbids ("wrapping a list item's text in a <p> changes
    its spacing for no benefit"). li never calls _paragraphs at all, so its
    children are untouched: unlike the top-level and other-block cases, a
    double <br> inside an <li> stays two literal <br> tags either way.
    """
    out = html("<ul><li>A<br><br>B</li></ul>")
    assert out == "<ul><li>A<br><br>B</li></ul>"


def test_a_block_boundary_flushes_pending_text_before_it_not_after():
    """Regression guard: deleting the `flush()` call at the top of
    _paragraphs's `Block` branch leaves the whole suite green while
    reordering content. Without it, text accumulated in `current` before a
    nested Block is only flushed by the loop's final, unconditional flush()
    -- so it lands in `out` *after* the Block that structurally follows it,
    silently reordering an archive's content rather than merely losing or
    mis-wrapping it.
    """
    out = html("<blockquote>Alpha<ul><li>one</li></ul></blockquote>")
    assert out == "<blockquote><p>Alpha</p><ul><li>one</li></ul></blockquote>"
