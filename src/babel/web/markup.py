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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from markupsafe import Markup, escape
from selectolax.parser import HTMLParser, Node

from babel.db.browse import ImageState

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
        # Same scheme check as an <a> href, applied to a stripped-and-
        # normalised COPY, not the value that gets stored: protocol-relative
        # sources ("//host/path") are common in older articles (see
        # crawler/images.py's normalise_url, which does the same "//" ->
        # "https://" rewrite before fetching, for the same reason), so
        # validation strips and prefixes a copy rather than rejecting "//"
        # for lacking a scheme. Image.source_url itself stores the RAW,
        # UNSTRIPPED attribute value: crawler/parser.py writes
        # img.attributes.get("src") to article_images verbatim, with no
        # .strip(), so a src carrying incidental whitespace (" //host/x ")
        # must be looked up the same way at render time or Task 5's
        # `images.get(item.source_url)` misses a blob that is on disk.
        #
        # This check is a scheme filter and nothing more. It accepts
        # anything whose normalised form parses with scheme http/https,
        # which still admits malformed or dangerous-looking netlocs
        # ("///evil", "//127.0.0.1/x.png", "//user:pass@evil.example/x")
        # that neither this check nor crawler/images.py's address guard
        # (which runs at fetch time, against already-collected URLs, not
        # here) rejects. Whether source_url is actually safe to link to or
        # fetch is Task 5's own emit-time decision, not a guarantee this
        # module makes.
        verbatim_src = node.attributes.get("src") or ""
        stripped = verbatim_src.strip()
        normalised = "https:" + stripped if stripped.startswith("//") else stripped
        return (Image(verbatim_src),) if _href(normalised) else ()

    children: list[object] = []
    for child in node.iter(include_text=True):
        children.extend(_convert(child, depth + 1))

    if kept is None:
        return tuple(children)  # unwrap: keep the content, drop the wrapper
    if kept in BLOCKS:
        # `li` is excluded: wrapping a list item's text in a <p> changes its
        # spacing for no benefit.
        #
        # A source <p> -- the game's single body wrapper, or one an author
        # nested -- dissolves directly into _paragraphs's own groups rather
        # than being wrapped a second time: each group _paragraphs returns
        # is already tagged "p", so wrapping it again in Block("p", ...)
        # would nest <p> inside <p>. (A literal port of this branch that
        # always did `Block(kept, tuple(_paragraphs(children)))` regardless
        # of kept produced exactly that double-<p> for every source <p>,
        # including the no-<br>-at-all case -- caught by
        # test_a_single_br_stays_a_line_break_inside_one_paragraph, which a
        # literal `out.count("<p>") == 1` cannot pass for the wrong reason.)
        #
        # Any other block -- blockquote, ul, ol, a heading -- keeps its own
        # tag as the wrapper, so a <blockquote> holding a double <br> reads
        # the same as the body does. But it only pays for the grouping pass,
        # and only gains an inner <p>, when its children actually contain a
        # paragraph boundary; content with none stays exactly as flat as
        # Task 3 left it. Skipping that check and always grouping broke
        # test_h1_becomes_h2_so_the_article_title_keeps_h1 -- a heading with
        # no <br> at all still produces "<h2><p>Head</p></h2>" instead of
        # "<h2>Head</h2>", because _paragraphs always wraps whatever it
        # accumulates in Block("p", ...), break or no break.
        if kept == "li":
            return (Block("li", tuple(children)),)
        if kept == "p":
            return _paragraphs(children)
        if _has_paragraph_break(children):
            return (Block(kept, _paragraphs(children)),)
        return (Block(kept, tuple(children)),)
    if kept == "a":
        href = _href(node.attributes.get("href"))
        # A link we will not follow is not a link. Keeping the <a> without an
        # href would render an inert underline that looks broken; the text is
        # what the author wrote either way.
        return (Inline("a", tuple(children), href),) if href else tuple(children)
    return (Inline(kept, tuple(children)),)


def _is_blank(items: Sequence[object]) -> bool:
    return all(
        isinstance(i, (Text, Break)) and not str(getattr(i, "value", "")).strip()
        for i in items
    )


def _has_paragraph_break(items: Sequence[object]) -> bool:
    """True if grouping `items` through `_paragraphs` would actually change
    anything: a run of two or more Breaks, or a nested Block that must stand
    apart from the inline content around it. A single Break, or plain text
    and inline elements with no Block among them, is content _paragraphs
    would return as one untouched group -- so a caller that only wants to
    know whether it needs to pay for grouping (and gains an inner <p> from
    it) can skip the call entirely when this is False.

    Whitespace-only Text between two Breaks is transparent to the run count,
    the same way `_paragraphs` itself treats it: the game writes
    "<br>\\n\\n<br>", not "<br><br>", so a whitespace Text node sitting
    between the pair must not look like it ends the run.
    """
    run = 0
    for item in items:
        if isinstance(item, Break):
            run += 1
            if run >= 2:
                return True
        elif isinstance(item, Block):
            return True
        elif isinstance(item, Text) and not item.value.strip():
            continue  # whitespace between two <br>s doesn't end the run either
        else:
            run = 0
    return False


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
        if isinstance(item, Text) and not item.value.strip() and (pending_breaks or not current):
            # Whitespace between the two <br>s of a real boundary, or leading
            # whitespace before any content, starts nothing and must not
            # reset pending_breaks: the game writes "<br>\n\n<br>", not
            # "<br><br>" -- selectolax parses that newline-newline gap into
            # its own Text node sitting *between* the two Break items, and
            # this module walks nodes in document order, so that node is
            # seen mid-run. Without the `pending_breaks` half of this guard,
            # that whitespace fell through to the same path as ordinary
            # text, which appended a lone Break and zeroed pending_breaks --
            # so a genuine two-<br> boundary was counted as two runs of one,
            # and no paragraph was ever created. Confirmed against all three
            # fixtures in tests/fixtures/: zero of their 35 <br> pairs are
            # strictly adjacent; every one has this exact whitespace gap.
            continue
        if pending_breaks >= 2:
            flush()
        elif pending_breaks == 1 and current:
            current.append(Break())
        pending_breaks = 0
        current.append(item)

    flush()
    return tuple(out)


def _placeholder_href(source_url: str) -> str | None:
    """The URL a missing-image placeholder links to as "original", if any.

    Normalised the same way _convert's scheme check normalises before
    validating an <img src>, so a protocol-relative source ("//host/x.png")
    still gets a link: crawler/images.py's normalise_url performs the same
    "//" -> "https://" rewrite before dialling, so "https:" + the stripped
    source is the URL the fetcher actually used, not a guess. Without this
    normalisation, `_href("//host/x.png")` returns None for lacking a
    scheme, and a naive "link to the original" would silently render no
    link at all for exactly the sources this archive most often carries —
    protocol-relative sources are common in older articles. This is only
    ever used to decide the placeholder's href; the lookup key into
    `images` stays the raw, unstripped `source_url` (see Image.source_url).
    """
    stripped = source_url.strip()
    normalised = "https:" + stripped if stripped.startswith("//") else stripped
    return _href(normalised)


def _emit_image(item: Image, images: Mapping[str, ImageState]) -> Markup:
    state = images.get(item.source_url)
    if state is not None and state.state == "ok" and state.sha256 is not None:
        return Markup('<img src="/img/%s" alt="" loading="lazy">') % state.sha256.hex()

    caption = _CAPTIONS[state.state if state is not None else None]
    href = None if (state is not None and state.state in _UNLINKED) else _placeholder_href(
        item.source_url
    )
    if href is None:
        return Markup('<span class="missing-image">%s</span>') % caption
    return Markup(
        '<span class="missing-image">%s '
        '<a href="%s" rel="nofollow noreferrer" target="_blank">original</a></span>'
    ) % (caption, href)


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
    # `%` escapes its arguments, and escape() returns Markup unchanged because
    # Markup carries __html__ — so `inner` is not double-escaped.
    return Markup("<%s>%s</%s>") % (item.tag, inner, item.tag)


def _image_urls(items: Sequence[object]) -> set[str]:
    found: set[str] = set()
    for item in items:
        if isinstance(item, Image):
            found.add(item.source_url)
        elif isinstance(item, (Inline, Block)):
            found |= _image_urls(item.children)
    return found


def render_body(raw: str, images: Mapping[str, ImageState]) -> RenderedBody:
    tree = HTMLParser(raw or "")
    # Remove every DROPPED subtree once, before the walk, rather than
    # relying only on the per-node check inside _convert. The depth
    # ceiling's flatten path calls node.text(), which walks descendant text
    # nodes in C and does not know about DROPPED -- so a <script> or
    # <style> sitting deeper than MAX_DEPTH would otherwise have its source
    # text resurrected as escaped but visible text. Stripping first means
    # there is nothing left under a too-deep node for that flatten to find.
    # The check inside _convert stays as defense in depth; it is simply
    # never reached for these tags once this has run.
    #
    # strip_tags(), not a Python-side removal loop: an earlier version used
    # `while (n := root.css_first(sel)) is not None: n.decompose()`
    # specifically to avoid double-decomposing a node whose ancestor was
    # already freed (a plain `for n in root.css(sel): n.decompose()` could
    # do that, since DROPPED can nest -- e.g. <form> containing <input>).
    # But css_first() rescans the whole tree on every call, so that loop is
    # O(n^2) in the number of dropped elements. Measured: 8,000 dropped
    # elements in one body (body_raw can hold roughly five times that many
    # under its 1,000,000-character cap) took 25.3s in the while-loop
    # version against ~5ms here -- long enough to stall the `web`
    # process's event loop, since the route handlers are async, and take
    # /healthz down with every other in-flight request. strip_tags() runs
    # entirely in selectolax's C layer and is linear.
    tree.strip_tags(sorted(DROPPED), recursive=True)
    root = tree.body
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
