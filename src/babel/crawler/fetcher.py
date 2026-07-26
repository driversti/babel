"""HTTP with retries, and a real HTTP adapter kept behind an injected coroutine.

Splitting orchestration from transport means the retry rules are unit-testable
without a network, which matters more here than usual: this logic runs a few
million times and its failure modes are the ones that quietly corrupt a crawl.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from curl_cffi.requests import AsyncSession

Getter = Callable[[str], Awaitable[tuple[int, str]]]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class FetchResult:
    status: str  # ok | missing | error
    html: str | None = None
    http_status: int | None = None
    error: str | None = None


def _looks_like_cloudflare(body: str) -> bool:
    return "cf_chl" in body or "Just a moment" in body


async def fetch_article(
    get: Getter, url: str, *, max_attempts: int = 3, backoff_sec: float = 2.0
) -> FetchResult:
    """Fetch one article page.

    404 means the article does not exist — deleted, or a gap in the ID sequence —
    and is a final answer, not a failure to retry. A 200 without a postBody is
    treated as an error rather than an empty article, because that shape is what
    an interstitial or a truncated response looks like, and recording it as a
    successful empty article would be an unrecoverable data loss.
    """
    last_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            status_code, body = await get(url)
        except Exception as e:  # noqa: BLE001 — transport errors are all retryable
            last_error = f"{type(e).__name__}: {e}"
        else:
            if status_code == 404:
                return FetchResult(status="missing", http_status=404)
            if status_code == 200 and "postBody" in body:
                return FetchResult(status="ok", html=body, http_status=200)
            if _looks_like_cloudflare(body):
                return FetchResult(
                    status="error",
                    http_status=status_code,
                    error="cloudflare challenge — exit node is being blocked",
                )
            last_error = f"http {status_code}, no postBody in {len(body)} bytes"
        if attempt < max_attempts:
            await asyncio.sleep(backoff_sec * attempt)
    return FetchResult(status="error", error=last_error)


def curl_getter(session: AsyncSession, timeout_sec: int) -> Getter:
    """Adapt a curl-cffi session to the Getter shape.

    curl-cffi impersonates a real Chrome TLS fingerprint, which is what keeps
    Cloudflare uninterested.
    """

    async def get(url: str) -> tuple[int, str]:
        response = await session.get(
            url, timeout=timeout_sec, headers={"User-Agent": USER_AGENT}
        )
        return response.status_code, response.text

    return get
