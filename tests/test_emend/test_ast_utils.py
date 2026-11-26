"""Tests for the symbol-tree helpers in ``emend.ast_utils``.

Covers ``find_nested_definitions``, ``find_symbol_by_path``,
``find_symbol_by_line`` and ``expand_wildcard_path``.
"""

import pytest

from emend.ast_utils import (
    find_nested_definitions,
    find_symbol_by_path,
    find_symbol_by_line,
    expand_wildcard_path,
)


# ---------------------------------------------------------------------------
# find_nested_definitions
# ---------------------------------------------------------------------------

SAMPLE_CODE = '''\
import os
from typing import Optional

@staticmethod
def standalone(x: int, /, y: str = "default", *args, kw_only: bool = True, **kwargs) -> list[str]:
    """A standalone function."""
    return [str(x)]

async def async_func(data: list) -> None:
    pass

class MyClass:
    """A sample class."""

    @property
    def name(self) -> str:
        return "test"

    def method(self, request):
        def closure():
            return request
        return closure()

    async def async_method(self, ctx) -> Optional[str]:
        return None
'''


@pytest.fixture
def sample_file(tmp_path):
    p = tmp_path / "sample.py"
    p.write_text(SAMPLE_CODE)
    return p


class TestFindNestedDefinitions:
    def test_top_level_function(self, sample_file):
        symbols = find_nested_definitions(str(sample_file))
        standalone = next(s for s in symbols if s.name == "standalone")
        assert standalone.kind == "function"
        assert standalone.line_start == 5
        assert standalone.line_end == 7
        assert standalone.col_offset == 0
        assert standalone.path == ["standalone"]
        assert "staticmethod" in standalone.decorators[0]
        assert standalone.decorator_line_start == 4

    def test_parameters(self, sample_file):
        symbols = find_nested_definitions(str(sample_file))
        standalone = next(s for s in symbols if s.name == "standalone")
        params = standalone.parameters
        assert "x" in params
        assert "/" in params
        assert "y" in params
        assert "*args" in params
        assert "kw_only" in params
        assert "**kwargs" in params

    def test_async_function(self, sample_file):
        symbols = find_nested_definitions(str(sample_file))
        async_func = next(s for s in symbols if s.name == "async_func")
        assert async_func.kind == "async_function"

    def test_class(self, sample_file):
        symbols = find_nested_definitions(str(sample_file))
        cls = next(s for s in symbols if s.name == "MyClass")
        assert cls.kind == "class"
        assert len(cls.children) == 3  # name, method, async_method

    def test_method(self, sample_file):
        symbols = find_nested_definitions(str(sample_file))
        cls = next(s for s in symbols if s.name == "MyClass")
        method = next(c for c in cls.children if c.name == "method")
        assert method.kind == "method"
        assert method.path == ["MyClass", "method"]

    def test_async_method(self, sample_file):
        symbols = find_nested_definitions(str(sample_file))
        cls = next(s for s in symbols if s.name == "MyClass")
        am = next(c for c in cls.children if c.name == "async_method")
        assert am.kind == "async_method"

    def test_nested_closure(self, sample_file):
        symbols = find_nested_definitions(str(sample_file))
        cls = next(s for s in symbols if s.name == "MyClass")
        method = next(c for c in cls.children if c.name == "method")
        closure = next(c for c in method.children if c.name == "closure")
        assert closure.kind == "function"  # closure in method is function, not method
        assert closure.path == ["MyClass", "method", "closure"]

    def test_max_depth(self, sample_file):
        symbols = find_nested_definitions(str(sample_file), max_depth=0)
        # max_depth=0 should return top-level symbols only
        names = [s.name for s in symbols]
        assert "standalone" in names
        assert "async_func" in names
        assert "MyClass" in names
        # But no children
        cls = next(s for s in symbols if s.name == "MyClass")
        assert len(cls.children) == 0

    def test_decorator_property(self, sample_file):
        symbols = find_nested_definitions(str(sample_file))
        cls = next(s for s in symbols if s.name == "MyClass")
        name_prop = next(c for c in cls.children if c.name == "name")
        assert "property" in name_prop.decorators


class TestFindSymbolByPath:
    def test_find_top_level(self, sample_file):
        symbols = find_nested_definitions(str(sample_file))
        result = find_symbol_by_path(symbols, ["standalone"])
        assert result is not None
        assert result.name == "standalone"

    def test_find_nested(self, sample_file):
        symbols = find_nested_definitions(str(sample_file))
        result = find_symbol_by_path(symbols, ["MyClass", "method"])
        assert result is not None
        assert result.name == "method"

    def test_find_deep_nested(self, sample_file):
        symbols = find_nested_definitions(str(sample_file))
        result = find_symbol_by_path(symbols, ["MyClass", "method", "closure"])
        assert result is not None
        assert result.name == "closure"

    def test_not_found(self, sample_file):
        symbols = find_nested_definitions(str(sample_file))
        result = find_symbol_by_path(symbols, ["nonexistent"])
        assert result is None


class TestFindSymbolByLine:
    def test_find_by_line(self, sample_file):
        symbols = find_nested_definitions(str(sample_file))
        result = find_symbol_by_line(symbols, 5)
        assert result is not None
        assert result.name == "standalone"

    def test_find_innermost(self, sample_file):
        symbols = find_nested_definitions(str(sample_file))
        # Line 20 is inside the closure inside method inside MyClass
        # The closure is on line 20-21
        cls = next(s for s in symbols if s.name == "MyClass")
        method = next(c for c in cls.children if c.name == "method")
        closure = next(c for c in method.children if c.name == "closure")
        result = find_symbol_by_line(symbols, closure.line_start)
        assert result is not None
        assert result.name == "closure"


class TestExpandWildcard:
    def test_wildcard_top_level(self, sample_file):
        symbols = find_nested_definitions(str(sample_file))
        results = expand_wildcard_path(symbols, ["*"])
        names = [s.name for s in results]
        assert "standalone" in names
        assert "async_func" in names
        assert "MyClass" in names

    def test_wildcard_methods(self, sample_file):
        symbols = find_nested_definitions(str(sample_file))
        results = expand_wildcard_path(symbols, ["MyClass", "*"])
        names = [s.name for s in results]
        assert "name" in names
        assert "method" in names
        assert "async_method" in names
