"""The image getter's request shape.

curl_cffi's `impersonate="chrome"` supplies the header set a browser sends when
it *navigates* to a URL. Image hosts distinguish that from an `<img>` subresource
load, and three of the largest ones answer the navigation-shaped request with
something other than the image. Measured on the first live run: giphy and postimg
returned 200 text/html landing pages, imgur returned 429, and all three returned
the real bytes once these headers were set. So the headers are part of the
contract, not a cosmetic detail — hence a test.
"""

import asyncio

import pytest

from babel.cli import IMAGE_FETCH_HEADERS, MAX_REDIRECT_HOPS, _bytes_getter
from babel.crawler.images import ImageOutcome, capture_image

# Nominal public IPv4 literals. Never actually dialled — every session below is
# faked — but a literal address short-circuits classify_url without touching
# DNS (see _is_publicly_routable in images.py), which is what keeps these
# tests hermetic despite exercising the real classify_url guard.
_PUBLIC_A = "93.184.216.34"
_PUBLIC_B = "8.8.8.8"


class _RecordingEvent:
    """Stands in for curl_cffi's asyncio.Event-typed `response.quit_now`.

    The real `aclose()` never touches `quit_now` at all (see I9 in CLAUDE.md) —
    it is `await self.astream_task`, which waits for curl's background fetch to
    finish rather than severing it. Only `quit_now.set()` makes curl's write
    callback (`qput`, in curl_cffi's own source) return CURL_WRITEFUNC_ERROR and
    abort the transfer early. A no-op fake `aclose()` would let a test pass
    whether or not the getter ever calls `quit_now.set()` — exactly the blind
    spot that let I9 sit open while CLAUDE.md claimed it was closed — so this
    fake records both whether and when `set()` was called, relative to
    `aclose()`, via the shared `events` list.
    """

    def __init__(self, events: list[str]):
        self._events = events
        self._is_set = False

    def set(self):
        self._is_set = True
        self._events.append("quit_now")

    def is_set(self):
        return self._is_set


class _Response:
    def __init__(self):
        self.status_code = 200
        self.headers = {"content-type": "image/png", "content-length": "9"}
        self.events: list[str] = []
        self.quit_now = _RecordingEvent(self.events)

    async def aiter_content(self):
        self.events.append("read")
        yield b"\x89PNG fake"

    async def aclose(self):
        self.events.append("aclose")


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
    """`chunks` lets a test spread a body over several `aiter_content` yields,
    so a mid-stream abort can be shown to stop partway rather than draining
    everything; `body` (a single bytes value) is the common-case shorthand.

    `chunk_delay` puts a real `await asyncio.sleep(...)` between chunks. With
    no delay, `aiter_content` never actually suspends — every chunk is
    produced synchronously once iteration starts — so there is no await point
    an external `task.cancel()` (what `asyncio.wait_for`'s timeout does) could
    ever land on mid-stream. A delay gives cancellation somewhere real to
    interrupt, the way curl_cffi's own `await self.queue.get()` does while
    waiting on the network.
    """

    def __init__(
        self,
        status_code,
        *,
        location=None,
        body=b"",
        chunks=None,
        content_type=None,
        chunk_delay=0.0,
    ):
        self.status_code = status_code
        self.headers = {}
        if location is not None:
            self.headers["location"] = location
        if content_type is not None:
            self.headers["content-type"] = content_type
        self._chunks = list(chunks) if chunks is not None else [body]
        self._chunk_delay = chunk_delay
        self.events: list[str] = []
        self.quit_now = _RecordingEvent(self.events)

    async def aiter_content(self):
        for chunk in self._chunks:
            if self._chunk_delay:
                await asyncio.sleep(self._chunk_delay)
            self.events.append("read")
            yield chunk

    async def aclose(self):
        self.events.append("aclose")


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


async def test_a_redirect_hop_is_aborted_via_quit_now_not_drained(tmp_path):
    """N1 (round 2 of review): the redirect loop `continue`s on a 3xx before
    either size check runs, and `aclose()` alone does not stop curl from
    pulling the rest of that response in the background — measured against
    real curl_cffi with a 302 carrying a 300 MB body: peak RSS went
    61 MB -> 466 MB and the capture still returned 'ok'. This is the same
    mechanism as I9 (CLAUDE.md): curl_cffi's async `aclose()` is just
    `await self.astream_task`, which waits for the transfer to finish rather
    than severing it. Only `response.quit_now.set()` makes curl's own write
    callback abort early. A fake whose `aclose()` is a no-op cannot see this
    bug at all — it was exactly why I9 stayed open once already — so this
    checks the actual sequence of events on the redirect response, not merely
    the final outcome.
    """
    redirect_resp = _ScriptedResponse(302, location=f"https://{_PUBLIC_B}/b.png")
    final_resp = _ScriptedResponse(200, body=b"\x89PNG fake", content_type="image/png")
    session = _ScriptedSession(
        {
            f"https://{_PUBLIC_A}/a.png": redirect_resp,
            f"https://{_PUBLIC_B}/b.png": final_resp,
        }
    )

    outcome = await capture_image(
        _bytes_getter(session, 5), tmp_path, f"https://{_PUBLIC_A}/a.png", max_bytes=10_000
    )

    assert outcome.status == "ok"
    assert "read" not in redirect_resp.events, "a 3xx body must never be read"
    assert redirect_resp.events == ["quit_now", "aclose"], redirect_resp.events
    # The terminal response is a normal, fully-consumed read. It must not be
    # reported as aborted — only abandoned responses set quit_now.
    assert not final_resp.quit_now.is_set()
    assert final_resp.events == ["read", "aclose"], final_resp.events


async def test_an_oversized_declared_length_is_aborted_via_quit_now(tmp_path):
    """A declared Content-Length past max_bytes is rejected before a single
    byte is read. The rejection must be a real abort, not merely an early
    `raise` that leaves curl free to keep pulling the rest of the body behind
    the getter's back — the same I9 mechanism as the redirect case above."""
    resp = _ScriptedResponse(200, content_type="image/png")
    resp.headers["content-length"] = str(50_000_000)
    session = _ScriptedSession({f"https://{_PUBLIC_A}/big.png": resp})

    outcome = await capture_image(
        _bytes_getter(session, 5), tmp_path, f"https://{_PUBLIC_A}/big.png", max_bytes=1_000
    )

    assert outcome.status == "error"
    assert "read" not in resp.events, "an oversized declared length must abort before reading"
    assert resp.events == ["quit_now", "aclose"], resp.events


async def test_an_oversized_stream_is_aborted_via_quit_now_mid_transfer(tmp_path):
    """No declared Content-Length, so the cap is only caught mid-stream — the
    shape of the report's 300 MB / 8 MiB measurement. The stream must be
    abandoned at the chunk that crosses max_bytes, with quit_now set before
    close, not drained to the end first."""
    chunks = [b"x" * 100 for _ in range(10)]  # 1000 bytes total, 100 at a time
    resp = _ScriptedResponse(200, chunks=chunks, content_type="image/png")
    session = _ScriptedSession({f"https://{_PUBLIC_A}/big.png": resp})

    outcome = await capture_image(
        _bytes_getter(session, 5), tmp_path, f"https://{_PUBLIC_A}/big.png", max_bytes=350
    )

    assert outcome.status == "error"
    read_events = [e for e in resp.events if e == "read"]
    assert len(read_events) < len(chunks), (
        "the stream must be abandoned once max_bytes is crossed, not drained to the end"
    )
    assert resp.events[-2:] == ["quit_now", "aclose"], resp.events


async def test_a_cancelled_fetch_is_severed_not_drained(tmp_path):
    """imageworker.py wraps capture_image in asyncio.wait_for(...,
    timeout=image_timeout_sec) specifically because a host that accepts a
    connection and then sends nothing once stalled the whole image archive —
    config.py records that history, and it is the defence image_timeout_sec
    exists for.

    Round 3 of review: none of the three abort paths fixed in round 2 cover
    this one. When wait_for's deadline fires, asyncio.CancelledError is
    raised inside aiter_content's suspension point — not one of the
    'completed' exits — and CancelledError is BaseException, not Exception,
    since Python 3.8, so capture_image's own generic handler does not (and
    must not) catch or hide it. Measured against real curl_cffi (see the
    report): without severing here, wait_for still raises at its own
    configured timeout, but curl keeps pulling the whole body in the
    background regardless — 100% of a 300 MB body pushed, RSS 61 -> 465 MB,
    for a host that never sends anything at all.

    A `finally` runs on cancellation exactly as it does on any other exit, so
    the inverted default in _bytes_getter (sever unless `completed` was set)
    covers this without any code specific to CancelledError — which is the
    whole point of inverting it: a future exit path does not have to
    remember to opt in.
    """
    chunks = [b"x" * 100 for _ in range(5)]
    resp = _ScriptedResponse(200, chunks=chunks, content_type="image/png", chunk_delay=0.05)
    session = _ScriptedSession({f"https://{_PUBLIC_A}/slow.png": resp})

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            capture_image(
                _bytes_getter(session, 5),
                tmp_path,
                f"https://{_PUBLIC_A}/slow.png",
                max_bytes=10_000,
            ),
            timeout=0.12,
        )

    read_events = [e for e in resp.events if e == "read"]
    assert len(read_events) < len(chunks), (
        "cancellation must sever mid-stream, not let the transfer run to completion"
    )
    assert resp.quit_now.is_set(), "a cancelled fetch must sever the response, not merely drop it"
    assert "aclose" in resp.events
