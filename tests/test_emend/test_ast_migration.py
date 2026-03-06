"""Regression tests for AST backend migration.

Validates that find_nested_definitions, query symbol collection,
and list-symbols produce correct output across AST backend changes.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from emend.ast_utils import (
    find_nested_definitions,
    find_symbol_by_path,
    find_symbol_by_line,
    expand_wildcard_path,
    get_symbol_source,
)
from emend.component_selector import (
    NestedSymbol,
    ExtendedSelector,
    parse_extended_selector,
)
from emend.query import query_symbols, QueryFilter, SymbolInfo


EMEND = str(Path(sys.executable).parent / "emend")


# ---------------------------------------------------------------------------
# find_nested_definitions regression tests
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


# ---------------------------------------------------------------------------
# query.py symbol collection regression tests
# ---------------------------------------------------------------------------

class TestQuerySymbolCollection:
    def test_collect_functions(self, sample_file):
        filters = QueryFilter(kinds=["function"])
        results = query_symbols(sample_file, filters)
        names = [s.name for s in results]
        assert "standalone" in names
        assert "method" not in names

    def test_collect_methods(self, sample_file):
        filters = QueryFilter(kinds=["method"])
        results = query_symbols(sample_file, filters)
        names = [s.name for s in results]
        assert "method" in names
        assert "standalone" not in names

    def test_collect_async(self, sample_file):
        filters = QueryFilter(kinds=["async_function"])
        results = query_symbols(sample_file, filters)
        names = [s.name for s in results]
        assert "async_func" in names
        assert "async_method" not in names

    def test_decorator_filter(self, sample_file):
        filters = QueryFilter(decorators=["property"])
        results = query_symbols(sample_file, filters)
        names = [s.name for s in results]
        assert "name" in names

    def test_returns_filter(self, sample_file):
        filters = QueryFilter(returns_patterns=["None"])
        results = query_symbols(sample_file, filters)
        names = [s.name for s in results]
        assert "async_func" in names

    def test_depth_filter(self, sample_file):
        filters = QueryFilter(depths=["1"])
        results = query_symbols(sample_file, filters)
        names = [s.name for s in results]
        assert "standalone" in names
        assert "MyClass" in names
        assert "method" not in names

    def test_parameter_info(self, sample_file):
        filters = QueryFilter(names=["standalone"])
        results = query_symbols(sample_file, filters)
        assert len(results) == 1
        params = results[0].parameters
        assert any("x: int" in p for p in params)

    def test_return_type_info(self, sample_file):
        filters = QueryFilter(names=["standalone"])
        results = query_symbols(sample_file, filters)
        assert len(results) == 1
        assert results[0].returns == "list[str]"


# ---------------------------------------------------------------------------
# Selector unification regression tests
# ---------------------------------------------------------------------------

class TestSelectorUnification:
    def test_nested_symbol_from_component_selector(self):
        """NestedSymbol is importable from component_selector."""
        from emend.component_selector import NestedSymbol
        sym = NestedSymbol(
            name="test", kind="function", line_start=1, line_end=2,
            col_offset=0, path=["test"],
        )
        assert sym.name == "test"

    def test_extended_selector_line_range(self):
        """ExtendedSelector has line_range property."""
        sel = ExtendedSelector(
            file_path="test.py", symbol_path=[], line_start=10, line_end=20,
        )
        assert sel.line_range == (10, 20)

    def test_extended_selector_line_range_none(self):
        """line_range is None when line_start/end not set."""
        sel = ExtendedSelector(file_path="test.py", symbol_path=["func"])
        assert sel.line_range is None

    def test_parse_extended_selector_symbol(self):
        """parse_extended_selector handles symbol paths."""
        sel = parse_extended_selector("file.py::func")
        assert sel.file_path == "file.py"
        assert sel.symbol_path == ["func"]

    def test_parse_extended_selector_line(self):
        """parse_extended_selector handles line-based selectors."""
        sel = parse_extended_selector("file.py:10")
        assert sel.file_path == "file.py"
        assert sel.line_start == 10

    def test_parse_extended_selector_line_range(self):
        """parse_extended_selector handles line range selectors."""
        sel = parse_extended_selector("file.py:10-20")
        assert sel.file_path == "file.py"
        assert sel.line_start == 10
        assert sel.line_end == 20


# ---------------------------------------------------------------------------
# list-symbols LibCST regression tests
# ---------------------------------------------------------------------------

class TestListSymbolsLibCST:
    def test_basic_output(self, tmp_path):
        """list-symbols produces correct output after LibCST migration."""
        code = '''\
def foo(x: int) -> str:
    return str(x)

class Bar:
    def method(self):
        pass
'''
        filepath = tmp_path / "test.py"
        filepath.write_text(code)

        result = subprocess.run(
            [EMEND, "search", str(filepath), "--output", "summary"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "foo(x: int) -> str" in result.stdout
        assert "Bar" in result.stdout
        assert "method(self)" in result.stdout

    def test_variables_in_tree(self, tmp_path):
        """Variables appear in tree mode with correct type annotations."""
        code = '''\
def func():
    x: int = 42
    y = "hello"
'''
        filepath = tmp_path / "test.py"
        filepath.write_text(code)

        result = subprocess.run(
            [EMEND, "search", str(filepath), "--output", "summary", "--depth", "2"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "var x: int" in result.stdout
        assert "var y" in result.stdout

    def test_outer_references(self, tmp_path):
        """Outer-scope references are detected correctly."""
        code = '''\
def outer():
    config = {"key": "value"}
    def inner():
        print(config)
    return inner
'''
        filepath = tmp_path / "test.py"
        filepath.write_text(code)

        result = subprocess.run(
            [EMEND, "search", str(filepath), "--output", "summary", "--depth", "3"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "ref config" in result.stdout

    def test_async_function_signature(self, tmp_path):
        """Async functions show correct signatures."""
        code = '''\
async def handler(request: Request, *, timeout: int = 30) -> Response:
    pass
'''
        filepath = tmp_path / "test.py"
        filepath.write_text(code)

        result = subprocess.run(
            [EMEND, "search", str(filepath), "--output", "summary"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "async def handler(request: Request, *, timeout: int = 30) -> Response" in result.stdout
