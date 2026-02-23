"""Tests for emend query command."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


# Use emend for query tests
EMEND = str(Path(sys.executable).parent / "emend")  # was emend2, now emend


def run_emend(args, check=True):
    """Run emend command and return result."""
    cmd = [EMEND] + args
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        print(f"STDOUT:\n{result.stdout}")
        print(f"STDERR:\n{result.stderr}")
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    return result


@pytest.fixture
def sample_file(tmp_path):
    """Create a sample Python file with various symbol types."""
    code = '''\
import os
from typing import Optional

def standalone_func(x: int) -> str:
    """A standalone function."""
    return str(x)

async def async_standalone(data: list) -> None:
    """An async standalone function."""
    pass

@pytest.fixture
def test_helper():
    """A test helper with decorator."""
    pass

class MyClass:
    """A sample class."""

    @property
    def name(self) -> str:
        return "test"

    def regular_method(self, request: Request) -> Response:
        """A regular method."""
        return Response()

    async def async_method(self, ctx: Context) -> Optional[str]:
        """An async method."""
        return None

    @staticmethod
    def static_method(value: int) -> int:
        return value * 2

    @classmethod
    def class_method(cls) -> str:
        return cls.__name__

    def _private_method(self):
        pass

class TestSuite:
    """A test class."""

    def test_create(self):
        pass

    def test_update(self, mock_db):
        pass

    def test_delete_async(self):
        pass

def helper_one():
    pass

def helper_two():
    pass
'''
    filepath = tmp_path / "sample.py"
    filepath.write_text(code)
    return filepath


class TestQueryKind:
    """Tests for --kind filter."""

    def test_query_kind_function(self, sample_file):
        """--kind function returns only top-level functions."""
        result = run_emend(["search", str(sample_file), "--kind", "function", "--json"])
        assert result.returncode == 0

        # Parse JSON output
        data = json.loads(result.stdout)
        assert isinstance(data, list), "JSON output should be a list"

        # Extract names and kinds from results
        names = {item["name"] for item in data}
        kinds = {item["kind"] for item in data}

        # Verify expected functions are included
        expected_functions = {"standalone_func", "test_helper", "helper_one", "helper_two"}
        assert expected_functions.issubset(names), (
            f"Expected all of {expected_functions} in results, but got {names}"
        )

        # Verify all items have kind="function" (not async_function or method)
        assert kinds == {"function"}, (
            f"Expected only 'function' kind, but got {kinds}"
        )

        # Verify no methods or async functions are included
        assert "regular_method" not in names, "Should not include methods"
        assert "async_standalone" not in names, "Should not include async functions"

        # Validate all items have required fields
        for item in data:
            assert "path" in item, "Missing 'path' field"
            assert "name" in item, "Missing 'name' field"
            assert "kind" in item, "Missing 'kind' field"
            assert "line" in item, "Missing 'line' field"
            assert "end_line" in item, "Missing 'end_line' field"
            assert isinstance(item["line"], int), "Line should be an integer"
            assert isinstance(item["end_line"], int), "End line should be an integer"
            assert item["line"] > 0, "Line should be positive"
            assert item["end_line"] >= item["line"], "End line should be >= line"

    def test_query_kind_method(self, sample_file):
        """--kind method returns only methods."""
        result = run_emend(["search", str(sample_file), "--kind", "method"])
        assert result.returncode == 0
        assert "regular_method" in result.stdout
        assert "_private_method" in result.stdout
        # Should not include standalone functions or async methods
        assert "standalone_func" not in result.stdout
        assert "async_method" not in result.stdout

    def test_query_kind_async_function(self, sample_file):
        """--kind async_function returns async standalone functions."""
        result = run_emend(["search", str(sample_file), "--kind", "async_function"])
        assert result.returncode == 0
        assert "async_standalone" in result.stdout
        assert "async_method" not in result.stdout
        assert "standalone_func" not in result.stdout

    def test_query_kind_async_method(self, sample_file):
        """--kind async_method returns async methods."""
        result = run_emend(["search", str(sample_file), "--kind", "async_method"])
        assert result.returncode == 0
        assert "async_method" in result.stdout
        assert "async_standalone" not in result.stdout

    def test_query_kind_class(self, sample_file):
        """--kind class returns only classes."""
        result = run_emend(["search", str(sample_file), "--kind", "class"])
        assert result.returncode == 0
        assert "MyClass" in result.stdout
        assert "TestSuite" in result.stdout
        assert "standalone_func" not in result.stdout

    def test_query_kind_async_wildcard(self, sample_file):
        """--kind async_* matches both async functions and async methods."""
        result = run_emend(["search", str(sample_file), "--kind", "async_*"])
        assert result.returncode == 0
        assert "async_standalone" in result.stdout
        assert "async_method" in result.stdout
        assert "standalone_func" not in result.stdout
        assert "regular_method" not in result.stdout


class TestQueryName:
    """Tests for --name / --name-matches filter."""

    def test_query_name_glob(self, sample_file):
        """--name with glob pattern matches names."""
        result = run_emend(["search", str(sample_file), "--name", "test_*"])
        assert result.returncode == 0
        assert "test_create" in result.stdout
        assert "test_update" in result.stdout
        assert "test_delete_async" in result.stdout
        assert "test_helper" in result.stdout
        assert "standalone_func" not in result.stdout

    def test_query_name_regex(self, sample_file):
        """--name with regex pattern (slash-delimited) matches names."""
        result = run_emend(["search", str(sample_file), "--name", "/^test_.*_async$/"])
        assert result.returncode == 0
        assert "test_delete_async" in result.stdout
        assert "test_create" not in result.stdout
        assert "test_update" not in result.stdout

    def test_query_name_matches_alias(self, sample_file):
        """--name-matches is an alias for --name."""
        result = run_emend(["search", str(sample_file), "--name", "helper_*"])
        assert result.returncode == 0
        assert "helper_one" in result.stdout
        assert "helper_two" in result.stdout
        assert "test_helper" not in result.stdout  # doesn't match glob


class TestQueryDecorator:
    """Tests for --has-decorator filter."""

    def test_query_has_decorator(self, sample_file):
        """--has-decorator matches decorated symbols."""
        result = run_emend(["search", str(sample_file), "--has-decorator", "property"])
        assert result.returncode == 0
        assert "name" in result.stdout
        assert "regular_method" not in result.stdout

    def test_query_has_decorator_staticmethod(self, sample_file):
        """--has-decorator staticmethod finds static methods."""
        result = run_emend(["search", str(sample_file), "--has-decorator", "staticmethod"])
        assert result.returncode == 0
        assert "static_method" in result.stdout
        assert "class_method" not in result.stdout

    def test_query_has_decorator_glob(self, sample_file):
        """--has-decorator with glob pattern."""
        result = run_emend(["search", str(sample_file), "--has-decorator", "*method"])
        assert result.returncode == 0
        assert "static_method" in result.stdout
        assert "class_method" in result.stdout

    def test_query_has_decorator_pytest(self, sample_file):
        """--has-decorator matches pytest decorators."""
        result = run_emend(["search", str(sample_file), "--has-decorator", "pytest.*"])
        assert result.returncode == 0
        assert "test_helper" in result.stdout


class TestQueryReturns:
    """Tests for --returns filter."""

    def test_query_returns_str(self, sample_file):
        """--returns str matches functions returning str."""
        result = run_emend(["search", str(sample_file), "--returns", "str"])
        assert result.returncode == 0
        assert "standalone_func" in result.stdout
        assert "name" in result.stdout  # property returning str
        assert "class_method" in result.stdout

    def test_query_returns_optional_wildcard(self, sample_file):
        """--returns with Optional pattern."""
        result = run_emend(["search", str(sample_file), "--returns", "Optional[*]"])
        assert result.returncode == 0
        assert "async_method" in result.stdout

    def test_query_returns_none(self, sample_file):
        """--returns None matches functions with no return value."""
        result = run_emend(["search", str(sample_file), "--returns", "None"])
        assert result.returncode == 0
        assert "async_standalone" in result.stdout


class TestQueryInClass:
    """Tests for --in-class filter."""

    def test_query_in_class(self, sample_file):
        """--in-class restricts to methods of named class."""
        result = run_emend(["search", str(sample_file), "--in-class", "MyClass"])
        assert result.returncode == 0
        assert "regular_method" in result.stdout
        assert "async_method" in result.stdout
        assert "static_method" in result.stdout
        # Should not include TestSuite methods
        assert "test_create" not in result.stdout
        # Should not include standalone functions
        assert "standalone_func" not in result.stdout

    def test_query_in_class_testsuite(self, sample_file):
        """--in-class TestSuite shows only test methods."""
        result = run_emend(["search", str(sample_file), "--in-class", "TestSuite"])
        assert result.returncode == 0
        assert "test_create" in result.stdout
        assert "test_update" in result.stdout
        assert "regular_method" not in result.stdout


class TestQueryDepth:
    """Tests for --depth filter."""

    def test_query_depth_1(self, tmp_path):
        """--depth 1 shows only top-level symbols."""
        code = '''\
def top_level_func():
    def nested_func():
        pass
    return nested_func

class OuterClass:
    def method_one(self):
        def inner_func():
            pass
'''
        filepath = tmp_path / "nested.py"
        filepath.write_text(code)

        result = run_emend(["search", str(filepath), "--depth", "1", "--output", "selector"])
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        # Should have exactly 2 top-level symbols
        assert len(lines) == 2
        assert any("::top_level_func" in line and line.endswith("::top_level_func") for line in lines)
        assert any("::OuterClass" in line and line.endswith("::OuterClass") for line in lines)

    def test_query_depth_2(self, tmp_path):
        """--depth 2 shows symbols at depth 2."""
        code = '''\
def top_level_func():
    def nested_func():
        def deeply_nested():
            pass
    return nested_func

class OuterClass:
    def method_one(self):
        pass
'''
        filepath = tmp_path / "nested.py"
        filepath.write_text(code)

        result = run_emend(["search", str(filepath), "--depth", "2", "--output", "selector"])
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        # Should have nested_func and method_one (both at depth 2)
        assert len(lines) == 2
        assert any(line.endswith("nested_func") for line in lines)
        assert any(line.endswith("method_one") for line in lines)

    def test_query_depth_2_plus(self, tmp_path):
        """--depth 2+ shows symbols at depth 2 or more."""
        code = '''\
def top_level_func():
    def nested_func():
        def deeply_nested():
            pass
    return nested_func
'''
        filepath = tmp_path / "nested.py"
        filepath.write_text(code)

        result = run_emend(["search", str(filepath), "--depth", "2+", "--output", "selector"])
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        # Should have nested_func (depth 2) and deeply_nested (depth 3)
        assert len(lines) == 2
        assert any(line.endswith("nested_func") for line in lines)
        assert any(line.endswith("deeply_nested") for line in lines)


class TestQueryHasParam:
    """Tests for --has-param filter."""

    def test_query_has_param_self(self, sample_file):
        """--has-param self matches methods."""
        result = run_emend(["search", str(sample_file), "--has-param", "self"])
        assert result.returncode == 0
        assert "regular_method" in result.stdout
        assert "async_method" in result.stdout
        assert "_private_method" in result.stdout
        # staticmethod doesn't have self
        assert "static_method" not in result.stdout

    def test_query_has_param_request(self, sample_file):
        """--has-param request matches functions with request param."""
        result = run_emend(["search", str(sample_file), "--has-param", "request"])
        assert result.returncode == 0
        assert "regular_method" in result.stdout
        assert "async_method" not in result.stdout

    def test_query_has_param_with_type(self, sample_file):
        """--has-param with type annotation pattern."""
        result = run_emend(["search", str(sample_file), "--has-param", "request: Request"])
        assert result.returncode == 0
        assert "regular_method" in result.stdout


class TestQueryFilterComposition:
    """Tests for filter composition (AND/OR logic)."""

    def test_query_multiple_kinds_or(self, sample_file):
        """Multiple --kind uses OR logic."""
        result = run_emend([
            "search", str(sample_file),
            "--kind", "function",
            "--kind", "method"
        ])
        assert result.returncode == 0
        # Functions
        assert "standalone_func" in result.stdout
        assert "helper_one" in result.stdout
        # Methods
        assert "regular_method" in result.stdout
        assert "_private_method" in result.stdout
        # Not async
        assert "async_standalone" not in result.stdout
        assert "async_method" not in result.stdout

    def test_query_different_filters_and(self, sample_file):
        """Different filter types use AND logic."""
        result = run_emend([
            "search", str(sample_file),
            "--kind", "method",
            "--in-class", "MyClass"
        ])
        assert result.returncode == 0
        # MyClass methods only
        assert "regular_method" in result.stdout
        assert "_private_method" in result.stdout
        # Not TestSuite methods
        assert "test_create" not in result.stdout

    def test_query_kind_and_decorator(self, sample_file):
        """Combine --kind and --has-decorator."""
        result = run_emend([
            "search", str(sample_file),
            "--kind", "method",
            "--has-decorator", "property"
        ])
        assert result.returncode == 0
        assert "name" in result.stdout
        # Other methods without @property
        assert "regular_method" not in result.stdout


class TestQueryOutput:
    """Tests for output formats."""

    def test_query_json_output(self, sample_file):
        """--json returns structured output."""
        result = run_emend(["search", str(sample_file), "--kind", "class", "--json"])
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) == 2  # MyClass, TestSuite

        # Check structure of first item
        item = data[0]
        assert "path" in item
        assert "name" in item
        assert "kind" in item
        assert item["kind"] == "class"
        assert "line" in item

    def test_query_paths_only(self, sample_file):
        """--paths-only returns just selector paths."""
        result = run_emend(["search", str(sample_file), "--kind", "class", "--output", "selector"])
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 2
        assert "::MyClass" in lines[0] or "::MyClass" in lines[1]
        assert "::TestSuite" in lines[0] or "::TestSuite" in lines[1]

    def test_query_count(self, sample_file):
        """--count returns match count."""
        result = run_emend(["search", str(sample_file), "--kind", "class", "--count"])
        assert result.returncode == 0
        assert result.stdout.strip() == "2"

    def test_query_default_output(self, sample_file):
        """Default output is human-readable."""
        result = run_emend(["search", str(sample_file), "--kind", "class"])
        assert result.returncode == 0
        # Should include class names and line numbers
        assert "MyClass" in result.stdout or "TestSuite" in result.stdout
        lines = result.stdout.strip().split("\n")
        # Each line should contain symbol info (name, kind, line number)
        for line in lines:
            if line.strip():  # Skip empty lines
                assert ":" in line or "class" in line


class TestQueryEdgeCases:
    """Tests for edge cases and error handling."""

    def test_query_no_matches(self, sample_file):
        """No matches returns empty result, exit 0."""
        result = run_emend([
            "search", str(sample_file),
            "--name", "nonexistent_symbol"
        ], check=False)
        assert result.returncode == 0
        # Should have completely empty output (no symbols found)
        output = result.stdout.strip()
        assert output == "", f"Expected empty output but got: {output}"

    def test_query_no_filters(self, sample_file):
        """No filters returns all symbols."""
        result = run_emend(["search", f"{sample_file}::*", "--count"])
        assert result.returncode == 0
        count = int(result.stdout.strip())
        # Should have many symbols
        assert count > 10

    def test_query_case_insensitive(self, sample_file):
        """-i flag enables case-insensitive matching."""
        result = run_emend([
            "search", str(sample_file),
            "--name", "MYCLASS",
            "-i"
        ])
        assert result.returncode == 0
        assert "MyClass" in result.stdout

    def test_query_file_not_found(self, tmp_path):
        """Non-existent file returns error."""
        result = run_emend([
            "search", str(tmp_path / "nonexistent.py"),
            "--kind", "function"
        ], check=False)
        assert result.returncode != 0


class TestQueryJsonStructure:
    """Tests for JSON output structure details."""

    def test_query_json_includes_decorators(self, sample_file):
        """JSON output includes decorators."""
        result = run_emend([
            "search", str(sample_file),
            "--has-decorator", "property",
            "--json"
        ])
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert "decorators" in data[0]
        # Verify decorator is actually property
        decorators = data[0]["decorators"]
        assert isinstance(decorators, (list, str)), f"decorators should be list or str, got {type(decorators)}"
        if isinstance(decorators, list):
            assert any("property" in d for d in decorators), f"Expected property in {decorators}"
        else:
            assert "property" in decorators, f"Expected property in {decorators}"

    def test_query_json_includes_parameters(self, sample_file):
        """JSON output includes parameters for functions."""
        result = run_emend([
            "search", str(sample_file),
            "--name", "standalone_func",
            "--json"
        ])
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert "parameters" in data[0]
        # Verify parameters contain expected parameter (x: int)
        params = data[0]["parameters"]
        assert isinstance(params, (list, str)), f"parameters should be list or str, got {type(params)}"
        if isinstance(params, list):
            assert len(params) > 0, "Should have at least one parameter"
            assert any("x" in str(p) for p in params), f"Expected parameter 'x' in {params}"
        else:
            assert "x" in params, f"Expected parameter 'x' in {params}"

    def test_query_json_includes_returns(self, sample_file):
        """JSON output includes return type."""
        result = run_emend([
            "search", str(sample_file),
            "--name", "standalone_func",
            "--json"
        ])
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert "returns" in data[0]
        assert data[0]["returns"] == "str"

    def test_query_json_includes_parent(self, sample_file):
        """JSON output includes parent for nested symbols."""
        result = run_emend([
            "search", str(sample_file),
            "--in-class", "MyClass",
            "--name", "regular_method",
            "--json"
        ])
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert "parent" in data[0]
        assert data[0]["parent"] == "MyClass"


class TestQuerySmartCase:
    """Tests for --smart-case pattern matching."""

    def test_query_smart_case_snake_to_camel(self, tmp_path):
        """Pattern process_request matches processRequest with --smart-case."""
        code = '''\
def process_request():
    pass

def processRequest():
    pass

def ProcessRequest():
    pass
'''
        filepath = tmp_path / "smartcase.py"
        filepath.write_text(code)

        result = run_emend([
            "search", str(filepath),
            "--name", "process_request",
            "--smart-case",
            "--output", "selector"
        ])
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 3, f"Expected 3 matches but got {len(lines)}: {lines}"

        # Extract symbol names from paths (last part after ::)
        symbol_names = [line.split("::")[-1] if "::" in line else line.strip() for line in lines]

        # Verify all three variants are present
        assert "process_request" in symbol_names, f"Missing process_request in {symbol_names}"
        assert "processRequest" in symbol_names, f"Missing processRequest in {symbol_names}"
        assert "ProcessRequest" in symbol_names, f"Missing ProcessRequest in {symbol_names}"

    def test_query_smart_case_camel_to_snake(self, tmp_path):
        """Pattern processRequest matches process_request with --smart-case."""
        code = '''\
def process_request():
    pass

def processRequest():
    pass

def ProcessRequest():
    pass
'''
        filepath = tmp_path / "smartcase.py"
        filepath.write_text(code)

        result = run_emend([
            "search", str(filepath),
            "--name", "processRequest",
            "--smart-case",
            "--output", "selector"
        ])
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 3, f"Expected 3 matches but got {len(lines)}: {lines}"

        # Extract symbol names from paths (last part after ::)
        symbol_names = [line.split("::")[-1] if "::" in line else line.strip() for line in lines]

        # Verify all three variants are present
        assert "process_request" in symbol_names, f"Missing process_request in {symbol_names}"
        assert "processRequest" in symbol_names, f"Missing processRequest in {symbol_names}"
        assert "ProcessRequest" in symbol_names, f"Missing ProcessRequest in {symbol_names}"

    def test_query_smart_case_pascal(self, tmp_path):
        """Pattern ProcessRequest matches all variants with --smart-case."""
        code = '''\
def process_request():
    pass

def processRequest():
    pass

def ProcessRequest():
    pass
'''
        filepath = tmp_path / "smartcase.py"
        filepath.write_text(code)

        result = run_emend([
            "search", str(filepath),
            "--name", "ProcessRequest",
            "--smart-case",
            "--output", "selector"
        ])
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 3, f"Expected 3 matches but got {len(lines)}: {lines}"

        # Extract symbol names from paths (last part after ::)
        symbol_names = [line.split("::")[-1] if "::" in line else line.strip() for line in lines]

        # Verify all three variants are present
        assert "process_request" in symbol_names, f"Missing process_request in {symbol_names}"
        assert "processRequest" in symbol_names, f"Missing processRequest in {symbol_names}"
        assert "ProcessRequest" in symbol_names, f"Missing ProcessRequest in {symbol_names}"

    def test_query_smart_case_with_decorators(self, tmp_path):
        """--has-decorator respects smart-case."""
        code = '''\
@property
def process_request():
    pass

@property
def processRequest():
    pass
'''
        filepath = tmp_path / "smartcase.py"
        filepath.write_text(code)

        result = run_emend([
            "search", str(filepath),
            "--name", "process_request",
            "--smart-case",
            "--has-decorator", "property",
            "--output", "selector"
        ])
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 2

    def test_query_smart_case_with_params(self, tmp_path):
        """--has-param respects smart-case."""
        code = '''\
def process_request(user_id):
    pass

def processRequest(userId):
    pass

def ProcessRequest(UserId):
    pass

def other_function(data):
    pass
'''
        filepath = tmp_path / "smartcase.py"
        filepath.write_text(code)

        result = run_emend([
            "search", str(filepath),
            "--name", "process_request",
            "--smart-case",
            "--has-param", "user_id",
            "--output", "selector"
        ])
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        # Should match all 3 variants (smart-case applies to both name and param)
        assert len(lines) == 3, f"Expected 3 matches but got {len(lines)}: {lines}"

        # Extract symbol names from paths (last part after ::)
        symbol_names = [line.split("::")[-1] if "::" in line else line.strip() for line in lines]

        # Verify all three function variants are present
        assert "process_request" in symbol_names, f"Missing process_request in {symbol_names}"
        assert "processRequest" in symbol_names, f"Missing processRequest in {symbol_names}"
        assert "ProcessRequest" in symbol_names, f"Missing ProcessRequest in {symbol_names}"

    def test_query_smart_case_multiple_words(self, tmp_path):
        """Three+ word names work correctly with --smart-case."""
        code = '''\
def get_http_response():
    pass

def getHttpResponse():
    pass

def GetHttpResponse():
    pass
'''
        filepath = tmp_path / "smartcase.py"
        filepath.write_text(code)

        result = run_emend([
            "search", str(filepath),
            "--name", "get_http_response",
            "--smart-case",
            "--output", "selector"
        ])
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 3, f"Expected 3 matches but got {len(lines)}: {lines}"

        # Extract symbol names from paths (last part after ::)
        symbol_names = [line.split("::")[-1] if "::" in line else line.strip() for line in lines]

        # Verify all three variants are present
        assert "get_http_response" in symbol_names, f"Missing get_http_response in {symbol_names}"
        assert "getHttpResponse" in symbol_names, f"Missing getHttpResponse in {symbol_names}"
        assert "GetHttpResponse" in symbol_names, f"Missing GetHttpResponse in {symbol_names}"

    def test_query_smart_case_single_word(self, tmp_path):
        """Single words match case-insensitively with --smart-case."""
        code = '''\
def process():
    pass

def Process():
    pass

def PROCESS():
    pass
'''
        filepath = tmp_path / "smartcase.py"
        filepath.write_text(code)

        result = run_emend([
            "search", str(filepath),
            "--name", "process",
            "--smart-case",
            "--output", "selector"
        ])
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 3, f"Expected 3 matches but got {len(lines)}: {lines}"

        # Extract symbol names from paths (last part after ::)
        symbol_names = [line.split("::")[-1] if "::" in line else line.strip() for line in lines]

        # Verify all three case variants are present
        assert "process" in symbol_names, f"Missing process in {symbol_names}"
        assert "Process" in symbol_names, f"Missing Process in {symbol_names}"
        assert "PROCESS" in symbol_names, f"Missing PROCESS in {symbol_names}"
