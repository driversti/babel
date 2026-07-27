from babel.web.blobs import parse_digest, serving_type

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HTML = b"<!doctype html><html><body>gone</body></html>"


def test_valid_digest_parses():
    assert parse_digest("ab" * 32) == bytes.fromhex("ab" * 32)


def test_traversal_and_junk_are_rejected():
    for raw in (
        "../../etc/passwd",
        "AB" * 32,
        "ab" * 31,
        "ab" * 33,
        "",
        "zz" * 32,
        "ab/cd",
        "ab" * 32 + "\x00",
        "\x00" + "ab" * 32,
        "ab" * 32 + "\n",  # `$` matches before a trailing newline; fullmatch must not.
        "١" * 64,  # Arabic-Indic digits: not in [0-9a-f], unlike \d
    ):
        assert parse_digest(raw) is None


def test_png_is_served_inline_as_png():
    assert serving_type(PNG) == ("image/png", True)


def test_jpeg_is_served_inline_even_though_hosts_call_it_image_jpg():
    # images.mime holds the remote host's header verbatim, so 'image/jpg' and
    # 'image/x-png' are both in the column. The bytes are what decide.
    assert serving_type(JPEG) == ("image/jpeg", True)


def test_webp_is_recognised_at_offset_eight():
    assert serving_type(WEBP) == ("image/webp", True)


def test_svg_is_a_download_not_an_inline_document():
    assert serving_type(SVG) == ("application/octet-stream", False)


def test_a_removal_notice_page_is_a_download():
    assert serving_type(HTML) == ("application/octet-stream", False)


def test_empty_bytes_do_not_crash():
    assert serving_type(b"") == ("application/octet-stream", False)
