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
    return RenderedBody(
        html=Markup("").join(_emit(item) for item in items),
        image_urls=frozenset(),
    )
