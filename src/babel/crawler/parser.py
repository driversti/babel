"""Pure HTML -> models. No network, no database, no I/O of any kind.

Structure of an article page, as observed 2026-07:

    div.postContent[itemType="https://schema.org/Article"]
      h2 > a                                  the title
      meta[itemprop=datePublished]            a real UTC timestamp
      a[href^="/en/main/news/latest/all/"]    "Published in {Country}"
      div.postBody                            the article, images inline

The <title> tag carries the game day ("... on day 6,822 ..."), and the meta
description carries the comment count ("... 25 comments. ...").

Two clocks are in play and they disagree. datePublished is genuine UTC; the day
number and the description's date are the game's own reckoning in PST. An
article published after 16:00 PST is already "tomorrow" in UTC. published_at
takes the UTC stamp, e_day takes the game's.

The byline inside postContent ("by Woody Woodpacker") is plain text, not a
link — there is no citizen profile id next to it. The only anchors to
/en/citizen/profile/{id} on the page live in the comment thread, where authors
often turn up replying to their own article. So author_id is recovered by
matching author_name (from meta[itemprop=author], or the <title> fallback)
against citizen profile links anywhere in the document, not just within
postContent. When no such link exists (an author who never comments), author_id
stays None — that is a real, expected outcome, not a parse failure.
"""

import datetime
import re
import zoneinfo

from selectolax.parser import HTMLParser
from selectolax.parser import Node as HTMLNode

from babel.models import Article, Comment, ImageRef

GAME_TZ = zoneinfo.ZoneInfo("America/Los_Angeles")
GAME_EPOCH = datetime.date(2007, 11, 21)  # day 1, not day 0

_EDAY_RE = re.compile(r"on day ([\d,]+)")
_COMMENTS_RE = re.compile(r"\.\s*(\d+)\s+comments?\b")
_COUNTRY_HREF_RE = re.compile(r"^/en/main/news/latest/all/([^/]+)/")
_CITIZEN_HREF_RE = re.compile(r"^/en/citizen/profile/(\d+)")
_PUBLISHED_BY_RE = re.compile(r"published by (.+?) on day")
_BR_RE = re.compile(r"<br\b[^>]*>", re.IGNORECASE)
_BLOCK_CLOSE_RE = re.compile(
    r"</(?:p|div|li|ul|ol|h[1-6]|tr|table|blockquote|pre)\s*>", re.IGNORECASE
)


def eday_to_date(eday: int) -> datetime.date:
    """Game day number -> calendar date in game time. Day 1 is 2007-11-21."""
    return GAME_EPOCH + datetime.timedelta(days=eday - 1)


def parse_article(html: str, article_id: int) -> Article | None:
    """Return the parsed article, or None if this is not an article page."""
    tree = HTMLParser(html)
    post = tree.css_first("div.postContent")
    body_node = tree.css_first("div.postBody")
    if post is None or body_node is None:
        return None

    published_at = _parse_published_at(post)
    if published_at is None:
        return None

    title_node = post.css_first("h2 a") or post.css_first("h2")
    title = title_node.text(strip=True) if title_node else ""

    title_tag = tree.css_first("title")
    title_text = title_tag.text() if title_tag else ""

    country = _parse_country(post)
    author_name = _parse_author_name(post, title_text)
    author_id = _parse_author_id(tree, author_name)
    e_day = _parse_eday(title_text)
    comment_count = _parse_comment_count(tree)
    images = _parse_images(body_node)
    comments = parse_comments(html)

    return Article(
        id=article_id,
        title=title,
        body=_body_text(body_node),
        author_id=author_id,
        author_name=author_name,
        country=country,
        published_at=published_at,
        e_day=e_day,
        comment_count=comment_count,
        images=images,
        comments=comments,
    )


def _body_text(body_node: HTMLNode) -> str:
    """The article text with block structure preserved as newlines.

    selectolax joins text nodes with a single separator and knows nothing about
    block boundaries, so `text(separator=" ")` welded a heading to the sentence
    after it and produced bodies containing no newline at all — measured at 0
    newlines against 41 `<br>` in one fixture. The stored column is the only copy
    (raw HTML is deliberately not archived, see SPEC.md "Decisions"), so the
    structure has to survive the parse or it is gone.

    Breaks are injected into the markup rather than reconstructed from the text,
    because by the time selectolax has flattened it the boundary is a space and
    indistinguishable from the spaces inside a sentence.
    """
    html = body_node.html or ""
    html = _BR_RE.sub("\n", html)
    html = _BLOCK_CLOSE_RE.sub(lambda m: "\n" + m.group(0), html)

    raw = HTMLParser(html).text(separator=" ", strip=False)

    lines: list[str] = []
    for line in raw.split("\n"):
        stripped = " ".join(line.split())
        # Keep at most one blank line: authors separate paragraphs with runs of
        # <br>, and every one of them would otherwise become its own gap.
        if stripped or (lines and lines[-1]):
            lines.append(stripped)
    return "\n".join(lines).strip()


def _parse_published_at(post: HTMLNode) -> datetime.datetime | None:
    node = post.css_first('meta[itemprop="datePublished"]')
    raw = (node.attributes.get("content") or "").strip() if node else ""
    if not raw:
        return None
    raw = raw.removesuffix(" GMT").strip()
    try:
        naive = datetime.datetime.strptime(raw, "%b %d %Y %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=datetime.UTC)


def _parse_country(post: HTMLNode) -> str | None:
    for link in post.css("a"):
        m = _COUNTRY_HREF_RE.match(link.attributes.get("href") or "")
        if m:
            return m.group(1).replace("-", " ")
    return None


def _parse_author_name(post: HTMLNode, title_text: str) -> str | None:
    meta = post.css_first('meta[itemprop="author"]')
    name = meta.attributes.get("content") if meta else None
    if name:
        return name
    m = _PUBLISHED_BY_RE.search(title_text)
    return m.group(1).strip() if m else None


def _parse_author_id(tree: HTMLParser, author_name: str | None) -> int | None:
    if not author_name:
        return None
    for link in tree.css('a[href^="/en/citizen/profile/"]'):
        label = link.attributes.get("title") or link.text(strip=True)
        if label != author_name:
            continue
        m = _CITIZEN_HREF_RE.match(link.attributes.get("href") or "")
        if m:
            return int(m.group(1))
    return None


def _parse_eday(title_text: str) -> int | None:
    m = _EDAY_RE.search(title_text)
    return int(m.group(1).replace(",", "")) if m else None


def _parse_comment_count(tree: HTMLParser) -> int:
    desc = tree.css_first('meta[name="description"]')
    desc_text = (desc.attributes.get("content") or "") if desc else ""
    m = _COMMENTS_RE.search(desc_text)
    return int(m.group(1)) if m else 0


def _parse_images(body_node: HTMLNode) -> tuple[ImageRef, ...]:
    # Enumerate after filtering, so positions stay contiguous even when an
    # <img> carries no src.
    sources = [src for src in (img.attributes.get("src") for img in body_node.css("img")) if src]
    return tuple(ImageRef(position=i, source_url=src) for i, src in enumerate(sources))


_COMMENT_ID_RE = re.compile(r"comment(\d+)")
_PADDING_RE = re.compile(r"padding-left:\s*(\d+)px")
_COMMENT_TIME_RE = re.compile(r"Day\s+([\d,]+),\s*(\d{1,2}):(\d{2})")

# Each nesting level is indented by a fixed 30px in the rendered thread.
_INDENT_PX_PER_LEVEL = 30


def parse_comments(html: str) -> tuple[Comment, ...]:
    """Extract the comment thread in document order.

    Removed comments render as "[removed]" and are kept with a NULL body: the
    slot existing is itself information, and dropping it would renumber the
    thread.
    """
    tree = HTMLParser(html)
    out: list[Comment] = []
    for position, node in enumerate(tree.css("div.commentWrapper")):
        id_m = _COMMENT_ID_RE.fullmatch(node.attributes.get("id") or "")
        if not id_m:
            continue

        depth = 0
        for child in node.css("div[style]"):
            pad_m = _PADDING_RE.search(child.attributes.get("style") or "")
            if pad_m:
                depth = int(pad_m.group(1)) // _INDENT_PX_PER_LEVEL
                break

        author_id, author_name = None, None
        link = node.css_first('a[href^="/en/citizen/profile/"]')
        if link is not None:
            m = _CITIZEN_HREF_RE.match(link.attributes.get("href") or "")
            author_id = int(m.group(1)) if m else None
            author_name = link.attributes.get("title") or link.text(strip=True) or None

        posted_at = None
        time_node = node.css_first("div.details span")
        if time_node is not None:
            posted_at = _parse_comment_time(time_node.text(strip=True))

        body_node = node.css_first("div.details p")
        # text(strip=True) only strips each individual text fragment before
        # joining with the separator -- an HTML comment (present on every
        # comment <p>) plus surrounding newlines still leaves whitespace
        # around the joined result, so the final string needs its own strip.
        body = body_node.text(separator=" ", strip=True).strip() if body_node else ""
        if not body or body == "[removed]":
            body = None

        out.append(
            Comment(
                id=int(id_m.group(1)),
                position=position,
                depth=depth,
                author_id=author_id,
                author_name=author_name,
                posted_at=posted_at,
                body=body,
            )
        )
    return tuple(out)


def _parse_comment_time(text: str) -> datetime.datetime | None:
    """"Day 6,819, 21:34" → an aware datetime in game time."""
    m = _COMMENT_TIME_RE.search(text)
    if not m:
        return None
    day = eday_to_date(int(m.group(1).replace(",", "")))
    hour, minute = int(m.group(2)), int(m.group(3))
    return datetime.datetime(day.year, day.month, day.day, hour, minute, tzinfo=GAME_TZ)
