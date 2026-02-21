"""Tests for comprehension patterns (Phase 3a)."""

import pytest
import libcst as cst

from emend.transform import find_pattern, replace_pattern


def test_find_listcomp(tmp_path):
    """Test finding list comprehension patterns."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "result = [x for x in items]\n"
        "other = [a + 1 for a in range(10)]\n"
    )

    matches = find_pattern("[$X for $X in $Y]", str(test_file))
    assert len(matches) == 1
    # Check metavar captures
    assert "X" in matches[0].captures
    assert "Y" in matches[0].captures


def test_find_dictcomp(tmp_path):
    """Test finding dict comprehension patterns."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "result = {k: v for k, v in items}\n"
        "other = {k: v * 2 for k, v in pairs}\n"
    )

    matches = find_pattern("{$K: $V for $K, $V in $ITEMS}", str(test_file))
    assert len(matches) == 1
    # Check captures
    assert "K" in matches[0].captures
    assert "V" in matches[0].captures
    assert "ITEMS" in matches[0].captures


def test_find_genexp(tmp_path):
    """Test finding generator expression patterns."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "result = any(x for x in items)\n"
        "other = sum(x * 2 for x in nums)\n"
    )

    # Match the Call with a genexp argument
    matches = find_pattern("any($X for $X in $Y)", str(test_file))
    assert len(matches) == 1


def test_find_setcomp(tmp_path):
    """Test finding set comprehension patterns."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "result = {x for x in items}\n"
        "other = {x * 2 for x in nums}\n"
    )

    matches = find_pattern("{$X for $X in $Y}", str(test_file))
    assert len(matches) == 1


def test_replace_listcomp(tmp_path):
    """Test replacing list comprehension patterns."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "result = [x for x in items]\n"
    )

    diff, count = replace_pattern("[$X for $X in $Y]", "list($Y)", str(test_file), apply=True)
    assert count == 1
    content = test_file.read_text()
    assert "result = list(items)" in content


def test_find_comp_with_if(tmp_path):
    """Test finding comprehensions with if clauses."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "result = [x for x in items if x > 0]\n"
        "other = [y for y in nums if y % 2]\n"
    )

    matches = find_pattern("[$X for $X in $Y if $Z]", str(test_file))
    assert len(matches) == 2
    assert "X" in matches[0].captures
    assert "Y" in matches[0].captures
    assert "Z" in matches[0].captures


def test_find_nested_comp(tmp_path):
    """Test finding nested comprehensions."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "result = [[x for x in row] for row in matrix]\n"
    )

    # Find inner comprehension
    matches = find_pattern("[$X for $X in row]", str(test_file))
    assert len(matches) == 1
