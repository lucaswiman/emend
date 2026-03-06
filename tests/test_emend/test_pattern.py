"""Tests for pattern parsing."""
import pytest
from emend.pattern import parse_pattern, MetaVar


class TestPatternParsing:
    def test_simple_metavar(self):
        pat = parse_pattern("print($MSG)")
        assert len(pat.metavars) == 1
        assert pat.metavars[0].name == "MSG"

    def test_multiple_metavars(self):
        pat = parse_pattern("func($A, $B)")
        assert len(pat.metavars) == 2

    def test_ellipsis_metavar(self):
        pat = parse_pattern("func($...ARGS)")
        assert pat.metavars[0].ellipsis is True

    def test_anonymous_metavar(self):
        pat = parse_pattern("func($_, $X)")
        assert pat.metavars[0].name == "_"

    def test_typed_metavar(self):
        pat = parse_pattern("print($MSG:str)")
        assert pat.metavars[0].type_constraint == "str"

    def test_no_metavars(self):
        pat = parse_pattern("print('hello')")
        assert len(pat.metavars) == 0


class TestCompilePattern:
    """Tests for compile_pattern_to_rust_ir() function."""

    def test_compile_simple_call(self):
        """Compile a simple function call pattern."""
        from emend.pattern import compile_pattern_to_rust_ir

        ir = compile_pattern_to_rust_ir("print('hello')")
        assert ir is not None
        assert ir["type"] == "call"

    def test_compile_with_metavar(self):
        """Compile pattern with a single metavariable."""
        from emend.pattern import compile_pattern_to_rust_ir

        ir = compile_pattern_to_rust_ir("print($X)")
        assert ir is not None
        assert ir["type"] == "call"
        assert ir["args"][0] == {"type": "metavar", "name": "X"}

    def test_compile_multiple_metavars(self):
        """Compile pattern with multiple metavariables."""
        from emend.pattern import compile_pattern_to_rust_ir

        ir = compile_pattern_to_rust_ir("func($A, $B)")
        assert ir is not None
        assert len(ir["args"]) == 2

    def test_compile_ellipsis_returns_ir(self):
        """Compile ellipsis pattern returns IR with ellipsis node."""
        from emend.pattern import compile_pattern_to_rust_ir

        ir = compile_pattern_to_rust_ir("func($...ARGS)")
        assert ir is not None
        assert ir["type"] == "call"
        assert ir["args"][0] == {"type": "ellipsis", "name": "ARGS"}

    def test_compile_mixed_ellipsis_returns_ir(self):
        """Compile pattern with mixed captures returns correct IR."""
        from emend.pattern import compile_pattern_to_rust_ir

        ir = compile_pattern_to_rust_ir("func($X, $...REST)")
        assert ir is not None
        assert ir["args"][0] == {"type": "metavar", "name": "X"}
        assert ir["args"][1] == {"type": "ellipsis", "name": "REST"}
