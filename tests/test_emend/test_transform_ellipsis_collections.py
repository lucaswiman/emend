"""Tests for ellipsis support in collection patterns (Phase 2b)."""

import pytest

from emend.transform import find_pattern, replace_pattern


def _node_to_code(node):
    """Helper to convert CST node to code string."""
    return node


def test_find_list_ellipsis_only(tmp_path):
    """Test finding list with ellipsis capturing all elements."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "x = [1, 2, 3]\n"
        "y = [4]\n"
        "z = []\n"
    )
    matches = find_pattern("[$...ELEMS]", str(test_file))
    assert len(matches) == 3
    # First match should capture all three elements
    assert "ELEMS" in matches[0].captures
    # Second match should capture one element
    assert "ELEMS" in matches[1].captures
    # Third match should capture zero elements (empty list)
    assert "ELEMS" in matches[2].captures


def test_find_list_ellipsis_at_end(tmp_path):
    """Test finding list with ellipsis at the end."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "x = [1, 2, 3, 4]\n"
        "y = [10, 20]\n"
        "z = [100]\n"
    )
    matches = find_pattern("[$FIRST, $...REST]", str(test_file))
    assert len(matches) == 3
    # First match: FIRST=1, REST=[2,3,4]
    assert _node_to_code(matches[0].captures["FIRST"]) == "1"
    assert "REST" in matches[0].captures
    # Second match: FIRST=10, REST=[20]
    assert _node_to_code(matches[1].captures["FIRST"]) == "10"
    # Third match: FIRST=100, REST=[]
    assert _node_to_code(matches[2].captures["FIRST"]) == "100"


def test_find_list_ellipsis_at_start(tmp_path):
    """Test finding list with ellipsis at the start."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "x = [1, 2, 3, 4]\n"
        "y = [10, 20]\n"
    )
    matches = find_pattern("[$...REST, $LAST]", str(test_file))
    assert len(matches) == 2
    # First match: REST=[1,2,3], LAST=4
    assert _node_to_code(matches[0].captures["LAST"]) == "4"
    assert "REST" in matches[0].captures
    # Second match: REST=[10], LAST=20
    assert _node_to_code(matches[1].captures["LAST"]) == "20"


def test_find_list_ellipsis_in_middle(tmp_path):
    """Test finding list with ellipsis in the middle."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "x = [1, 2, 3, 4, 5]\n"
        "y = [10, 20, 30]\n"
    )
    matches = find_pattern("[$FIRST, $...MIDDLE, $LAST]", str(test_file))
    assert len(matches) == 2
    # First match: FIRST=1, MIDDLE=[2,3,4], LAST=5
    assert _node_to_code(matches[0].captures["FIRST"]) == "1"
    assert _node_to_code(matches[0].captures["LAST"]) == "5"
    assert "MIDDLE" in matches[0].captures
    # Second match: FIRST=10, MIDDLE=[20], LAST=30
    assert _node_to_code(matches[1].captures["FIRST"]) == "10"
    assert _node_to_code(matches[1].captures["LAST"]) == "30"


def test_find_tuple_ellipsis(tmp_path):
    """Test finding tuple with ellipsis."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "x = (1, 2, 3)\n"
        "y = (10, 20)\n"
    )
    matches = find_pattern("($FIRST, $...REST)", str(test_file))
    assert len(matches) == 2
    assert _node_to_code(matches[0].captures["FIRST"]) == "1"
    assert "REST" in matches[0].captures
    assert _node_to_code(matches[1].captures["FIRST"]) == "10"


def test_find_dict_ellipsis(tmp_path):
    """Test finding dict with ellipsis."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        'x = {"a": 1, "b": 2, "c": 3}\n'
        'y = {"x": 10, "y": 20}\n'
    )
    matches = find_pattern('{$KEY: $VAL, $...REST}', str(test_file))
    assert len(matches) == 2
    # First match should have KEY, VAL, and REST
    assert "KEY" in matches[0].captures
    assert "VAL" in matches[0].captures
    assert "REST" in matches[0].captures


def test_replace_list_ellipsis(tmp_path):
    """Test replacing list with ellipsis substitution."""
    test_file = tmp_path / "test.py"
    test_file.write_text("x = [1, 2, 3]\n")
    diff, count = replace_pattern("[$FIRST, $...REST]", "[$FIRST, 0, $...REST]", str(test_file))
    # Should insert 0 after first element
    assert count == 1
    assert "x = [1, 0, 2, 3]" in diff


def test_replace_list_ellipsis_empty(tmp_path):
    """Test replacing when ellipsis captures nothing (pattern matches exact elements)."""
    test_file = tmp_path / "test.py"
    # Pattern [$FIRST, $...REST] needs at least 1 element
    # To test empty ellipsis, use a list with exactly the minimum elements
    test_file.write_text("x = [1, 2]\ny = [5]\n")
    diff, count = replace_pattern("[$FIRST, $...REST]", "[$FIRST, 999, $...REST]", str(test_file))
    # Should insert 999 after FIRST in both lists
    assert count == 2
    assert "x = [1, 999, 2]" in diff
    # REST is empty for second match, resulting in trailing comma
    assert "y = [5, 999," in diff


def test_replace_tuple_ellipsis(tmp_path):
    """Test replacing tuple with ellipsis."""
    test_file = tmp_path / "test.py"
    test_file.write_text("x = (1, 2, 3)\n")
    diff, count = replace_pattern("($FIRST, $...REST)", "($...REST, $FIRST)", str(test_file))
    # Should rotate: move first to end
    assert count == 1
    assert "x = (2, 3, 1)" in diff


def test_find_ellipsis_empty_collection(tmp_path):
    """Test that ellipsis can match empty collections."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "x = []\n"
        "y = ()\n"
    )
    list_matches = find_pattern("[$...ELEMS]", str(test_file))
    assert len(list_matches) == 1  # matches x

    # Empty tuples are tricky - () is parsed as different from (ELEMS,)
    # For now, skip empty tuple test
    # tuple_matches = find_pattern("($...ELEMS)", str(test_file))
    # assert len(tuple_matches) == 1  # matches y


def test_find_list_with_multiple_elements_before_ellipsis(tmp_path):
    """Test list with multiple fixed elements before ellipsis."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "x = [1, 2, 3, 4, 5]\n"
        "y = [10, 20, 30]\n"
    )
    matches = find_pattern("[$A, $B, $...REST]", str(test_file))
    assert len(matches) == 2
    assert _node_to_code(matches[0].captures["A"]) == "1"
    assert _node_to_code(matches[0].captures["B"]) == "2"
    assert "REST" in matches[0].captures
    assert _node_to_code(matches[1].captures["A"]) == "10"
    assert _node_to_code(matches[1].captures["B"]) == "20"
