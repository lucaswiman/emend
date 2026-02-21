"""Tests for the unified lookup command."""

import pytest
import json
from pathlib import Path
from emend.transform import cmd_lookup


class TestLookupGet:
    """Test lookup in 'get' mode (component extraction)."""

    def test_lookup_get_params(self, tmp_path):
        """Test extracting function parameters."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def greet(name: str, greeting: str = "Hello"):
    pass
""")

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            selector_str=f"{test_file}::greet[params]",
        )
        assert "name: str" in result
        assert "greeting: str = \"Hello\"" in result

    def test_lookup_get_returns(self, tmp_path):
        """Test extracting return type."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def get_count() -> int:
    return 42
""")

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            selector_str=f"{test_file}::get_count[returns]",
        )
        assert "int" in result

    def test_lookup_get_decorators(self, tmp_path):
        """Test extracting decorators."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
@pytest.fixture
def my_fixture():
    pass
""")

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            selector_str=f"{test_file}::my_fixture[decorators]",
        )
        assert "@pytest.fixture" in result or "pytest.fixture" in result

    def test_lookup_get_with_accessor(self, tmp_path):
        """Test extracting specific parameter by name."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def greet(name: str, greeting: str = "Hello"):
    pass
""")

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            selector_str=f"{test_file}::greet[params][name]",
        )
        assert "name: str" in result

    def test_lookup_get_nonexistent_symbol(self, tmp_path):
        """Test error when symbol not found."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo(): pass\n")

        with pytest.raises(ValueError, match="Symbol.*not found"):
            cmd_lookup(
                file_or_pattern=str(test_file),
                selector_str=f"{test_file}::nonexistent[params]",
            )


class TestLookupShow:
    """Test lookup in 'show' mode (display source)."""

    def test_lookup_show_function(self, tmp_path):
        """Test displaying function source."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def greet(name):
    return f"Hello {name}"
""")

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            selector_str=f"{test_file}::greet",
        )
        assert "def greet" in result
        assert "Hello" in result

    def test_lookup_show_class(self, tmp_path):
        """Test displaying class source."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
class MyClass:
    def __init__(self):
        pass
""")

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            selector_str=f"{test_file}::MyClass",
        )
        assert "class MyClass" in result

    def test_lookup_show_method(self, tmp_path):
        """Test displaying method source."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
class MyClass:
    def greet(self, name):
        return f"Hello {name}"
""")

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            selector_str=f"{test_file}::MyClass.greet",
        )
        assert "def greet" in result
        assert "Hello" in result


class TestLookupQuery:
    """Test lookup in 'query' mode (filter-based search)."""

    def test_lookup_query_by_kind(self, tmp_path):
        """Test querying symbols by kind."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def func1(): pass
def func2(): pass
class MyClass: pass
""")

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            kind=["function"],
        )
        assert "func1" in result
        assert "func2" in result
        assert "MyClass" not in result or "class" not in result.split("MyClass")[0]

    def test_lookup_query_by_name(self, tmp_path):
        """Test querying symbols by name pattern."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def test_foo(): pass
def test_bar(): pass
def helper(): pass
""")

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            name=["test_*"],
        )
        assert "test_foo" in result
        assert "test_bar" in result
        assert "helper" not in result

    def test_lookup_query_by_decorator(self, tmp_path):
        """Test querying symbols by decorator."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
@pytest.fixture
def fixture1(): pass

@pytest.mark.skip
def test_foo(): pass
""")

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            has_decorator=["pytest.fixture"],
        )
        assert "fixture1" in result

    def test_lookup_query_json_output(self, tmp_path):
        """Test JSON output mode."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def foo(): pass
def bar(): pass
""")

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            kind=["function"],
            json_output=True,
        )
        data = json.loads(result)
        assert isinstance(data, list)
        assert len(data) == 2

    def test_lookup_query_paths_only(self, tmp_path):
        """Test paths-only output mode."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def foo(): pass
class MyClass:
    def method(self): pass
""")

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            kind=["function", "class", "method"],
            paths_only=True,
        )
        assert "foo" in result
        assert "MyClass" in result
        assert "method" in result

    def test_lookup_query_count(self, tmp_path):
        """Test count output mode."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def foo(): pass
def bar(): pass
def baz(): pass
""")

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            kind=["function"],
            count=True,
        )
        # Result should be a count
        assert "3" in result


class TestLookupWildcards:
    """Test lookup with wildcard patterns in symbol path."""

    def test_lookup_wildcard_all_params(self, tmp_path):
        """Test wildcard to extract all function parameters."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def func1(a, b): pass
def func2(x, y, z): pass
class MyClass:
    def method1(self, p): pass
""")

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            selector_str=f"{test_file}::*[params]",
        )
        # Should extract params from all functions
        assert "a, b" in result or "a" in result
        assert "x, y, z" in result or "x" in result

    def test_lookup_wildcard_class_methods(self, tmp_path):
        """Test wildcard to extract components from class methods."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
class MyClass:
    def method1(self, a): pass
    def method2(self, b): pass
""")

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            selector_str=f"{test_file}::MyClass.*[params]",
        )
        assert "self, a" in result or "self" in result
        assert "self, b" in result or "self" in result

    def test_lookup_wildcard_with_json(self, tmp_path):
        """Test wildcard with JSON output."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def func1(a): pass
def func2(b): pass
""")

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            selector_str=f"{test_file}::*[params]",
            json_output=True,
        )
        data = json.loads(result)
        assert isinstance(data, list)
        assert len(data) >= 2


class TestLookupErrorHandling:
    """Test error handling in lookup command."""

    def test_lookup_file_not_found(self):
        """Test error when file doesn't exist."""
        with pytest.raises(FileNotFoundError):
            cmd_lookup(
                file_or_pattern="/nonexistent/file.py",
                selector_str="/nonexistent/file.py::foo",
            )

    def test_lookup_invalid_selector(self, tmp_path):
        """Test error with invalid selector."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo(): pass\n")

        # Invalid component for class
        with pytest.raises(ValueError):
            cmd_lookup(
                file_or_pattern=str(test_file),
                selector_str=f"{test_file}::foo[returns]",  # functions don't have returns by default
            )
