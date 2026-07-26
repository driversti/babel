import asyncio
import hashlib

import pytest

from babel.crawler.images import (
    ImageTooLarge,
    capture_image,
    have_space,
    image_path,
    normalise_url,
    resolve_mime,
    sniff_image_mime,
    store_bytes,
)


def test_normalise_url_expands_protocol_relative():
    assert normalise_url("//x.example/a.png") == "https://x.example/a.png"
    assert normalise_url("https://x.example/a.png") == "https://x.example/a.png"


def test_path_is_sharded_by_the_first_two_byte_pairs(tmp_path):
    digest = bytes.fromhex("ab" + "cd" + "ef" * 30)
    path = image_path(tmp_path, digest)
    assert path.parent.name == "cd"
    assert path.parent.parent.name == "ab"
    assert path.name == digest.hex()


def test_image_path_rejects_a_digest_that_is_not_32_bytes(tmp_path):
    with pytest.raises(ValueError, match="32 bytes"):
        image_path(tmp_path, b"")
    with pytest.raises(ValueError, match="32 bytes"):
        image_path(tmp_path, b"\x00" * 31)


def test_store_bytes_writes_once_and_returns_the_digest(tmp_path):
    data = b"an image, allegedly"
    digest = store_bytes(tmp_path, data)
    assert digest == hashlib.sha256(data).digest()
    assert image_path(tmp_path, digest).read_bytes() == data


def test_storing_identical_bytes_twice_does_not_duplicate(tmp_path):
    data = b"same bytes"
    first = store_bytes(tmp_path, data)
    second = store_bytes(tmp_path, data)
    assert first == second
    assert len([p for p in tmp_path.rglob("*") if p.is_file()]) == 1


def test_different_bytes_get_different_files(tmp_path):
    store_bytes(tmp_path, b"one")
    store_bytes(tmp_path, b"two")
    assert len([p for p in tmp_path.rglob("*") if p.is_file()]) == 2


async def test_concurrent_writers_of_identical_bytes_all_succeed(tmp_path):
    """Flags, avatars and unit logos recur across thousands of articles, so concurrent
    writers racing to store the same bytes is the normal case, not the edge case. The
    loser of the race must not see the winner's rename as a failure.
    """
    data = b"same bytes, many workers"
    expected = hashlib.sha256(data).digest()

    results = await asyncio.gather(
        *(asyncio.to_thread(store_bytes, tmp_path, data) for _ in range(16))
    )

    assert all(digest == expected for digest in results)
    assert len([p for p in tmp_path.rglob("*") if p.is_file()]) == 1


def test_have_space_is_false_when_the_floor_is_absurd(tmp_path):
    assert have_space(tmp_path, 0)
    assert not have_space(tmp_path, 10**18)


def test_have_space_walks_up_to_an_existing_ancestor(tmp_path):
    deep = tmp_path / "does" / "not" / "exist" / "yet"
    assert have_space(deep, 0)
    assert not have_space(deep, 10**18)


async def test_capture_stores_a_live_image(tmp_path):
    async def get_bytes(url, max_bytes):
        return 200, b"\x89PNG fake", "image/png"

    outcome = await capture_image(
        get_bytes, tmp_path, "https://x.example/a.png", max_bytes=10_000
    )
    assert outcome.status == "ok"
    assert outcome.mime == "image/png"
    assert image_path(tmp_path, outcome.digest).exists()


async def test_capture_marks_a_404_as_dead(tmp_path):
    async def get_bytes(url, max_bytes):
        return 404, b"", None

    outcome = await capture_image(
        get_bytes, tmp_path, "https://x.example/gone.png", max_bytes=10_000
    )
    assert outcome.status == "dead"
    assert outcome.digest is None


async def test_capture_marks_a_non_image_response_as_dead(tmp_path):
    async def get_bytes(url, max_bytes):
        return 200, b"<html>parked domain</html>", "text/html"

    outcome = await capture_image(
        get_bytes, tmp_path, "https://x.example/a.png", max_bytes=10_000
    )
    assert outcome.status == "dead"


async def test_capture_marks_a_410_as_dead(tmp_path):
    """410 Gone is the other code that positively states the image is not coming back."""

    async def get_bytes(url, max_bytes):
        return 410, b"", None

    outcome = await capture_image(
        get_bytes, tmp_path, "https://x.example/gone.png", max_bytes=10_000
    )
    assert outcome.status == "dead"


@pytest.mark.parametrize("status_code", [429, 403, 500, 502, 503, 408])
async def test_a_host_refusing_us_is_retryable_not_dead(tmp_path, status_code):
    """'dead' is permanent, so only 404/410 may produce it.

    The first live run recorded 429s from imgur as 'dead', which is never
    reclaimed — every rate-limited image was lost for good. A host that
    throttles, breaks, or blocks us says nothing about whether the image
    exists.
    """

    async def get_bytes(url, max_bytes):
        return status_code, b"", "application/json"

    outcome = await capture_image(
        get_bytes, tmp_path, "https://x.example/a.png", max_bytes=10_000
    )
    assert outcome.status == "error", f"{status_code} must stay retryable"


async def test_an_empty_200_is_retryable_not_dead(tmp_path):
    """A 200 with no body is a host misbehaving, not a removal notice."""

    async def get_bytes(url, max_bytes):
        return 200, b"", "image/png"

    outcome = await capture_image(
        get_bytes, tmp_path, "https://x.example/a.png", max_bytes=10_000
    )
    assert outcome.status == "error"


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
GIF = b"GIF89a" + b"\x00" * 20
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 12


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (GIF, "image/gif"),
        (WEBP, "image/webp"),
        (b"BM" + b"\x00" * 20, "image/bmp"),
        (b"II*\x00" + b"\x00" * 20, "image/tiff"),
        (b"<!DOCTYPE html><html>gone</html>", None),
        (b"", None),
    ],
)
def test_sniff_image_mime(data, expected):
    assert sniff_image_mime(data) == expected


def test_a_declared_image_type_is_trusted_even_without_a_signature():
    """SVG has no magic bytes, and neither will the next format. Sniffing may
    only ever widen what we accept, never narrow it."""
    assert resolve_mime("image/svg+xml", b"<svg xmlns='...'></svg>") == "image/svg+xml"
    assert resolve_mime("image/png; charset=binary", PNG) == "image/png; charset=binary"


def test_a_generic_declaration_over_image_bytes_resolves_to_the_real_type():
    assert resolve_mime("application/octet-stream", PNG) == "image/png"
    assert resolve_mime(None, JPEG) == "image/jpeg"


def test_a_generic_declaration_over_a_web_page_stays_unresolved():
    assert resolve_mime("application/octet-stream", b"<!DOCTYPE html>") is None


async def test_capture_stores_an_image_mislabelled_as_octet_stream(tmp_path):
    """content.screencast.com serves live 2014 PNGs as application/octet-stream.

    Trusting the label marked 45 of them 'dead' — permanently — on the first
    live run, while every one of them downloaded fine.
    """

    async def get_bytes(url, max_bytes):
        return 200, PNG, "application/octet-stream"

    outcome = await capture_image(
        get_bytes, tmp_path, "https://x.example/a.png", max_bytes=10_000
    )
    assert outcome.status == "ok"
    assert outcome.mime == "image/png", "the stored type must be the real one, not the claimed one"
    assert image_path(tmp_path, outcome.digest).exists()


async def test_capture_refuses_an_oversized_image(tmp_path):
    async def get_bytes(url, max_bytes):
        raise ImageTooLarge

    outcome = await capture_image(
        get_bytes, tmp_path, "https://x.example/big.png", max_bytes=1000
    )
    assert outcome.status == "error"


async def test_transport_failure_is_an_error_not_a_dead_link(tmp_path):
    async def get_bytes(url, max_bytes):
        raise TimeoutError("slow host")

    outcome = await capture_image(
        get_bytes, tmp_path, "https://x.example/a.png", max_bytes=10_000
    )
    assert outcome.status == "error"
