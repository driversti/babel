import pytest

from babel.cli import parse_host_list


def test_splits_a_comma_separated_list():
    assert parse_host_list("i49.tinypic.com,prikachi.com") == ["i49.tinypic.com", "prikachi.com"]


def test_tolerates_whitespace_and_lowercases():
    assert parse_host_list(" I49.TinyPic.com , Prikachi.COM ") == ["i49.tinypic.com", "prikachi.com"]


def test_drops_blanks_and_duplicates_keeping_first_seen_order():
    assert parse_host_list("b.example,,a.example, b.example ,") == ["b.example", "a.example"]


def test_a_single_host_is_a_one_element_list():
    assert parse_host_list("only.example") == ["only.example"]


def test_rejects_an_empty_selection():
    with pytest.raises(ValueError, match="no host"):
        parse_host_list("  , ,")
