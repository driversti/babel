"""One live look at an image host, so the report can tell dead from merely slow.

`probe_host` reuses the same two seams the worker fetches through — `classify_url`
for DNS and address safety, and a `get_bytes` for the actual request — and
collapses the result into a single word the operator reads next to the row
counts. It never stores anything; it only reports what the host does right now.
"""

import socket

from babel.crawler.hostprobe import probe_host
from babel.crawler.images import ImageBlocked, ImageTooLarge

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


async def _resolves(_host):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


async def _nxdomain(_host):
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


def _getter(*, status=200, data=_PNG, mime="image/png", raises=None):
    async def get_bytes(url, max_bytes):
        if raises is not None:
            raise raises
        return status, data, mime

    return get_bytes


async def test_nxdomain_when_the_name_does_not_resolve():
    dialled = False

    async def get_bytes(url, max_bytes):
        nonlocal dialled
        dialled = True
        return 200, _PNG, "image/png"

    verdict = await probe_host(get_bytes, "https://gone.example/a.png", max_bytes=1000, resolve=_nxdomain)

    assert verdict == "nxdomain"
    assert not dialled, "a name that will not resolve is not worth a request"


async def test_blocked_when_the_url_resolves_to_a_private_address():
    dialled = False

    async def get_bytes(url, max_bytes):
        nonlocal dialled
        dialled = True
        return 200, _PNG, "image/png"

    verdict = await probe_host(get_bytes, "http://127.0.0.1/x.png", max_bytes=1000)

    assert verdict == "blocked"
    assert not dialled


async def test_unreachable_when_the_fetch_raises_a_transport_error():
    verdict = await probe_host(
        _getter(raises=ConnectionRefusedError()),
        "https://host.example/a.png",
        max_bytes=1000,
        resolve=_resolves,
    )
    assert verdict == "unreachable"


async def test_blocked_when_a_redirect_hop_is_private():
    verdict = await probe_host(
        _getter(raises=ImageBlocked("http://10.0.0.1/x")),
        "https://host.example/a.png",
        max_bytes=1000,
        resolve=_resolves,
    )
    assert verdict == "blocked"


async def test_alive_when_the_image_is_too_large_to_buffer():
    """The getter aborts past the cap — but it was streaming a real image body."""
    verdict = await probe_host(
        _getter(raises=ImageTooLarge("too big")),
        "https://host.example/huge.png",
        max_bytes=8,
        resolve=_resolves,
    )
    assert verdict == "alive"


async def test_gone_on_404_and_410():
    for code in (404, 410):
        verdict = await probe_host(
            _getter(status=code, data=b"<html>not found</html>", mime="text/html"),
            "https://host.example/a.png",
            max_bytes=1000,
            resolve=_resolves,
        )
        assert verdict == "gone", code


async def test_http_error_on_5xx_and_403():
    for code in (500, 503, 403, 429):
        verdict = await probe_host(
            _getter(status=code, data=b"", mime="text/html"),
            "https://host.example/a.png",
            max_bytes=1000,
            resolve=_resolves,
        )
        assert verdict == "http-error", code


async def test_not_image_when_200_is_a_landing_page():
    verdict = await probe_host(
        _getter(status=200, data=b"<!doctype html><title>Postimages</title>", mime="text/html"),
        "https://host.example/a.png",
        max_bytes=1000,
        resolve=_resolves,
    )
    assert verdict == "not-image"


async def test_not_image_on_an_empty_200():
    verdict = await probe_host(
        _getter(status=200, data=b"", mime="image/png"),
        "https://host.example/a.png",
        max_bytes=1000,
        resolve=_resolves,
    )
    assert verdict == "not-image"


async def test_alive_when_200_returns_image_bytes():
    verdict = await probe_host(
        _getter(status=200, data=_PNG, mime="image/png"),
        "https://host.example/a.png",
        max_bytes=1000,
        resolve=_resolves,
    )
    assert verdict == "alive"


async def test_alive_when_the_declared_type_is_an_image_without_a_signature():
    """SVG has no magic bytes; a declared image/* is trusted, matching resolve_mime."""
    verdict = await probe_host(
        _getter(status=200, data=b"<svg xmlns='...'></svg>", mime="image/svg+xml"),
        "https://host.example/a.svg",
        max_bytes=1000,
        resolve=_resolves,
    )
    assert verdict == "alive"


async def test_protocol_relative_sample_url_is_handled():
    verdict = await probe_host(
        _getter(status=200, data=_PNG, mime="image/png"),
        "//host.example/a.png",
        max_bytes=1000,
        resolve=_resolves,
    )
    assert verdict == "alive"
