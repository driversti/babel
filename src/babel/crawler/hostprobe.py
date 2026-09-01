"""One live request to an image host, reduced to a single verdict.

The `article_images` rows carry no failure reason — only a status and an attempt
count — and the in-memory circuit breaker is gone on every restart. So the only
way to tell a host that is gone for good (tinypic, NXDOMAIN since 2019) from one
that timed out once is to ask it again now. `probe_host` does exactly that, once
per host, through the same seams the worker fetches through, and hands
`babel image-hosts` a word to print next to the backlog counts.

Nothing is stored. The verdict is advisory — the operator, not this function,
decides what to write off.
"""

import logging

from babel.crawler.images import (
    ImageBlocked,
    ImageTooLarge,
    Resolver,
    classify_url,
    normalise_url,
    resolve_mime,
)

log = logging.getLogger("babel.images")

# What the report may show. 'alive' and 'http-error' are the two that say "do not
# write this host off": the first serves images, the second (403 hotlink guard,
# 429, 5xx) is not positive evidence the images are gone.
VERDICTS = frozenset(
    {"nxdomain", "blocked", "unreachable", "gone", "http-error", "not-image", "alive"}
)

# The verdicts that, together with a lifetime 'ok' of zero, make a host safe to
# pass to `kill-image-host` without --force.
WRITE_OFF_VERDICTS = frozenset({"nxdomain", "blocked", "unreachable", "gone", "not-image"})


async def probe_host(get_bytes, sample_url: str, *, max_bytes: int, resolve: Resolver | None = None) -> str:
    """Fetch one of a host's queued images and classify what came back.

    `get_bytes` is the worker's own getter: `(url, max_bytes) -> (status, bytes,
    mime)`, raising `ImageBlocked` for a redirect into a private address,
    `ImageTooLarge` once a body passes the cap, and ordinary transport errors
    otherwise.
    """
    verdict = await classify_url(sample_url, resolve=resolve)
    if verdict == "blocked":
        return "blocked"
    if verdict == "unresolved":
        return "nxdomain"

    try:
        status, data, mime = await get_bytes(normalise_url(sample_url), max_bytes)
    except ImageBlocked:
        return "blocked"
    except ImageTooLarge:
        # The getter only aborts after bytes have started arriving, and it rejects
        # an over-large Content-Length before reading any — either way the host
        # was answering with a real image body.
        return "alive"
    except Exception:  # noqa: BLE001 — any transport fault means we could not reach it
        return "unreachable"

    if status == 200 and data and resolve_mime(mime, data) is not None:
        return "alive"
    if status in (404, 410):
        return "gone"
    if 400 <= status < 600:
        return "http-error"
    return "not-image"
