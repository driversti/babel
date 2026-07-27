# Article markup — design

Date: 2026-07-27
Status: approved, not implemented

## The problem

Archived articles render as one unbroken wall of text. There are two separate
causes and they need different fixes.

**Old rows.** `_body_text` (`crawler/parser.py`) only started injecting `\n` at
block boundaries on this branch, and that changes new fetches only. Every article
collected before it is stored as a single line. CLAUDE.md already carries the
remedy as a mandatory pre-launch step ("Re-collect the article bodies"); nothing
in this design changes that, it only widens what the pass writes.

**Markup discarded at parse time.** `articles.body` is markup-stripped text, so
bold, italic, underline, links and image position do not exist in the database at
all. Images are served as a gallery at the foot of the page rather than where the
author put them. No re-collection fixes this, because the information is dropped
before it is stored.

The goal is a rendered article as close to the in-game one as the archive can
honestly get.

## What the game actually serves

Measured against `tests/fixtures/*.html` (2026-07). The body is BBCode rendered
to HTML:

```html
<p>Aziz eTürkiyem o/<br><br>
<u>30 ve üstü</u> oyu geçebilirsek yorum atan her vatandaşıma <b>1000 Q7</b> …<br><br>
<a href="https://resmim.net/" target="_blank"><img src="…ECBQFR.png" class="bbcode_img"></a><br><br>
Turan Parisi Yönetimi</p>
```

Tag counts across the three fixtures — body: `br` 76, `p` 2, `b` 2, `a` 2,
`img` 2, `u` 1, `q` 1. Comment bodies: `br` 20, `a` 9, `i` 1.

Two properties matter:

- **A paragraph is a run of two or more `<br>`.** The whole body sits in one `<p>`.
- **Emoji are elements**, not characters: `<q class="emoji emoji_1f635">😵</q>`.
  The game's own emoji pass produces these, sometimes by accident — one fixture
  turns the literal text `100%)` into a face.

This sample is three pages from 2026. The archive spans twenty years of BBCode,
so the long tail (tables, `[quote]`, `[center]`, `[color]`, lists) is unmeasured
and almost certainly present. See "Measurement" below.

Comment bodies carry the same markup and are currently worse off: `parse_comments`
uses `text(separator=" ")`, so a comment's `<br>` does not even survive as a
newline. Comments outnumber articles 16:1 (161,155 against 10,093), so they are
most of the archive by row count.

## Decision: store the raw markup, transform at render time

Three storage models were considered. The axis that decides between them is not
security or size — it is **what a mistake costs**, because the transform has to
be written against markup we have not seen.

- **A. Rebuild at ingest, store safe HTML.** Cheapest read path. A bug in the
  converter costs a full re-walk of 2.8M articles — roughly 32 days. This is
  precisely the position the `\n` defect put the project in.
- **B. Store a structured JSON document, render through an autoescaped macro.**
  Same ingest-time cost on a bug. Largest on disk. Its one real advantage —
  author bytes never reach the output unescaped — is available to C as well.
- **C. Store the game's raw `postBody`, transform on read.** Chosen.

C wins on one argument and it is decisive for this project:

- **A rendering bug costs a redeploy, not a re-crawl.** CLAUDE.md records four
  image defects found by watching a live run rather than by reasoning, and
  `migrations/004_image_identity.sql` explicitly laments that `body` is stripped
  text, leaving `source_url` as the only surviving record of an article's images.
  Storing the markup closes that class permanently: any future improvement to
  rendering is `docker compose up -d web`.
- **The allowlist becomes measurable.** Today we cannot count which tags occur
  across twenty years, because the markup is not in the database. After this
  change we can, and the allowlist widens from data rather than from guesswork.
- **Security is not worse than A.** A also ends in `|safe`, over a string written
  earlier by the same code we would write. The sanitizer is ours either way; only
  its timing differs. C additionally adopts B's rendering discipline (below), so
  the escaping property is structural in both.

Cost: this reverses SPEC.md's "Raw HTML is not archived". The reversal is
narrower than what that decision rejected — that was the full page at 118 GB;
this is `postBody` and comment bodies only, about 1.4× the text already stored,
so roughly 13 GB across the full archive against 1.7 TB free. It must be recorded
in SPEC.md as a revised decision with its reason, not silently broken.

`body` remains a separate plain-text column. Phase 3's cross-language search and
the list snippets need text; markup must never enter a full-text index.

### Rejected outright

**Hotlinking a missing image from its original host.** It discloses every
reader's IP to third parties, breaks the premise of a self-contained archive, and
shows a live image that will be gone tomorrow. `img-src 'self'` in the existing
CSP would block it regardless.

**Rewriting in-article erepublik links to internal archive links.** Considered
and declined: links stay exactly as the author wrote them.

## Schema

Migration `007_body_markup.sql`:

```sql
ALTER TABLE articles ADD COLUMN body_raw text;
ALTER TABLE comments ADD COLUMN body_raw text;
```

**The column is named `body_raw`, not `body_html`.** It holds untrusted bytes
from a third-party server. `body_html` would read as "already safe" and would
invite `|safe` in a template. The migration says so in a comment, and a test
guards against `body_raw` appearing with `|safe` anywhere in `templates/`.

**Nullable, and `body` stays `NOT NULL`.** `body_raw IS NULL` means "collected
before this change" and renders through the existing `white-space: pre-wrap`
path. This is what makes the deploy safe at any moment: the site keeps working
for the 10,093 articles and 161,155 comments not yet re-collected, and the
re-collection can proceed incrementally instead of gating `web`.

`ALTER TABLE ... ADD COLUMN` with no default is instant in Postgres, so this
migration does not carry migration 005's `ShareLock` problem.

## Parser

`Article` and `Comment` gain `body_raw: str | None`.

- `parse_article` captures `div.postBody`'s **outer** HTML (`node.html`).
- `parse_comments` captures `div.details p`'s outer HTML.

Outer rather than inner because the walker unwraps `div` and `p` wrappers
anyway, so no string surgery is needed to strip them and none can go wrong.

Both already hold the node, so this adds no DOM traversal and no requests.

`_body_text` is unchanged, and its existing tests are the regression guard that
`body` still means what it meant.

**Size ceiling.** `body_raw` is whatever a remote server sent; one pathological
article must not be able to bloat the table. Bodies average 3.4 KB; the limit is
1,000,000 characters of the markup string, and exceeding it truncates and logs.
Truncated markup is harmless because the render-time parse is lenient.

`_parse_images` is unchanged. `save_article` and the comment upsert
(`db/repo.py`) each gain one column in the `INSERT` and in the `DO UPDATE SET`.

## Rendering

New module `src/babel/web/markup.py`, one entry point:

```python
render_body(raw: str, images: Mapping[str, ImageState]) -> RenderedBody
```

`RenderedBody` carries the `Markup` and the set of `source_url`s the walk
actually emitted. The gallery below needs that set, and taking it from the walk
is exact — a substring scan of `body_raw` for a URL would be a heuristic over
markup that may be truncated or may mention a URL in text without rendering it.

### Step 1 — walk against an allowlist

Parse with selectolax, walk the tree, build our own nodes. A tag in the output
can only come from a literal dict in this module; every text node passes through
`markupsafe.escape`. There is therefore no path by which an author's bytes reach
the output unescaped — the structural property that motivated approach B, kept
here.

Three categories:

- **Kept:** `p`, `br`, `b`/`strong`, `i`/`em`, `u`, `s`/`strike`/`del`,
  `blockquote`, `ul`, `ol`, `li`, `h1`–`h6`, `a`, `img`. `b`→`strong`,
  `i`→`em`, `h1`→`h2` so `h1` stays the article title's.
- **Unwrapped** (tag dropped, children kept): `div`, `span`, `font`, `center`,
  `q`, and **everything unrecognised**. The emoji `<q>` becomes its character.
  Twenty years of unmeasured BBCode degrades to text with structure rather than
  vanishing.
- **Dropped with their children:** `script`, `style`, `iframe`, `object`,
  `embed`, `svg`, `noscript`, `template`, `head`, `meta`, `link`. Unwrapping
  these would dump JavaScript or CSS source into the page as visible text.

No **input** attribute survives the walk except `href` on `<a>` and `src` on
`<img>`. `href` is checked for scheme: `http` and `https` only. `javascript:`,
`data:`, `vbscript:`, protocol-relative and relative URLs produce no link — the
text remains. The attributes that do appear in the output are ones we add
ourselves: surviving links get `rel="nofollow noreferrer ugc"` and
`target="_blank"`; images get `src`, `alt`, `loading`.

**Depth ceiling of 100.** selectolax will parse arbitrarily deep markup and a
recursive emitter would exhaust the stack on it. At the ceiling the subtree is
replaced by its escaped text content, taken via selectolax's own `.text()` —
*not* unwrapped, since unwrapping keeps the children and so keeps recursing,
which would not bound the stack at all.

### Step 2 — paragraphs

The game puts the whole body in one `<p>` and separates paragraphs with a double
`<br>`. Runs of **two or more** `<br>` become a paragraph boundary; a single
`<br>` stays a `<br>`. Empty paragraphs at either end are dropped — one fixture's
body opens with `<br><br>`.

This is the one deliberate departure from literal fidelity. The game would keep
`<br><br>`; we emit `<p>`. Visually equivalent (a blank line either way), but it
yields real margins controllable from CSS and meaningful structure instead of
`<br>` soup.

### Step 3 — images

One extra query per article page builds a map `source_url → (status, sha256)`
from `article_images`, which migration 004 already keys on
`(article_id, source_url)`, so it is a direct lookup. The `src` in the markup is
the same string `_parse_images` recorded at ingest, so the match is exact.

- `ok` with a `sha256` → `<img src="/img/{hex}" loading="lazy" alt="">`, served
  from our own disk. The existing `img-src 'self'` CSP permits this and blocks
  everything else.
- anything else → a placeholder in position: a bordered box captioned by status
  (`dead` → "image lost", `pending` → "not captured yet", attempts exhausted →
  "could not be retrieved") plus a `rel="nofollow noreferrer"` link to the
  original URL. No automatic request to a third-party host; following it stays
  the reader's deliberate choice.

`<a href><img></a>` is the BBCode idiom `[url=…][img]…[/img][/url]`, and both body
images in `tests/fixtures/article_with_images.html` use it. **This document
originally said it needs no special case. That was wrong**, and review found two
defects behind the claim:

- **The takedown rule breaks.** Suppressing the placeholder's own link for a
  withheld blob does nothing about the author's anchor around it, so
  `<a href="https://h/1.png"><img src="https://h/1.png"></a>` at `withheld` still
  renders a live link to the original of a blob `babel hide --image` was run on.
- **Nested `<a>` does not survive parsing.** Re-parsed with lexbor — the same
  HTML5 tree construction a browser performs — the adoption-agency algorithm
  hoists the placeholder's "original" link out of `.missing-image` *and* out of
  the author's anchor, so the placeholder's styling misses it and the caption
  text inherits the author's href.

The rule adopted instead: **an `<a>` containing an image is dropped, its children
kept, unless every image inside it renders as a real `<img>`.** The anchor exists
to make the image clickable; with no image there is nothing to click, and
dropping it satisfies the takedown rule outright rather than by case analysis on
hrefs.

For a present image nothing more is needed: it stays an inline node and
`display: block; max-width: 100%` in CSS does the rest.

### The foot-of-page gallery changes purpose

Today it shows every captured blob. Once images render in position, it holds only
`ok` blobs whose `source_url` is absent from `RenderedBody.image_urls` — the case
`004_image_identity.sql` protects, where the author removed an image after we had
already stored it, which is exactly what this archive exists for. Its caption
says so. For rows with `body_raw IS NULL` the gallery behaves exactly as it does
now.

### Comments

The same `render_body`, the same module, and the same `body_raw IS NULL`
fallback to the stored text.

One difference: an `<img>` in a comment has no `article_images` row at all,
because `_parse_images` scans `postBody` only. A comment therefore renders with
an empty image map, and every image in it takes the placeholder path. Its caption
must not say "not captured yet", which would promise a capture that is never
coming — comment images get their own wording, "not archived", plus the link.

Capturing comment images is **out of scope**: the image queue already fails to
keep up with the walk (0.04–1.35 img/s against the ~5.3 img/s articles produce)
and comments outnumber articles 16:1. That is a separate decision for later.

### CSS

`.body-text` no longer needs `white-space: pre-wrap` on the new path; the
declaration stays as a fallback class for `body_raw IS NULL` rows. New rules for
`p`, `ul`, `blockquote`, `img` and the placeholder box, legible in both colour
schemes like the rest of the sheet.

## Deployment

The re-collection pass this needs is one CLAUDE.md already requires before
launch, for the `\n` fix. The same pass now fills `body_raw` too. Zero additional
HTTP requests: this changes what gets written during work that is already
scheduled, not the amount of work.

`007_body_markup.sql` must be added to the required-migrations list `web` checks
before serving (`web/app.py`). Browse queries will select `body_raw`; without the
check a skipped migrate answers 503 on every page while `/healthz` stays green —
the trap this project has already documented once.

```bash
docker compose stop crawler images
docker compose run --rm crawler babel migrate
docker compose build crawler images web
docker compose up -d web
```

`web` can come up immediately after the migration and before any re-collection —
rows with `body_raw IS NULL` take the old path. Then the re-collection procedure
from README (`babel refetch --from/--to`, then a one-shot `babel run --no-poll`
until the queue drains; finding M1 means a running service will not reach its
sweep phase for another ~32 days), and finally
`docker compose up -d crawler images`.

Documentation to update: SPEC.md gets the revised raw-markup decision with its
reason, plus a new section recording the measured structure of an article body —
SPEC is where facts about the source live. CLAUDE.md's pre-launch step 2 is
reworded to cover markup, not only line breaks.

## Testing

`tests/web/test_markup.py` is the core, because all of the risk is in one module:

- allowlist mapping; an unrecognised tag unwraps; `script`/`style`/`iframe`
  disappear **with their children**
- `href`: `javascript:`, `data:`, `vbscript:`, protocol-relative `//evil`, and
  smuggling through whitespace or control characters (`java&#9;script:`) all
  produce no link
- attributes: `onclick`, `style`, `srcset`, `formaction` do not survive
- `<br><br>` becomes a paragraph, a single `<br>` stays one, empty paragraphs at
  the edges are dropped
- the emoji `<q>` unwraps to its character; the depth ceiling holds; markup
  truncated at the size limit does not raise
- **invariant test**: parse the output back and assert every tag is in the
  allowlist and every attribute is in the permitted set. This is the test that
  catches the case nobody anticipated, which is what was missing twice before in
  this project.

Then: `test_parser.py` — `body_raw` captured for article and comments, existing
`body` assertions unchanged, the size ceiling truncates and logs;
`test_repo.py` — round-trip through `save_article` including the update branch;
`test_article_page.py` — inline `/img/{hex}`, placeholder with a `nofollow` link
for `dead`/`pending`, the old path when `body_raw IS NULL`, the gallery holding
only images absent from the body; `test_app.py` — `007` in the required-migrations
check; and a guard test that no template pairs `body_raw` with `|safe`. The
existing 204-test suite stays green.

## Measurement

Before the allowlist is fixed, sample a few hundred articles spread across e-days
through the tunnel and histogram the tags in `postBody`. The current evidence is
three pages from 2026 against an archive spanning twenty years. This is cheap and
it is how this project already works — four image defects were found by measuring
a live run, not by reasoning about it.

Getting the allowlist wrong is survivable precisely because of the storage
decision above: the fix is `docker compose up -d web`, not another 32-day crawl.

## Out of scope

- Capturing images referenced from comments.
- Rewriting in-article erepublik links to internal archive links.
- Reproducing the game's visual style (fonts, colours, chrome). This is about
  structure; the archive keeps its own minimal sheet.
- Full-text search over the markup. `body` remains the text column phase 3 will
  index.
