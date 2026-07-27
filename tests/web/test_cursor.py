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
