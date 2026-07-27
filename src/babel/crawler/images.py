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

import asyncio
import hashlib
import ipaddress
import logging
import pathlib
import shutil
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

log = logging.getLogger("babel.images")

BytesGetter = Callable[[str, int], Awaitable[tuple[int, bytes, str | None]]]

# The only codes that positively say the image is gone. Everything else is a
# host having a bad moment, and must stay retryable.
GONE_STATUS_CODES = frozenset({404, 410})

# Leading bytes that identify an image regardless of what the host claims in
# Content-Type. Needed because hosts lie by omission: content.screencast.com
# serves 2014-era Jing PNGs — twelve years old and still alive, exactly what
# this archive exists to rescue — labelled 'application/octet-stream', and
# trusting the label discarded all of them.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)


def sniff_image_mime(data: bytes) -> str | None:
    """The image type the bytes actually are, or None if they are not an image.

    RIFF containers carry their format at offset 8, so WebP needs a second look
    rather than a prefix match.
    """
    for prefix, mime in _MAGIC:
        if data.startswith(prefix):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def resolve_mime(declared: str | None, data: bytes) -> str | None:
    """Settle on a type, or None if this is not an image at all.

    A declared `image/*` is trusted even when the bytes carry no signature we
    know — SVG has none, and neither will the next format — so this only ever
    widens what we accept. What it adds is the reverse case: a generic or absent
    declaration over bytes that are unmistakably an image.
    """
    if declared and declared.split(";")[0].strip().startswith("image/"):
        return declared
    return sniff_image_mime(data)


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


def url_host(url: str) -> str:
    """Host alone. Warnings group by host because that is the unit failures arrive
    in and the unit `requeue-images --host` recovers. Shared with the worker."""
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


_ALLOWED_SCHEMES = frozenset({"http", "https"})


async def classify_url(url: str) -> str:
    """Whether this image URL may be fetched at all: ok | blocked | unresolved.

    `source_url` is raw `img@src` from article HTML written by anyone, and the
    worker runs inside gluetun's namespace with FIREWALL_OUTBOUND_SUBNETS
    covering the whole Docker bridge range. Without this an author could point an
    <img> at an internal service; once the browser publishes /img/{sha256} they
    could then read the response back off their own article page.

    The three outcomes are not cosmetic. 'blocked' is a permanent property of the
    URL and maps to 'dead'; 'unresolved' is a resolver having a bad minute and
    must stay retryable, because a false 'dead' is the expensive mistake in this
    project and DNS is exactly the kind of thing that fails transiently.

    Residual, deliberately accepted: the address is checked before the fetch, so
    a host that answers this lookup publicly and the fetch privately (DNS
    rebinding) is not covered. Closing that needs connect-time pinning inside
    curl_cffi, which is a larger change than the exposure warrants.
    """
    parts = urlsplit(normalise_url(url))
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return "blocked"
    host = parts.hostname
    if not host:
        return "blocked"
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except socket.gaierror:
        return "unresolved"
    if not infos:
        return "unresolved"
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return "blocked"
        # is_global is False for loopback, link-local, private, reserved,
        # multicast and CGNAT alike, which is exactly the set we refuse.
        if not address.is_global:
            return "blocked"
    return "ok"


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
    verdict = await classify_url(source_url)
    if verdict == "blocked":
        log.warning("%s is not a publicly routable address, refusing", url_host(source_url))
        return ImageOutcome(status="dead")
    if verdict == "unresolved":
        return ImageOutcome(status="error")
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
    resolved = resolve_mime(mime, data)
    if resolved is None:
        # Neither the declared type nor the bytes themselves are an image. The
        # host answered with something else — usually a page saying the upload
        # was removed — so this stays permanent. It is logged per host because
        # every false 'dead' so far arrived in a batch from a single host, and
        # silence is what let the first one run unnoticed.
        log.warning("%s answered %s, not an image", url_host(source_url), mime)
        return ImageOutcome(status="dead")

    digest = store_bytes(root, data)
    return ImageOutcome(status="ok", digest=digest, mime=resolved, size=len(data))
