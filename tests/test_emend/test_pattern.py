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
    """Tests for compile_pattern_to_matcher() function."""

    def test_compile_simple_call(self):
        """Compile a simple function call pattern."""
        from emend.pattern import compile_pattern_to_matcher

        pat = parse_pattern("print('hello')")
        matcher = compile_pattern_to_matcher(pat)
        assert matcher is not None

    def test_compile_with_metavar(self):
        """Compile pattern with a single metavariable."""
        from emend.pattern import compile_pattern_to_matcher

        pat = parse_pattern("print($X)")
        matcher = compile_pattern_to_matcher(pat)
        assert matcher is not None

    def test_compile_multiple_metavars(self):
        """Compile pattern with multiple metavariables."""
        from emend.pattern import compile_pattern_to_matcher

        pat = parse_pattern("func($A, $B)")
        matcher = compile_pattern_to_matcher(pat)
        assert matcher is not None

    def test_compile_ellipsis_returns_info(self):
        """Compile ellipsis pattern returns ellipsis_info dict."""
        from emend.pattern import compile_pattern_to_matcher

        pat = parse_pattern("func($...ARGS)")
        result = compile_pattern_to_matcher(pat)

        # Should return tuple (matcher, ellipsis_info)
        assert isinstance(result, tuple)
        assert len(result) == 2
        matcher, ellipsis_info = result
        assert matcher is not None
        assert isinstance(ellipsis_info, dict)
        assert "ARGS" in ellipsis_info

    def test_compile_mixed_ellipsis_returns_info(self):
        """Compile pattern with mixed captures returns correct ellipsis_info."""
        from emend.pattern import compile_pattern_to_matcher

        pat = parse_pattern("func($X, $...REST)")
        result = compile_pattern_to_matcher(pat)

        assert isinstance(result, tuple)
        matcher, ellipsis_info = result
        assert "REST" in ellipsis_info
        assert "X" not in ellipsis_info  # Regular captures not in ellipsis_info
