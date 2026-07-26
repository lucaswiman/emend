"""Tests for emend query / search command filters.

Simple filter-combination tests use @pytest.mark.parametrize (test_query_filter).
Complex-assertion tests (JSON structure, smart-case, depth, edge cases) are kept
as individual functions.
"""

import json
import pytest
from typer.testing import CliRunner

from emend.cli import app

runner = CliRunner()

SAMPLE_SOURCE = '''\
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


@pytest.fixture
def sample_file(tmp_path):
    """Create a sample Python file with various symbol types."""
    filepath = tmp_path / "sample.py"
    filepath.write_text(SAMPLE_SOURCE)
    return filepath


# ---------------------------------------------------------------------------
# Parametrized filter tests — all share sample_file, simple stdout assertions
# ---------------------------------------------------------------------------

FILTER_CASES = [
    # kind filters
    pytest.param(
        ["--kind", "method"],
        ["regular_method", "_private_method"],
        ["standalone_func", "async_method"],
        id="kind_method",
    ),
    pytest.param(
        ["--kind", "async_function"],
        ["async_standalone"],
        ["async_method", "standalone_func"],
        id="kind_async_function",
    ),
    pytest.param(
        ["--kind", "async_method"],
        ["async_method"],
        ["async_standalone"],
        id="kind_async_method",
    ),
    pytest.param(
        ["--kind", "class"],
        ["MyClass", "TestSuite"],
        ["standalone_func"],
        id="kind_class",
    ),
    pytest.param(
        ["--kind", "async_*"],
        ["async_standalone", "async_method"],
        ["standalone_func", "regular_method"],
        id="kind_async_wildcard",
    ),
    # name filters
    pytest.param(
        ["--name", "test_*"],
        ["test_create", "test_update", "test_delete_async", "test_helper"],
        ["standalone_func"],
        id="name_test_glob",
    ),
    pytest.param(
        ["--name", "/^test_.*_async$/"],
        ["test_delete_async"],
        ["test_create", "test_update"],
        id="name_regex",
    ),
    pytest.param(
        ["--name", "helper_*"],
        ["helper_one", "helper_two"],
        ["test_helper"],
        id="name_helper_glob",
    ),
    # decorator / --where filters
    pytest.param(
        ["--where", "@property"],
        ["name"],
        ["regular_method"],
        id="decorator_property",
    ),
    pytest.param(
        ["--where", "@staticmethod"],
        ["static_method"],
        ["class_method"],
        id="decorator_staticmethod",
    ),
    pytest.param(
        ["--where", "@*method"],
        ["static_method", "class_method"],
        [],
        id="decorator_glob",
    ),
    pytest.param(
        ["--where", "@pytest.*"],
        ["test_helper"],
        [],
        id="decorator_pytest",
    ),
    # returns filters
    pytest.param(
        ["--returns", "str"],
        ["standalone_func", "class_method"],
        [],
        id="returns_str",
    ),
    pytest.param(
        ["--returns", "Optional[*]"],
        ["async_method"],
        [],
        id="returns_optional_wildcard",
    ),
    pytest.param(
        ["--returns", "None"],
        ["async_standalone"],
        [],
        id="returns_none",
    ),
    # --where class filters
    pytest.param(
        ["--where", "class MyClass"],
        ["regular_method", "async_method", "static_method"],
        ["test_create", "standalone_func"],
        id="in_class_myclass",
    ),
    pytest.param(
        ["--where", "class TestSuite"],
        ["test_create", "test_update"],
        ["regular_method"],
        id="in_class_testsuite",
    ),
    # --has-param filters
    pytest.param(
        ["--has-param", "self"],
        ["regular_method", "async_method", "_private_method"],
        ["static_method"],
        id="has_param_self",
    ),
    pytest.param(
        ["--has-param", "request"],
        ["regular_method"],
        ["async_method"],
        id="has_param_request",
    ),
    pytest.param(
        ["--has-param", "request: Request"],
        ["regular_method"],
        [],
        id="has_param_with_type",
    ),
    # filter composition
    pytest.param(
        ["--kind", "function", "--kind", "method"],
        ["standalone_func", "helper_one", "regular_method", "_private_method"],
        ["async_standalone", "async_method"],
        id="multiple_kinds_or",
    ),
    pytest.param(
        ["--kind", "method", "--where", "class MyClass"],
        ["regular_method", "_private_method"],
        ["test_create"],
        id="different_filters_and",
    ),
    pytest.param(
        ["--kind", "method", "--where", "@property"],
        ["name"],
        ["regular_method"],
        id="kind_and_decorator",
    ),
    # output format
    pytest.param(
        ["--kind", "class", "--output", "selector"],
        ["::MyClass", "::TestSuite"],
        [],
        id="paths_only",
    ),
    pytest.param(
        ["--kind", "class", "--output", "count"],
        ["2"],
        [],
        id="count_output",
    ),
    pytest.param(
        ["--kind", "class"],
        ["MyClass", "TestSuite"],
        [],
        id="default_output",
    ),
]


@pytest.mark.parametrize("args,expected,not_expected", FILTER_CASES)
def test_query_filter(sample_file, args, expected, not_expected):
    result = runner.invoke(app, ["search", str(sample_file)] + args)
    assert result.exit_code == 0, result.stdout
    for sym in expected:
        assert sym in result.stdout
    for sym in not_expected:
        assert sym not in result.stdout


# ---------------------------------------------------------------------------
# Complex JSON assertions kept as individual tests
# ---------------------------------------------------------------------------

def test_query_kind_function(sample_file):
    """--kind function returns only top-level functions with correct JSON structure."""
    result = runner.invoke(app, ["search", str(sample_file), "--kind", "function", "--output", "json"])
    assert result.exit_code == 0, result.stdout

    data = json.loads(result.stdout)
    assert isinstance(data, list), "JSON output should be a list"

    names = {item["name"] for item in data}
    kinds = {item["kind"] for item in data}

    expected_functions = {"standalone_func", "test_helper", "helper_one", "helper_two"}
    assert expected_functions.issubset(names), (
        f"Expected all of {expected_functions} in results, but got {names}"
    )
    assert kinds == {"function"}, f"Expected only 'function' kind, but got {kinds}"
    assert "regular_method" not in names, "Should not include methods"
    assert "async_standalone" not in names, "Should not include async functions"

    for item in data:
        assert "path" in item
        assert "name" in item
        assert "kind" in item
        assert "line" in item
        assert "end_line" in item
        assert isinstance(item["line"], int)
        assert isinstance(item["end_line"], int)
        assert item["line"] > 0
        assert item["end_line"] >= item["line"]


def test_query_json_output_structure(sample_file):
    """--output json returns structured list with required fields."""
    result = runner.invoke(app, ["search", str(sample_file), "--kind", "class", "--output", "json"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 2  # MyClass, TestSuite

    item = data[0]
    assert "path" in item
    assert "name" in item
    assert "kind" in item
    assert item["kind"] == "class"
    assert "line" in item


# ---------------------------------------------------------------------------
# JSON structure tests
# ---------------------------------------------------------------------------

def test_query_json_includes_decorators(sample_file):
    """JSON output includes decorators."""
    result = runner.invoke(app, ["search", str(sample_file), "--where", "@property", "--output", "json"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert len(data) == 1
    assert "decorators" in data[0]
    decorators = data[0]["decorators"]
    assert isinstance(decorators, (list, str))
    if isinstance(decorators, list):
        assert any("property" in d for d in decorators)
    else:
        assert "property" in decorators


def test_query_json_includes_parameters(sample_file):
    """JSON output includes parameters for functions."""
    result = runner.invoke(app, ["search", str(sample_file), "--name", "standalone_func", "--output", "json"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert len(data) == 1
    assert "parameters" in data[0]
    params = data[0]["parameters"]
    assert isinstance(params, (list, str))
    if isinstance(params, list):
        assert len(params) > 0
        assert any("x" in str(p) for p in params)
    else:
        assert "x" in params


def test_query_json_includes_returns(sample_file):
    """JSON output includes return type."""
    result = runner.invoke(app, ["search", str(sample_file), "--name", "standalone_func", "--output", "json"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert len(data) == 1
    assert "returns" in data[0]
    assert data[0]["returns"] == "str"


def test_query_json_includes_parent(sample_file):
    """JSON output includes parent for nested symbols."""
    result = runner.invoke(app, ["search", str(sample_file), "--where", "class MyClass", "--name", "regular_method", "--output", "json"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert len(data) == 1
    assert "parent" in data[0]
    assert data[0]["parent"] == "MyClass"


# ---------------------------------------------------------------------------
# Depth tests (use their own files — not sample_file)
# ---------------------------------------------------------------------------

def test_query_depth_1(tmp_path):
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

    result = runner.invoke(app, ["search", str(filepath), "--depth", "1", "--output", "selector"])
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.strip().split("\n")
    assert len(lines) == 2
    assert any("::top_level_func" in line and line.endswith("::top_level_func") for line in lines)
    assert any("::OuterClass" in line and line.endswith("::OuterClass") for line in lines)


def test_query_depth_2(tmp_path):
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

    result = runner.invoke(app, ["search", str(filepath), "--depth", "2", "--output", "selector"])
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.strip().split("\n")
    assert len(lines) == 2
    assert any(line.endswith("nested_func") for line in lines)
    assert any(line.endswith("method_one") for line in lines)


def test_query_depth_2_plus(tmp_path):
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

    result = runner.invoke(app, ["search", str(filepath), "--depth", "2+", "--output", "selector"])
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.strip().split("\n")
    assert len(lines) == 2
    assert any(line.endswith("nested_func") for line in lines)
    assert any(line.endswith("deeply_nested") for line in lines)


# ---------------------------------------------------------------------------
# Edge cases / error handling
# ---------------------------------------------------------------------------

def test_query_no_matches(sample_file):
    """No matches returns empty result, exit 0."""
    result = runner.invoke(app, ["search", str(sample_file), "--name", "nonexistent_symbol"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "", f"Expected empty output but got: {result.stdout}"


def test_query_depth_invalid_spec(sample_file):
    """Invalid --depth value should give a clear, user-friendly error message.

    Passing a non-integer like '--depth foo' previously surfaced a raw Python
    internal error: "invalid literal for int() with base 10: 'foo'".
    The error message should mention 'depth' so the user knows what to fix.
    """
    result = runner.invoke(app, ["search", str(sample_file), "--depth", "foo"])
    assert result.exit_code != 0, "Expected non-zero exit for invalid depth"
    # The error message should mention 'depth' so the user knows what went wrong
    assert "depth" in result.output.lower(), (
        f"Expected 'depth' in error output, got: {result.output!r}"
    )


def test_query_no_filters(sample_file):
    """No filters returns all symbols."""
    result = runner.invoke(app, ["search", f"{sample_file}::*", "--output", "count"])
    assert result.exit_code == 0, result.stdout
    count = int(result.stdout.strip())
    assert count > 10


def test_query_case_insensitive(sample_file):
    """-i flag enables case-insensitive matching."""
    result = runner.invoke(app, ["search", str(sample_file), "--name", "MYCLASS", "-i"])
    assert result.exit_code == 0, result.stdout
    assert "MyClass" in result.stdout


def test_query_file_not_found(tmp_path):
    """Non-existent file returns error."""
    result = runner.invoke(app, ["search", str(tmp_path / "nonexistent.py"), "--kind", "function"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Smart-case tests
# ---------------------------------------------------------------------------

SMARTCASE_SOURCE = '''\
def process_request():
    pass

def processRequest():
    pass

def ProcessRequest():
    pass
'''


@pytest.fixture
def smartcase_file(tmp_path):
    filepath = tmp_path / "smartcase.py"
    filepath.write_text(SMARTCASE_SOURCE)
    return filepath


@pytest.mark.parametrize("name_arg", ["process_request", "processRequest", "ProcessRequest"])
def test_query_smart_case_variants(smartcase_file, name_arg):
    """Any case variant of the name matches all 3 variants with --smart-case."""
    result = runner.invoke(app, [
        "search", str(smartcase_file),
        "--name", name_arg,
        "--smart-case",
        "--output", "selector",
    ])
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.strip().split("\n")
    assert len(lines) == 3, f"Expected 3 matches for {name_arg!r} but got {len(lines)}: {lines}"
    symbol_names = [line.split("::")[-1] if "::" in line else line.strip() for line in lines]
    assert "process_request" in symbol_names
    assert "processRequest" in symbol_names
    assert "ProcessRequest" in symbol_names


def test_query_smart_case_with_decorators(tmp_path):
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

    result = runner.invoke(app, [
        "search", str(filepath),
        "--name", "process_request",
        "--smart-case",
        "--where", "@property",
        "--output", "selector",
    ])
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.strip().split("\n")
    assert len(lines) == 2


def test_query_smart_case_with_params(tmp_path):
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

    result = runner.invoke(app, [
        "search", str(filepath),
        "--name", "process_request",
        "--smart-case",
        "--has-param", "user_id",
        "--output", "selector",
    ])
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.strip().split("\n")
    assert len(lines) == 3, f"Expected 3 matches but got {len(lines)}: {lines}"
    symbol_names = [line.split("::")[-1] if "::" in line else line.strip() for line in lines]
    assert "process_request" in symbol_names
    assert "processRequest" in symbol_names
    assert "ProcessRequest" in symbol_names


def test_query_smart_case_multiple_words(tmp_path):
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

    result = runner.invoke(app, [
        "search", str(filepath),
        "--name", "get_http_response",
        "--smart-case",
        "--output", "selector",
    ])
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.strip().split("\n")
    assert len(lines) == 3, f"Expected 3 matches but got {len(lines)}: {lines}"
    symbol_names = [line.split("::")[-1] if "::" in line else line.strip() for line in lines]
    assert "get_http_response" in symbol_names
    assert "getHttpResponse" in symbol_names
    assert "GetHttpResponse" in symbol_names


def test_query_smart_case_single_word(tmp_path):
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

    result = runner.invoke(app, [
        "search", str(filepath),
        "--name", "process",
        "--smart-case",
        "--output", "selector",
    ])
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.strip().split("\n")
    assert len(lines) == 3, f"Expected 3 matches but got {len(lines)}: {lines}"
    symbol_names = [line.split("::")[-1] if "::" in line else line.strip() for line in lines]
    assert "process" in symbol_names
    assert "Process" in symbol_names
    assert "PROCESS" in symbol_names


def test_query_smart_case_does_not_substring_match(tmp_path):
    """Smart-case should do full-match, not substring match."""
    filepath = tmp_path / "sample.py"
    filepath.write_text(
        "def process_request(): pass\n"
        "def handle_process_request_async(): pass\n"
    )
    result = runner.invoke(app, [
        "search", str(filepath),
        "--name", "process_request",
        "--smart-case",
        "--output", "selector",
    ])
    assert result.exit_code == 0, result.stdout
    lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
    symbol_names = [line.split("::")[-1] if "::" in line else line.strip() for line in lines]
    assert "process_request" in symbol_names
    assert "handle_process_request_async" not in symbol_names


# ---------------------------------------------------------------------------
# Bug: _extract_params_from_signature splits on commas inside brackets/parens
# ---------------------------------------------------------------------------

def test_extract_params_bracketed_annotation():
    """A parameter whose annotation contains a comma inside ``[]`` must stay
    intact (e.g. ``b: Dict[str, int]``), not be split on the inner comma."""
    from emend.query import _extract_params_from_signature

    params = _extract_params_from_signature("(a: int, b: Dict[str, int]) -> None")
    assert params == ["a: int", "b: Dict[str, int]"]


def test_extract_params_paren_default():
    from emend.query import _extract_params_from_signature

    params = _extract_params_from_signature("(x=(1, 2), y=3)")
    assert params == ["x=(1, 2)", "y=3"]


def test_extract_params_nested_callable():
    from emend.query import _extract_params_from_signature

    params = _extract_params_from_signature("(cb: Callable[[int], str])")
    assert params == ["cb: Callable[[int], str]"]


def test_query_has_param_with_bracketed_type(tmp_path):
    """--has-param filtering must match a parameter whose type contains a comma."""
    filepath = tmp_path / "sample.py"
    filepath.write_text(
        "from typing import Dict\n"
        "def f(a: int, b: Dict[str, int]) -> None: pass\n"
    )
    result = runner.invoke(app, [
        "search", str(filepath),
        "--name", "f",
        "--output", "json",
    ])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    syms = data if isinstance(data, list) else data.get("results", data.get("symbols", []))
    f_syms = [s for s in syms if s.get("name") == "f"]
    assert f_syms, data
    assert f_syms[0]["parameters"] == ["a: int", "b: Dict[str, int]"]


# ---------------------------------------------------------------------------
# Bug: _collect_symbols cache key ignores file extension/language
# ---------------------------------------------------------------------------

def test_collect_symbols_cache_key_includes_language(tmp_path):
    """Two files with identical byte content but different languages must not
    collide in the symbol cache — the ``.ts`` file must be parsed as
    TypeScript, not returned from the cached Python result."""
    from emend.query import _collect_symbols

    # Valid Python, but NOT valid TypeScript (no ``def`` keyword in TS).
    content = "def foo():\n    pass\n"
    py = tmp_path / "a.py"
    ts = tmp_path / "b.ts"
    py.write_text(content)
    ts.write_text(content)

    py_syms = _collect_symbols(py, content)
    ts_syms = _collect_symbols(ts, content)

    assert [s.name for s in py_syms] == ["foo"]
    # Parsed as TypeScript, ``def foo(): pass`` yields no function symbol.
    assert [s.name for s in ts_syms] == []


# ---------------------------------------------------------------------------
# Bug: _extract_params_from_signature ignored string literals
# ---------------------------------------------------------------------------

def test_extract_params_comma_inside_string_default():
    """A comma inside a string default must not split the parameter."""
    from emend.query import _extract_params_from_signature

    params = _extract_params_from_signature('(sep: str = ", ", end: str = "!") -> str')
    assert params == ['sep: str = ", "', 'end: str = "!"']


def test_extract_params_arrow_inside_string_default():
    """A ``->`` inside a string default must not truncate the signature."""
    from emend.query import _extract_params_from_signature

    params = _extract_params_from_signature('(label: str = "a -> b", n: int = 1) -> int')
    assert params == ['label: str = "a -> b"', "n: int = 1"]


def test_extract_params_bracket_inside_string_default():
    """An unbalanced bracket inside a string must not corrupt depth tracking."""
    from emend.query import _extract_params_from_signature

    params = _extract_params_from_signature('(fmt: str = "[{}", n: int = 1)')
    assert params == ['fmt: str = "[{}"', "n: int = 1"]


def test_extract_params_escaped_quote_in_default():
    from emend.query import _extract_params_from_signature

    params = _extract_params_from_signature('(q: str = "a\\"b", n: int = 1)')
    assert params == ['q: str = "a\\"b"', "n: int = 1"]


def test_has_param_finds_param_after_string_default(tmp_path, capsys):
    """End-to-end: --has-param must see parameters that follow a string default."""
    from emend.query import cmd_query

    f = tmp_path / "s.py"
    f.write_text(
        'def joiner(sep: str = ", ", end: str = "!") -> str:\n'
        "    return sep\n"
        "\n"
        'def arrow(label: str = "a -> b", n: int = 1) -> int:\n'
        "    return n\n"
    )

    cmd_query(str(f), params=["n"])
    out = capsys.readouterr().out
    assert "arrow" in out
    assert "joiner" not in out
