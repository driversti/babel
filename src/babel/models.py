"""Row shapes shared by the parser, the repository and the ingest pipeline.

Frozen so a parsed article cannot be mutated on its way to the database.
"""

import datetime
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ImageRef:
    position: int
    source_url: str


@dataclass(frozen=True, slots=True)
class Comment:
    id: int
    position: int
    depth: int
    author_id: int | None
    author_name: str | None
    posted_at: datetime.datetime | None
    body: str | None  # None when the comment was removed
    body_raw: str | None = None  # the markup the game served, untrusted


@dataclass(frozen=True, slots=True)
class Article:
    id: int
    title: str
    body: str
    author_id: int | None
    author_name: str | None
    country: str | None
    published_at: datetime.datetime
    e_day: int | None
    comment_count: int
    body_raw: str | None = None  # the markup the game served, untrusted
    images: tuple[ImageRef, ...] = field(default_factory=tuple)
    comments: tuple[Comment, ...] = field(default_factory=tuple)
