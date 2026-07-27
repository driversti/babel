"""Cursor encoding and the UTC/game-time boundary.

Two clocks are in play, as SPEC.md's "Date conversion" records: published_at is
a genuine UTC instant, while the game — and the e_day printed on the article
page — reckons in America/Los_Angeles. They disagree for the last 7-8 hours of
every game day, so rendering published_at raw would show a list row one day
ahead of the article it links to, on roughly a third of all rows.

Every conversion happens here, in Python, and never in a WHERE clause. Measured
on 300k rows: the row comparison is an Index Cond at 0.062ms, while
`published_at AT TIME ZONE 'America/Los_Angeles' < $1` degrades to a Filter that
removes 296,641 rows.
"""

import datetime
import re

from babel.crawler.parser import GAME_TZ
from babel.db.browse import Cursor

# [0-9], not \d: \d matches any Unicode decimal digit (e.g. Arabic-Indic
# ١٢٣), which int() also happily parses, so \d would accept cursors that
# are not the ASCII decimal this module emits.
_CURSOR_RE = re.compile(r"^([0-9]{1,19})-([0-9]{1,19})$")


def encode_cursor(published_at: datetime.datetime, article_id: int) -> str:
    """Microseconds since the epoch and the id, both unsigned decimal."""
    micros = int(published_at.timestamp() * 1_000_000)
    return f"{micros}-{article_id}"


def decode_cursor(raw: str | None) -> Cursor | None:
    """The cursor, or None if it is not one.

    None is not an error path: a link shared into a chat and truncated is the
    normal way this arrives, and the route redirects to the unpositioned list
    rather than showing a 400 for something the reader did not do.
    """
    if not raw:
        return None
    match = _CURSOR_RE.match(raw)
    if match is None:
        return None
    micros, article_id = int(match.group(1)), int(match.group(2))
    try:
        published_at = datetime.datetime.fromtimestamp(micros / 1_000_000, tz=datetime.UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return Cursor(published_at=published_at, article_id=article_id)


def to_game_time(ts: datetime.datetime) -> datetime.datetime:
    return ts.astimezone(GAME_TZ)


def game_date_to_utc(day: datetime.date) -> datetime.datetime:
    """Midnight of a game day, as an instant the cursor can use."""
    return datetime.datetime(day.year, day.month, day.day, tzinfo=GAME_TZ)


def parse_game_date(raw: str | None) -> datetime.date | None:
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return None
