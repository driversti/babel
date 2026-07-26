import hashlib

from babel.crawler.images import (
    capture_image,
    have_space,
    image_path,
    normalise_url,
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


def test_have_space_is_false_when_the_floor_is_absurd(tmp_path):
    assert have_space(tmp_path, 0)
    assert not have_space(tmp_path, 10**18)


async def test_capture_stores_a_live_image(tmp_path):
    async def get_bytes(url):
        return 200, b"\x89PNG fake", "image/png"

    outcome = await capture_image(
        get_bytes, tmp_path, "https://x.example/a.png", min_free_bytes=0, max_bytes=10_000
    )
    assert outcome.status == "ok"
    assert outcome.mime == "image/png"
    assert image_path(tmp_path, outcome.digest).exists()


async def test_capture_marks_a_404_as_dead(tmp_path):
    async def get_bytes(url):
        return 404, b"", None

    outcome = await capture_image(
        get_bytes, tmp_path, "https://x.example/gone.png", min_free_bytes=0, max_bytes=10_000
    )
    assert outcome.status == "dead"
    assert outcome.digest is None


async def test_capture_marks_a_non_image_response_as_dead(tmp_path):
    async def get_bytes(url):
        return 200, b"<html>parked domain</html>", "text/html"

    outcome = await capture_image(
        get_bytes, tmp_path, "https://x.example/a.png", min_free_bytes=0, max_bytes=10_000
    )
    assert outcome.status == "dead"


async def test_capture_skips_when_below_the_free_space_floor(tmp_path):
    called = {"n": 0}

    async def get_bytes(url):
        called["n"] += 1
        return 200, b"\x89PNG", "image/png"

    outcome = await capture_image(
        get_bytes, tmp_path, "https://x.example/a.png", min_free_bytes=10**18, max_bytes=10_000
    )
    assert outcome.status == "skipped_no_space"
    assert called["n"] == 0  # the floor is checked before the network call


async def test_capture_refuses_an_oversized_image(tmp_path):
    async def get_bytes(url):
        return 200, b"x" * 5000, "image/png"

    outcome = await capture_image(
        get_bytes, tmp_path, "https://x.example/big.png", min_free_bytes=0, max_bytes=1000
    )
    assert outcome.status == "error"


async def test_transport_failure_is_an_error_not_a_dead_link(tmp_path):
    async def get_bytes(url):
        raise TimeoutError("slow host")

    outcome = await capture_image(
        get_bytes, tmp_path, "https://x.example/a.png", min_free_bytes=0, max_bytes=10_000
    )
    assert outcome.status == "error"
