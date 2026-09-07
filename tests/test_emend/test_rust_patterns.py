"""Tests for tree-sitter pattern matching via compile_pattern_to_rust_ir.

Verifies that patterns compile to Rust IR correctly and that the Rust
tree-sitter matcher produces the expected results.
"""

import pytest

from emend.pattern import compile_pattern_to_rust_ir
from emend.transform import find_pattern


def _rust_match(file_contents, pattern_str):
    """Run pattern matching via the Rust fast-path directly.

    Returns list of (file, line, col, end_line, end_col, text) tuples.
    """
    from emend import emend_core

    ir = compile_pattern_to_rust_ir(pattern_str)
    assert ir is not None, f"Pattern {pattern_str!r} did not compile to Rust IR"
    return emend_core.find_pattern_in_files(file_contents, ir, None, None)


def _assert_rust_python_parity(tmp_path, source, pattern_str):
    """Assert that Rust and Python paths produce the same match count.

    Creates a temp file, runs both paths, and compares.
    """
    f = tmp_path / "test.py"
    f.write_text(source)
    python_matches = find_pattern(pattern_str, str(f))
    rust_matches = _rust_match([(str(f), source)], pattern_str)
    assert len(rust_matches) == len(python_matches), (
        f"Pattern {pattern_str!r}: Rust found {len(rust_matches)} matches, "
        f"Python found {len(python_matches)} matches"
    )
    return len(rust_matches)


# ============================================================================
# IR compilation tests
# ============================================================================


class TestRustIRCompilation:
    """Tests that patterns compile to the expected Rust IR."""

    def test_subscript_single(self):
        ir = compile_pattern_to_rust_ir("Optional[$X]")
        assert ir is not None
        assert ir["type"] == "subscript"
        assert ir["value"] == {"type": "name", "value": "Optional"}
        assert len(ir["slices"]) == 1
        assert ir["slices"][0] == {"type": "metavar", "name": "X"}

    def test_subscript_multi(self):
        ir = compile_pattern_to_rust_ir("dict[$K, $V]")
        assert ir is not None
        assert ir["type"] == "subscript"
        assert len(ir["slices"]) == 2

    def test_subscript_nested(self):
        ir = compile_pattern_to_rust_ir("Optional[list[$X]]")
        assert ir is not None
        assert ir["type"] == "subscript"
        assert ir["slices"][0]["type"] == "subscript"

    @pytest.mark.parametrize("pattern, op", [
        ("$X + $Y", "+"),
        ("$X - $Y", "-"),
        ("$X * $Y", "*"),
        ("$X / $Y", "/"),
        ("$X // $Y", "//"),
        ("$X % $Y", "%"),
        ("$X ** $Y", "**"),
        ("$X & $Y", "&"),
        ("$X | $Y", "|"),
        ("$X ^ $Y", "^"),
        ("$X and $Y", "and"),
        ("$X or $Y", "or"),
    ])
    def test_binary_op(self, pattern, op):
        ir = compile_pattern_to_rust_ir(pattern)
        assert ir is not None
        assert ir["type"] == "binary_op"
        assert ir["op"] == op

    def test_assign_none(self):
        ir = compile_pattern_to_rust_ir("$X = None")
        assert ir is not None
        assert ir["type"] == "assign"
        assert ir["value"] == {"type": "none_literal"}

    def test_assign_true(self):
        ir = compile_pattern_to_rust_ir("$X = True")
        assert ir is not None
        assert ir["value"] == {"type": "bool", "value": True}

    def test_assign_false(self):
        ir = compile_pattern_to_rust_ir("$X = False")
        assert ir is not None
        assert ir["value"] == {"type": "bool", "value": False}

    def test_assign_integer(self):
        ir = compile_pattern_to_rust_ir("$X = 42")
        assert ir is not None
        assert ir["value"] == {"type": "integer", "value": "42"}

    def test_assign_string(self):
        ir = compile_pattern_to_rust_ir("$X = 'hello'")
        assert ir is not None
        assert ir["value"]["type"] == "string"

    def test_assign_name(self):
        ir = compile_pattern_to_rust_ir("$X = foo")
        assert ir is not None
        assert ir["value"] == {"type": "name", "value": "foo"}

    @pytest.mark.parametrize("pattern, op", [
        ("$A == $B", "=="),
        ("$A != $B", "!="),
        ("$A < $B", "<"),
        ("$A > $B", ">"),
        ("$A <= $B", "<="),
        ("$A >= $B", ">="),
        ("$X is None", "is"),
        ("$X is not None", "is not"),
        ("$X in $Y", "in"),
        ("$X not in $Y", "not in"),
    ])
    def test_compare(self, pattern, op):
        ir = compile_pattern_to_rust_ir(pattern)
        assert ir is not None
        assert ir["type"] == "compare"
        assert ir["ops"][0]["op"] == op

    def test_unary_not(self):
        ir = compile_pattern_to_rust_ir("not $X")
        assert ir is not None
        assert ir["type"] == "unary_op"
        assert ir["op"] == "not"

    def test_unary_minus(self):
        ir = compile_pattern_to_rust_ir("-$X")
        assert ir is not None
        assert ir["op"] == "-"

    @pytest.mark.parametrize("pattern,kind,names", [
        ("($X, $Y)", "tuple", "XY"),
        ("($X, $Y, $Z)", "tuple", "XYZ"),
        ("()", "tuple", ""),
        ("[$X, $Y]", "list", "XY"),
        ("{$X, $Y}", "set", "XY"),
    ])
    def test_container_elements(self, pattern, kind, names):
        assert compile_pattern_to_rust_ir(pattern) == {
            "type": kind,
            "elements": [{"type": "metavar", "name": name} for name in names],
        }

    def test_none_literal_in_call(self):
        """None in patterns should map to 'none_literal' type, not 'name'."""
        ir = compile_pattern_to_rust_ir("print(None)")
        assert ir is not None
        assert ir["args"][0] == {"type": "none_literal"}

    def test_true_literal_in_call(self):
        ir = compile_pattern_to_rust_ir("print(True)")
        assert ir is not None
        assert ir["args"][0] == {"type": "bool", "value": True}

    def test_false_literal_in_call(self):
        ir = compile_pattern_to_rust_ir("print(False)")
        assert ir is not None
        assert ir["args"][0] == {"type": "bool", "value": False}

    def test_call(self):
        ir = compile_pattern_to_rust_ir("isinstance($X, str)")
        assert ir is not None
        assert ir["type"] == "call"

    def test_method_call(self):
        ir = compile_pattern_to_rust_ir("$X.objects.filter($...ARGS)")
        assert ir is not None
        assert ir["type"] == "call"

    def test_name(self):
        ir = compile_pattern_to_rust_ir("foo")
        assert ir is not None
        assert ir == {"type": "name", "value": "foo"}

    def test_empty_list(self):
        ir = compile_pattern_to_rust_ir("[]")
        assert ir is not None
        assert ir["type"] == "empty_list"

    def test_integer(self):
        ir = compile_pattern_to_rust_ir("42")
        assert ir is not None
        assert ir == {"type": "integer", "value": "42"}

    def test_string(self):
        ir = compile_pattern_to_rust_ir("'hello'")
        assert ir is not None
        assert ir["type"] == "string"

    def test_funcdef(self):
        ir = compile_pattern_to_rust_ir("def foo($...ARGS):")
        assert ir is not None
        assert ir["type"] == "funcdef"

    def test_classdef(self):
        ir = compile_pattern_to_rust_ir("class Foo(Base):")
        assert ir is not None
        assert ir["type"] == "classdef"
        assert ir["name"] == {"type": "name", "value": "Foo"}
        assert ir["bases"] == [{"type": "name", "value": "Base"}]

    def test_ellipsis_metavar(self):
        ir = compile_pattern_to_rust_ir("print($...ARGS)")
        assert ir is not None
        assert any(a.get("type") == "ellipsis" for a in ir["args"])


# ============================================================================
# Subscript matching
# ============================================================================


class TestRustSubscriptMatching:
    """Tests that subscript patterns produce correct matches via Rust fast-path."""

    def test_optional_single_type(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text(
            "from typing import Optional\n"
            "x: Optional[int] = None\n"
            "y: Optional[str] = None\n"
            "z: list[int] = []\n"
        )
        matches = find_pattern("Optional[$X]", str(f))
        assert len(matches) == 2

    def test_dict_two_type_params(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text(
            "x: dict[str, int] = {}\n"
            "y: dict[str, list[int]] = {}\n"
            "z: list[int] = []\n"
        )
        matches = find_pattern("dict[$K, $V]", str(f))
        assert len(matches) == 2

    def test_subscript_in_expression_context(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text(
            "x = some_dict['key']\n"
            "y = items[0]\n"
            "z = matrix[1][2]\n"
        )
        matches = find_pattern("$X[$Y]", str(f))
        assert len(matches) >= 3

    def test_subscript_in_annotation_context(self, tmp_path):
        """Subscripts in type annotations (generic_type in tree-sitter)."""
        f = tmp_path / "test.py"
        f.write_text(
            "from typing import Optional, List\n"
            "def foo(x: Optional[int], y: List[str]) -> Optional[bool]:\n"
            "    pass\n"
        )
        matches = find_pattern("Optional[$X]", str(f))
        assert len(matches) == 2

    def test_subscript_nested_annotation(self, tmp_path):
        """Nested subscripts: Optional[list[int]]"""
        f = tmp_path / "test.py"
        f.write_text(
            "x: Optional[list[int]] = None\n"
            "y: Optional[dict[str, int]] = None\n"
            "z: int = 0\n"
        )
        matches = find_pattern("Optional[$X]", str(f))
        assert len(matches) == 2

    def test_subscript_union_type(self, tmp_path):
        """Union[str, int] as subscript."""
        f = tmp_path / "test.py"
        f.write_text(
            "x: Union[str, int] = ''\n"
            "y: Union[float, None] = 0.0\n"
        )
        matches = find_pattern("Union[$X, $Y]", str(f))
        assert len(matches) == 2

    def test_subscript_no_false_positives(self, tmp_path):
        """Should not match non-subscript nodes."""
        f = tmp_path / "test.py"
        f.write_text(
            "Optional = 'not a type'\n"
            "x = Optional\n"
        )
        matches = find_pattern("Optional[$X]", str(f))
        assert len(matches) == 0

    def test_subscript_list_annotation(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text(
            "x: list[int] = []\n"
            "y: list[str] = []\n"
            "z: set[int] = set()\n"
        )
        matches = find_pattern("list[$X]", str(f))
        assert len(matches) == 2

    def test_subscript_specific_slice(self, tmp_path):
        """Match subscript with a specific type argument."""
        f = tmp_path / "test.py"
        f.write_text(
            "x: Optional[int] = None\n"
            "y: Optional[str] = None\n"
        )
        matches = find_pattern("Optional[int]", str(f))
        assert len(matches) == 1

    def test_subscript_parity(self, tmp_path):
        n = _assert_rust_python_parity(
            tmp_path,
            "x: Optional[int] = None\ny: list[str] = []\n",
            "Optional[$X]",
        )
        assert n == 1


# ============================================================================
# Assignment matching
# ============================================================================


class TestRustAssignMatching:
    """Tests that assignment patterns produce correct matches."""

    @pytest.mark.parametrize("source, pattern, expected", [
        ("x = None\ny = None\nz = 1\nw = 'hello'\n", "$X = None", 2),
        ("debug = True\nverbose = False\nflag = True\n", "$X = True", 2),
        ("debug = True\nverbose = False\nflag = False\n", "$X = False", 2),
        ("x = 42\ny = 42\nz = 0\n", "$X = 42", 2),
        ("x = 'hello'\ny = 'hello'\nz = 'world'\n", "$X = 'hello'", 2),
        ("x = foo\ny = bar\nz = foo\n", "$X = foo", 2),
        ("x = []\ny = [1]\nz = []\n", "$X = []", 2),
    ])
    def test_assign_match_count(self, tmp_path, source, pattern, expected):
        f = tmp_path / "test.py"
        f.write_text(source)
        assert len(find_pattern(pattern, str(f))) == expected

    def test_assign_no_false_positive_annotated(self, tmp_path):
        """Typed assignments like x: int = 0 are a different tree-sitter node."""
        f = tmp_path / "test.py"
        f.write_text("x = None\ny: int = None\n")
        matches = find_pattern("$X = None", str(f))
        assert len(matches) == 1
        assert matches[0].line == 1

    def test_assign_parity(self, tmp_path):
        n = _assert_rust_python_parity(
            tmp_path,
            "x = None\ny = 1\nz = None\n",
            "$X = None",
        )
        assert n == 2


# ============================================================================
# Binary operation matching
# ============================================================================


class TestRustBinaryOpMatching:
    """Tests that binary operation patterns produce correct matches."""

    @pytest.mark.parametrize("source, pattern, expected", [
        ("x = a + b\ny = c - d\nz = e + f\n", "$X + $Y", 2),
        ("x = a + b\ny = c - d\nz = e - f\n", "$X - $Y", 2),
        ("x = a * b\ny = c + d\n", "$X * $Y", 1),
        ("x = a / b\ny = c // d\n", "$X / $Y", 1),
        ("x = a / b\ny = c // d\n", "$X // $Y", 1),
        ("x = a % b\ny = c + d\n", "$X % $Y", 1),
        ("x = a ** 2\ny = b ** 3\n", "$X ** $Y", 2),
        ("x = a | b\ny = c & d\n", "$X | $Y", 1),
        ("x = a | b\ny = c & d\n", "$X & $Y", 1),
        ("if a or b:\n    pass\nif c and d:\n    pass\n", "$X or $Y", 1),
        ("if a or b:\n    pass\nif c and d:\n    pass\n", "$X and $Y", 1),
        # Right operand pinned to a specific literal.
        ("x = a + 1\ny = b + 2\nz = c + 1\n", "$X + 1", 2),
    ])
    def test_binary_op_match_count(self, tmp_path, source, pattern, expected):
        f = tmp_path / "test.py"
        f.write_text(source)
        assert len(find_pattern(pattern, str(f))) == expected

    def test_binary_parity(self, tmp_path):
        _assert_rust_python_parity(
            tmp_path,
            "x = a + b\ny = c - d\nz = e + f\n",
            "$X + $Y",
        )


# ============================================================================
# Comparison matching
# ============================================================================


class TestRustCompareMatching:
    """Tests that comparison patterns produce correct matches."""

    @pytest.mark.parametrize("source, pattern, expected", [
        ("if x == 1:\n    pass\nif y != 2:\n    pass\nif z == 3:\n    pass\n", "$X == $Y", 2),
        ("if x == 1:\n    pass\nif y != 2:\n    pass\n", "$X != $Y", 1),
        ("if x < 10:\n    pass\nif y > 20:\n    pass\n", "$X < $Y", 1),
        ("if x < 10:\n    pass\nif y > 20:\n    pass\n", "$X > $Y", 1),
        ("if x is None:\n    pass\nif y is not None:\n    pass\n", "$X is None", 1),
        ("if x is None:\n    pass\nif y is not None:\n    pass\n", "$X is not None", 1),
        ("if x in items:\n    pass\nif y not in items:\n    pass\n", "$X in $Y", 1),
        ("if x in items:\n    pass\nif y not in items:\n    pass\n", "$X not in $Y", 1),
        # Right operand pinned to a specific literal.
        ("if x == 0:\n    pass\nif y == 0:\n    pass\nif z == 1:\n    pass\n", "$X == 0", 2),
    ])
    def test_compare_match_count(self, tmp_path, source, pattern, expected):
        f = tmp_path / "test.py"
        f.write_text(source)
        assert len(find_pattern(pattern, str(f))) == expected

    def test_compare_parity(self, tmp_path):
        _assert_rust_python_parity(
            tmp_path,
            "if x == 1:\n    pass\nif y != 2:\n    pass\n",
            "$X == $Y",
        )


# ============================================================================
# Unary operation matching
# ============================================================================


class TestRustUnaryOpMatching:
    """Tests that unary operation patterns produce correct matches."""

    def test_not(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("if not x:\n    pass\nif not y:\n    pass\nif z:\n    pass\n")
        matches = find_pattern("not $X", str(f))
        assert len(matches) == 2

    def test_not_specific(self, tmp_path):
        """Match 'not True' specifically."""
        f = tmp_path / "test.py"
        f.write_text("x = not True\ny = not False\nz = not x\n")
        matches = find_pattern("not True", str(f))
        assert len(matches) == 1

    def test_negative(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("x = -1\ny = -x\nz = +1\n")
        matches = find_pattern("-$X", str(f))
        assert len(matches) == 2

    def test_unary_parity(self, tmp_path):
        _assert_rust_python_parity(
            tmp_path,
            "if not x:\n    pass\nif y:\n    pass\n",
            "not $X",
        )


# ============================================================================
# Tuple matching
# ============================================================================


class TestRustTupleMatching:
    """Tests that tuple patterns produce correct matches."""

    def test_two_element_tuple(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("a = (1, 2)\nb = (3, 4, 5)\nc = (6, 7)\n")
        matches = find_pattern("($X, $Y)", str(f))
        assert len(matches) == 2

    def test_three_element_tuple(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("a = (1, 2)\nb = (3, 4, 5)\nc = (6, 7, 8)\n")
        matches = find_pattern("($X, $Y, $Z)", str(f))
        assert len(matches) == 2

    def test_specific_tuple(self, tmp_path):
        """Match a tuple with specific values."""
        f = tmp_path / "test.py"
        f.write_text("a = (1, 2)\nb = (1, 3)\nc = (2, 2)\n")
        matches = find_pattern("(1, $X)", str(f))
        assert len(matches) == 2


# ============================================================================
# None / True / False literal matching
# ============================================================================


class TestRustLiteralMatching:
    """Tests for None/True/False handling in various contexts."""

    def test_none_as_call_arg(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("foo(None)\nfoo(1)\nbar(None)\n")
        matches = find_pattern("foo(None)", str(f))
        assert len(matches) == 1

    def test_true_as_call_arg(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("foo(True)\nfoo(False)\nfoo(True)\n")
        matches = find_pattern("foo(True)", str(f))
        assert len(matches) == 2

    def test_false_as_call_arg(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("foo(True)\nfoo(False)\nfoo(True)\n")
        matches = find_pattern("foo(False)", str(f))
        assert len(matches) == 1

    def test_none_comparison(self, tmp_path):
        n = _assert_rust_python_parity(
            tmp_path,
            "if x is None:\n    pass\nif y == None:\n    pass\n",
            "$X is None",
        )
        assert n == 1


# ============================================================================
# Existing patterns still work (regression tests)
# ============================================================================


class TestRustExistingPatterns:
    """Regression tests for patterns that already worked before the changes."""

    def test_simple_call(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("print('hello')\nprint(42)\nlen(x)\n")
        matches = find_pattern("print($X)", str(f))
        assert len(matches) == 2

    def test_isinstance(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("isinstance(x, str)\nisinstance(y, int)\ntype(z)\n")
        matches = find_pattern("isinstance($X, str)", str(f))
        assert len(matches) == 1

    def test_method_call(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("x.append(1)\ny.append(2)\nz.extend([3])\n")
        matches = find_pattern("$X.append($Y)", str(f))
        assert len(matches) == 2

    def test_ellipsis_args(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("print(1, 2, 3)\nprint()\nprint('a')\n")
        matches = find_pattern("print($...ARGS)", str(f))
        assert len(matches) == 3

    def test_chained_attr_call(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text(
            "qs.filter(x=1)\nqs.exclude(y=2)\n"
            "Model.objects.filter(z=3)\n"
        )
        matches = find_pattern("$X.filter($...ARGS)", str(f))
        assert len(matches) == 2

    def test_funcdef(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text(
            "def foo():\n    pass\n"
            "def bar(x):\n    pass\n"
            "def baz(x, y):\n    pass\n"
        )
        matches = find_pattern("def $F($...ARGS):", str(f))
        assert len(matches) == 3

    def test_empty_list(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("x = []\ny = [1]\nz = []\n")
        matches = find_pattern("[]", str(f))
        assert len(matches) == 2


# ============================================================================
# Multi-file Rust fast-path
# ============================================================================


class TestRustMultiFilePatterns:
    """Tests that Rust fast-path works correctly across multiple files."""

    def test_subscript_multi_file(self, tmp_path):
        """Ensure the Rust multi-file path is exercised for subscript patterns."""
        (tmp_path / "a.py").write_text(
            "from typing import Optional\nx: Optional[int] = None\n"
        )
        (tmp_path / "b.py").write_text(
            "from typing import Optional\ny: Optional[str] = None\n"
        )
        (tmp_path / "c.py").write_text("z = 42\n")
        from emend import emend_core

        file_strs = emend_core.collect_python_files(str(tmp_path))
        assert len(file_strs) == 3
        file_contents = emend_core.read_and_filter_files(file_strs, ["Optional"])
        assert len(file_contents) == 2

        ir = compile_pattern_to_rust_ir("Optional[$X]")
        assert ir is not None
        raw = emend_core.find_pattern_in_files(list(file_contents), ir, None, None)
        assert len(raw) == 2

    def test_assign_none_multi_file(self, tmp_path):
        (tmp_path / "a.py").write_text("x = None\ny = 1\n")
        (tmp_path / "b.py").write_text("z = None\n")
        from emend import emend_core

        file_strs = emend_core.collect_python_files(str(tmp_path))
        file_contents = emend_core.read_and_filter_files(file_strs, [])
        ir = compile_pattern_to_rust_ir("$X = None")
        assert ir is not None
        raw = emend_core.find_pattern_in_files(list(file_contents), ir, None, None)
        assert len(raw) == 2

    def test_binary_op_multi_file(self, tmp_path):
        (tmp_path / "a.py").write_text("x = a + b\n")
        (tmp_path / "b.py").write_text("y = c + d\nz = e - f\n")
        from emend import emend_core

        file_strs = emend_core.collect_python_files(str(tmp_path))
        file_contents = emend_core.read_and_filter_files(file_strs, [])
        ir = compile_pattern_to_rust_ir("$X + $Y")
        assert ir is not None
        raw = emend_core.find_pattern_in_files(list(file_contents), ir, None, None)
        assert len(raw) == 2

    def test_compare_multi_file(self, tmp_path):
        (tmp_path / "a.py").write_text("if x == 1:\n    pass\n")
        (tmp_path / "b.py").write_text("if y == 2:\n    pass\nif z != 3:\n    pass\n")
        from emend import emend_core

        file_strs = emend_core.collect_python_files(str(tmp_path))
        file_contents = emend_core.read_and_filter_files(file_strs, [])
        ir = compile_pattern_to_rust_ir("$X == $Y")
        assert ir is not None
        raw = emend_core.find_pattern_in_files(list(file_contents), ir, None, None)
        assert len(raw) == 2

    def test_inside_constraint_with_subscript(self, tmp_path):
        """Test inside constraint works with subscript patterns via Rust."""
        from emend.pattern import compile_constraint_to_rust_ir
        from emend import emend_core

        (tmp_path / "test.py").write_text(
            "def foo():\n    x: Optional[int] = None\n"
            "x: Optional[str] = None\n"
        )
        file_strs = emend_core.collect_python_files(str(tmp_path))
        file_contents = emend_core.read_and_filter_files(file_strs, [])

        ir = compile_pattern_to_rust_ir("Optional[$X]")
        inside_ir = compile_constraint_to_rust_ir("def")
        assert ir is not None
        assert inside_ir is not None
        raw = emend_core.find_pattern_in_files(
            list(file_contents), ir, inside_ir, None
        )
        # Only the one inside def foo() should match
        assert len(raw) == 1


# ============================================================================
# Edge cases and complex patterns
# ============================================================================


class TestRustEdgeCases:
    """Edge cases for Rust pattern matching."""

    def test_nested_call_with_none(self, tmp_path):
        """foo(bar(None)) should match both foo($X) and bar(None)."""
        f = tmp_path / "test.py"
        f.write_text("foo(bar(None))\n")
        matches = find_pattern("bar(None)", str(f))
        assert len(matches) == 1

    def test_chained_comparison(self, tmp_path):
        """x < y < z is not a simple comparison."""
        f = tmp_path / "test.py"
        f.write_text("if 0 < x < 10:\n    pass\nif y < 5:\n    pass\n")
        matches = find_pattern("$X < $Y", str(f))
        # At least the simple y < 5 should match
        assert len(matches) >= 1

    def test_assign_with_call_value(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("x = foo()\ny = bar()\nz = 1\n")
        matches = find_pattern("$X = foo()", str(f))
        assert len(matches) == 1

    def test_subscript_with_call(self, tmp_path):
        """Match subscript where the value is a call result."""
        f = tmp_path / "test.py"
        f.write_text("x = items[0]\ny = get_items()[0]\n")
        matches = find_pattern("$X[0]", str(f))
        assert len(matches) == 2

    def test_binary_with_call(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("x = len(a) + len(b)\ny = len(c) - 1\n")
        matches = find_pattern("len($X) + $Y", str(f))
        assert len(matches) == 1

    def test_empty_file(self, tmp_path):
        """Empty files should not crash."""
        f = tmp_path / "test.py"
        f.write_text("")
        matches = find_pattern("$X = None", str(f))
        assert len(matches) == 0

    def test_syntax_error_file(self, tmp_path):
        """Files with syntax errors should not crash via Rust path."""
        from emend import emend_core

        ir = compile_pattern_to_rust_ir("$X = None")
        assert ir is not None
        raw = emend_core.find_pattern_in_files(
            [("test.py", "def foo(\n")], ir, None, None
        )
        assert len(raw) == 0

    def test_complex_nested_subscript(self, tmp_path):
        """Complex nested generic types."""
        f = tmp_path / "test.py"
        f.write_text(
            "x: dict[str, list[Optional[int]]] = {}\n"
            "y: dict[str, int] = {}\n"
        )
        matches = find_pattern("dict[$K, $V]", str(f))
        assert len(matches) == 2

    def test_multiple_patterns_same_line(self, tmp_path):
        """Multiple matches on the same line."""
        f = tmp_path / "test.py"
        f.write_text("x = a + b + c\n")
        matches = find_pattern("$X + $Y", str(f))
        # a + b and (a + b) + c - at least 1, possibly 2
        assert len(matches) >= 1


class TestProjectBatchFastPath:
    """Regression tests for the Stage-3 Rust batch fast-path in
    ``find_pattern_in_project`` (transform/project_iter.py).

    The Rust ``find_pattern_in_files`` returns 7-tuples
    ``(file, line, col, end_line, end_col, text, captures)``. A previous
    version unpacked only 6 values, so the batch path always raised
    ``ValueError`` and silently fell through to the slower Python
    Stage-4 fallback (and captures were hardcoded to ``{}``). These tests
    sabotage the Stage-4 fallback so they only pass when Stage 3 works.
    """

    def _sabotage_fallback(self, monkeypatch):
        import emend.transform.patterns as patterns

        def _boom(*args, **kwargs):
            raise RuntimeError("Stage-4 fallback should not be reached")

        # find_pattern_in_project does `from .patterns import find_pattern`
        # inside the function body, so patching the module attribute works.
        monkeypatch.setattr(patterns, "find_pattern", _boom)

    def test_batch_fast_path_returns_matches(self, tmp_path, monkeypatch):
        from emend.transform import find_pattern_in_project

        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text('print("hello")\n')
        b.write_text('print("world")\n')

        self._sabotage_fallback(monkeypatch)

        results = find_pattern_in_project(
            'print($MSG)', [str(a), str(b)],
        )
        # If the batch path worked, we get both matches without ever
        # touching the sabotaged fallback.
        assert len(results) == 2
        assert {r.file_path for r in results} == {str(a), str(b)}

    def test_batch_fast_path_populates_captures(self, tmp_path, monkeypatch):
        from emend.transform import find_pattern_in_project

        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text('print("hello")\n')
        b.write_text('print("world")\n')

        self._sabotage_fallback(monkeypatch)

        results = find_pattern_in_project(
            'print($MSG)', [str(a), str(b)],
        )
        by_file = {r.file_path: r.match for r in results}
        assert by_file[str(a)].captures.get("MSG") == '"hello"'
        assert by_file[str(b)].captures.get("MSG") == '"world"'

    def test_batch_fast_path_honors_language_override(self, tmp_path, monkeypatch):
        from emend.transform import find_pattern_in_project

        files = [tmp_path / name for name in ("a.py", "b.py")]
        for path in files:
            path.write_text("const result = first == second;\n")
        self._sabotage_fallback(monkeypatch)

        results = find_pattern_in_project(
            "$X == $Y", [str(path) for path in files], language="typescript",
        )

        assert {result.file_path for result in results} == {
            str(path) for path in files
        }
