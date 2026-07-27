# Article Markup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store the markup eRepublik actually served for each article and comment, and render it — paragraphs, emphasis, links and in-position images — instead of the markup-stripped single block the archive serves today.

**Architecture:** A new nullable `body_raw` column on `articles` and `comments` holds the source markup verbatim. A new pure module `src/babel/web/markup.py` turns that untrusted markup into `Markup` at render time, against a literal allowlist, escaping every text node. Rows without `body_raw` keep rendering through the existing plain-text path, so the deploy is safe before any re-collection has happened.

**Tech Stack:** Python 3.13, selectolax (already a dependency, used by the parser), markupsafe (already present via Jinja2), asyncpg, FastAPI + Jinja2, pytest + testcontainers.

**Spec:** `docs/superpowers/specs/2026-07-27-article-markup-design.md`

## Global Constraints

- Column is named `body_raw`, never `body_html`. It holds untrusted bytes; the name must not read as "already safe".
- `body_raw` is nullable on both tables. `articles.body` stays `NOT NULL` and keeps its current meaning — derived plain text for search and snippets.
- `_body_text` in `crawler/parser.py` is not modified. Its existing tests are the regression guard that `body` still means what it meant.
- No template may contain `|safe`. There is none today; `render_body` returns `Markup`, so autoescape passes it through untouched and none is needed.
- A tag or attribute may appear in rendered output only if it comes from a literal in `src/babel/web/markup.py`. Every text node passes through `markupsafe.escape`.
- Size ceiling on captured markup: 1,000,000 characters, then truncate and log.
- Depth ceiling in the walker: 100.
- Link schemes permitted: `http`, `https`. Nothing else produces a link.
- Run `uv run pytest` and `uv run ruff check src tests` before every commit. The
  baseline measured on this branch at 0c51f62 is **348 passed** in ~4m40s (Docker
  must be running for the `postgres:17` testcontainer). It stays green; the count
  only goes up. CLAUDE.md's "204 tests" is stale — do not treat it as the target.
- Commit messages follow this repository's house style, which is unusually
  demanding — read `git log -6` before writing one. A short imperative subject
  ("Serve stored image blobs", "Name the permission gap instead of crashing
  verify_schema"), then a body that explains **why**, names what was measured,
  and says what the alternative would have cost. Bodies cite concrete evidence
  ("measured, 6 concurrent requests became 6 sequential scans"), not intentions.
  Every commit ends with the trailer:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
  The one-line messages given in each task's commit step are subjects only —
  write the body.

---

### Task 1: Parser captures the raw markup

Pure Python — no database, no network. `Article` and `Comment` gain the field; `parse_article` and `parse_comments` fill it.

**Files:**
- Modify: `src/babel/models.py`
- Modify: `src/babel/crawler/parser.py`
- Test: `tests/crawler/test_parser.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Article.body_raw: str | None`, `Comment.body_raw: str | None`,
  `babel.crawler.parser.MAX_BODY_RAW_CHARS: int = 1_000_000`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/crawler/test_parser.py`:

```python
from babel.crawler.parser import MAX_BODY_RAW_CHARS, parse_comments


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/crawler/test_parser.py -k "body_raw" -v`
Expected: FAIL — `ImportError: cannot import name 'MAX_BODY_RAW_CHARS'`.

- [ ] **Step 3: Add the model fields**

In `src/babel/models.py`, add to `Comment` (after `body`):

```python
    body_raw: str | None = None  # the markup the game served, untrusted
```

and to `Article` (after `body`, before `author_id` — but as a defaulted field it
must go after every non-defaulted one, so put it immediately before `images`):

```python
    body_raw: str | None = None  # the markup the game served, untrusted
```

Note: `Comment` has no defaulted fields today, so appending `body_raw` last is
fine. `Article`'s defaulted fields are `images` and `comments`; `body_raw` goes
directly above them.

- [ ] **Step 4: Capture it in the parser**

In `src/babel/crawler/parser.py`, add at the top of the imports:

```python
import logging
```

and after the existing module constants:

```python
log = logging.getLogger(__name__)

# body_raw is whatever a third-party server sent, and it is now stored. Bodies
# average 3.4 KB (SPEC.md); one pathological article must not be able to bloat
# the table. Truncated markup is harmless — the render-time parse is lenient.
MAX_BODY_RAW_CHARS = 1_000_000


def _capture_raw(node: HTMLNode | None) -> str | None:
    """The node's outer HTML, bounded.

    Outer rather than inner: the renderer unwraps the `div`/`p` wrapper anyway,
    so there is no string surgery to get wrong.
    """
    raw = node.html if node is not None else None
    if raw is None:
        return None
    if len(raw) > MAX_BODY_RAW_CHARS:
        log.warning("body markup truncated at %d chars", MAX_BODY_RAW_CHARS)
        return raw[:MAX_BODY_RAW_CHARS]
    return raw
```

In `parse_article`'s `return Article(...)`, add after `body=_body_text(body_node),`:

```python
        body_raw=_capture_raw(body_node),
```

In `parse_comments`, replace the body block:

```python
        body_node = node.css_first("div.details p")
        # text(strip=True) only strips each individual text fragment before
        # joining with the separator -- an HTML comment (present on every
        # comment <p>) plus surrounding newlines still leaves whitespace
        # around the joined result, so the final string needs its own strip.
        body = body_node.text(separator=" ", strip=True).strip() if body_node else ""
        if not body or body == "[removed]":
            body = None
        # A removed comment keeps its slot and nothing else. Capturing the
        # "[removed]" markup would give the renderer a body to render for a
        # comment whose whole point is that there is no body.
        body_raw = _capture_raw(body_node) if body is not None else None
```

and add `body_raw=body_raw,` to the `Comment(...)` construction.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/crawler/test_parser.py -v`
Expected: PASS, including every pre-existing `_body_text` test.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src tests
git add src/babel/models.py src/babel/crawler/parser.py tests/crawler/test_parser.py
git commit -m "Capture the markup the game served, alongside the stripped text"
```

---

### Task 2: Migration 007 and the write path

**Files:**
- Create: `migrations/007_body_markup.sql`
- Modify: `src/babel/db/repo.py:77-105`
- Test: `tests/db/test_repo.py`

**Interfaces:**
- Consumes: `Article.body_raw`, `Comment.body_raw` from Task 1.
- Produces: `articles.body_raw`, `comments.body_raw` columns; `save_article`
  persists both on insert and on the conflict-update branch.

- [ ] **Step 1: Write the failing test**

Append to `tests/db/test_repo.py` (match the file's existing helper for building
an `Article`; if it has none, this test builds its own):

```python
async def test_save_article_round_trips_the_raw_markup(pg):
    article = Article(
        id=9101, title="T", body="text only",
        body_raw='<div class="postBody"><p>A<br><br><b>B</b></p></div>',
        author_id=None, author_name="ann", country="Poland",
        published_at=datetime.datetime(2026, 7, 21, 5, 53, tzinfo=datetime.UTC),
        e_day=6817, comment_count=1,
        comments=(Comment(id=55, position=0, depth=0, author_id=None,
                          author_name="bob", posted_at=None,
                          body="hi", body_raw="<p>hi<br>there</p>"),),
    )
    await save_article(pg, article)

    row = await pg.fetchrow("SELECT body, body_raw FROM articles WHERE id = 9101")
    assert row["body"] == "text only"
    assert row["body_raw"] == '<div class="postBody"><p>A<br><br><b>B</b></p></div>'
    comment = await pg.fetchrow("SELECT body_raw FROM comments WHERE id = 55")
    assert comment["body_raw"] == "<p>hi<br>there</p>"


async def test_a_refetch_replaces_the_raw_markup(pg):
    """The update branch is a separate SQL path and has been wrong before."""
    def build(raw):
        return Article(
            id=9102, title="T", body="text", body_raw=raw,
            author_id=None, author_name="ann", country="Poland",
            published_at=datetime.datetime(2026, 7, 21, 5, 53, tzinfo=datetime.UTC),
            e_day=6817, comment_count=1,
            comments=(Comment(id=56, position=0, depth=0, author_id=None,
                              author_name="bob", posted_at=None,
                              body="hi", body_raw=raw),),
        )

    await save_article(pg, build("<p>first</p>"))
    await save_article(pg, build("<p>second</p>"))

    assert await pg.fetchval("SELECT body_raw FROM articles WHERE id = 9102") == "<p>second</p>"
    assert await pg.fetchval("SELECT body_raw FROM comments WHERE id = 56") == "<p>second</p>"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/db/test_repo.py -k raw_markup -v`
Expected: FAIL — `UndefinedColumnError: column "body_raw" does not exist`.

- [ ] **Step 3: Write the migration**

Create `migrations/007_body_markup.sql`:

```sql
-- The markup the game served, kept so rendering can be fixed without a re-crawl.
--
-- SPEC.md's "Raw HTML is not archived" is deliberately narrowed here, not
-- broken: that decision rejected the whole page at 118 GB. This is postBody and
-- the comment <p> only — about 1.4x the text already stored, so roughly 13 GB
-- across the full archive. What it buys is the thing the archive kept paying
-- for: a rendering or parsing mistake becomes `docker compose up -d web`
-- instead of another 32-day walk. migration 004's own comment records the cost
-- of not having it.
--
-- NAMED body_raw, NOT body_html. It is untrusted bytes from a third-party
-- server. `body_html` would read as "already sanitised" and would invite
-- `|safe` in a template. Nothing may render this column except
-- babel.web.markup.render_body.
--
-- Nullable on purpose: NULL means "collected before this change" and renders
-- through the old plain-text path, so `web` can be deployed before any
-- re-collection has run. Unlike 005 this migration is metadata-only — ADD
-- COLUMN with no default takes ACCESS EXCLUSIVE but rewrites nothing.

ALTER TABLE articles ADD COLUMN body_raw text;
ALTER TABLE comments ADD COLUMN body_raw text;
```

- [ ] **Step 4: Persist it**

In `src/babel/db/repo.py`, the articles statement becomes:

```python
            INSERT INTO articles (id, title, body, body_raw, author_id, author_name,
                                  country, published_at, e_day, comment_count, fetched_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10, now())
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title, body = EXCLUDED.body,
                body_raw = EXCLUDED.body_raw,
                author_id = EXCLUDED.author_id, author_name = EXCLUDED.author_name,
                country = EXCLUDED.country, published_at = EXCLUDED.published_at,
                e_day = EXCLUDED.e_day, comment_count = EXCLUDED.comment_count,
                fetched_at = now()
            """,
            article.id, article.title, article.body, article.body_raw,
            article.author_id, article.author_name, article.country,
            article.published_at, article.e_day, article.comment_count,
```

and the comments statement:

```python
                """INSERT INTO comments (id, article_id, position, depth, author_id,
                                         author_name, posted_at, body, body_raw)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                   ON CONFLICT (id) DO UPDATE SET
                       position = EXCLUDED.position, depth = EXCLUDED.depth,
                       author_id = EXCLUDED.author_id, author_name = EXCLUDED.author_name,
                       posted_at = EXCLUDED.posted_at, body = EXCLUDED.body,
                       body_raw = EXCLUDED.body_raw""",
                [
                    (c.id, article.id, c.position, c.depth, c.author_id,
                     c.author_name, c.posted_at, c.body, c.body_raw)
                    for c in article.comments
                ],
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/db/ -v`
Expected: PASS. Docker must be running for the `postgres:17` testcontainer.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src tests
git add migrations/007_body_markup.sql src/babel/db/repo.py tests/db/test_repo.py
git commit -m "Store the captured markup"
```

---

### Task 3: The markup walker

The security-critical module. No images and no paragraph grouping yet — those
are Tasks 4 and 5. This task establishes the allowlist, the escaping and the
two ceilings.

**Files:**
- Create: `src/babel/web/markup.py`
- Test: `tests/web/test_markup.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `KEPT: dict[str, str]`, `EMITTED_TAGS: frozenset[str]`,
    `DROPPED: frozenset[str]`, `BLOCKS: frozenset[str]`,
    `ALLOWED_ATTRIBUTES: frozenset[str]`, `MAX_DEPTH: int`
  - node types `Text(value: str)`, `Break()`, `Image(source_url: str)`,
    `Inline(tag: str, children: tuple, href: str | None)`,
    `Block(tag: str, children: tuple)`
  - `_href(raw: str | None) -> str | None`
  - `_convert(node, depth: int) -> tuple[object, ...]`
  - `render_body(raw: str, images) -> RenderedBody` (final shape in Task 5;
    this task lands a version whose `images` argument is accepted and unused,
    and which does no paragraph grouping)
  - `RenderedBody(html: Markup, image_urls: frozenset[str])`

- [ ] **Step 1: Write the failing tests**

Create `tests/web/test_markup.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/web/test_markup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'babel.web.markup'`.

- [ ] **Step 3: Write the module**

Create `src/babel/web/markup.py`:

```python
"""Untrusted article markup -> safe HTML. No network, no database, no I/O.

`body_raw` is whatever eRepublik's server sent for an article or a comment, and
anyone with an account can write it. Two invariants make rendering it safe, and
both are properties of this file rather than of a library:

  1. A tag or an attribute reaches the output only from a literal below.
  2. Every text node goes through `markupsafe.escape`.

There is therefore no code path by which an author's bytes reach the output
unescaped. `render_body` returns `Markup`, so templates never say `|safe` —
which is what makes "no template contains |safe" a meaningful guard rather than
a style rule.

The markup itself is BBCode rendered by the game: one `<p>` holding the whole
body, `<br>` for line breaks, a run of two or more for a paragraph, and `<q
class="emoji ...">` around emoji. See SPEC.md.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from markupsafe import Markup, escape
from selectolax.parser import HTMLParser, Node

# Source tag -> the tag we emit. The only source of tag names in the output.
KEPT: dict[str, str] = {
    "p": "p",
    "br": "br",
    "b": "strong", "strong": "strong",
    "i": "em", "em": "em",
    "u": "u",
    "s": "s", "strike": "s", "del": "s",
    "blockquote": "blockquote",
    "ul": "ul", "ol": "ol", "li": "li",
    "h1": "h2", "h2": "h2", "h3": "h3", "h4": "h4", "h5": "h5", "h6": "h6",
    "a": "a",
    "img": "img",
}

# Every tag this module may put in the output. `span` is here and deliberately
# NOT in KEPT: no author <span> survives the walk (it is unwrapped), but Task 5
# emits `<span class="missing-image">` of its own. Keeping the two sets apart is
# what lets the invariant test check the real output alphabet without pretending
# an author's <span> is kept.
EMITTED_TAGS: frozenset[str] = frozenset(KEPT.values()) | {"span"}

# Dropped together with their children. Unwrapping these would spill JavaScript
# or CSS source into the page as visible text, which is not dangerous but is
# certainly not the article.
DROPPED: frozenset[str] = frozenset({
    "script", "style", "iframe", "object", "embed", "svg", "noscript",
    "template", "head", "meta", "link", "base", "form", "input", "button",
    "select", "textarea", "applet", "frame", "frameset",
})

# Emitted tags that end the paragraph being accumulated. Task 4 uses this.
BLOCKS: frozenset[str] = frozenset({
    "p", "blockquote", "ul", "ol", "li", "h2", "h3", "h4", "h5", "h6",
})

# Attributes that may appear in the output. Every one is written by this module;
# no input attribute is ever copied through.
ALLOWED_ATTRIBUTES: frozenset[str] = frozenset({
    "href", "rel", "target", "src", "alt", "loading", "class",
})

# selectolax parses arbitrarily deep markup; a recursive emitter would exhaust
# the stack on it. See _convert for why the remedy is flattening, not unwrapping.
MAX_DEPTH = 100

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_VOID = frozenset({"br", "img"})


@dataclass(frozen=True, slots=True)
class Text:
    value: str


@dataclass(frozen=True, slots=True)
class Break:
    pass


@dataclass(frozen=True, slots=True)
class Image:
    source_url: str


@dataclass(frozen=True, slots=True)
class Inline:
    tag: str
    children: tuple
    href: str | None = None


@dataclass(frozen=True, slots=True)
class Block:
    tag: str
    children: tuple


@dataclass(frozen=True, slots=True)
class RenderedBody:
    html: Markup
    image_urls: frozenset[str]


def _href(raw: str | None) -> str | None:
    """The URL if we are willing to link to it, else None.

    Tab, CR and LF are stripped before the scheme is read because a browser
    strips them too: `java<TAB>script:alert(1)` is a relative URL to a naive
    reader and script to Chrome. Python's own urlsplit already removes them, so
    doing it here keeps the string we check identical to the string we emit.
    """
    if not raw:
        return None
    cleaned = "".join(ch for ch in raw.strip() if ch not in "\t\r\n")
    try:
        scheme = urlsplit(cleaned).scheme.lower()
    except ValueError:
        return None
    return cleaned if scheme in _ALLOWED_SCHEMES else None


def _convert(node: Node, depth: int) -> tuple[object, ...]:
    if node.tag == "-text":
        return (Text(node.text_content or ""),)
    if node.tag in DROPPED:
        return ()
    if depth >= MAX_DEPTH:
        # Flattened to text, NOT unwrapped. Unwrapping keeps the children and so
        # keeps recursing, which bounds nothing at all. selectolax's own text()
        # walks the subtree in C, so it cannot exhaust the Python stack.
        return (Text(node.text() or ""),)

    kept = KEPT.get(node.tag)
    if kept == "br":
        return (Break(),)
    if kept == "img":
        src = (node.attributes.get("src") or "").strip()
        return (Image(src),) if src else ()

    children: list[object] = []
    for child in node.iter(include_text=True):
        children.extend(_convert(child, depth + 1))

    if kept is None:
        return tuple(children)  # unwrap: keep the content, drop the wrapper
    if kept in BLOCKS:
        return (Block(kept, tuple(children)),)
    if kept == "a":
        href = _href(node.attributes.get("href"))
        # A link we will not follow is not a link. Keeping the <a> without an
        # href would render an inert underline that looks broken; the text is
        # what the author wrote either way.
        return (Inline("a", tuple(children), href),) if href else tuple(children)
    return (Inline(kept, tuple(children)),)


def _emit(item: object) -> Markup:
    if isinstance(item, Text):
        return escape(item.value)
    if isinstance(item, Break):
        return Markup("<br>")
    if isinstance(item, Image):
        return Markup("")  # Task 5
    inner = Markup("").join(_emit(child) for child in item.children)
    if isinstance(item, Inline) and item.tag == "a":
        return Markup('<a href="%s" rel="nofollow noreferrer ugc" target="_blank">%s</a>') % (
            item.href, inner,
        )
    # `%` escapes its arguments, and escape() returns Markup unchanged because
    # Markup carries __html__ — so `inner` is not double-escaped.
    return Markup("<%s>%s</%s>") % (item.tag, inner, item.tag)


def render_body(raw: str, images: Mapping[str, object]) -> RenderedBody:
    root = HTMLParser(raw or "").body
    if root is None:
        return RenderedBody(html=Markup(""), image_urls=frozenset())
    items: list[object] = []
    for child in root.iter(include_text=True):
        items.extend(_convert(child, 0))
    return RenderedBody(
        html=Markup("").join(_emit(item) for item in items),
        image_urls=frozenset(),
    )
```

Note on `span`: it is in `KEPT` mapping to itself only so that
`ALLOWED_ATTRIBUTES`/`KEPT.values()` stay a single source of truth for the
invariant test once Task 5 emits `<span class="missing-image">`. `_convert`
unwraps it, so no author `<span>` survives.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/web/test_markup.py -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests
git add src/babel/web/markup.py tests/web/test_markup.py
git commit -m "Add the markup walker: an allowlist, escaped text, two ceilings"
```

---

### Task 4: Paragraphs from runs of `<br>`

The problem that started this: the game separates paragraphs with a double
`<br>` inside one `<p>`, so a stripped body is one block.

**Files:**
- Modify: `src/babel/web/markup.py`
- Test: `tests/web/test_markup.py`

**Interfaces:**
- Consumes: `Text`, `Break`, `Block`, `Inline`, `_convert` from Task 3.
- Produces: `_paragraphs(items: Sequence[object]) -> tuple[object, ...]`;
  `render_body` now returns block-level HTML.

- [ ] **Step 1: Write the failing tests**

Append to `tests/web/test_markup.py`:

```python
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
    raw = (
        '<div class="postBody"><p>Aziz eTürkiyem o/<br><br>'
        "Meclis seçimleri.<br><br>"
        "<u>30 ve üstü</u> oyu geçebilirsek <b>1000 Q7</b>.<br><br>"
        "Turan Parisi Yönetimi</p></div>"
    )
    out = html(raw)
    assert out.count("<p>") == 4
    assert "<u>30 ve üstü</u>" in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/web/test_markup.py -k "paragraph or br or fixture" -v`
Expected: FAIL — output is one flat run with `<br>` in it, `out.count("<p>")` is 1.

- [ ] **Step 3: Implement the grouping**

In `src/babel/web/markup.py`, add after `_convert`:

```python
def _is_blank(items: Sequence[object]) -> bool:
    return all(isinstance(i, (Text, Break)) and not str(getattr(i, "value", "")).strip()
               for i in items)


def _paragraphs(items: Sequence[object]) -> tuple[object, ...]:
    """Group a flat item stream into blocks.

    A run of two or more Breaks is a paragraph boundary; a single Break stays a
    <br>. The game writes the whole body as one <p> and separates paragraphs
    with a double <br>, so this is where an article stops being a wall of text.
    """
    out: list[object] = []
    current: list[object] = []
    pending_breaks = 0

    def flush() -> None:
        while current and isinstance(current[-1], Break):
            current.pop()
        if current and not _is_blank(current):
            out.append(Block("p", tuple(current)))
        current.clear()

    for item in items:
        if isinstance(item, Block):
            flush()
            pending_breaks = 0
            out.append(item)
            continue
        if isinstance(item, Break):
            pending_breaks += 1
            continue
        if isinstance(item, Text) and not item.value.strip() and not current:
            continue  # leading whitespace between breaks starts nothing
        if pending_breaks >= 2:
            flush()
        elif pending_breaks == 1 and current:
            current.append(Break())
        pending_breaks = 0
        current.append(item)

    flush()
    return tuple(out)
```

Add `from collections.abc import Mapping, Sequence` to the imports.

A `Block`'s own children must be grouped too, so in `_convert` replace the
`BLOCKS` branch:

```python
    if kept in BLOCKS:
        # A nested block's contents get the same treatment, so a <blockquote>
        # holding a double <br> reads the same as the body does.
        inner = children if kept == "li" else _paragraphs(children)
        return (Block(kept, tuple(inner)),)
```

`li` is excluded because wrapping a list item's text in a `<p>` changes its
spacing for no benefit.

Finally, in `render_body`, group before emitting:

```python
    blocks = _paragraphs(items)
    return RenderedBody(
        html=Markup("").join(_emit(item) for item in blocks),
        image_urls=frozenset(),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/web/test_markup.py -v`
Expected: PASS, including every Task 3 test.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests
git add src/babel/web/markup.py tests/web/test_markup.py
git commit -m "Turn runs of br into paragraphs"
```

---

### Task 5: Images in position

**Files:**
- Modify: `src/babel/web/markup.py`
- Test: `tests/web/test_markup.py`

**Interfaces:**
- Consumes: `Image`, `_emit`, `render_body` from Tasks 3 and 4.
- Produces:
  - `ImageState(state: str, sha256: bytes | None)` — imported by Task 6's
    `browse.get_image_map`, which is what builds it. `state` is one of
    `ok | dead | waiting | exhausted | withheld`.
  - `render_body(raw: str, images: Mapping[str, ImageState]) -> RenderedBody`
    with `RenderedBody.image_urls` populated with every `source_url` the walk
    emitted.

`ImageState` is defined in `db/browse.py`, not in `web/markup.py`, because
`db` must never import `web` and `browse.get_image_map` (Task 6) is what builds
it. Task 5 adds the dataclass to `db/browse.py` and imports it here; nothing
moves later.

- [ ] **Step 1: Write the failing tests**

Append to `tests/web/test_markup.py`:

```python
from babel.db.browse import ImageState

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
    out = str(render_body(raw, images).html)
    assert 'href="https://src.example/"' in out
    assert f'src="/img/{DIGEST.hex()}"' in out


def test_rendered_image_urls_are_reported_for_the_gallery():
    images = {
        "https://h/1.png": ImageState(state="ok", sha256=DIGEST),
        "https://h/2.png": ImageState(state="ok", sha256=DIGEST),
    }
    rendered = render_body('<p><img src="https://h/1.png"></p>', images)
    assert rendered.image_urls == frozenset({"https://h/1.png"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/web/test_markup.py -k image -v`
Expected: FAIL — `ImportError: cannot import name 'ImageState' from 'babel.db.browse'`.

- [ ] **Step 3: Add the row shape**

In `src/babel/db/browse.py`, next to `BlobRow`:

```python
@dataclass(frozen=True, slots=True)
class ImageState:
    """One image slot as the page needs to render it.

    `state` collapses status, attempts and the blob's tombstone into the single
    fact the renderer acts on, in the same four buckets image_status_counts
    uses plus 'withheld'. Keeping the bucket definition in SQL means the note
    under the article and the placeholder in it cannot drift apart.
    """

    state: str  # ok | dead | waiting | exhausted | withheld
    sha256: bytes | None
```

- [ ] **Step 4: Emit the images**

In `src/babel/web/markup.py`:

```python
from babel.db.browse import ImageState
```

Captions, after `MAX_DEPTH`:

```python
# What the reader is told when the bytes are not on our disk. 'withheld' gets no
# link: `babel hide --image` is a takedown, and linking round it would defeat it.
_CAPTIONS = {
    "dead": "Image already gone when we looked",
    "waiting": "Image not captured yet",
    "exhausted": "Image could not be retrieved",
    "withheld": "Image not available",
    None: "Image not archived",
}
_UNLINKED = frozenset({"withheld"})
```

Replace the `Image` branch of `_emit` — `_emit` now takes the map:

```python
def _emit(item: object, images: Mapping[str, ImageState]) -> Markup:
    if isinstance(item, Text):
        return escape(item.value)
    if isinstance(item, Break):
        return Markup("<br>")
    if isinstance(item, Image):
        return _emit_image(item, images)
    inner = Markup("").join(_emit(child, images) for child in item.children)
    if isinstance(item, Inline) and item.tag == "a":
        return Markup('<a href="%s" rel="nofollow noreferrer ugc" target="_blank">%s</a>') % (
            item.href, inner,
        )
    return Markup("<%s>%s</%s>") % (item.tag, inner, item.tag)


def _emit_image(item: Image, images: Mapping[str, ImageState]) -> Markup:
    state = images.get(item.source_url)
    if state is not None and state.state == "ok" and state.sha256 is not None:
        return Markup('<img src="/img/%s" alt="" loading="lazy">') % state.sha256.hex()

    caption = _CAPTIONS[state.state if state is not None else None]
    href = None if (state is not None and state.state in _UNLINKED) else _href(item.source_url)
    if href is None:
        return Markup('<span class="missing-image">%s</span>') % caption
    return Markup(
        '<span class="missing-image">%s '
        '<a href="%s" rel="nofollow noreferrer" target="_blank">original</a></span>'
    ) % (caption, href)
```

Collect the URLs and thread the map through `render_body`:

```python
def _image_urls(items) -> set[str]:
    found: set[str] = set()
    for item in items:
        if isinstance(item, Image):
            found.add(item.source_url)
        elif isinstance(item, (Inline, Block)):
            found |= _image_urls(item.children)
    return found


def render_body(raw: str, images: Mapping[str, ImageState]) -> RenderedBody:
    root = HTMLParser(raw or "").body
    if root is None:
        return RenderedBody(html=Markup(""), image_urls=frozenset())
    items: list[object] = []
    for child in root.iter(include_text=True):
        items.extend(_convert(child, 0))
    blocks = _paragraphs(items)
    return RenderedBody(
        html=Markup("").join(_emit(item, images) for item in blocks),
        image_urls=frozenset(_image_urls(blocks)),
    )
```

`_is_blank` must not treat an `Image` as blank — it already does not, since it
only returns True for `Text`/`Break`. An image alone between two double breaks
therefore keeps its own paragraph.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/web/test_markup.py -v`
Expected: PASS, all of Tasks 3–5.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src tests
git add src/babel/web/markup.py src/babel/db/browse.py tests/web/test_markup.py
git commit -m "Render images where the author put them"
```

---

### Task 6: The read path

**Files:**
- Modify: `src/babel/db/browse.py` (`ArticleDetail`, `CommentRow`, `get_article`,
  `get_comments`; replace `get_ok_image_digests` with `get_image_map`)
- Modify: `src/babel/web/routes.py` (the one call site of `get_ok_image_digests`)
- Test: `tests/db/test_browse.py` (or wherever `get_ok_image_digests` is tested —
  find it with `grep -rn get_ok_image_digests tests/`)

**Interfaces:**
- Consumes: `ImageState` from Task 5, the `body_raw` columns from Task 2.
- Produces:
  - `ArticleDetail.body_raw: str | None`, `CommentRow.body_raw: str | None`
  - `get_image_map(conn, article_id) -> dict[str, ImageState]`, ordered by
    `article_images.position`
  - `get_ok_image_digests` is **deleted**. Its only caller moves to the map.

- [ ] **Step 1: Write the failing tests**

Add to the browse tests:

```python
async def test_get_article_returns_the_raw_markup(pg):
    await pg.execute(
        """INSERT INTO articles (id, title, body, body_raw, published_at, comment_count)
           VALUES (7001, 't', 'text', '<p>A<br><br>B</p>', now(), 0)"""
    )
    detail = await browse.get_article(pg, 7001)
    assert detail.body_raw == "<p>A<br><br>B</p>"


async def test_get_comments_returns_the_raw_markup(pg):
    await pg.execute(
        """INSERT INTO articles (id, title, body, published_at, comment_count)
           VALUES (7002, 't', 'text', now(), 1)"""
    )
    await pg.execute(
        """INSERT INTO comments (id, article_id, position, depth, body, body_raw)
           VALUES (81, 7002, 0, 0, 'hi', '<p>hi<br>there</p>')"""
    )
    assert (await browse.get_comments(pg, 7002))[0].body_raw == "<p>hi<br>there</p>"


async def test_image_map_buckets_match_the_counts_the_note_shows(pg):
    await pg.execute(
        """INSERT INTO articles (id, title, body, published_at, comment_count)
           VALUES (7003, 't', 'text', now(), 0)"""
    )
    digest = bytes.fromhex("cd" * 32)
    await pg.execute("INSERT INTO images (sha256, bytes, mime) VALUES ($1, 3, 'image/png')",
                     digest)
    await pg.executemany(
        """INSERT INTO article_images (article_id, position, source_url, status,
                                       attempts, sha256)
           VALUES (7003, $1, $2, $3, $4, $5)""",
        [
            (0, "https://h/ok.png", "ok", 1, digest),
            (1, "https://h/dead.png", "dead", 1, None),
            (2, "https://h/wait.png", "pending", 0, None),
            (3, "https://h/gone.png", "error", 5, None),
        ],
    )
    mapping = await browse.get_image_map(pg, 7003)
    assert [s.state for s in mapping.values()] == ["ok", "dead", "waiting", "exhausted"]
    assert list(mapping) == ["https://h/ok.png", "https://h/dead.png",
                             "https://h/wait.png", "https://h/gone.png"]
    assert mapping["https://h/ok.png"].sha256 == digest


async def test_a_withheld_blob_reports_withheld_not_ok(pg):
    await pg.execute(
        """INSERT INTO articles (id, title, body, published_at, comment_count)
           VALUES (7004, 't', 'text', now(), 0)"""
    )
    digest = bytes.fromhex("ef" * 32)
    await pg.execute(
        """INSERT INTO images (sha256, bytes, mime, withheld_at)
           VALUES ($1, 3, 'image/png', now())""", digest,
    )
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status, sha256)
           VALUES (7004, 0, 'https://h/x.png', 'ok', $1)""", digest,
    )
    mapping = await browse.get_image_map(pg, 7004)
    assert mapping["https://h/x.png"].state == "withheld"


async def test_a_hidden_article_yields_an_empty_image_map(pg):
    await pg.execute(
        """INSERT INTO articles (id, title, body, published_at, comment_count, hidden_at)
           VALUES (7005, 't', 'text', now(), 0, now())"""
    )
    await pg.execute(
        """INSERT INTO article_images (article_id, position, source_url, status)
           VALUES (7005, 0, 'https://h/x.png', 'pending')"""
    )
    assert await browse.get_image_map(pg, 7005) == {}
```

Check the `images` table's exact column list first with
`sed -n '33,45p' migrations/001_initial.sql` and adjust the two INSERTs above to
match; the rest of the test does not depend on it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/db/ -k "raw_markup or image_map or withheld" -v`
Expected: FAIL — `AttributeError: module 'browse' has no attribute 'get_image_map'`.

- [ ] **Step 3: Extend the read path**

In `src/babel/db/browse.py`:

Add `body_raw: str | None` to `ArticleDetail` (after `body`) and to `CommentRow`
(after `body`). Add `body_raw` to both SELECT lists:

```python
        """SELECT id, title, body, body_raw, author_name, author_id, country,
                  published_at, e_day, comment_count
             FROM articles
            WHERE id = $1 AND hidden_at IS NULL""",
```

```python
        """SELECT id, position, depth, author_id, author_name, posted_at, body, body_raw
             FROM comments
            WHERE article_id = $1
              AND EXISTS (SELECT 1 FROM articles WHERE id = $1 AND hidden_at IS NULL)
            ORDER BY position""",
```

Replace `get_ok_image_digests` entirely with:

```python
async def get_image_map(conn: asyncpg.Connection, article_id: int) -> dict[str, ImageState]:
    """Every image slot of an article, keyed by the URL the article cited.

    Keyed on source_url because that is what the renderer has in hand: the
    `src` in body_raw is the same string `_parse_images` recorded at ingest, and
    migration 004 already made (article_id, source_url) the primary key.

    The buckets mirror image_status_counts exactly, plus 'withheld' for a blob
    `babel hide --image` has taken down — which must not be served and must not
    be linked round.

    The same EXISTS-on-$1 check as get_comments: a hidden article's images must
    not surface here even though each row's own status is fine, because the
    article-level tombstone has to dominate.
    """
    rows = await conn.fetch(
        """SELECT ai.source_url,
                  ai.sha256,
                  CASE
                    WHEN i.withheld_at IS NOT NULL                  THEN 'withheld'
                    WHEN ai.status = 'ok' AND ai.sha256 IS NOT NULL THEN 'ok'
                    WHEN ai.status = 'dead'                         THEN 'dead'
                    WHEN ai.status = 'error' AND ai.attempts >= $2  THEN 'exhausted'
                    ELSE 'waiting'
                  END AS state
             FROM article_images ai
             LEFT JOIN images i ON i.sha256 = ai.sha256
            WHERE ai.article_id = $1
              AND EXISTS (SELECT 1 FROM articles WHERE id = $1 AND hidden_at IS NULL)
            ORDER BY ai.position""",
        article_id, MAX_IMAGE_ATTEMPTS,
    )
    return {r["source_url"]: ImageState(state=r["state"], sha256=r["sha256"]) for r in rows}
```

`LEFT JOIN` because `sha256` is NULL for every `pending` and `dead` row;
`(NULL IS NOT NULL)` is false, so those rows are never mislabelled `withheld`.

- [ ] **Step 4: Point the route at it (compile-level only; Task 7 uses it)**

In `src/babel/web/routes.py`, replace

```python
            digests = await browse.get_ok_image_digests(conn, article_id)
```

with

```python
            image_map = await browse.get_image_map(conn, article_id)
```

and, for now, keep the template contract working:

```python
                "digests": [s.sha256.hex() for s in image_map.values()
                            if s.state == "ok" and s.sha256],
```

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS. Any test still naming `get_ok_image_digests` must be rewritten
against `get_image_map` — that function no longer exists, and leaving a dead
alias behind is exactly the smell finding M3 records against
`RETRYABLE_STATUSES`.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src tests
git add src/babel/db/browse.py src/babel/web/routes.py tests/
git commit -m "Read the raw markup and the image slots the renderer needs"
```

---

### Task 7: Render it

**Files:**
- Modify: `src/babel/web/routes.py` (the `/article/{article_id}` handler)
- Modify: `src/babel/web/templates/article.html`
- Modify: `src/babel/web/static/style.css`
- Test: `tests/web/test_article_page.py`

**Interfaces:**
- Consumes: `render_body`, `RenderedBody` (Task 5), `get_image_map`,
  `ArticleDetail.body_raw`, `CommentRow.body_raw` (Task 6).
- Produces: template context keys `body_html: Markup | None`,
  `comment_html: dict[int, Markup]`, `gallery: list[str]`,
  `gallery_is_leftover: bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/web/test_article_page.py`:

```python
async def test_markup_is_rendered_when_body_raw_is_present(client, pool):
    await _article(pool, 2100, body="Alpha Beta")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE articles SET body_raw = $1 WHERE id = 2100",
            "<p>Alpha<br><br><b>Beta</b></p>",
        )
    body = (await client.get("/article/2100")).text
    assert "<p>Alpha</p>" in body
    assert "<strong>Beta</strong>" in body


async def test_a_row_without_body_raw_still_renders_the_plain_text(client, pool):
    await _article(pool, 2101, body="Line one.\nLine two.")
    body = (await client.get("/article/2101")).text
    assert "body-text" in body
    assert "Line one." in body


async def test_a_script_in_body_raw_cannot_reach_the_page(client, pool):
    await _article(pool, 2102)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE articles SET body_raw = $1 WHERE id = 2102",
            '<p>ok<script>alert(1)</script><a href="javascript:alert(2)">x</a></p>',
        )
    body = (await client.get("/article/2102")).text
    assert "<script>alert(1)</script>" not in body
    assert "javascript:alert(2)" not in body
    assert "ok" in body


async def test_a_captured_image_renders_inline_and_not_in_the_gallery(client, pool):
    await _article(pool, 2103)
    digest = bytes.fromhex("1a" * 32)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE articles SET body_raw = $1 WHERE id = 2103",
            '<p>see<br><br><img src="https://h/a.png"></p>',
        )
        await conn.execute(
            "INSERT INTO images (sha256, bytes, mime) VALUES ($1, 3, 'image/png')", digest,
        )
        await conn.execute(
            """INSERT INTO article_images (article_id, position, source_url, status, sha256)
               VALUES (2103, 0, 'https://h/a.png', 'ok', $1)""", digest,
        )
    body = (await client.get("/article/2103")).text
    assert f'/img/{digest.hex()}' in body
    assert body.count(f'/img/{digest.hex()}') == 1  # inline only, not also below


async def test_an_image_dropped_from_the_article_still_shows_below(client, pool):
    await _article(pool, 2104)
    digest = bytes.fromhex("2b" * 32)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE articles SET body_raw = $1 WHERE id = 2104", "<p>no images now</p>",
        )
        await conn.execute(
            "INSERT INTO images (sha256, bytes, mime) VALUES ($1, 3, 'image/png')", digest,
        )
        await conn.execute(
            """INSERT INTO article_images (article_id, position, source_url, status, sha256)
               VALUES (2104, 0, 'https://h/old.png', 'ok', $1)""", digest,
        )
    body = (await client.get("/article/2104")).text
    assert f'/img/{digest.hex()}' in body
    assert "no longer in the article" in body


async def test_a_missing_image_renders_a_placeholder_with_a_link(client, pool):
    await _article(pool, 2105)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE articles SET body_raw = $1 WHERE id = 2105",
            '<p><img src="https://h/gone.png"></p>',
        )
        await conn.execute(
            """INSERT INTO article_images (article_id, position, source_url, status)
               VALUES (2105, 0, 'https://h/gone.png', 'dead')"""
        )
    body = (await client.get("/article/2105")).text
    assert "already gone" in body
    assert 'href="https://h/gone.png"' in body


async def test_comment_markup_is_rendered(client, pool):
    await _article(pool, 2106, comment_count=1)
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO comments (id, article_id, position, depth, author_name,
                                     body, body_raw)
               VALUES (91, 2106, 0, 0, 'bob', 'one two',
                       '<p>one<br><br><a href="https://e.org/">two</a></p>')"""
        )
    body = (await client.get("/article/2106")).text
    assert 'href="https://e.org/"' in body
    assert "<p>one</p>" in body
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/web/test_article_page.py -v`
Expected: FAIL — the markup is escaped and shown as text.

- [ ] **Step 3: Render in the route**

In `src/babel/web/routes.py`, add the import:

```python
from babel.web.markup import render_body
```

and replace the body of the article handler after the comments/map fetch:

```python
            comments = await browse.get_comments(conn, article_id)
            image_map = await browse.get_image_map(conn, article_id)
            counts = await browse.image_status_counts(conn, article_id)

        rendered = render_body(detail.body_raw, image_map) if detail.body_raw else None
        comment_html = {
            c.id: render_body(c.body_raw, {}).html for c in comments if c.body_raw
        }

        # With markup, every image the article still cites is shown in place, so
        # the strip below holds only blobs whose URL the article has dropped
        # since we captured them — the case migration 004 exists to protect.
        # Without markup (a row not re-collected yet) it is the whole gallery,
        # exactly as before.
        shown = rendered.image_urls if rendered else frozenset()
        gallery = [
            state.sha256.hex()
            for url, state in image_map.items()
            if state.state == "ok" and state.sha256 and url not in shown
        ]

        return templates.TemplateResponse(
            request=request,
            name="article.html",
            context={
                "article": detail,
                "body_html": rendered.html if rendered else None,
                "comments": comments,
                "comment_html": comment_html,
                "gallery": gallery,
                "gallery_is_leftover": rendered is not None,
                "counts": counts,
                "game_time": to_game_time,
                "settings": app.state.settings,
            },
            headers={"Cache-Control": "public, max-age=300"},
        )
```

Comment images get an empty map deliberately: `_parse_images` scans `postBody`
only, so a comment's image has no queue row and renders as "not archived".

- [ ] **Step 4: Update the template**

In `src/babel/web/templates/article.html`, replace the body div:

```jinja
  {% if body_html %}
  <div class="body">{{ body_html }}</div>
  {% else %}
  <div class="body-text">{{ article.body }}</div>
  {% endif %}
```

No `|safe`: `render_body` returns `Markup`, which Jinja's autoescape passes
through untouched. That is what keeps the Task 8 guard meaningful.

Replace the gallery:

```jinja
  {% if gallery %}
  <div class="gallery">
    {% if gallery_is_leftover %}
    <p class="meta">Images no longer in the article's text</p>
    {% endif %}
    {% for hex in gallery %}<img src="/img/{{ hex }}" alt="" loading="lazy">{% endfor %}
  </div>
  {% endif %}
```

Replace the comment body line:

```jinja
    {% if c.body is none %}
    <p class="body-text">[removed]</p>
    {% elif comment_html.get(c.id) %}
    <div class="body">{{ comment_html[c.id] }}</div>
    {% else %}
    <p class="body-text">{{ c.body }}</p>
    {% endif %}
```

- [ ] **Step 5: Style it**

Append to `src/babel/web/static/style.css`:

```css
/* Rendered article markup. `.body-text` above still serves rows collected
   before the markup was archived, which is why its pre-wrap stays. */

.body p {
  margin: 0 0 1rem;
}

.body h2,
.body h3 {
  font-size: 1.15rem;
  margin: 1.5rem 0 0.5rem;
}

.body ul,
.body ol {
  margin: 0 0 1rem;
  padding-left: 1.5rem;
}

.body blockquote {
  margin: 0 0 1rem;
  padding-left: 1rem;
  border-left: 3px solid var(--border);
  color: var(--muted);
}

.body img {
  display: block;
  max-width: 100%;
  height: auto;
  margin: 0.5rem 0;
}

.missing-image {
  display: block;
  padding: 0.75rem 1rem;
  margin: 0.5rem 0;
  border: 1px dashed var(--border);
  border-radius: 4px;
  color: var(--muted);
  font-size: 0.9rem;
}

.gallery img {
  max-width: 100%;
  height: auto;
  margin: 0.5rem 0;
}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/web/ -v`
Expected: PASS, including the pre-existing
`test_script_in_stored_text_is_escaped`, which covers the `body_raw IS NULL`
path and must still hold.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src tests
git add src/babel/web/routes.py src/babel/web/templates/article.html \
        src/babel/web/static/style.css tests/web/test_article_page.py
git commit -m "Serve the article as the game laid it out"
```

---

### Task 8: The two guards

**Files:**
- Modify: `src/babel/web/app.py:48`
- Test: `tests/web/test_cli_serve.py`, `tests/web/test_packaging.py`

**Interfaces:**
- Consumes: `migrations/007_body_markup.sql` from Task 2.
- Produces: `REQUIRED_MIGRATIONS = ("005_browse.sql", "007_body_markup.sql")`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/web/test_cli_serve.py`, beside the existing
`test_verify_schema_names_the_missing_migration`. That test uses
`REQUIRED_MIGRATIONS[0]`, so it would keep passing whatever the tuple holds;
this one names 007 outright, which is the fact worth pinning. Note the ledger's
column is `name`, not `filename`:

```python
async def test_verify_schema_names_the_markup_migration_too(pg):
    """A skipped migrate must be named, not answered as 503 forever.

    Without 007 the browse queries select body_raw, asyncpg raises
    UndefinedColumnError, which is a PostgresError, which lands in the
    database-down handler — so every page reports the database as not answering
    while /healthz and the compose healthcheck both stay green.
    """
    await pg.execute("DELETE FROM schema_migrations WHERE name = '007_body_markup.sql'")
    with pytest.raises(RuntimeError, match="007_body_markup.sql"):
        await verify_schema(pg)
```

Add to `tests/web/test_packaging.py` (it needs `import pathlib` if the file does
not already have it):

```python
def test_no_template_uses_the_safe_filter():
    """render_body returns Markup, so nothing needs |safe — and body_raw is
    untrusted, so nothing may have it. Keeping the count at zero is cheaper to
    enforce than auditing each use."""
    for path in (pathlib.Path(__file__).parents[2]
                 / "src/babel/web/templates").rglob("*.html"):
        assert "|safe" not in path.read_text(), f"{path.name} uses |safe"
        assert "| safe" not in path.read_text(), f"{path.name} uses | safe"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/web/test_cli_serve.py tests/web/test_packaging.py -v`
Expected: the migration test FAILS (no refusal is raised, because 007 is not in
`REQUIRED_MIGRATIONS` yet); the `|safe` test passes already and is there to keep
passing.

- [ ] **Step 3: Add the migration to the required tuple**

In `src/babel/web/app.py`:

```python
REQUIRED_MIGRATIONS = ("005_browse.sql", "007_body_markup.sql")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS, all 204+ tests.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests
git add src/babel/web/app.py tests/web/test_cli_serve.py tests/web/test_packaging.py
git commit -m "Refuse to serve without the markup migration, and keep |safe out of templates"
```

---

### Task 9: Documentation

No code. These three files are how the next person avoids re-deriving all of it.

**Files:**
- Modify: `SPEC.md`
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Record the reversed decision in SPEC.md**

Under "Decisions", immediately after the existing "Raw HTML is not archived"
entry, add:

```markdown
**The article's own markup *is* archived, as of 2026-07-27.** The entry above
rejected 118 GB of whole pages and still does. What is stored now is `postBody`
and each comment's `<p>` — about 1.4x the text already held, so roughly 13 GB
across the full archive. The reason is the one this project keeps paying for: a
rendering or parsing mistake becomes a redeploy instead of another 32-day walk.
`migrations/004_image_identity.sql` records what its absence cost — with the
markup stripped, `source_url` was the only surviving record of an article's
images and there was nothing left to reconcile against. Storing the text alone
also meant articles served as one unbroken block, because the parser dropped
every paragraph boundary before the row was written.

The column is `body_raw`, never `body_html`: it is untrusted bytes from a
third-party server, and only `babel.web.markup.render_body` may render it.
```

- [ ] **Step 2: Record the measured structure in SPEC.md**

Add a new subsection near the existing page-structure notes:

```markdown
### The body is BBCode rendered to HTML

Measured against `tests/fixtures/*.html` (2026-07). The whole body sits in one
`<p>`; `<br>` is the line break and **a run of two or more is a paragraph**.
Emphasis arrives as `<b>`, `<i>`, `<u>`; links as `<a target="_blank">`; images
as `<img class="bbcode_img">`, often wrapped in an `<a>`. Emoji are elements,
not characters: `<q class="emoji emoji_1f635">😵</q>` — produced by the game's
own emoji pass, sometimes by accident (one fixture turns the literal `100%)`
into a face).

Tag counts across the three fixtures — body: `br` 76, `p` 2, `b` 2, `a` 2,
`img` 2, `u` 1, `q` 1. Comments: `br` 20, `a` 9, `i` 1. That is three pages from
2026 against twenty years of BBCode, so the allowlist in
`src/babel/web/markup.py` is a floor, not a survey. Task 10 of
`docs/superpowers/plans/2026-07-27-article-markup.md` widens it from the archive
itself; widening costs a redeploy, which is the whole point of storing the
markup.
```

- [ ] **Step 3: Reword the pre-launch gate in CLAUDE.md**

In "Two things gate the site being reachable", item 2 currently says the parser
"only started emitting `"\n"` at block boundaries on this branch". Replace its
first sentence with:

```markdown
2. **Re-collect the article bodies.** Every article and comment collected before
   migration 007 has no `body_raw`, so it renders through the plain-text
   fallback: no paragraphs for the oldest rows, and no emphasis, links or
   in-position images for any of them. Re-collection is what fills the column;
   it is a deliberate stop/sweep/restart pass, not a queued job, because of M1
   above.
```

- [ ] **Step 4: Note the deploy order in README.md**

In the section that carries the migration-005 deploy dance, add:

```markdown
Migration 007 adds two nullable columns and rewrites nothing, so it does not
carry 005's `ShareLock` problem. It still goes through stop/migrate/start,
because `web` refuses to serve without it and all three images are being rebuilt
anyway:

```bash
docker compose stop crawler images
docker compose run --rm crawler babel migrate
docker compose build crawler images web
docker compose up -d web
```

`web` is safe to bring up straight away, before any re-collection: rows without
`body_raw` render exactly as they do today. Then run the re-collection pass
below, and finally `docker compose up -d crawler images`.
```

- [ ] **Step 5: Commit**

```bash
git add SPEC.md CLAUDE.md README.md
git commit -m "Write down the reversed decision, the measured markup, and the deploy order"
```

---

### Task 10: Widen the allowlist from the archive

**This task runs after the re-collection pass, on the deploy host.** It replaces
the spec's "sample a few hundred live articles" step, and is strictly better:
after re-collection the markup of every article is already in the database, so
the histogram is complete rather than a sample and costs no HTTP requests at
all. The spec's ordering assumed the allowlist had to be right before shipping;
it does not, because widening it is a redeploy.

**Files:**
- Modify: `src/babel/web/markup.py` (only if the histogram shows something worth
  keeping)
- Test: `tests/web/test_markup.py`

- [ ] **Step 1: Histogram the tags actually present**

On the deploy host:

```bash
docker compose exec db psql -U babel -d babel -c "
SELECT lower((regexp_matches(body_raw, '<\s*([a-zA-Z][a-zA-Z0-9]*)', 'g'))[1]) AS tag,
       count(*) AS n
  FROM articles TABLESAMPLE SYSTEM (2)
 WHERE body_raw IS NOT NULL
 GROUP BY 1 ORDER BY 2 DESC LIMIT 60;"
```

`TABLESAMPLE SYSTEM (2)` keeps this off a full scan of a live table. Drop it for
an exact count once the crawl is idle.

- [ ] **Step 2: Decide, one tag at a time**

For each tag above the noise floor that is not already in `KEPT` or `DROPPED`:
does rendering it change what the article means? `<center>`, `<font>` and
`<span>` do not — they stay unwrapped. `<table>`/`<tr>`/`<td>` do: a table read
as a flat run of words is a different document. Add only what earns it.

- [ ] **Step 3: Write a failing test per tag added**

For example, if tables turn out to be common:

```python
def test_a_table_keeps_its_rows_and_cells():
    out = html("<table><tr><td>a</td><td>b</td></tr></table>")
    assert "<table><tr><td>a</td><td>b</td></tr></table>" in out
```

- [ ] **Step 4: Extend `KEPT` and `BLOCKS`, run the suite**

Run: `uv run pytest tests/web/test_markup.py -v`
Expected: PASS, including the output-alphabet invariant test, which now covers
the new tags automatically because it reads `EMITTED_TAGS`.

- [ ] **Step 5: Commit and redeploy**

```bash
uv run ruff check src tests
git add src/babel/web/markup.py tests/web/test_markup.py
git commit -m "Widen the markup allowlist to what the archive actually contains"
```

Then on the host: `git pull && docker compose build web && docker compose up -d web`.
No re-crawl — this is the property the storage decision bought.

---

## Plan self-review

**Spec coverage.** Schema → Task 2. Parser capture and the size ceiling → Task 1.
Walker, allowlist, escaping, href scheme, depth ceiling → Task 3. Paragraphs from
`<br>` runs → Task 4. Images in position, placeholders, `RenderedBody.image_urls`
→ Task 5. Read path and the image map → Task 6. Route, template, CSS, the
gallery's changed purpose, the `body_raw IS NULL` fallback, comments → Task 7.
`REQUIRED_MIGRATIONS` and the `|safe` guard → Task 8. SPEC/CLAUDE/README → Task 9.
Measurement → Task 10.

**Two deliberate departures from the spec**, both flagged where they occur:

1. The spec puts the tag measurement *before* the allowlist is fixed, sampled
   from live articles. Task 10 does it *after* re-collection, from the database.
   Complete data, zero requests, and widening costs a redeploy — which the spec
   itself argues is the point of storing the markup.
2. The spec says `ImageState` carries `status`, `attempts`, `sha256` and
   `withheld`. Task 5/6 collapse the first, second and fourth into one `state`
   string computed in SQL, in the same buckets `image_status_counts` already
   uses. That keeps the bucket definition in one place, so the note under the
   article and the placeholder inside it cannot drift apart, and keeps
   `MAX_IMAGE_ATTEMPTS` out of `web/markup.py`.

**Type consistency.** `body_raw: str | None` is the same name in `models.Article`,
`models.Comment`, `browse.ArticleDetail`, `browse.CommentRow` and both tables.
`ImageState(state, sha256)` is defined once in `db/browse.py` (Task 5, Step 3)
and imported by `web/markup.py`; `db` never imports `web`. `render_body(raw,
images) -> RenderedBody(html, image_urls)` keeps that signature from Task 3
onward — Tasks 3 and 4 land it with `images` accepted and unused and
`image_urls` empty, and Task 5 fills both.
