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

from babel.cli import IMAGE_FETCH_HEADERS, _bytes_getter


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
    status, data, mime = await _bytes_getter(session, 20)("https://x.example/a.png", 10_000)

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
