"""Content-addressed image storage.

Files are named by the SHA-256 of their bytes and sharded two levels deep, which
deduplicates the flags, avatars and unit logos that recur across thousands of
articles, and keeps any one directory small enough to list. The whole tree moves
with rsync because nothing outside it records a path.

The distinction between 'dead' and 'error' is load-bearing. 'dead' means the
host answered and the image is genuinely gone — permanent knowledge, never worth
retrying. 'error' means we could not tell, and should look again later.

Because 'dead' is permanent, it needs positive evidence, not merely an unhappy
response. The first version inferred it from any non-200, which meant a 429 threw
the image away forever; the first live run lost every rate-limited imgur image
that way. Retryability is the safe default here — this archive exists because
these links die, so the cost of a wasted retry is nothing beside the cost of
recording a live image as gone.
"""

import hashlib
import logging
import pathlib
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

log = logging.getLogger("babel.images")

BytesGetter = Callable[[str, int], Awaitable[tuple[int, bytes, str | None]]]

# The only codes that positively say the image is gone. Everything else is a
# host having a bad moment, and must stay retryable.
GONE_STATUS_CODES = frozenset({404, 410})


class ImageTooLarge(Exception):  # noqa: N818 — name is a fixed interface, not open to renaming
    """Raised by a getter that aborted a download past max_bytes."""


@dataclass(frozen=True, slots=True)
class ImageOutcome:
    status: str  # ok | dead | error
    digest: bytes | None = None
    mime: str | None = None
    size: int = 0


def normalise_url(src: str) -> str:
    """Protocol-relative sources are common in older articles."""
    if src.startswith("//"):
        return "https:" + src
    return src


def _host(url: str) -> str:
    """Host alone, so a warning groups by host instead of printing 17M URLs."""
    return urlsplit(normalise_url(url)).netloc or "?"


def image_path(root: pathlib.Path, digest: bytes) -> pathlib.Path:
    if len(digest) != 32:
        raise ValueError(f"digest must be 32 bytes (sha-256), got {len(digest)}")
    hex_digest = digest.hex()
    return pathlib.Path(root) / hex_digest[0:2] / hex_digest[2:4] / hex_digest


def have_space(root: pathlib.Path, min_free_bytes: int) -> bool:
    target = pathlib.Path(root)
    while not target.exists() and target != target.parent:
        target = target.parent
    return shutil.disk_usage(target).free >= min_free_bytes


def store_bytes(root: pathlib.Path, data: bytes) -> bytes:
    """Write bytes under their own hash. A second write of the same bytes is a no-op.

    Content-addressing means concurrent writers of identical bytes are the normal
    case, not the edge case: the whole point of this module is to deduplicate the
    flags, avatars and unit logos that recur across thousands of articles. Each
    writer gets a uniquely-named temporary file (digest alone is not enough — two
    workers hashing the same bytes would otherwise pick the same `.part` path and
    stomp on each other), so the write-then-rename below never races with itself.
    But two writers can still both pass the `path.exists()` check above before either
    finishes writing, and only one of them gets to be first at renaming into the
    content address. That is not a failure: bytes at a content address are
    interchangeable by definition, so the loser just discards its redundant copy
    instead of raising.
    """
    digest = hashlib.sha256(data).digest()
    path = image_path(root, digest)
    if path.exists():
        return digest
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.part.{uuid.uuid4().hex}")
    tmp.write_bytes(data)
    if path.exists():
        # Lost the race: another writer already stored these identical bytes.
        tmp.unlink()
        return digest
    tmp.rename(path)  # atomic, so a crash never leaves a truncated file in place
    return digest


async def capture_image(
    get_bytes: BytesGetter,
    root: pathlib.Path,
    source_url: str,
    *,
    max_bytes: int,
) -> ImageOutcome:
    """Fetch and store one image.

    Free space is deliberately not checked here. The worker checks it, because
    the correct response to a full disk is to sleep with the row still queued,
    and only the loop can do that.
    """
    try:
        status_code, data, mime = await get_bytes(normalise_url(source_url), max_bytes)
    except ImageTooLarge:
        return ImageOutcome(status="error")
    except Exception:  # noqa: BLE001 — could not determine, so not 'dead'
        return ImageOutcome(status="error")

    if status_code in GONE_STATUS_CODES:
        return ImageOutcome(status="dead")
    if status_code != 200 or not data:
        # Anything else the host said is not evidence the image is gone: 429 and
        # 5xx are explicitly "ask again", and an empty 200 is a host misbehaving.
        # These used to land in 'dead', which never retries — the first live run
        # discarded every rate-limited imgur image permanently.
        return ImageOutcome(status="error")
    if mime is None or not mime.startswith("image/"):
        # The host answered with a page rather than an image. Usually that page
        # says the upload was removed, so this stays permanent — but two hosts
        # served a landing page to a request that merely looked like navigation,
        # so log it: a run where this dominates means a header or a host changed,
        # not that the images died.
        log.warning("%s answered %s, not an image", _host(source_url), mime)
        return ImageOutcome(status="dead")

    digest = store_bytes(root, data)
    return ImageOutcome(status="ok", digest=digest, mime=mime, size=len(data))
