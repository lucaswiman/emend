"""Tests for --inside/--not-inside structural constraints (Phase 2c)."""

import pytest

from emend.transform import find_pattern, replace_pattern


def test_find_inside_def_keyword(tmp_path):
    """Test finding patterns inside function definitions using 'def' keyword."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "print('module level')\n"
        "def func():\n"
        "    print('inside func')\n"
        "class MyClass:\n"
        "    def method(self):\n"
        "        print('inside method')\n"
        "print('module level 2')\n"
    )

    matches = find_pattern("print($X)", str(test_file), inside="def")
    # Should only match prints inside functions, not module level
    assert len(matches) == 2
    # Verify captured strings are the ones inside functions
    captured_values = {match.captures["X"] for match in matches}
    assert captured_values == {"'inside func'", "'inside method'"}


def test_find_inside_class_keyword(tmp_path):
    """Test finding patterns inside class definitions using 'class' keyword."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "x = 1\n"
        "class MyClass:\n"
        "    y = 2\n"
        "    def method(self):\n"
        "        z = 3\n"
        "w = 4\n"
    )

    matches = find_pattern("$NAME = $VALUE", str(test_file), inside="class")
    # Should match y=2 and z=3 (inside class), not x=1 or w=4
    assert len(matches) == 2
    # Verify the matched variable names are y and z
    captured_names = {match.captures["NAME"] for match in matches}
    assert captured_names == {"y", "z"}


def test_find_not_inside_if(tmp_path):
    """Test finding patterns NOT inside if statements."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "print('before')\n"
        "if condition:\n"
        "    print('inside if')\n"
        "print('after')\n"
    )

    matches = find_pattern("print($X)", str(test_file), not_inside="if")
    # Should only match prints outside if blocks
    assert len(matches) == 2
    for match in matches:
        node_code = match.captures["X"]
        assert node_code in ["'before'", "'after'"]


def test_find_inside_for_keyword(tmp_path):
    """Test finding patterns inside for loops."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "x = 1\n"
        "for i in range(10):\n"
        "    y = 2\n"
        "    print(i)\n"
        "z = 3\n"
    )

    matches = find_pattern("$NAME = $VALUE", str(test_file), inside="for")
    # Should only match y=2 inside the for loop
    assert len(matches) == 1
    node_code = matches[0].captures["NAME"]
    assert node_code == "y"


def test_find_inside_async_def_keyword(tmp_path):
    """Test finding patterns inside async function definitions."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "def sync_func():\n"
        "    await_var = 1\n"
        "async def async_func():\n"
        "    result = await something()\n"
        "x = 2\n"
    )

    matches = find_pattern("$NAME = $VALUE", str(test_file), inside="async def")
    # Should only match inside async functions
    assert len(matches) == 1
    node_code = matches[0].captures["NAME"]
    assert node_code == "result"


def test_find_inside_while_keyword(tmp_path):
    """Test finding patterns inside while loops."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "x = 0\n"
        "while x < 10:\n"
        "    x += 1\n"
        "    print(x)\n"
        "y = 0\n"
    )

    matches = find_pattern("print($X)", str(test_file), inside="while")
    assert len(matches) == 1


def test_find_inside_try_keyword(tmp_path):
    """Test finding patterns inside try blocks."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "x = 1\n"
        "try:\n"
        "    y = 2\n"
        "    risky()\n"
        "except:\n"
        "    z = 3\n"
        "w = 4\n"
    )

    matches = find_pattern("$NAME = $VALUE", str(test_file), inside="try")
    # Should match assignments in try and except blocks
    assert len(matches) == 2  # y=2 and z=3


def test_find_inside_with_keyword(tmp_path):
    """Test finding patterns inside with statements."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "x = 1\n"
        "with open('file.txt') as f:\n"
        "    y = f.read()\n"
        "    print(y)\n"
        "z = 2\n"
    )

    matches = find_pattern("$NAME = $VALUE", str(test_file), inside="with")
    assert len(matches) == 1
    node_code = matches[0].captures["NAME"]
    assert node_code == "y"


def test_replace_inside(tmp_path):
    """Test replacement only applies inside constraint."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "print('module')\n"
        "def func():\n"
        "    print('function')\n"
    )

    diff, count = replace_pattern("print($X)", "logger.info($X)", str(test_file), inside="def")
    # Should only replace inside function
    assert count == 1
    assert "logger.info('function')" in diff
    # Module level print should remain
    assert "print('module')" in diff or "-print('module')" not in diff


def test_find_not_inside_class(tmp_path):
    """Test finding patterns NOT inside classes."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "x = 1\n"
        "class MyClass:\n"
        "    y = 2\n"
        "z = 3\n"
    )

    matches = find_pattern("$NAME = $VALUE", str(test_file), not_inside="class")
    assert len(matches) == 2  # x=1 and z=3, not y=2


def test_find_inside_nested_structures(tmp_path):
    """Test finding patterns in nested structures."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "class MyClass:\n"
        "    def method(self):\n"
        "        for i in range(10):\n"
        "            print(i)\n"
        "print('outside')\n"
    )

    # Should find print inside for loop (which is also inside method and class)
    matches = find_pattern("print($X)", str(test_file), inside="for")
    assert len(matches) == 1
    node_code = matches[0].captures["X"]
    assert node_code == "i"


def test_find_inside_conflicts_with_not_inside(tmp_path):
    """Test that both inside and not_inside cannot be used together."""
    test_file = tmp_path / "test.py"
    test_file.write_text("x = 1\n")

    with pytest.raises(ValueError, match="Cannot specify both.*inside.*not_inside"):
        find_pattern("$X = $Y", str(test_file), inside="def", not_inside="class")


def test_replace_inside_listcomp(tmp_path):
    """Test replacement inside list comprehension with --inside flag."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "result = [func(x) for x in items]\n"
        "other = func(y)\n"
    )

    diff, count = replace_pattern("func($X)", "new_func($X)", str(test_file), inside="for")
    # Should only replace inside the list comprehension
    assert count == 1
    assert "new_func(x)" in diff
    # Module level func(y) should remain
    assert "-other = func(y)" not in diff


def test_replace_inside_ifexp(tmp_path):
    """Test replacement inside ternary expression (IfExp) with --inside flag."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "result = func(a) if condition else func(b)\n"
        "other = func(c)\n"
    )

    diff, count = replace_pattern("func($X)", "new_func($X)", str(test_file), inside="if")
    # Should replace func calls inside the ternary
    assert count == 2
    assert "new_func(a)" in diff
    assert "new_func(b)" in diff
    # Module level func(c) should remain
    assert "-other = func(c)" not in diff


def test_replace_inside_setcomp(tmp_path):
    """Test replacement inside set comprehension with --inside flag."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "result = {func(x) for x in items}\n"
        "other = func(y)\n"
    )

    diff, count = replace_pattern("func($X)", "new_func($X)", str(test_file), inside="for")
    # Should only replace inside the set comprehension
    assert count == 1
    assert "new_func(x)" in diff
    # Module level func(y) should remain
    assert "-other = func(y)" not in diff


def test_replace_inside_dictcomp(tmp_path):
    """Test replacement inside dict comprehension with --inside flag."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "result = {k: func(v) for k, v in items.items()}\n"
        "other = func(y)\n"
    )

    diff, count = replace_pattern("func($X)", "new_func($X)", str(test_file), inside="for")
    # Should only replace inside the dict comprehension
    assert count == 1
    assert "new_func(v)" in diff
    # Module level func(y) should remain
    assert "-other = func(y)" not in diff


def test_replace_inside_generatorexp(tmp_path):
    """Test replacement inside generator expression with --inside flag."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "result = (func(x) for x in items)\n"
        "other = func(y)\n"
    )

    diff, count = replace_pattern("func($X)", "new_func($X)", str(test_file), inside="for")
    # Should only replace inside the generator expression
    assert count == 1
    assert "new_func(x)" in diff
    # Module level func(y) should remain
    assert "-other = func(y)" not in diff


def test_replace_listcomp_itself_with_inside_flag(tmp_path):
    """Test replacing the listcomp node itself, not just contents."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "def outer():\n"
        "    result = [x for x in items]\n"
        "data = [y for y in data]\n"
    )

    # Replace listcomp pattern itself when inside function
    diff, count = replace_pattern("[$X for $X in $Y]", "list($Y)", str(test_file), inside="def")
    # Should only replace the listcomp inside the function
    assert count == 1
    assert "list(items)" in diff
    # Module level listcomp should remain
    assert "-data = [y for y in data]" not in diff
