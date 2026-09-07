"""The shared compiler must produce executable patterns in every language."""
import pytest

from emend.transform import find_pattern


@pytest.mark.parametrize("extension, source", [
    ("py", "func(1)\nfunc(2, 3)\nother(4)\n"),
    ("ts", "func(1);\nfunc(2, 3);\nother(4);\n"),
    ("rs", "fn main() { func(1); func(2, 3); other(4); }"),
])
@pytest.mark.parametrize("pattern, captures", [
    ("func($X)", [{"X": "1"}]),
    ("func($_)", [{}]),
    ("func($...ARGS)", [{"ARGS": "1"}, {"ARGS": "2, 3"}]),
])
def test_shared_compiler_matching(tmp_path, extension, source, pattern, captures):
    path = tmp_path / f"sample.{extension}"
    path.write_text(source)
    assert [match.captures for match in find_pattern(pattern, str(path))] == captures


@pytest.mark.parametrize("extension, pattern, source, captures", [
    ("py", "x = $V", "x = 1\ny = 2\n", {"V": "1"}),
    ("py", "import $M", "import pathlib\nx = 1\n", {"M": "pathlib"}),
    ("py", "from __future__ import $X", "from __future__ import annotations\nfrom typing import Any\n", {"X": "annotations"}),
    ("ts", "console.log($X)", "console.log(1); console.warn(2);", {"X": "1"}),
    ("rs", "Vec::new()", "fn main() { Vec::new(); Vec::empty(); Map::new(); }", {}),
    ("rs", "$X.parse::<i32>()",
     "fn main() { text.parse::<i32>(); text.parse::<u32>(); text.into::<i32>(); }", {"X": "text"}),
])
def test_shared_compiler_statements(tmp_path, extension, pattern, source, captures):
    path = tmp_path / f"sample.{extension}"
    path.write_text(source)
    assert [match.captures for match in find_pattern(pattern, str(path))] == [captures]
