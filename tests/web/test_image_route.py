import hashlib

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


async def _store(pool, image_root, data, *, declared_mime, withheld=False):
    from babel.crawler.images import store_bytes

    digest = store_bytes(image_root, data)
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO images (sha256, mime, bytes, withheld_at)
               VALUES ($1, $2, $3, CASE WHEN $4 THEN now() END)
               ON CONFLICT (sha256) DO UPDATE SET mime = EXCLUDED.mime,
                                                  withheld_at = EXCLUDED.withheld_at""",
            digest, declared_mime, len(data), withheld,
        )
    return digest.hex()


async def test_png_declared_as_image_jpg_is_still_served_as_png(client, pool, image_root):
    # The declaration is the remote host's and is never echoed.
    hex_digest = await _store(pool, image_root, PNG, declared_mime="image/jpg")
    response = await client.get(f"/img/{hex_digest}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert "content-disposition" not in response.headers


async def test_svg_declared_as_png_is_a_download(client, pool, image_root):
    hex_digest = await _store(pool, image_root, SVG, declared_mime="image/png")
    response = await client.get(f"/img/{hex_digest}")
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert "attachment" in response.headers["content-disposition"]


async def test_non_latin1_declared_mime_does_not_500(client, pool, image_root):
    hex_digest = await _store(pool, image_root, PNG, declared_mime="image/png;charset=€")
    response = await client.get(f"/img/{hex_digest}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")


async def test_image_responses_are_sandboxed(client, pool, image_root):
    hex_digest = await _store(pool, image_root, PNG, declared_mime="image/png")
    response = await client.get(f"/img/{hex_digest}")
    assert response.headers["x-content-type-options"] == "nosniff"
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "sandbox" in csp


async def test_traversal_and_junk_are_404(client):
    for raw in ("../../etc/passwd", "AB" * 32, "ab" * 31, "zz" * 32):
        assert (await client.get(f"/img/{raw}")).status_code == 404


async def test_missing_file_is_404_not_500(client, pool, image_root):
    digest = hashlib.sha256(b"never written").digest()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO images (sha256, mime, bytes) VALUES ($1,'image/png',5) "
            "ON CONFLICT (sha256) DO NOTHING",
            digest,
        )
    assert (await client.get(f"/img/{digest.hex()}")).status_code == 404


async def test_withheld_blob_is_404(client, pool, image_root):
    hex_digest = await _store(pool, image_root, PNG, declared_mime="image/png", withheld=True)
    assert (await client.get(f"/img/{hex_digest}")).status_code == 404


async def test_cache_is_revalidatable_not_immutable(client, pool, image_root):
    # Content addressing would justify immutable, but this content is other
    # people's and has to stay retractable.
    hex_digest = await _store(pool, image_root, PNG, declared_mime="image/png")
    cache = (await client.get(f"/img/{hex_digest}")).headers["cache-control"]
    assert "immutable" not in cache
    assert "must-revalidate" in cache
