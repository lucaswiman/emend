"""Boundary contracts for the native transform API."""

import pytest

from emend import emend_core

@pytest.mark.parametrize(
    ("source", "edits", "expected"),
    [
        ("x", [(0, 0, "A"), (0, 0, "B")], "ABx"),
        ("x", [(0, 1, "X"), (0, 0, "L")], "LX"),
        ("abcd", [(3, 2, "X")], None),
        ("abcd", [(0, 5, "X")], None),
        ("é", [(1, 2, "X")], None),
        ("abcd", [(1, 3, "X"), (2, 4, "Y")], None),
    ],
)
def test_file_transform_edit_boundaries(source, edits, expected):
    transform = emend_core.PyFileTransform(source)
    for start, end, replacement in edits:
        transform.replace_range(start, end, replacement)
    assert transform.apply() == expected
