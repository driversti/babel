"""The image getter's request shape.

curl_cffi's `impersonate="chrome"` supplies the header set a browser sends when
it *navigates* to a URL. Image hosts distinguish that from an `<img>` subresource
load, and three of the largest ones answer the navigation-shaped request with
something other than the image. Measured on the first live run: giphy and postimg
returned 200 text/html landing pages, imgur returned 429, and all three returned
the real bytes once these headers were set. So the headers are part of the
contract, not a cosmetic detail — hence a test.
"""

import pytest

from babel.cli import IMAGE_FETCH_HEADERS, MAX_REDIRECT_HOPS, _bytes_getter
from babel.crawler.images import ImageOutcome, capture_image

# Nominal public IPv4 literals. Never actually dialled — every session below is
# faked — but a literal address short-circuits classify_url without touching
# DNS (see _is_publicly_routable in images.py), which is what keeps these
# tests hermetic despite exercising the real classify_url guard.
_PUBLIC_A = "93.184.216.34"
_PUBLIC_B = "8.8.8.8"


class _Response:
    def __init__(self):
        self.status_code = 200
        self.headers = {"content-type": "image/png", "content-length": "9"}

    async def aiter_content(self):
        yield b"\x89PNG fake"

    async def aclose(self):
        pass


class _RecordingSession:
    """Captures the kwargs the getter passes, the way curl_cffi would receive them."""

    def __init__(self):
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response()


async def test_getter_asks_for_an_image_the_way_a_browser_does():
    session = _RecordingSession()
    status, data, mime = await _bytes_getter(session, 20)(f"https://{_PUBLIC_A}/a.png", 10_000)

    assert (status, mime) == (200, "image/png")
    assert data == b"\x89PNG fake"

    _, kwargs = session.calls[0]
    sent = kwargs["headers"]
    assert sent["Sec-Fetch-Dest"] == "image", "the header postimg and giphy key off"
    assert sent["Accept"].startswith("image/"), "an image request must prefer images"


def test_no_referer_is_sent():
    """A Referer also satisfies postimg, but it would disclose our crawling to
    every author-chosen third-party host. Sec-Fetch-Dest achieves the same
    without telling anyone anything."""
    assert not any(h.lower() == "referer" for h in IMAGE_FETCH_HEADERS)


@pytest.mark.parametrize("header", ["User-Agent", "user-agent"])
def test_no_user_agent_override(header):
    """Same rule as `curl_getter`: a UA that disagrees with the impersonated TLS
    fingerprint is a stronger bot signal than not impersonating at all."""
    assert header not in IMAGE_FETCH_HEADERS


class _ScriptedResponse:
    def __init__(self, status_code, *, location=None, body=b"", content_type=None):
        self.status_code = status_code
        self.headers = {}
        if location is not None:
            self.headers["location"] = location
        if content_type is not None:
            self.headers["content-type"] = content_type
        self._body = body

    async def aiter_content(self):
        yield self._body

    async def aclose(self):
        pass


class _ScriptedSession:
    """Maps each URL to the canned response it should return, the way a
    redirect chain is dialled hop by hop. Records every URL actually
    requested, so a test can assert a blocked hop was never dialled."""

    def __init__(self, responses: dict[str, _ScriptedResponse]):
        self._responses = responses
        self.requested: list[str] = []

    async def get(self, url, **kwargs):
        assert kwargs.get("allow_redirects") is False, (
            "redirects must be followed by hand so every hop can be classified"
        )
        self.requested.append(url)
        return self._responses[url]


async def test_redirect_into_a_private_address_is_refused_and_recorded_dead(tmp_path):
    """Measured directly: curl_cffi follows up to 30 redirects by default, so a
    public host that 302s into 127.0.0.1 would sail straight through
    classify_url's pre-fetch guard in capture_image, which only ever sees the
    URL the article wrote — never the hop it redirects to."""
    session = _ScriptedSession(
        {f"https://{_PUBLIC_A}/a.png": _ScriptedResponse(302, location="http://127.0.0.1/secret")}
    )

    outcome = await capture_image(
        _bytes_getter(session, 5), tmp_path, f"https://{_PUBLIC_A}/a.png", max_bytes=10_000
    )

    assert outcome == ImageOutcome(status="dead")
    assert "http://127.0.0.1/secret" not in session.requested, (
        "the private hop must be classified before it is dialled, not after"
    )


async def test_redirect_between_two_public_hosts_still_captures_the_image(tmp_path):
    """Real image hosts redirect constantly (CDN migrations, URL shorteners).
    Refusing every redirect outright would have discarded those as 'error' or
    'dead' — the same class of mistake that once discarded every rate-limited
    imgur image permanently."""
    session = _ScriptedSession(
        {
            f"https://{_PUBLIC_A}/a.png": _ScriptedResponse(
                302, location=f"https://{_PUBLIC_B}/b.png"
            ),
            f"https://{_PUBLIC_B}/b.png": _ScriptedResponse(
                200, body=b"\x89PNG fake", content_type="image/png"
            ),
        }
    )

    outcome = await capture_image(
        _bytes_getter(session, 5), tmp_path, f"https://{_PUBLIC_A}/a.png", max_bytes=10_000
    )

    assert outcome.status == "ok"
    assert outcome.mime == "image/png"


async def test_a_redirect_loop_is_retryable_not_dead(tmp_path):
    """A loop — or any chain past the hop ceiling — is not evidence the image
    is gone. It must stay 'error', the same rule this module applies to every
    other transport oddity it cannot positively explain."""
    session = _ScriptedSession(
        {
            f"https://{_PUBLIC_A}/a.png": _ScriptedResponse(
                302, location=f"https://{_PUBLIC_B}/b.png"
            ),
            f"https://{_PUBLIC_B}/b.png": _ScriptedResponse(
                302, location=f"https://{_PUBLIC_A}/a.png"
            ),
        }
    )

    outcome = await capture_image(
        _bytes_getter(session, 5), tmp_path, f"https://{_PUBLIC_A}/a.png", max_bytes=10_000
    )

    assert outcome.status == "error"
    # Not just "some exception happened" — it must have actually walked the
    # loop up to the ceiling rather than given up on the first hop.
    assert len(session.requested) == MAX_REDIRECT_HOPS + 1
