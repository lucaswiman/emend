"""Tests for f-string patterns (Phase 3b)."""

import pytest

from emend.transform import find_pattern, replace_pattern


def test_find_fstring_simple(tmp_path):
    """Test finding simple f-string patterns."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        'msg = f"hello {name}"\n'
        'other = f"goodbye {user}"\n'
        'plain = "not an fstring"\n'
    )

    matches = find_pattern('f"hello {$X}"', str(test_file))
    assert len(matches) == 1
    assert "X" in matches[0].captures


def test_find_fstring_multi_expr(tmp_path):
    """Test finding f-strings with multiple interpolations."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        'msg = f"{x} is {y}"\n'
        'other = f"{a} is {b}"\n'
        'wrong = f"{x} and {y}"\n'
    )

    matches = find_pattern('f"{$X} is {$Y}"', str(test_file))
    assert len(matches) == 2
    assert "X" in matches[0].captures
    assert "Y" in matches[0].captures


def test_replace_fstring(tmp_path):
    """Test replacing f-string patterns."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        'log = f"debug: {value}"\n'
    )

    diff, count = replace_pattern('f"debug: {$X}"', 'f"info: {$X}"', str(test_file), apply=True)
    assert count == 1
    content = test_file.read_text()
    assert 'f"info: {value}"' in content


@pytest.mark.parametrize("middle", ["middle", "{{middle}}"])
def test_find_fstring_literal_parts(tmp_path, middle):
    """Test f-strings with mixed literal and metavar parts."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        'msg1 = f"prefix {x} middle {y} suffix"\n'
        'msg2 = f"prefix {a} middle {b} suffix"\n'
        'wrong = f"prefix {x} wrong {y} suffix"\n'.replace("middle", middle)
    )

    matches = find_pattern('f"prefix {$X} middle {$Y} suffix"'.replace("middle", middle), str(test_file))
    assert [match.captures for match in matches] == [{"X": "x", "Y": "y"}, {"X": "a", "Y": "b"}]


def test_find_fstring_no_match(tmp_path):
    """Test that plain strings don't match f-string patterns."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        'msg = "hello {name}"\n'  # Plain string, not f-string
        'other = f"hello"\n'  # F-string without interpolation
    )

    matches = find_pattern('f"hello {$X}"', str(test_file))
    assert len(matches) == 0
