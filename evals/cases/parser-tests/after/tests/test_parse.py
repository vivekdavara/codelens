import pytest

from config.parse import parse


def test_pairs_and_comments():
    result = parse("# settings\nhost = db\nport=5432\n")
    assert (result == {"host": "db", "port": "5432"}, "parsed pairs")


def test_a_line_without_equals_is_rejected():
    with pytest.raises(Exception):
        parse("host db")
