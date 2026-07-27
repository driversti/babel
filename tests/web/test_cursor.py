import datetime

from babel.db.browse import Cursor
from babel.web.cursor import (
    decode_cursor,
    encode_cursor,
    game_date_to_utc,
    parse_game_date,
    to_game_time,
)

UTC = datetime.UTC


def test_cursor_round_trips():
    ts = datetime.datetime(2026, 7, 21, 5, 53, 10, tzinfo=UTC)
    decoded = decode_cursor(encode_cursor(ts, 2797025))
    assert decoded == Cursor(published_at=ts, article_id=2797025)


def test_malformed_cursors_decode_to_none():
    for raw in (None, "", "abc", "123", "-1", "12-", "-12", "1.5-2", "1-2-3", "١٢٣-٤"):
        assert decode_cursor(raw) is None


def test_cursor_survives_a_url_round_trip():
    ts = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    raw = encode_cursor(ts, 5)
    assert "-" in raw
    assert raw.replace("-", "").isdigit()


def test_cursor_round_trip_is_exact_across_the_2038_float64_boundary():
    """Pins exact round-tripping, not just today's sample of it.

    `int(ts.timestamp() * 1_000_000)` / `micros / 1_000_000` go through
    float64. That is exact for small timestamps, but seconds-since-epoch
    passes 2**31 at 2038-01-19 03:14:08 UTC, and from there a meaningful
    fraction of microsecond values lose the last microsecond of mantissa
    precision and decode one microsecond off - permanently, since the archive
    is meant to run indefinitely. A cursor off by one microsecond lands on the
    wrong side of its own row, which is exactly the repeated-or-skipped
    boundary row keyset pagination exists to prevent.

    Confirmed against the float-based implementation before the fix: of
    these five cases, three (the +3us ones) decoded one microsecond low.
    """
    boundary = datetime.datetime(2038, 1, 19, 3, 14, 8, tzinfo=UTC)  # 2**31 seconds since epoch
    cases = (
        boundary,
        boundary + datetime.timedelta(microseconds=3),
        boundary + datetime.timedelta(days=200, microseconds=3),
        datetime.datetime(2040, 6, 15, 12, 0, 0, 3, tzinfo=UTC),
        datetime.datetime(2099, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
    )
    for ts in cases:
        decoded = decode_cursor(encode_cursor(ts, 1))
        assert decoded.published_at == ts, f"{ts.isoformat()} round-tripped to {decoded.published_at.isoformat()}"


def test_evening_publication_reads_as_the_previous_game_day():
    # The fixture case: datePublished 2026-07-21 05:53 GMT is game day 6817,
    # 20 July. Rendering the UTC date would put the list a day ahead of the
    # article page it links to.
    ts = datetime.datetime(2026, 7, 21, 5, 53, 10, tzinfo=UTC)
    assert to_game_time(ts).date() == datetime.date(2026, 7, 20)


def test_game_midnight_converts_to_a_utc_instant():
    bound = game_date_to_utc(datetime.date(2026, 7, 20))
    assert bound.tzinfo is not None
    assert bound.astimezone(UTC) == datetime.datetime(2026, 7, 20, 7, 0, tzinfo=UTC)


def test_game_midnight_in_january_uses_pst_not_a_hardcoded_offset():
    # July is PDT (UTC-7); the brief's own test only exercises that case. January
    # is PST (UTC-8), so this is the case that would catch a `timedelta(hours=7)`
    # standing in for a real zoneinfo conversion.
    bound = game_date_to_utc(datetime.date(2026, 1, 20))
    assert bound.astimezone(UTC) == datetime.datetime(2026, 1, 20, 8, 0, tzinfo=UTC)


def test_parse_game_date_rejects_rubbish():
    assert parse_game_date("2026-07-20") == datetime.date(2026, 7, 20)
    assert parse_game_date(None) is None
    assert parse_game_date("") is None
    assert parse_game_date("20/07/2026") is None
    assert parse_game_date("2026-13-40") is None
