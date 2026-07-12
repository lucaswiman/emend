"""Tests for transform engine (find, replace, imports, symbols, references, rename, move).

Component operation tests (get/set/add/remove) are in test_component_operations.py.
Basic find_pattern() tests are in test_find.py and test_cli_transform.py.
"""

import pytest

from emend.component_selector import ExtendedSelector


def sel(file_path, *symbol_path, component=None, accessor=None):
    """Build an ExtendedSelector with a positional symbol path."""
    return ExtendedSelector(
        file_path=str(file_path),
        symbol_path=list(symbol_path),
        component=component,
        accessor=accessor,
    )


def assert_find(tmp_path, source, pattern, count):
    """Write *source*, run find_pattern(*pattern*) and assert the match count.

    Returns the matches so callers can make further assertions.
    """
    from emend.transform import find_pattern

    test_file = tmp_path / "test.py"
    test_file.write_text(source)
    matches = find_pattern(pattern, str(test_file))
    assert len(matches) == count
    return matches


def assert_replace(tmp_path, source, pattern, replacement, count):
    """Write *source*, apply replace_pattern and assert the count.

    Returns the rewritten file content for further assertions.
    """
    from emend.transform import replace_pattern

    test_file = tmp_path / "test.py"
    test_file.write_text(source)
    _, actual = replace_pattern(pattern, replacement, str(test_file), apply=True)
    assert actual == count
    return test_file.read_text()


class TestReplacePattern:
    """Tests for replace_pattern() function."""

    def test_replace_simple(self, tmp_path):
        """Replace simple pattern without metavariables."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "x = 5\n"
        )

        diff, count = replace_pattern("print('hello')", "logger.info('hello')", str(test_file), apply=False)

        assert "print('hello')" in diff
        assert "logger.info('hello')" in diff
        assert count == 1

    def test_replace_with_metavar(self, tmp_path):
        """Replace pattern with metavariable."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "print('world')\n"
        )

        diff, count = replace_pattern("print($X)", "logger.info($X)", str(test_file), apply=False)

        assert "-print('hello')" in diff or "print('hello')" in diff
        assert "logger.info('hello')" in diff
        assert "logger.info('world')" in diff
        assert count == 2

    def test_replace_multiple_metavars(self, tmp_path):
        """Replace pattern with multiple metavariables."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "assertEqual(x, 5)\n"
            "assertEqual(y, 10)\n"
        )

        diff, count = replace_pattern("assertEqual($A, $B)", "assert $A == $B", str(test_file), apply=False)

        assert "assertEqual(x, 5)" in diff
        assert "assert x == 5" in diff
        assert "assert y == 10" in diff
        assert count == 2

    def test_replace_prefix_metavar_not_corrupted(self, tmp_path):
        """$X must not be replaced inside $XY when $X is a prefix of $XY."""
        from emend.transform import _substitute_metavars

        result = _substitute_metavars("$X + $XY", {"X": "a", "XY": "b"})
        assert result == "a + b", f"Expected 'a + b', got '{result}'"

    def test_replace_no_matches(self, tmp_path):
        """Replace pattern that doesn't match returns empty diff."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "x = 5\n"
            "y = 10\n"
        )

        diff, count = replace_pattern("print($X)", "logger.info($X)", str(test_file), apply=False)

        # Empty diff or no changes
        assert diff == "" or "@@" not in diff
        assert count == 0

    def test_replace_apply_writes_file(self, tmp_path):
        """Setting apply=True writes changes to file."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "print('world')\n"
        )

        diff, count = replace_pattern("print($X)", "logger.info($X)", str(test_file), apply=True)

        # Verify file was modified
        content = test_file.read_text()
        assert "logger.info('hello')" in content
        assert "logger.info('world')" in content
        assert "print(" not in content

        # Diff should still be returned
        assert "print('hello')" in diff or "-print" in diff
        assert "logger.info" in diff
        assert count == 2

    def test_replace_apply_with_multibyte_chars(self, tmp_path):
        """Replacement offsets must be byte-based.  When an earlier line
        contains multi-byte UTF-8 characters (e.g. emoji in a comment), the
        char-vs-byte offset mismatch previously mangled the file."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "# comment \U0001F389\U0001F389\U0001F389\n"
            'print("hello")\n'
        )

        diff, count = replace_pattern('print($X)', 'log($X)', str(test_file), apply=True)

        content = test_file.read_text()
        assert content == "# comment \U0001F389\U0001F389\U0001F389\n" 'log("hello")\n'
        assert count == 1

    def test_replace_apply_multibyte_same_line(self, tmp_path):
        """A multi-byte character before the match on the same line must not
        offset the replacement."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text('x = "\U0001F389"; print("hi")\n')

        diff, count = replace_pattern('print($X)', 'log($X)', str(test_file), apply=True)

        content = test_file.read_text()
        assert content == 'x = "\U0001F389"; log("hi")\n'
        assert count == 1

    def test_replace_scoped_to_function(self, tmp_path):
        """Replace pattern only within a specific function scope."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def helper(x):\n"
            "    return x + 1\n"
            "\n"
            "def main():\n"
            "    result1 = helper(5)\n"
            "    result2 = helper(10)\n"
            "    return result1 + result2\n"
        )

        diff, count = replace_pattern(
            "helper($X)",
            "helper_v2($X)",
            str(test_file),
            scope=["main"],
            apply=True
        )

        content = test_file.read_text()
        assert count == 2
        # helper in main should be replaced
        assert "helper_v2(5)" in content
        assert "helper_v2(10)" in content
        # helper function itself should be unchanged
        assert "def helper(x):" in content

    def test_replace_scoped_to_method(self, tmp_path):
        """Replace pattern only within a class method."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "class Calculator:\n"
            "    def add(self, x, y):\n"
            "        return x + y\n"
            "\n"
            "    def process(self):\n"
            "        result1 = self.add(1, 2)\n"
            "        result2 = self.add(3, 4)\n"
            "        return result1 + result2\n"
        )

        diff, count = replace_pattern(
            "self.add($X, $Y)",
            "self.new_add($X, $Y)",
            str(test_file),
            scope=["Calculator", "process"],
            apply=True
        )

        content = test_file.read_text()
        assert count == 2
        # self.add in process should be replaced
        assert "self.new_add(1, 2)" in content
        assert "self.new_add(3, 4)" in content
        # add method definition should be unchanged
        assert "def add(self, x, y):" in content

    def test_replace_scoped_no_change_outside_scope(self, tmp_path):
        """Replace pattern does not affect code outside scope."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "old_name = 1\n"
            "\n"
            "def func():\n"
            "    old_name = 2\n"
            "    return old_name\n"
            "\n"
            "x = old_name\n"
        )

        diff, count = replace_pattern(
            "old_name",
            "new_name",
            str(test_file),
            scope=["func"],
            apply=True
        )

        content = test_file.read_text()
        assert count == 2
        # old_name inside func should be replaced
        assert "def func():" in content
        assert "new_name = 2" in content
        assert "return new_name" in content
        # old_name outside func should be unchanged
        lines = content.split('\n')
        assert lines[0] == "old_name = 1"
        assert "x = old_name" in content

    def test_replace_comparison_pattern(self, tmp_path):
        """Replace pattern with comparison operators."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "if x == 5:\n"
            "    pass\n"
            "if y == 10:\n"
            "    pass\n"
        )

        diff, count = replace_pattern("$A == $B", "$A is $B", str(test_file), apply=True)

        content = test_file.read_text()
        assert count == 2
        assert "x is 5" in content
        assert "y is 10" in content

    def test_find_comparison_is_none(self, tmp_path):
        """Find pattern with 'is None' comparison (not '== None')."""
        assert_find(
            tmp_path,
            "if x is None:\n"
            "    pass\n"
            "if y == None:\n"
            "    pass\n"
            "if z is None:\n"
            "    pass\n",
            "$X is None",
            2,
        )

    def test_replace_binary_operation_pattern(self, tmp_path):
        """Replace pattern with binary operators (only +, not *)."""
        content = assert_replace(
            tmp_path,
            "result = a + b\n"
            "total = x + y\n"
            "product = m * n\n",
            "$A + $B",
            "$A - $B",
            2,
        )
        assert "a - b" in content
        assert "x - y" in content
        assert "m * n" in content  # Unchanged

    def test_replace_boolean_operation_pattern(self, tmp_path):
        """Replace pattern with boolean operators."""
        content = assert_replace(
            tmp_path,
            "if a and b:\n"
            "    pass\n"
            "if x or y:\n"
            "    pass\n",
            "$A and $B",
            "$A or $B",
            1,
        )
        assert "a or b" in content

    def test_find_unary_operation_pattern(self, tmp_path):
        """Find pattern with unary operators."""
        assert_find(
            tmp_path,
            "result = not x\n"
            "value = -y\n"
            "flag = not z\n",
            "not $X",
            2,
        )

    def test_replace_subscript_pattern(self, tmp_path):
        """Replace pattern with subscripts."""
        content = assert_replace(
            tmp_path,
            "value = config['key']\n"
            "item = data['field']\n",
            "$X['key']",
            "$X.get('key')",
            1,
        )
        assert "config.get('key')" in content

    def test_find_with_anonymous_metavar(self, tmp_path):
        """Find pattern using $_ anonymous metavar that matches without capturing."""
        from emend.transform import find_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "print('world')\n"
            "x = 5\n"
        )

        matches = find_pattern("print($_)", str(test_file))

        assert len(matches) == 2
        # Verify that $_ was not captured
        for match in matches:
            assert '_' not in match.captures

    def test_replace_with_anonymous_metavar_in_pattern(self, tmp_path):
        """Replace pattern using $_ to match without capturing."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "foo(1, bar())\n"
            "foo(2, baz())\n"
        )

        # Replace foo($_, $Y) - we don't care about first arg
        diff, count = replace_pattern("foo($_, $Y)", "new_foo($Y)", str(test_file), apply=True)

        content = test_file.read_text()
        assert count == 2
        assert "new_foo(bar())" in content
        assert "new_foo(baz())" in content

    def test_replace_omits_a_captured_metavar(self, tmp_path):
        """A replacement may reference a subset of the captured metavars,
        dropping the ones it does not use."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "result = func(a, b, c)\n"
            "value = func(x, y, z)\n"
        )

        # Match three args but keep only the first and third, dropping $B.
        diff, count = replace_pattern("func($A, $B, $C)", "func($A, $C)", str(test_file), apply=True)

        content = test_file.read_text()
        assert count == 2
        assert "func(a, c)" in content
        assert "func(x, z)" in content

    def test_find_float_literal(self, tmp_path):
        """Find float literal patterns."""
        assert_find(
            tmp_path,
            "x = 3.14\n"
            "y = 2.718\n"
            "z = 3.14\n"
            "w = 42\n",
            "3.14",
            2,
        )

    def test_replace_float_literal(self, tmp_path):
        """Replace float literals; the '3.14' pattern must not match the
        substring of the larger float 3.14159 (regression)."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "pi = 3.14\n"
            "approx_pi = 3.14159\n"
            "radius = 3.14\n"
        )

        diff, count = replace_pattern("3.14", "math.pi", str(test_file), apply=True)

        content = test_file.read_text()
        assert count == 2
        assert "pi = math.pi" in content
        assert "radius = math.pi" in content
        assert "approx_pi = 3.14159" in content  # not matched as a substring

    def test_find_ternary_ifexp(self, tmp_path):
        """Find ternary (if-else) expression patterns."""
        assert_find(
            tmp_path,
            "result = x if condition else y\n"
            "value = a if test() else b\n"
            "z = 1 + 2\n",
            "$A if $B else $C",
            2,
        )

    def test_replace_ternary_ifexp(self, tmp_path):
        """Replace ternary expressions."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "result = x if condition else y\n"
            "output = a if b else c\n"
        )

        diff, count = replace_pattern(
            "$A if $B else $C",
            "ternary($A, $B, $C)",
            str(test_file),
            apply=True
        )

        content = test_file.read_text()
        assert count == 2
        assert "ternary(x, condition, y)" in content
        assert "ternary(a, b, c)" in content

    def test_find_await_expression(self, tmp_path):
        """Find await expression patterns."""
        assert_find(
            tmp_path,
            "async def func():\n"
            "    result = await fetch_data()\n"
            "    value = await get_value()\n"
            "    return result\n",
            "await $X",
            2,
        )

    def test_replace_await_expression(self, tmp_path):
        """Replace await expressions."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "async def func():\n"
            "    result = await old_fetch(url)\n"
            "    value = await old_fetch(endpoint)\n"
        )

        diff, count = replace_pattern(
            "await old_fetch($X)",
            "await new_fetch($X)",
            str(test_file),
            apply=True
        )

        content = test_file.read_text()
        assert count == 2
        assert "await new_fetch(url)" in content
        assert "await new_fetch(endpoint)" in content

    def test_find_tuple_pattern(self, tmp_path):
        """Find tuple patterns (only 2-element tuples)."""
        assert_find(
            tmp_path,
            "coords = (x, y)\n"
            "point = (a, b)\n"
            "triple = (1, 2, 3)\n",
            "($A, $B)",
            2,
        )

    def test_replace_tuple_pattern(self, tmp_path):
        """Replace tuple patterns, swapping coordinates. The match coordinates
        must exclude the surrounding parens so no double-paren form is produced
        (regression)."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "point = (x, y)\n"
            "coords = (a, b)\n"
        )

        diff, count = replace_pattern("($A, $B)", "($B, $A)", str(test_file), apply=True)

        content = test_file.read_text()
        assert count == 2
        assert "point = (y, x)" in content
        assert "coords = (b, a)" in content
        assert "((y, x))" not in content
        assert "((b, a))" not in content

    def test_find_list_pattern(self, tmp_path):
        """Find list patterns."""
        assert_find(
            tmp_path,
            "items = [a, b]\n"
            "values = [x, y]\n"
            "triple = [1, 2, 3]\n",
            "[$A, $B]",
            2,
        )

    def test_replace_list_to_tuple(self, tmp_path):
        """Replace list with tuple."""
        content = assert_replace(
            tmp_path,
            "coords = [x, y]\n"
            "point = [a, b]\n",
            "[$A, $B]",
            "($A, $B)",
            2,
        )
        assert "coords = (x, y)" in content
        assert "point = (a, b)" in content

    def test_find_set_pattern(self, tmp_path):
        """Find set patterns."""
        assert_find(
            tmp_path,
            "items = {a, b}\n"
            "values = {x, y}\n"
            "triple = {1, 2, 3}\n",
            "{$A, $B}",
            2,
        )

    def test_replace_set_pattern(self, tmp_path):
        """Replace set with list."""
        content = assert_replace(
            tmp_path,
            "coords = {x, y}\n"
            "point = {a, b}\n",
            "{$A, $B}",
            "[$A, $B]",
            2,
        )
        assert "coords = [x, y]" in content
        assert "point = [a, b]" in content

    def test_find_set_ellipsis(self, tmp_path):
        """Find sets with ellipsis matching (all sets match)."""
        assert_find(
            tmp_path,
            "small = {1}\n"
            "medium = {1, 2}\n"
            "large = {1, 2, 3}\n",
            "{$...ELEMS}",
            3,
        )

    def test_replace_set_ellipsis(self, tmp_path):
        """Replace set patterns with ellipsis."""
        content = assert_replace(
            tmp_path,
            "items = {1, 2, 3}\n"
            "values = {a, b}\n",
            "{$FIRST, $...REST}",
            "[$FIRST, $...REST]",
            2,
        )
        assert "items = [1, 2, 3]" in content
        assert "values = [a, b]" in content

    def test_find_dict_pattern(self, tmp_path):
        """Find dict literal patterns."""
        from emend.transform import find_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "config = {'key': value}\n"
            "data = {'name': 'John', 'age': 30}\n"
            "empty = {}\n"
        )

        matches = find_pattern("{'key': $V}", str(test_file))

        assert len(matches) == 1
        captures = matches[0].captures
        assert "V" in captures

    def test_find_dict_metavar_key(self, tmp_path):
        """Find dict patterns with metavar keys."""
        from emend.transform import find_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "config = {key: value}\n"
            "data = {name: 'John'}\n"
        )

        # Use no space after colon to avoid type constraint parsing
        matches = find_pattern("{$K:$V}", str(test_file))

        assert len(matches) == 2
        captures = matches[0].captures
        assert "K" in captures
        assert "V" in captures

    def test_replace_dict_pattern(self, tmp_path):
        """Replace dict literal patterns."""
        content = assert_replace(
            tmp_path,
            "data = {name: 'old'}\n",
            "{$K: 'old'}",
            "{$K: 'new'}",
            1,
        )
        assert "{name: 'new'}" in content

    def test_find_lambda_pattern(self, tmp_path):
        """Find lambda expression patterns."""
        from emend.transform import find_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "f = lambda: x\n"
            "g = lambda: y\n"
            "h = lambda a: a + 1\n"
        )

        matches = find_pattern("lambda: $X", str(test_file))

        # 'lambda: $X' matches only parameterless lambdas (not 'lambda a: ...')
        assert len(matches) == 2
        captures = matches[0].captures
        assert "X" in captures

    def test_replace_lambda_pattern(self, tmp_path):
        """Replace lambda expression patterns."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "f = lambda: x\n"
            "g = lambda: y + 1\n"
        )

        diff, count = replace_pattern("lambda: $X", "lambda: $X + 1", str(test_file), apply=True)

        content = test_file.read_text()
        assert count == 2
        assert "lambda: x + 1" in content
        assert "lambda: y + 1 + 1" in content

    def test_find_lambda_with_args(self, tmp_path):
        """`lambda $...PARAMS: $BODY` matches only lambdas that take parameters."""
        from emend.transform import find_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "f = lambda x: x + 1\n"
            "g = lambda a, b: a + b\n"
            "h = lambda: 42\n"
        )

        matches = find_pattern("lambda $...PARAMS: $BODY", str(test_file))

        # Only the two parameterized lambdas match, not the parameterless one.
        assert len(matches) == 2
        bodies = {m.captures["BODY"] for m in matches}
        assert bodies == {"x + 1", "a + b"}

    def test_find_walrus_operator(self, tmp_path):
        """Find walrus operator (named expression) patterns."""
        from emend.transform import find_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "if (n := len(data)) > 10:\n"
            "    print(n)\n"
            "while (line := file.readline()):\n"
            "    process(line)\n"
        )

        matches = find_pattern("($X := $Y)", str(test_file))

        assert len(matches) == 2
        captures = matches[0].captures
        assert "X" in captures
        assert "Y" in captures

    def test_replace_walrus_operator(self, tmp_path):
        """Replace walrus operator patterns."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "if (n := len(data)) > 10:\n"
            "    print(n)\n"
        )

        # Remove walrus by just keeping the value side
        # Note: The named_expression match replaces the inner `:=` expression.
        # The surrounding parens are a separate tree-sitter node and remain.
        diff, count = replace_pattern("($X := $Y)", "$Y", str(test_file), apply=True)

        content = test_file.read_text()
        assert count == 1
        assert "if (len(data)) > 10:" in content

    def test_find_walrus_in_comprehension(self, tmp_path):
        """Find walrus operator in list comprehension."""
        from emend.transform import find_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "results = [y for x in data if (y := process(x)) is not None]\n"
        )

        matches = find_pattern("($X := $Y)", str(test_file))

        assert len(matches) == 1
        captures = matches[0].captures
        assert "X" in captures
        assert "Y" in captures

    def test_find_assignment(self, tmp_path):
        """Find assignment statement patterns."""
        from emend.transform import find_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "x = 5\n"
            "y = func(10)\n"
            "z = a + b\n"
        )

        matches = find_pattern("$X = $Y", str(test_file))

        assert len(matches) == 3
        captures = matches[0].captures
        assert "X" in captures
        assert "Y" in captures

    def test_replace_assignment(self, tmp_path):
        """Replace assignment patterns."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "result = func(data)\n"
            "value = other(x)\n"
        )

        # Transform function calls in assignments
        diff, count = replace_pattern("$X = func($Y)", "$X = new_func($Y)", str(test_file), apply=True)

        content = test_file.read_text()
        assert count == 1
        assert "result = new_func(data)" in content

    def test_find_augmented_assignment(self, tmp_path):
        """Find augmented assignment patterns (+=, -=, etc.)."""
        from emend.transform import find_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "x += 5\n"
            "y -= 10\n"
            "z *= 2\n"
        )

        matches = find_pattern("$X += $Y", str(test_file))

        assert len(matches) == 1
        captures = matches[0].captures
        assert "X" in captures
        assert "Y" in captures

    def test_replace_augmented_assignment(self, tmp_path):
        """Replace augmented assignment patterns."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "counter += 1\n"
            "total += value\n"
        )

        # Convert += to regular assignment with addition
        diff, count = replace_pattern("$X += $Y", "$X = $X + $Y", str(test_file), apply=True)

        content = test_file.read_text()
        assert count == 2
        assert "counter = counter + 1" in content
        assert "total = total + value" in content

    def test_find_return_statement(self, tmp_path):
        """Find return statement patterns."""
        assert_find(
            tmp_path,
            "def func1():\n"
            "    return x\n"
            "def func2():\n"
            "    return y\n"
            "def func3():\n"
            "    pass\n",
            "return $X",
            2,
        )

    def test_replace_return_statement(self, tmp_path):
        """Replace return statements."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def func():\n"
            "    return value\n"
            "    return result\n"
        )

        diff, count = replace_pattern("return $X", "return ($X,)", str(test_file), apply=True)

        content = test_file.read_text()
        assert count == 2
        assert "return (value,)" in content
        assert "return (result,)" in content

    def test_find_assert_statement(self, tmp_path):
        """Find assert statement patterns."""
        assert_find(
            tmp_path,
            "assert x == 5\n"
            "assert y > 0\n"
            "z = 10\n",
            "assert $X",
            2,
        )

    def test_replace_assert_with_specific_pattern(self, tmp_path):
        """Replace assert with specific comparison pattern."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "assert x == 5\n"
            "assert y == 10\n"
            "assert z > 0\n"
        )

        diff, count = replace_pattern("assert $A == $B", "assert $B == $A", str(test_file), apply=True)

        content = test_file.read_text()
        assert count == 2
        assert "assert 5 == x" in content
        assert "assert 10 == y" in content
        assert "assert z > 0" in content  # Unchanged

    def test_find_raise_statement(self, tmp_path):
        """Find raise statement patterns."""
        from emend.transform import find_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "raise ValueError('bad')\n"
            "raise TypeError('wrong')\n"
            "return None\n"
        )

        matches = find_pattern("raise $X", str(test_file))

        assert len(matches) == 2

    def test_replace_raise_statement(self, tmp_path):
        """Replace raise statements."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "raise ValueError(msg)\n"
            "raise TypeError(error)\n"
        )

        diff, count = replace_pattern(
            "raise ValueError($X)",
            "raise CustomError($X)",
            str(test_file),
            apply=True
        )

        content = test_file.read_text()
        assert count == 1
        assert "raise CustomError(msg)" in content
        assert "raise TypeError(error)" in content  # Unchanged

    def test_find_del_statement(self, tmp_path):
        """Find del statement patterns."""
        from emend.transform import find_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "del my_var\n"
            "del other_var\n"
            "x = 1\n"
        )

        matches = find_pattern("del $X", str(test_file))

        assert len(matches) == 2

    def test_replace_del_statement(self, tmp_path):
        """Replace del statements with alternative."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "del items[key]\n"
            "del other\n"
        )

        diff, count = replace_pattern(
            "del $X[$Y]",
            "$X.pop($Y)",
            str(test_file),
            apply=True
        )

        content = test_file.read_text()
        assert count == 1
        assert "items.pop(key)" in content
        assert "del other" in content  # Unchanged

    def test_find_global_statement(self, tmp_path):
        """Find global statement patterns."""
        from emend.transform import find_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "global my_var\n"
            "def func():\n"
            "    global other_var\n"
            "x = 1\n"
        )

        matches = find_pattern("global $X", str(test_file))

        assert len(matches) == 2

    def test_find_nonlocal_statement(self, tmp_path):
        """Find nonlocal statement patterns."""
        from emend.transform import find_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def outer():\n"
            "    x = 1\n"
            "    def inner():\n"
            "        nonlocal x\n"
            "        nonlocal y\n"
        )

        matches = find_pattern("nonlocal $X", str(test_file))

        assert len(matches) == 2

    def test_find_import_from(self, tmp_path):
        """Find 'from ... import ...' patterns."""
        from emend.transform import find_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "from os import path\n"
            "from sys import argv\n"
            "import json\n"
        )

        matches = find_pattern("from $MOD import $NAME", str(test_file))

        assert len(matches) == 2

    def test_replace_import_from(self, tmp_path):
        """Replace 'from ... import ...' changing module."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "from old_pkg import func\n"
            "from old_pkg import helper\n"
            "from other import thing\n"
        )

        diff, count = replace_pattern(
            "from old_pkg import $NAME",
            "from new_pkg import $NAME",
            str(test_file),
            apply=True
        )

        content = test_file.read_text()
        assert count == 2
        assert "from new_pkg import func" in content
        assert "from new_pkg import helper" in content
        assert "from other import thing" in content  # Unchanged

    def test_find_import(self, tmp_path):
        """Find 'import' patterns."""
        from emend.transform import find_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "import os\n"
            "import sys\n"
            "from typing import List\n"
        )

        matches = find_pattern("import $MOD", str(test_file))

        assert len(matches) == 2

    def test_replace_import_alias(self, tmp_path):
        """Replace import with alias pattern."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "import numpy as np\n"
            "import pandas as pd\n"
        )

        diff, count = replace_pattern(
            "import $MOD as $ALIAS",
            "import new_$MOD as $ALIAS",
            str(test_file),
            apply=True
        )

        content = test_file.read_text()
        assert count == 2
        assert "import new_numpy as np" in content
        assert "import new_pandas as pd" in content

    def test_find_import_from_dotted(self, tmp_path):
        """Find imports from dotted module paths."""
        from emend.transform import find_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "from os.path import join\n"
            "from os.path import dirname\n"
            "from os import getcwd\n"
        )

        matches = find_pattern("from os.path import $NAME", str(test_file))

        assert len(matches) == 2

    def test_replace_with_where_parameter(self, tmp_path):
        """replace_pattern should accept where= without crashing."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def test_foo():\n"
            "    print('hello')\n"
            "\n"
            "def main():\n"
            "    print('world')\n"
        )

        diff, count = replace_pattern(
            "print($X)", "log($X)", str(test_file),
            where="def test_*", apply=True,
        )
        content = test_file.read_text()
        assert count >= 1
        assert "log(" in content

    def test_replace_with_non_ascii_before_match(self, tmp_path):
        """Multi-byte chars before a match must not corrupt byte offsets.

        Regression: line_starts/end_offset were computed in character units,
        but PyFileTransform.replace_range indexes bytes, so a non-ASCII
        comment before the match caused a Rust char-boundary panic.
        """
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text('# café ☕\nprint("hello")\n')

        diff, count = replace_pattern(
            'print($X)', 'logging.info($X)', str(test_file), apply=True,
        )

        assert count == 1
        content = test_file.read_text()
        assert 'logging.info("hello")' in content
        assert 'print("hello")' not in content
        # The non-ASCII comment must be preserved intact.
        assert '# café ☕' in content


class TestImportsComponent:
    """Tests for [imports] component at module level."""

    def test_get_imports(self, tmp_path):
        """Get all imports from a module."""
        from emend.transform import get_component

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "def foo():\n"
            "    pass\n"
        )

        selector = sel(test_file, component="imports")

        result = get_component(selector)
        assert "import os" in result
        assert "import sys" in result
        assert "from pathlib import Path" in result

    def test_get_imports_empty(self, tmp_path):
        """Get imports from module with no imports."""
        from emend.transform import get_component

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def foo():\n"
            "    pass\n"
        )

        selector = sel(test_file, component="imports")

        result = get_component(selector)
        assert result == ""

    def test_get_imports_with_multiline(self, tmp_path):
        """Get imports including multiline imports."""
        from emend.transform import get_component

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "from typing import (\n"
            "    List,\n"
            "    Dict,\n"
            ")\n"
            "import os\n"
            "\n"
            "def foo():\n"
            "    pass\n"
        )

        selector = sel(test_file, component="imports")

        result = get_component(selector)
        assert "from typing import" in result
        assert "List" in result
        assert "Dict" in result
        assert "import os" in result

    def test_add_import(self, tmp_path):
        """Add an import to a module."""
        from emend.transform import add_to_component

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "import os\n"
            "\n"
            "def foo():\n"
            "    pass\n"
        )

        selector = sel(test_file, component="imports")

        diff = add_to_component(selector, "import sys", position=-1, apply=True)

        content = test_file.read_text()
        assert "import os" in content
        assert "import sys" in content

    def test_add_import_prepend(self, tmp_path):
        """Add import at the beginning."""
        from emend.transform import add_to_component

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "import sys\n"
            "\n"
            "def foo():\n"
            "    pass\n"
        )

        selector = sel(test_file, component="imports")

        diff = add_to_component(selector, "import os", position=0, apply=True)

        content = test_file.read_text()
        assert "import os" in content
        assert "import sys" in content
        # os should come before sys
        assert content.index("import os") < content.index("import sys")


class TestGetSymbolSource:
    """Tests for get_symbol_source() function."""

    def test_get_simple_function(self, tmp_path):
        """Get source of a simple function."""
        from emend.transform import get_symbol_source

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def foo():\n"
            "    return 1\n"
        )

        selector = sel(test_file, "foo")

        source = get_symbol_source(selector)
        assert source == "def foo():\n    return 1\n"

    def test_get_function_with_decorator(self, tmp_path):
        """Get source of function with decorator."""
        from emend.transform import get_symbol_source

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "@decorator\n"
            "def foo():\n"
            "    return 1\n"
        )

        selector = sel(test_file, "foo")

        source = get_symbol_source(selector)
        assert "@decorator" in source
        assert "def foo():" in source
        assert "return 1" in source

    def test_get_function_with_multiline_decorator(self, tmp_path):
        """Get source of function with multiline decorator."""
        from emend.transform import get_symbol_source

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "@decorator(\n"
            "    arg1='value1',\n"
            "    arg2='value2'\n"
            ")\n"
            "def foo():\n"
            "    return 1\n"
        )

        selector = sel(test_file, "foo")

        source = get_symbol_source(selector)
        assert "@decorator(" in source
        assert "arg1='value1'" in source
        assert "def foo():" in source

    def test_get_method_from_class(self, tmp_path):
        """Get source of a method from a class."""
        from emend.transform import get_symbol_source

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "class MyClass:\n"
            "    def method(self):\n"
            "        return 42\n"
        )

        selector = sel(test_file, "MyClass", "method")

        source = get_symbol_source(selector)
        assert "def method(self):" in source
        assert "return 42" in source

    def test_get_nested_function(self, tmp_path):
        """Get source of a nested function."""
        from emend.transform import get_symbol_source

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def outer():\n"
            "    def inner():\n"
            "        return 'nested'\n"
            "    return inner\n"
        )

        selector = sel(test_file, "outer", "inner")

        source = get_symbol_source(selector)
        assert "def inner():" in source
        assert "return 'nested'" in source
        assert "def outer" not in source

    def test_get_class(self, tmp_path):
        """Get source of a class."""
        from emend.transform import get_symbol_source

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "class MyClass(Base):\n"
            "    '''Docstring'''\n"
            "    \n"
            "    def method(self):\n"
            "        pass\n"
        )

        selector = sel(test_file, "MyClass")

        source = get_symbol_source(selector)
        assert "class MyClass(Base):" in source
        assert "'''Docstring'''" in source
        assert "def method(self):" in source


class TestCopySymbol:
    """Tests for copy_symbol() function."""

    def test_copy_function_to_empty_file(self, tmp_path):
        """Copy a function to an empty file."""
        from emend.transform import copy_symbol

        source_file = tmp_path / "source.py"
        source_file.write_text(
            "def my_func(x, y):\n"
            "    return x + y\n"
        )

        dest_file = tmp_path / "dest.py"
        dest_file.write_text("")

        selector = sel(source_file, "my_func")

        diff = copy_symbol(selector, str(dest_file), apply=True)

        content = dest_file.read_text()
        assert "def my_func(x, y):" in content
        assert "return x + y" in content

    def test_copy_function_append(self, tmp_path):
        """Copy function to end of file with existing code."""
        from emend.transform import copy_symbol

        source_file = tmp_path / "source.py"
        source_file.write_text(
            "def func_to_copy():\n"
            "    return 'copied'\n"
        )

        dest_file = tmp_path / "dest.py"
        dest_file.write_text(
            "def existing():\n"
            "    return 'existing'\n"
        )

        selector = sel(source_file, "func_to_copy")

        diff = copy_symbol(selector, str(dest_file), position="end", apply=True)

        content = dest_file.read_text()
        assert "def existing():" in content
        assert "def func_to_copy():" in content
        # existing should come before func_to_copy
        assert content.index("existing") < content.index("func_to_copy")

    def test_copy_function_with_decorator(self, tmp_path):
        """Copy function with decorators."""
        from emend.transform import copy_symbol

        source_file = tmp_path / "source.py"
        source_file.write_text(
            "@decorator1\n"
            "@decorator2(arg='value')\n"
            "def decorated_func():\n"
            "    return 42\n"
        )

        dest_file = tmp_path / "dest.py"
        dest_file.write_text("")

        selector = sel(source_file, "decorated_func")

        diff = copy_symbol(selector, str(dest_file), apply=True)

        content = dest_file.read_text()
        assert "@decorator1" in content
        assert "@decorator2(arg='value')" in content
        assert "def decorated_func():" in content

    def test_copy_nested_function(self, tmp_path):
        """Copy a nested function."""
        from emend.transform import copy_symbol

        source_file = tmp_path / "source.py"
        source_file.write_text(
            "class Builder:\n"
            "    def _build(self):\n"
            "        def nested(a, b):\n"
            "            return a + b\n"
            "        return nested\n"
        )

        dest_file = tmp_path / "dest.py"
        dest_file.write_text("")

        selector = sel(source_file, "Builder", "_build", "nested")

        diff = copy_symbol(selector, str(dest_file), apply=True)

        content = dest_file.read_text()
        assert "def nested(a, b):" in content
        assert "return a + b" in content

    def test_copy_dry_run(self, tmp_path):
        """Test that dry-run doesn't modify destination."""
        from emend.transform import copy_symbol

        source_file = tmp_path / "source.py"
        source_file.write_text("def func(): pass\n")

        dest_file = tmp_path / "dest.py"
        dest_file.write_text("")

        selector = sel(source_file, "func")

        diff = copy_symbol(selector, str(dest_file), apply=False)

        # Dest file should still be empty
        assert dest_file.read_text() == ""
        # Diff should show what would change
        assert "+def func():" in diff

    def test_copy_nonexistent_symbol(self, tmp_path):
        """Error when copying nonexistent symbol."""
        from emend.transform import copy_symbol

        source_file = tmp_path / "source.py"
        source_file.write_text("def foo(): pass\n")

        dest_file = tmp_path / "dest.py"
        dest_file.write_text("")

        selector = sel(source_file, "nonexistent")

        with pytest.raises(ValueError, match="Symbol.*not found"):
            copy_symbol(selector, str(dest_file), apply=False)


class TestRemoveSymbol:
    """Tests for remove_symbol() function."""

    def test_remove_simple_function(self, tmp_path):
        """Remove a simple module-level function."""
        from emend.transform import remove_symbol

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def foo():\n"
            "    return 1\n"
            "\n"
            "def bar():\n"
            "    return 2\n"
            "\n"
            "def baz():\n"
            "    return 3\n"
        )

        selector = sel(test_file, "bar")

        diff = remove_symbol(selector, apply=True)

        content = test_file.read_text()
        assert "def foo():" in content
        assert "def bar():" not in content
        assert "def baz():" in content
        assert "-def bar():" in diff

    def test_remove_function_with_decorators(self, tmp_path):
        """Remove function with decorators."""
        from emend.transform import remove_symbol

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def foo():\n"
            "    return 1\n"
            "\n"
            "@decorator1\n"
            "@decorator2\n"
            "def bar():\n"
            "    return 2\n"
            "\n"
            "def baz():\n"
            "    return 3\n"
        )

        selector = sel(test_file, "bar")

        diff = remove_symbol(selector, apply=True)

        content = test_file.read_text()
        assert "def foo():" in content
        assert "@decorator1" not in content
        assert "@decorator2" not in content
        assert "def bar():" not in content
        assert "def baz():" in content

    def test_remove_class(self, tmp_path):
        """Remove an entire class."""
        from emend.transform import remove_symbol

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "class First:\n"
            "    pass\n"
            "\n"
            "class Second:\n"
            "    def method(self):\n"
            "        pass\n"
            "\n"
            "class Third:\n"
            "    pass\n"
        )

        selector = sel(test_file, "Second")

        diff = remove_symbol(selector, apply=True)

        content = test_file.read_text()
        assert "class First:" in content
        assert "class Second:" not in content
        assert "def method(self):" not in content
        assert "class Third:" in content

    def test_remove_method(self, tmp_path):
        """Remove a method from a class."""
        from emend.transform import remove_symbol

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "class MyClass:\n"
            "    def method1(self):\n"
            "        return 1\n"
            "\n"
            "    def method2(self):\n"
            "        return 2\n"
            "\n"
            "    def method3(self):\n"
            "        return 3\n"
        )

        selector = sel(test_file, "MyClass", "method2")

        diff = remove_symbol(selector, apply=True)

        content = test_file.read_text()
        assert "class MyClass:" in content
        assert "def method1(self):" in content
        assert "def method2(self):" not in content
        assert "def method3(self):" in content

    def test_remove_nested_function(self, tmp_path):
        """Remove a nested function."""
        from emend.transform import remove_symbol

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "class MyClass:\n"
            "    def outer(self):\n"
            "        def middle():\n"
            "            def inner():\n"
            "                return 1\n"
            "            return inner\n"
            "        return middle\n"
        )

        selector = sel(test_file, "MyClass", "outer", "middle", "inner")

        diff = remove_symbol(selector, apply=True)

        content = test_file.read_text()
        assert "class MyClass:" in content
        assert "def outer(self):" in content
        assert "def middle():" in content
        assert "def inner():" not in content
        assert "return inner" in content

    def test_remove_dry_run(self, tmp_path):
        """Test that dry-run mode doesn't modify file."""
        from emend.transform import remove_symbol

        test_file = tmp_path / "test.py"
        original = (
            "def foo():\n"
            "    return 1\n"
            "\n"
            "def bar():\n"
            "    return 2\n"
        )
        test_file.write_text(original)

        selector = sel(test_file, "bar")

        diff = remove_symbol(selector, apply=False)

        # File should be unchanged
        assert test_file.read_text() == original
        # Diff should show what would change
        assert "-def bar():" in diff

    def test_remove_nonexistent_symbol(self, tmp_path):
        """Error when symbol doesn't exist."""
        from emend.transform import remove_symbol

        test_file = tmp_path / "test.py"
        test_file.write_text("def foo():\n    pass\n")

        selector = sel(test_file, "nonexistent")

        with pytest.raises(ValueError, match="Symbol.*not found"):
            remove_symbol(selector, apply=False)

    def test_remove_function_with_multiline_decorator(self, tmp_path):
        """Remove function with multiline decorator."""
        from emend.transform import remove_symbol

        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def foo():\n"
            "    return 1\n"
            "\n"
            "@decorator(\n"
            "    arg1='value1',\n"
            "    arg2='value2'\n"
            ")\n"
            "def bar():\n"
            "    return 2\n"
            "\n"
            "def baz():\n"
            "    return 3\n"
        )

        selector = sel(test_file, "bar")

        diff = remove_symbol(selector, apply=True)

        content = test_file.read_text()
        assert "def foo():" in content
        assert "@decorator(" not in content
        assert "arg1='value1'" not in content
        assert "def bar():" not in content
        assert "def baz():" in content


class TestFindReferences:
    """Tests for find_references() function."""

    def test_find_simple_function_references(self, tmp_path):
        """Find all references to a simple function."""
        from emend.transform import find_references

        # Create a simple project structure
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Main file with function definition
        main_file = project_dir / "main.py"
        main_file.write_text(
            "def my_func():\n"
            "    return 42\n"
            "\n"
            "result = my_func()\n"
        )

        selector = sel(main_file, "my_func")

        refs = list(find_references(selector))

        # Should find at least the definition and the call
        assert len(refs) >= 2

        # Check that we have file paths
        for ref in refs:
            assert ref.file_path
            assert ref.line > 0

    def test_find_references_exclude_definition(self, tmp_path):
        """Find references excluding the definition."""
        from emend.transform import find_references

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        main_file = project_dir / "main.py"
        main_file.write_text(
            "def my_func():\n"
            "    return 42\n"
            "\n"
            "result = my_func()\n"
        )

        selector = sel(main_file, "my_func")

        refs = find_references(selector, include_definition=False)

        # Should only find the call, not the definition
        assert all(not ref.is_definition for ref in refs)

    def test_find_references_exclude_imports(self, tmp_path):
        """Find references excluding imports."""
        from emend.transform import find_references

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        main_file = project_dir / "main.py"
        main_file.write_text(
            "def my_func():\n"
            "    return 42\n"
        )

        other_file = project_dir / "other.py"
        other_file.write_text(
            "from main import my_func\n"
            "\n"
            "result = my_func()\n"
        )

        selector = sel(main_file, "my_func")

        refs = find_references(selector, include_imports=False)

        # Should not find any import statements
        assert all(not ref.is_import for ref in refs)


class TestRenameSymbol:
    """Tests for rename_symbol() function."""

    def test_rename_simple_function(self, tmp_path):
        """Rename a simple function across files."""
        from emend.transform import rename_symbol

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        main_file = project_dir / "main.py"
        main_file.write_text(
            "def old_func():\n"
            "    return 42\n"
            "\n"
            "result = old_func()\n"
        )

        selector = sel(main_file, "old_func")

        diffs = rename_symbol(selector, "new_func", apply=True)

        # Check the file was modified
        content = main_file.read_text()
        assert "def new_func():" in content
        assert "result = new_func()" in content
        assert "old_func" not in content

        # Check diffs are returned
        assert str(main_file) in diffs
        assert "old_func" in diffs[str(main_file)]
        assert "new_func" in diffs[str(main_file)]

    def test_rename_function_across_files(self, tmp_path):
        """Rename a function that's used across multiple files."""
        from emend.transform import rename_symbol

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        main_file = project_dir / "main.py"
        main_file.write_text(
            "def helper():\n"
            "    return 42\n"
        )

        other_file = project_dir / "other.py"
        other_file.write_text(
            "from main import helper\n"
            "\n"
            "result = helper()\n"
        )

        selector = sel(main_file, "helper")

        diffs = rename_symbol(selector, "helper_v2", apply=True)

        # Check both files were modified
        main_content = main_file.read_text()
        assert "def helper_v2():" in main_content

        other_content = other_file.read_text()
        assert "from main import helper_v2" in other_content
        assert "result = helper_v2()" in other_content

    def test_rename_dry_run(self, tmp_path):
        """Test rename in dry-run mode doesn't modify files."""
        from emend.transform import rename_symbol

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        main_file = project_dir / "main.py"
        original = (
            "def old_name():\n"
            "    return 42\n"
        )
        main_file.write_text(original)

        selector = sel(main_file, "old_name")

        diffs = rename_symbol(selector, "new_name", apply=False)

        # File should be unchanged
        assert main_file.read_text() == original

        # But diffs should show what would change
        assert str(main_file) in diffs
        assert "old_name" in diffs[str(main_file)]
        assert "new_name" in diffs[str(main_file)]

    def test_rename_with_non_ascii_before_reference(self, tmp_path):
        """Multi-byte chars before a reference must not shift byte offsets.

        Regression: references_in_file() returns byte offsets, but the
        endswith check / replace_range slicing used character indices. A
        non-ASCII comment before a reference caused that reference to be
        missed (or renamed at the wrong position).
        """
        from emend.transform import rename_symbol

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text('[project]\nname = "x"\n')

        main_file = project_dir / "main.py"
        main_file.write_text(
            "# café ☕ ☕ ☕ comment\n"
            "def old_name():\n"
            "    pass\n"
            "\n"
            "old_name()\n"
        )

        selector = sel(main_file, "old_name")

        diffs = rename_symbol(selector, "new_name", project_path=str(project_dir), apply=True)

        content = main_file.read_text()
        assert "def new_name():" in content
        # The call reference after the multi-byte comment must also be renamed.
        assert "new_name()" in content
        assert "old_name" not in content
        # The non-ASCII comment must be preserved intact.
        assert "# café ☕ ☕ ☕ comment" in content
        assert str(main_file) in diffs


class TestMoveSymbol:
    """Tests for move_symbol() function."""

    def test_move_function_to_new_file(self, tmp_path):
        """Move a function to a new file."""
        from emend.transform import move_symbol

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        source_file = project_dir / "source.py"
        source_file.write_text(
            "def my_func():\n"
            "    return 42\n"
            "\n"
            "def other_func():\n"
            "    return 1\n"
        )

        dest_file = project_dir / "dest.py"

        selector = sel(source_file, "my_func")

        diffs = move_symbol(
            selector,
            str(dest_file),
            update_imports=False,
            apply=True
        )

        # Check source file - my_func should be removed
        source_content = source_file.read_text()
        assert "def my_func():" not in source_content
        assert "def other_func():" in source_content

        # Check dest file - my_func should be added
        dest_content = dest_file.read_text()
        assert "def my_func():" in dest_content
        assert "return 42" in dest_content

        # Check diffs
        assert str(source_file) in diffs
        assert str(dest_file) in diffs

    def test_move_function_with_import_updates(self, tmp_path):
        """Move a function and update imports."""
        from emend.transform import move_symbol

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Source file with function
        source_file = project_dir / "source.py"
        source_file.write_text(
            "def helper():\n"
            "    return 42\n"
        )

        # File that imports the function
        other_file = project_dir / "other.py"
        other_file.write_text(
            "from source import helper\n"
            "\n"
            "result = helper()\n"
        )

        # Destination for the move
        dest_file = project_dir / "dest.py"

        selector = sel(source_file, "helper")

        diffs = move_symbol(
            selector,
            str(dest_file),
            update_imports=True,
            apply=True
        )

        # Check source file - helper should be removed
        source_content = source_file.read_text()
        assert "def helper():" not in source_content

        # Check dest file - helper should be added
        dest_content = dest_file.read_text()
        assert "def helper():" in dest_content

        # Check other file - import should be updated
        other_content = other_file.read_text()
        assert "from dest import helper" in other_content
        assert "result = helper()" in other_content

    def test_move_dry_run(self, tmp_path):
        """Test move in dry-run mode doesn't modify files."""
        from emend.transform import move_symbol

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        source_file = project_dir / "source.py"
        source_original = "def my_func():\n    return 42\n"
        source_file.write_text(source_original)

        dest_file = project_dir / "dest.py"
        dest_file.write_text("")

        selector = sel(source_file, "my_func")

        diffs = move_symbol(
            selector,
            str(dest_file),
            update_imports=False,
            apply=False
        )

        # Files should be unchanged
        assert source_file.read_text() == source_original
        assert dest_file.read_text() == ""

        # But diffs should show what would change
        assert str(source_file) in diffs
        assert str(dest_file) in diffs

    def test_move_without_import_updates(self, tmp_path):
        """Move a function without updating imports."""
        from emend.transform import move_symbol

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        source_file = project_dir / "source.py"
        source_file.write_text(
            "def helper():\n"
            "    return 42\n"
        )

        other_file = project_dir / "other.py"
        other_original = (
            "from source import helper\n"
            "\n"
            "result = helper()\n"
        )
        other_file.write_text(other_original)

        dest_file = project_dir / "dest.py"

        selector = sel(source_file, "helper")

        diffs = move_symbol(
            selector,
            str(dest_file),
            update_imports=False,
            apply=True
        )

        # Check that other.py was not modified
        assert other_file.read_text() == other_original

        # Check that only source and dest are in diffs
        assert str(source_file) in diffs
        assert str(dest_file) in diffs
        assert str(other_file) not in diffs



class TestEllipsisMatching:
    """Tests for ellipsis metavar matching ($...ARGS)."""

    def test_find_zero_args(self, tmp_path):
        """Match function call with zero arguments using $...ARGS."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "func()\n"
            "other(1, 2)\n"
        )

        from emend.transform import find_pattern
        matches = find_pattern("func($...ARGS)", str(test_file))
        assert len(matches) == 1
        captures = matches[0].captures
        assert "ARGS" in captures
        assert captures["ARGS"] == ""

    def test_find_one_arg(self, tmp_path):
        """Match function call with one argument using $...ARGS."""
        test_file = tmp_path / "test.py"
        test_file.write_text("func(42)\n")

        from emend.transform import find_pattern
        matches = find_pattern("func($...ARGS)", str(test_file))
        assert len(matches) == 1
        captures = matches[0].captures
        assert "ARGS" in captures
        assert captures["ARGS"] == "42"

    def test_find_multiple_args(self, tmp_path):
        """Match function call with multiple arguments using $...ARGS."""
        test_file = tmp_path / "test.py"
        test_file.write_text("func(1, 2, 3)\n")

        from emend.transform import find_pattern
        matches = find_pattern("func($...ARGS)", str(test_file))
        assert len(matches) == 1
        captures = matches[0].captures
        assert "ARGS" in captures
        assert captures["ARGS"] == "1, 2, 3"

    def test_find_mixed_captures(self, tmp_path):
        """Match with both regular and ellipsis captures."""
        test_file = tmp_path / "test.py"
        test_file.write_text("func(first, 2, 3)\n")

        from emend.transform import find_pattern
        matches = find_pattern("func($X, $...REST)", str(test_file))
        assert len(matches) == 1
        captures = matches[0].captures
        assert "X" in captures
        assert "REST" in captures
        assert captures["REST"] == "2, 3"

    def test_replace_zero_or_more_args(self, tmp_path):
        """Replace function preserving all arguments with $...ARGS."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "old()\n"
            "old(1)\n"
            "old(1, 2, 3)\n"
        )

        from emend.transform import replace_pattern
        result, count = replace_pattern(
            "old($...ARGS)",
            "new($...ARGS)",
            str(test_file),
            apply=True
        )

        content = test_file.read_text()
        assert count == 3
        assert "new()" in content
        assert "new(1)" in content
        assert "new(1, 2, 3)" in content

    def test_replace_with_fixed_and_ellipsis(self, tmp_path):
        """Replace preserving first arg and rest with $...REST."""
        test_file = tmp_path / "test.py"
        test_file.write_text("func(first, 2, 3)\n")

        from emend.transform import replace_pattern
        result, count = replace_pattern(
            "func($FIRST, $...REST)",
            "new_func($FIRST, 'extra', $...REST)",
            str(test_file),
            apply=True
        )

        content = test_file.read_text()
        assert count == 1
        assert "new_func(first, 'extra', 2, 3)" in content

    def test_replace_preserves_keyword_args(self, tmp_path):
        """Replacing with ellipsis preserves keyword arguments."""
        test_file = tmp_path / "test.py"
        test_file.write_text('old(name="task", priority=1)\n')

        from emend.transform import replace_pattern
        result, count = replace_pattern(
            "old($...ARGS)",
            "new($...ARGS)",
            str(test_file),
            apply=True
        )

        content = test_file.read_text()
        assert count == 1
        assert 'new(name="task", priority=1)' in content

    def test_replace_preserves_star_args(self, tmp_path):
        """Replacing with ellipsis preserves star and double-star args."""
        test_file = tmp_path / "test.py"
        test_file.write_text("old(*args, **kwargs)\n")

        from emend.transform import replace_pattern
        result, count = replace_pattern(
            "old($...ARGS)",
            "new($...ARGS)",
            str(test_file),
            apply=True
        )

        content = test_file.read_text()
        assert count == 1
        assert "new(*args, **kwargs)" in content

    def test_replace_ellipsis_empty_with_trailing_content(self, tmp_path):
        """Empty ellipsis with trailing content should not produce leading comma."""
        test_file = tmp_path / "test.py"
        test_file.write_text("old()\n")

        from emend.transform import replace_pattern
        result, count = replace_pattern(
            "old($...ARGS)",
            "new($...ARGS, extra=True)",
            str(test_file),
            apply=True
        )

        content = test_file.read_text()
        assert count == 1
        assert "new(extra=True)" in content
        assert "new(, extra=True)" not in content

    def test_replace_ellipsis_kwargs_with_additional_content(self, tmp_path):
        """Ellipsis with kwargs plus additional content should be comma-separated."""
        test_file = tmp_path / "test.py"
        test_file.write_text('old(name="x")\n')

        from emend.transform import replace_pattern
        result, count = replace_pattern(
            "old($...ARGS)",
            "new($...ARGS, extra=True)",
            str(test_file),
            apply=True
        )

        content = test_file.read_text()
        assert count == 1
        assert 'new(name="x", extra=True)' in content

    def test_find_captures_keyword_args(self, tmp_path):
        """find_pattern should capture keyword arguments in ellipsis."""
        test_file = tmp_path / "test.py"
        test_file.write_text('func(name="task", priority=1)\n')

        from emend.transform import find_pattern
        matches = find_pattern("func($...ARGS)", str(test_file))

        assert len(matches) == 1
        match = matches[0]
        # The ellipsis capture should contain cst.Arg nodes with keyword info
        # We just verify it captured something non-empty
        assert match.captures['ARGS']  # Should have captured args


class TestTypeConstraints:
    """Tests for type constraint matching ($X:int, $X:str, etc.)."""

    def test_int_constraint_matches_integers(self, tmp_path):
        """Type constraint $N:int matches only integer literals."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "range(10)\n"
            "range(n)\n"
            "range(5)\n"
        )

        from emend.transform import find_pattern
        matches = find_pattern("range($N:int)", str(test_file))
        assert len(matches) == 2  # Only the two integer literals

    def test_str_constraint_matches_strings(self, tmp_path):
        """Type constraint $MSG:str matches only string literals."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "print(msg)\n"
            "print(\"world\")\n"
        )

        from emend.transform import find_pattern
        matches = find_pattern("print($MSG:str)", str(test_file))
        assert len(matches) == 2  # Only the two string literals

    def test_identifier_constraint_matches_names(self, tmp_path):
        """Type constraint $X:identifier matches only names/identifiers."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print(x)\n"
            "print(42)\n"
            "print(y)\n"
        )

        from emend.transform import find_pattern
        matches = find_pattern("print($X:identifier)", str(test_file))
        assert len(matches) == 2  # Only the two identifiers x and y

    def test_mixed_type_constraints(self, tmp_path):
        """Multiple type constraints in one pattern."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "assertEqual(x, 5)\n"
            "assertEqual('hello', 'world')\n"
            "assertEqual(y, 10)\n"
        )

        from emend.transform import find_pattern
        matches = find_pattern("assertEqual($X:identifier, $N:int)", str(test_file))
        assert len(matches) == 2  # x, 5 and y, 10

    def test_float_constraint_matches_floats(self, tmp_path):
        """Type constraint $N:float matches only float literals."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "value = 3.14\n"
            "other = 42\n"
            "pi = 3.14159\n"
            "msg = 'hello'\n"
        )

        from emend.transform import find_pattern
        matches = find_pattern("$X = $N:float", str(test_file))
        assert len(matches) == 2  # Only the two float literals

    def test_call_constraint_matches_calls(self, tmp_path):
        """Type constraint $X:call matches only function calls."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "result = func()\n"
            "other = x\n"
            "value = process(data)\n"
            "number = 42\n"
        )

        from emend.transform import find_pattern
        matches = find_pattern("$NAME = $X:call", str(test_file))
        assert len(matches) == 2  # Only the two function calls

    def test_attr_constraint_matches_attributes(self, tmp_path):
        """Type constraint $X:attr matches only attribute access."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "result = obj.attr\n"
            "other = x\n"
            "value = instance.method\n"
            "call = func()\n"
        )

        from emend.transform import find_pattern
        matches = find_pattern("$NAME = $X:attr", str(test_file))
        assert len(matches) == 2  # Only the two attribute accesses

    def test_stmt_constraint_matches_statements(self, tmp_path):
        """Type constraint $X:stmt matches statement-level nodes."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "return 42\n"
            "assert x == 5\n"
            "raise ValueError('bad')\n"
            "x = 1\n"
            "del my_var\n"
        )

        from emend.transform import find_pattern
        matches = find_pattern("$X:stmt", str(test_file))
        # Every top-level line is a statement, so all five match.
        captured = {m.captures["X"] for m in matches}
        assert captured == {
            "return 42",
            "assert x == 5",
            "raise ValueError('bad')",
            "x = 1",
            "del my_var",
        }


class TestAnalyzeImports:
    """Tests for analyze_imports() function."""

    def test_simple_module_import(self, tmp_path):
        """Detect simple module imports like 'import ast'."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "import ast\n"
            "import json\n"
            "\n"
            "def func():\n"
            "    tree = ast.parse('x = 1')\n"
            "    return tree\n"
        )

        from emend.transform import analyze_imports
        source = "def func():\n    tree = ast.parse('x = 1')\n    return tree"
        imports = analyze_imports(source, str(test_file))

        assert "import ast" in imports
        assert "import json" not in imports  # json is not used

    def test_from_import_with_used_names(self, tmp_path):
        """Detect from imports and filter to only used names."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "from typing import List, Dict, Set\n"
            "from pathlib import Path\n"
            "\n"
            "def func(items: List[str]):\n"
            "    return Path('test')\n"
        )

        from emend.transform import analyze_imports
        source = "def func(items: List[str]):\n    return Path('test')"
        imports = analyze_imports(source, str(test_file))

        # Should include only used names from typing
        assert any("List" in imp for imp in imports)
        assert not any("Dict" in imp and "Set" not in imp for imp in imports)  # Dict not used
        assert "from pathlib import Path" in imports

    def test_attribute_access_imports(self, tmp_path):
        """Detect imports needed for attribute access like ast.parse."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "import ast\n"
            "import json\n"
            "\n"
            "def func():\n"
            "    return ast.parse('x = 1')\n"
        )

        from emend.transform import analyze_imports
        source = "def func():\n    return ast.parse('x = 1')"
        imports = analyze_imports(source, str(test_file))

        assert "import ast" in imports

    def test_no_imports_needed(self, tmp_path):
        """Return empty list when no imports are needed."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "import ast\n"
            "\n"
            "def func(x):\n"
            "    return x + 1\n"
        )

        from emend.transform import analyze_imports
        source = "def func(x):\n    return x + 1"
        imports = analyze_imports(source, str(test_file))

        assert imports == []


class TestFindPattern:
    """Tests for find_pattern() function."""

    def test_find_pattern_populates_line_numbers(self, tmp_path):
        """find_pattern should populate line numbers in PatternMatch objects."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('first')\n"
            "x = 5\n"
            "print('second')\n"
            "y = 10\n"
            "print('third')\n"
        )

        from emend.transform import find_pattern
        matches = find_pattern("print($X)", str(test_file))

        assert len(matches) == 3
        # All matches should have line numbers populated
        assert matches[0].line is not None
        assert matches[1].line is not None
        assert matches[2].line is not None
        # Verify correct line numbers
        assert matches[0].line == 1
        assert matches[1].line == 3
        assert matches[2].line == 5

    def test_find_pattern_with_scope(self, tmp_path):
        """find_pattern with scope should only find matches inside the scope."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('global')\n"
            "def my_func():\n"
            "    print('inside')\n"
            "def other_func():\n"
            "    print('other')\n"
        )

        from emend.transform import find_pattern
        matches = find_pattern("print($X)", str(test_file), scope=["my_func"])

        # Should find only the print inside my_func
        assert len(matches) == 1
        assert matches[0].line == 3

    def test_find_pattern_with_dotted_scope(self, tmp_path):
        """find_pattern with dotted scope should find matches inside nested scopes."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "class MyClass:\n"
            "    def method(self):\n"
            "        print('inside method')\n"
            "    def other_method(self):\n"
            "        print('other')\n"
            "def func():\n"
            "    print('in func')\n"
        )

        from emend.transform import find_pattern
        matches = find_pattern("print($X)", str(test_file), scope=["MyClass", "method"])

        # Should find only the print inside MyClass.method
        assert len(matches) == 1
        assert matches[0].line == 3


class TestContentInterpolation:
    """Tests for ${NAME.content} string interpolation in replace patterns."""

    def test_content_basic_double_quotes(self, tmp_path):
        """${X.content} strips double quotes from a captured SimpleString."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text('x = wrap("hello")\n')

        diff, count = replace_pattern(
            "wrap($X:str)", '"unwrapped: ${X.content}"', str(test_file), apply=True
        )
        assert count == 1
        content = test_file.read_text()
        assert content.strip() == 'x = "unwrapped: hello"'

    def test_content_single_quotes(self, tmp_path):
        """${X.content} strips single quotes from a captured SimpleString."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text("x = wrap('hello')\n")

        diff, count = replace_pattern(
            "wrap($X:str)", '"unwrapped: ${X.content}"', str(test_file), apply=True
        )
        assert count == 1
        content = test_file.read_text()
        assert content.strip() == 'x = "unwrapped: hello"'

    def test_content_union_to_pipe_string_first(self, tmp_path):
        """Union['MyClass', int] -> 'MyClass | int' with ${X.content}."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text('x: Union["MyClass", int]\n')

        diff, count = replace_pattern(
            "Union[$X:str, $Y]",
            '"${X.content} | $Y"',
            str(test_file),
            apply=True,
        )
        assert count == 1
        content = test_file.read_text()
        assert content.strip() == 'x: "MyClass | int"'

    def test_content_union_to_pipe_string_second(self, tmp_path):
        """Union[int, 'MyClass'] -> 'int | MyClass' with ${Y.content}."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text('x: Union[int, "MyClass"]\n')

        diff, count = replace_pattern(
            "Union[$X, $Y:str]",
            '"$X | ${Y.content}"',
            str(test_file),
            apply=True,
        )
        assert count == 1
        content = test_file.read_text()
        assert content.strip() == 'x: "int | MyClass"'

    def test_content_both_strings(self, tmp_path):
        """Union['Foo', 'Bar'] -> 'Foo | Bar' using ${.content} on both."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text('x: Union["Foo", "Bar"]\n')

        diff, count = replace_pattern(
            "Union[$X:str, $Y:str]",
            '"${X.content} | ${Y.content}"',
            str(test_file),
            apply=True,
        )
        assert count == 1
        content = test_file.read_text()
        assert content.strip() == 'x: "Foo | Bar"'

    def test_content_mixed_with_regular_metavar(self, tmp_path):
        """${X.content} and $Y can coexist in one replacement."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text('convert("name", default)\n')

        diff, count = replace_pattern(
            'convert($X:str, $Y)',
            'convert_named("${X.content}", $Y)',
            str(test_file),
            apply=True,
        )
        assert count == 1
        content = test_file.read_text()
        assert content.strip() == 'convert_named("name", default)'

    def test_content_no_match_non_string(self, tmp_path):
        """${X.content} on a non-string capture leaves the reference as-is."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text("x = wrap(42)\n")

        # $X captures an Integer, not a string — ${X.content} cannot resolve
        diff, count = replace_pattern(
            "wrap($X)", '"result: ${X.content}"', str(test_file), apply=True
        )
        # Replacement should fail to parse and be skipped
        assert count == 0

    def test_content_dry_run(self, tmp_path):
        """${X.content} works in dry-run mode."""
        from emend.transform import replace_pattern

        test_file = tmp_path / "test.py"
        test_file.write_text('x: Union["MyClass", int]\n')

        diff, count = replace_pattern(
            "Union[$X:str, $Y]",
            '"${X.content} | $Y"',
            str(test_file),
            apply=False,
        )
        assert count == 1
        assert '"MyClass | int"' in diff
