"""What to send for a stored blob, decided by its bytes.

`images.mime` is never echoed. It holds the third party's Content-Type stored
verbatim — `resolve_mime` returns the declared string unchanged, and
tests/crawler/test_images.py pins exactly that — so the column contains
`image/jpg`, `image/x-png` and `image/jpeg; charset=binary`. Testing those
against a literal allowlist fails genuine JPEGs and PNGs, and stops nothing,
because the string is chosen by whoever we downloaded from: SVG bytes served as
`image/png` are stored as `image/png`. A stored type carrying a non-latin-1
character cannot be re-emitted as an HTTP header at all — it raises
UnicodeEncodeError, an unhandled 500 for that blob on every request. And
save_image_blob is ON CONFLICT DO NOTHING, so a deduplicated blob keeps whatever
the first host declared and one bad host poisons the type for every article
citing those bytes.

This is the rule SPEC.md already states for ingest — "Content-Type is a hint;
the bytes are the evidence" — applied to serving. Deciding here repairs every
existing row with no migration and no re-collection.

`nosniff` and `default-src 'none'; sandbox` are the security controls. The type
list below is a serving decision, not a boundary.
"""

import re

from babel.crawler.images import sniff_image_mime

# [0-9a-f], not \d or a case-insensitive flag: \d would accept Unicode decimal
# digits (e.g. Arabic-Indic ١٢٣) and a case-insensitive match would accept
# uppercase hex, both of which digest.hex() never produces, so both would be
# accepting a string this module did not itself mint. fullmatch, not match: with
# `$` a *64-hex-char string plus one trailing "\n"* still matches, because `$`
# is permitted to match just before a trailing newline — bytes.fromhex then
# silently ignores that newline as whitespace and returns 32 valid bytes anyway.
# fullmatch has no such exception; it requires the newline to be consumed by
# the pattern, which [0-9a-f]{64} cannot do.
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# What may be rendered inline. SVG is deliberately absent: it is a document that
# can carry script, and an archived one is a document written by a stranger.
INLINE_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/tiff"}
)

# WebP identifies itself at offset 8-12, so a shorter read would miss it.
HEAD_BYTES = 16


def parse_digest(hex_digest: str) -> bytes | None:
    """The digest as bytes, or None if this is not one.

    Lowercase hex only, fixed length. The on-disk path is built from the value
    this returns, so traversal is not filtered out — it is unrepresentable:
    `image_path` derives every path segment from `digest.hex()`, not from this
    argument, and a bytes object's `.hex()` can only ever produce 64 lowercase
    hex characters — never `/`, `..`, a null byte, or anything else a
    filesystem would treat specially. That holds regardless of how the bytes
    were obtained, which is what makes strictness here a robustness property
    rather than the actual traversal boundary.
    """
    if not SHA256_RE.fullmatch(hex_digest):
        return None
    return bytes.fromhex(hex_digest)


def serving_type(head: bytes) -> tuple[str, bool]:
    """(Content-Type, render inline?) for a blob's leading bytes."""
    sniffed = sniff_image_mime(head)
    if sniffed in INLINE_TYPES:
        return sniffed, True
    return "application/octet-stream", False
