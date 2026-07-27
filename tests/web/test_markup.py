"""What may leave this module, and what may not.

body_raw is written by anyone with an eRepublik account. These tests assert the
output alphabet, not the implementation: the invariant test at the bottom is
the one that catches a case nobody thought of.
"""

import pathlib
import time

import pytest
from selectolax.parser import HTMLParser

from babel.db.browse import ImageState
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
    # These four round out coverage of every EMITTED_TAGS member reachable
    # without a captured image (em, u, s, blockquote, ol, h2, h4, h5, h6).
    # "img" and "span" are covered separately, further down, by a case that
    # actually reaches the missing-image placeholder path. Before these, the
    # sweep below reached 7 of 18 EMITTED_TAGS; a tag-alphabet regression in
    # any of these nine could have shipped without the invariant test ever
    # touching it.
    '<p><em onclick="alert(1)">em</em><u style="color:red">u</u>'
    '<s onmouseover="alert(1)">s</s></p>',
    '<blockquote onclick="alert(1)">quote<script>alert(1)</script></blockquote>',
    '<ol><li formaction="/x">item</li></ol>',
    '<h2 onclick="alert(1)">H2</h2><h4 onclick="alert(1)">H4</h4>'
    '<h5 onclick="alert(1)">H5</h5><h6 onclick="alert(1)">H6</h6>',
    # Closes the "span" half of the gap the comment above used to leave open.
    # This sweep always calls render_body(raw, {}) -- an empty image map --
    # so every <img> in it takes the missing-image placeholder path, never
    # the "ok" branch; "img" itself stays excluded below for that reason,
    # since its src is always this module's own literal (a hex digest), never
    # attacker-controlled, and no HOSTILE shape can reach it. The placeholder
    # path DOES put author bytes into an attribute -- the "original" link's
    # href, built from source_url -- so this entry targets exactly that: a
    # quoted attribute-breakout attempt inside an <img src>, the same shape
    # already proven safe for <a href> above.
    '<p><img src="https://evil.example/x.png&quot; onmouseover=&quot;alert(3)"></p>',
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

    The tag check is pinned to every EMITTED_TAGS member this sweep can
    actually reach. Task 5 closed the "span" half of what used to be excluded
    here by adding a HOSTILE case whose image has no queue row, so it takes
    the missing-image placeholder path and emits a real
    <span class="missing-image">. "img" stays excluded on purpose, not by
    omission: every case in HOSTILE renders through render_body(raw, {}), an
    empty image map, so no case can ever reach the "ok" branch that emits a
    real <img> -- and that branch's only attribute value (a hex digest this
    module computes itself) never carries author bytes anyway, so excluding
    it costs this test nothing. Asserting the full reachable set, not a
    handful of representative tags, is what turns "the sweep observes most of
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
    assert (EMITTED_TAGS - {"img"}) <= seen_tags


def test_an_image_with_a_disallowed_scheme_is_dropped_before_task_5_sees_it():
    """Image.source_url must be a checked URL, the same way Inline("a").href is.

    Asserted directly against _convert's output, which is part of this
    module's documented interface, rather than through render_body: a
    disallowed-scheme <img> never becomes an Image node at all, so no
    render_body input can exercise _emit_image's own validation for this
    case at all. Confirmed directly (see the task report's mutation check
    for test_an_image_source_that_is_not_http_gets_no_link): _emit_image is
    never even called for this input, because _convert already dropped it
    here.
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


DIGEST = bytes.fromhex("ab" * 32)


def test_a_captured_image_is_served_from_our_own_disk():
    images = {"https://h/1.png": ImageState(state="ok", sha256=DIGEST)}
    out = str(render_body('<p><img src="https://h/1.png"></p>', images).html)
    assert f'src="/img/{DIGEST.hex()}"' in out
    assert 'loading="lazy"' in out
    assert "https://h/1.png" not in out


@pytest.mark.parametrize(
    ("state", "caption"),
    [
        ("dead", "already gone"),
        ("waiting", "not captured yet"),
        ("exhausted", "could not be retrieved"),
    ],
)
def test_a_missing_image_is_a_placeholder_that_links_to_the_original(state, caption):
    images = {"https://h/1.png": ImageState(state=state, sha256=None)}
    out = str(render_body('<p><img src="https://h/1.png"></p>', images).html)
    assert caption in out
    assert 'href="https://h/1.png"' in out
    assert 'rel="nofollow noreferrer"' in out
    assert "<img" not in out
    # Task 7's placeholder styling hooks off this class; unguarded before
    # this line (mutant M13: dropping the class attribute left all 81 tests
    # in this file green).
    assert 'class="missing-image"' in out


def test_a_withheld_blob_is_not_linked_to_its_original():
    """`babel hide --image` is a takedown. Linking round it would defeat it."""
    images = {"https://h/1.png": ImageState(state="withheld", sha256=DIGEST)}
    out = str(render_body('<p><img src="https://h/1.png"></p>', images).html)
    assert "not available" in out
    assert "https://h/1.png" not in out
    assert DIGEST.hex() not in out


def test_an_image_with_no_row_reads_as_not_archived():
    """Comments are parsed for text only, so their images have no queue row."""
    out = str(render_body('<p><img src="https://h/1.png"></p>', {}).html)
    assert "not archived" in out
    assert "not captured yet" not in out
    assert 'href="https://h/1.png"' in out


def test_an_image_source_that_is_not_http_gets_no_link():
    out = str(render_body('<p><img src="javascript:alert(1)"></p>', {}).html)
    assert "<a " not in out
    assert "alert(1)" not in out or "javascript:" not in out


def test_a_linked_image_keeps_the_authors_link_around_it():
    images = {"https://h/1.png": ImageState(state="ok", sha256=DIGEST)}
    raw = '<p><a href="https://src.example/"><img src="https://h/1.png"></a></p>'
    rendered = render_body(raw, images)
    out = str(rendered.html)
    assert 'href="https://src.example/"' in out
    assert f'src="/img/{DIGEST.hex()}"' in out
    # _image_urls' recursion into Inline was unguarded (mutant M9: deleting
    # it left all 81 tests in this file green, silently emptying
    # image_urls for every image inside an author link -- the common
    # shape, and the one Task 7's gallery subtracts against).
    assert rendered.image_urls == frozenset({"https://h/1.png"})


def test_rendered_image_urls_are_reported_for_the_gallery():
    images = {
        "https://h/1.png": ImageState(state="ok", sha256=DIGEST),
        "https://h/2.png": ImageState(state="ok", sha256=DIGEST),
    }
    rendered = render_body('<p><img src="https://h/1.png"></p>', images)
    assert rendered.image_urls == frozenset({"https://h/1.png"})


def test_a_missing_protocol_relative_image_still_links_to_its_original():
    """Fact 3 from the task brief: `_href("//h/1.png")` returns None for
    lacking a scheme, so linking straight to `item.source_url` would
    silently drop the link for exactly the protocol-relative sources this
    archive commonly holds (older articles; crawler/images.py's
    normalise_url does the same "//" -> "https://" rewrite before dialling,
    so that is the URL the fetcher actually used, not a guess). The
    placeholder must link to that normalised https:// form rather than
    render no link at all.
    """
    out = str(render_body('<p><img src="//h/1.png"></p>', {}).html)
    assert 'href="https://h/1.png"' in out
    assert "not archived" in out


def test_a_captured_image_is_found_by_the_exact_unstripped_key():
    """images.get() must key on item.source_url exactly as stored, not a
    stripped copy of it -- crawler/parser.py writes the src attribute
    verbatim, with no .strip() (settled in an earlier review round), so
    article_images can hold a row keyed on a string with incidental
    whitespace. Mutating the lookup to `images.get(item.source_url.strip())`
    is exactly the "obvious cleanup" that misses such a row silently --
    survived the whole file before this test existed.
    """
    padded = " https://h/1.png "
    images = {padded: ImageState(state="ok", sha256=DIGEST)}
    out = str(render_body(f'<p><img src="{padded}"></p>', images).html)
    assert f'src="/img/{DIGEST.hex()}"' in out


def test_an_ok_state_with_no_digest_yet_degrades_to_a_placeholder():
    """_CAPTIONS has no "ok" key of its own -- the ok+digest branch above it
    is the only path that ever wants that word. A row that says "ok" but
    has no sha256 yet is exactly the shape the ok-branch's own condition
    (`state.sha256 is not None`) anticipates falling through on, and a bare
    `_CAPTIONS[state.state]` subscript KeyErrors on it, which would take
    the whole article page down instead of degrading the one image slot.
    """
    images = {"https://h/1.png": ImageState(state="ok", sha256=None)}
    out = str(render_body('<p><img src="https://h/1.png"></p>', images).html)
    assert 'class="missing-image"' in out
    assert "<img" not in out


def test_an_unrecognised_state_degrades_to_a_placeholder_instead_of_crashing():
    """'pending' and 'error' are the real article_images statuses -- not
    among _CAPTIONS' five buckets -- and would reach here unchanged if a
    future caller (Task 6's SQL) ever passed one through without collapsing
    it first. A bare subscript KeyErrors; render_body must degrade the one
    image slot instead of taking the whole article page down with it.
    """
    images = {"https://h/1.png": ImageState(state="pending", sha256=None)}
    out = str(render_body('<p><img src="https://h/1.png"></p>', images).html)
    assert 'class="missing-image"' in out


def test_a_placeholder_inside_an_author_link_does_not_nest_anchors():
    """`[url=x][img]x[/img][/url]` is the BBCode idiom, so an author `<a>`
    almost always wraps exactly one image. With no queue row the image
    renders as a placeholder that already contains its own "original" <a>
    -- keeping the author's wrapping <a> around it nests one <a> inside
    another, which is invalid HTML5. Verified directly with lexbor (the
    same tree-construction algorithm a real browser runs): parsing the
    un-fixed output split the pair apart via the adoption-agency algorithm
    and hoisted the inner <a> out from under the outer one, so the escaped
    markup didn't even describe the nesting this module wrote. The fix
    drops the author's <a>, keeping its content.
    """
    raw = '<p><a href="https://src.example/"><img src="https://h/1.png"></a></p>'
    out = str(render_body(raw, {}).html)
    assert out.count("<a ") == 1
    assert 'href="https://src.example/"' not in out
    assert 'href="https://h/1.png"' in out  # the placeholder's own "original" link


def test_a_withheld_blob_inside_an_author_link_leaves_no_link_at_all():
    """`babel hide --image` is a takedown, and `[url=x][img]x[/img][/url]`
    means the author's own href commonly points at the exact URL that was
    hidden -- so a case-by-case check on which href to suppress is the
    wrong shape of fix; only dropping the wrapping <a> unconditionally
    closes it. Before the fix, this exact input rendered
    `<a href="https://h/1.png" ...><span class="missing-image">Image not
    available</span></a>` -- a live link to the original of a blob someone
    ran `babel hide --image` on, defeating the takedown the placeholder's
    own `_UNLINKED` check had already correctly honoured for itself.
    """
    images = {"https://h/1.png": ImageState(state="withheld", sha256=DIGEST)}
    raw = '<p><a href="https://h/1.png"><img src="https://h/1.png"></a></p>'
    out = str(render_body(raw, images).html)
    assert "https://h/1.png" not in out
    assert "<a " not in out
    assert "not available" in out


def test_anchor_unwrap_examines_every_image_not_just_the_first_one_it_meets():
    """Regression guard for three properties of _anchor_must_unwrap that no
    other test pins -- each is a mutant that survives the whole file
    otherwise (mutation-tested individually; see the task report):

    - It must not stop scanning the moment it meets a GOOD image (`continue`,
      not `break`). This anchor has two images; the good one (h/2.png) sits
      later in the anchor's own child order but is visited FIRST by the
      traversal's LIFO stack (stack.pop() pops the last-pushed child first),
      so a `break` on the first good image found would exit before ever
      reaching the bad one nested inside <b>.
    - It must recurse into a non-Image child's own children
      (`stack.extend(children)`). The bad image (h/1.png) is not a direct
      child of the <a> -- it's nested one level inside a <b> -- so a check
      that only inspects direct children would never see it.
    - `state.state == "ok"` alone isn't enough to call an image real:
      `state.sha256 is None` is its own disqualifying condition. h/1.png is
      exactly that shape -- the one
      test_an_ok_state_with_no_digest_yet_degrades_to_a_placeholder exists to
      cover -- and dropping this clause restores the I1/I2 defect (an author
      <a> surviving around a placeholder) for it specifically.
    """
    images = {
        "https://h/1.png": ImageState(state="ok", sha256=None),
        "https://h/2.png": ImageState(state="ok", sha256=DIGEST),
    }
    raw = (
        '<p><a href="https://src.example/"><b><img src="https://h/1.png"></b>'
        '<img src="https://h/2.png"></a></p>'
    )
    out = str(render_body(raw, images).html)
    assert 'href="https://src.example/"' not in out
    assert out.count("<a ") == 1
