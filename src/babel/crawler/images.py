"""Content-addressed image storage.

Files are named by the SHA-256 of their bytes and sharded two levels deep, which
deduplicates the flags, avatars and unit logos that recur across thousands of
articles, and keeps any one directory small enough to list. The whole tree moves
with rsync because nothing outside it records a path.

The distinction between 'dead' and 'error' is load-bearing. 'dead' means the
host answered and the image is genuinely gone — permanent knowledge, never worth
retrying. 'error' means we could not tell, and should look again later.
"""

import hashlib
import pathlib
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

BytesGetter = Callable[[str], Awaitable[tuple[int, bytes, str | None]]]


@dataclass(frozen=True, slots=True)
class ImageOutcome:
    status: str  # ok | dead | error | skipped_no_space
    digest: bytes | None = None
    mime: str | None = None
    size: int = 0


def normalise_url(src: str) -> str:
    """Protocol-relative sources are common in older articles."""
    if src.startswith("//"):
        return "https:" + src
    return src


def image_path(root: pathlib.Path, digest: bytes) -> pathlib.Path:
    hex_digest = digest.hex()
    return pathlib.Path(root) / hex_digest[0:2] / hex_digest[2:4] / hex_digest


def have_space(root: pathlib.Path, min_free_bytes: int) -> bool:
    target = pathlib.Path(root)
    while not target.exists() and target != target.parent:
        target = target.parent
    return shutil.disk_usage(target).free >= min_free_bytes


def store_bytes(root: pathlib.Path, data: bytes) -> bytes:
    """Write bytes under their own hash. A second write of the same bytes is a no-op."""
    digest = hashlib.sha256(data).digest()
    path = image_path(root, digest)
    if path.exists():
        return digest
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.rename(path)  # atomic, so a crash never leaves a truncated file in place
    return digest


async def capture_image(
    get_bytes: BytesGetter,
    root: pathlib.Path,
    source_url: str,
    *,
    min_free_bytes: int,
    max_bytes: int,
) -> ImageOutcome:
    if not have_space(root, min_free_bytes):
        return ImageOutcome(status="skipped_no_space")
    try:
        status_code, data, mime = await get_bytes(normalise_url(source_url))
    except Exception:  # noqa: BLE001 — could not determine, so not 'dead'
        return ImageOutcome(status="error")

    if status_code != 200 or not data:
        return ImageOutcome(status="dead")
    if mime is None or not mime.startswith("image/"):
        # Parked domains and "file removed" pages answer 200 with HTML.
        return ImageOutcome(status="dead")
    if len(data) > max_bytes:
        return ImageOutcome(status="error", size=len(data))

    digest = store_bytes(root, data)
    return ImageOutcome(status="ok", digest=digest, mime=mime, size=len(data))
