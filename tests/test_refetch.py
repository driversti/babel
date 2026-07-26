import pytest

from babel.cli import parse_id_selection


def test_parses_an_explicit_list():
    assert parse_id_selection("3,1,2", None, None) == [1, 2, 3]


def test_tolerates_whitespace_and_duplicates():
    assert parse_id_selection(" 5 , 5, 4 ", None, None) == [4, 5]


def test_parses_an_inclusive_range():
    assert parse_id_selection(None, 10, 13) == [10, 11, 12, 13]


def test_a_single_id_range_is_one_id():
    assert parse_id_selection(None, 7, 7) == [7]


def test_rejects_a_reversed_range():
    with pytest.raises(ValueError, match="--from must not exceed --to"):
        parse_id_selection(None, 20, 10)


def test_rejects_giving_neither():
    with pytest.raises(ValueError, match="either --ids or --from/--to"):
        parse_id_selection(None, None, None)


def test_rejects_giving_both():
    with pytest.raises(ValueError, match="not both"):
        parse_id_selection("1,2", 10, 20)


def test_rejects_half_a_range():
    with pytest.raises(ValueError, match="both --from and --to"):
        parse_id_selection(None, 10, None)


def test_rejects_non_numeric_ids():
    with pytest.raises(ValueError, match="not a number"):
        parse_id_selection("1,two,3", None, None)
