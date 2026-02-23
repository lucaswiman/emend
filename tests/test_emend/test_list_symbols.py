"""Tests for emend list-symbols command with --tree-depth option."""

import re
import subprocess
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_py_file():
    """Create a temporary Python file for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        yield f
    Path(f.name).unlink(missing_ok=True)


def parse_symbol_output(line: str) -> dict:
    """Parse a single symbol output line into structured components.

    Expected formats:
    - Symbols with line range: "[indent]kind signature  [line_range]"
    - References (no line range): "[indent]ref name"

    Returns dict with keys: kind, signature, line_start, line_end, indent_level.
    For references, line_start and line_end are None.
    Returns None for non-symbol lines (like Module header) or lines that don't match format.
    """
    # Try matching symbols with line ranges (class, def, async def, var)
    match = re.match(r'^(\s*)(class|async def|def|var)\s+(.+?)\s{2}\[L(\d+)(?:-L(\d+))?\]$', line.rstrip())
    if match:
        indent, kind, signature, line_start, line_end = match.groups()
        return {
            'kind': kind,
            'signature': signature,
            'line_start': int(line_start),
            'line_end': int(line_end) if line_end else int(line_start),
            'indent_level': len(indent) // 2,
        }

    # Try matching references (no line range)
    match = re.match(r'^(\s*)ref\s+(.+)$', line.rstrip())
    if match:
        indent, signature = match.groups()
        return {
            'kind': 'ref',
            'signature': signature,
            'line_start': None,
            'line_end': None,
            'indent_level': len(indent) // 2,
        }

    return None


def validate_line_range(parsed: dict, expected_start: int, expected_end: int = None) -> bool:
    """Validate that parsed line range matches expected values.

    For references, line_start and line_end are None, so this validation will fail.
    Only use this for symbols with line ranges (cls, fn, var).
    """
    if expected_end is None:
        expected_end = expected_start
    if parsed['line_start'] is None or parsed['line_end'] is None:
        return False
    return parsed['line_start'] == expected_start and parsed['line_end'] == expected_end


class TestListSymbolsTreeDepth:
    def test_basic_depth_1(self, emend_cmd, temp_py_file):
        """Test tree-depth=1 shows only top-level symbols with new format."""
        temp_py_file.write(
            """\
from typing import List

def foo(x: int) -> List[str]:
    y: str = "hello"
    z: List[str] = []
    def bar(a: float) -> None:
        return z + str(a)
    return [y] * x
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "search", temp_py_file.name, "--output", "summary", "--depth", "1"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Parse and validate the output
        lines = result.stdout.strip().split('\n')
        parsed_lines = [parse_symbol_output(line) for line in lines if line.strip()]
        # Filter out None values (non-symbol lines)
        parsed_lines = [p for p in parsed_lines if p is not None]

        # Should have exactly one top-level function
        assert len(parsed_lines) == 1, f"Expected 1 top-level symbol, got {len(parsed_lines)}"

        # Validate the foo function
        foo_line = parsed_lines[0]
        assert foo_line is not None, "Failed to parse foo symbol"
        assert foo_line['kind'] == 'def', "foo should be a function"
        assert 'foo(x: int) -> List[str]' in foo_line['signature'], "foo signature incorrect"
        assert validate_line_range(foo_line, 3, 8), f"foo line range incorrect: {foo_line}"
        # Top-level symbols are indented at level 1 (2 spaces) relative to module
        assert foo_line['indent_level'] == 1, "foo should have one indent level (2 spaces)"

        # Should not show nested function bar
        assert not any('bar' in p['signature'] for p in parsed_lines if p), "Nested bar should not appear with depth=1"

    def test_nested_functions_depth_3(self, emend_cmd, temp_py_file):
        """Test tree-depth=3 shows nested functions with compact prefixes and line numbers."""
        temp_py_file.write(
            """\
from typing import List

def foo(x: int) -> List[str]:
    y: str = "hello"
    z: List[str] = []
    def bar(a: float) -> None:
        return z + str(a)
    return [y] * x
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "search", temp_py_file.name, "--output", "summary", "--depth", "3"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Parse and validate output
        lines = result.stdout.strip().split('\n')
        parsed_lines = [parse_symbol_output(line) for line in lines if line.strip()]
        # Filter out None values (non-symbol lines like module header)
        parsed_lines = [p for p in parsed_lines if p is not None]

        # Find and validate top-level function
        foo_line = next((p for p in parsed_lines if p and 'foo(x: int) -> List[str]' in p['signature']), None)
        assert foo_line is not None, "foo function not found"
        assert foo_line['kind'] == 'def', "foo should be a function"
        assert validate_line_range(foo_line, 3, 8), f"foo line range incorrect: {foo_line}"
        assert foo_line['indent_level'] == 1, "foo should have one indent level (top-level symbols)"

        # Find and validate variables inside foo
        y_line = next((p for p in parsed_lines if p and p['signature'] == 'y: str'), None)
        assert y_line is not None, "y variable not found"
        assert y_line['kind'] == 'var', "y should be a variable"
        assert validate_line_range(y_line, 4, 4), f"y line range incorrect: {y_line}"
        assert y_line['indent_level'] == 2, "y should be indented two levels (inside foo)"

        z_line = next((p for p in parsed_lines if p and p['signature'] == 'z: List[str]'), None)
        assert z_line is not None, "z variable not found"
        assert z_line['kind'] == 'var', "z should be a variable"
        assert validate_line_range(z_line, 5, 5), f"z line range incorrect: {z_line}"
        assert z_line['indent_level'] == 2, "z should be indented two levels (inside foo)"

        # Find and validate nested function
        bar_line = next((p for p in parsed_lines if p and 'bar(a: float) -> None' in p['signature']), None)
        assert bar_line is not None, "bar function not found"
        assert bar_line['kind'] == 'def', "bar should be a function"
        assert validate_line_range(bar_line, 6, 7), f"bar line range incorrect: {bar_line}"
        assert bar_line['indent_level'] == 2, "bar should be indented two levels (inside foo)"

        # Find and validate reference to outer-scope variable
        z_ref_line = next((p for p in parsed_lines if p and p['kind'] == 'ref' and 'z' in p['signature']), None)
        assert z_ref_line is not None, "reference to z not found"
        assert z_ref_line['kind'] == 'ref', "z reference should be marked as ref"

    def test_type_annotations(self, emend_cmd, temp_py_file):
        """Test type annotations are extracted correctly with new format."""
        temp_py_file.write(
            """\
def process(data: dict[str, int], flag: bool = True) -> list[str]:
    result: list[str] = []
    return result
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "search", temp_py_file.name, "--output", "summary", "--depth", "2"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Parse and validate output
        lines = result.stdout.strip().split('\n')
        parsed_lines = [parse_symbol_output(line) for line in lines if line.strip()]
        # Filter out None values (non-symbol lines like module header)
        parsed_lines = [p for p in parsed_lines if p is not None]

        # Find and validate process function
        process_line = next((p for p in parsed_lines if p and 'process(data: dict[str, int]' in p['signature']), None)
        assert process_line is not None, "process function not found"
        assert process_line['kind'] == 'def', "process should be a function"
        assert 'flag: bool = True' in process_line['signature'], "process signature should include default parameter"
        assert '-> list[str]' in process_line['signature'], "process signature should include return type"
        assert validate_line_range(process_line, 1, 3), f"process line range incorrect: {process_line}"

        # Find and validate result variable
        result_line = next((p for p in parsed_lines if p and p['signature'] == 'result: list[str]'), None)
        assert result_line is not None, "result variable not found"
        assert result_line['kind'] == 'var', "result should be a variable"
        assert validate_line_range(result_line, 2, 2), f"result line range incorrect: {result_line}"

    def test_references_to_outer_scope(self, emend_cmd, temp_py_file):
        """Test detection of outer-scope variable references with new format."""
        temp_py_file.write(
            """\
def outer():
    config = {"key": "value"}
    data = []

    def inner():
        print(config)
        data.append(1)

    return inner
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "search", temp_py_file.name, "--output", "summary", "--depth", "3"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Parse and validate output
        lines = result.stdout.strip().split('\n')
        parsed_lines = [parse_symbol_output(line) for line in lines if line.strip()]
        # Filter out None values (non-symbol lines like module header)
        parsed_lines = [p for p in parsed_lines if p is not None]

        # Find and validate outer function
        outer_line = next((p for p in parsed_lines if p and p['signature'] == 'outer()'), None)
        assert outer_line is not None, "outer function not found"
        assert outer_line['kind'] == 'def', "outer should be a function"
        assert validate_line_range(outer_line, 1, 9), f"outer line range incorrect: {outer_line}"

        # Find and validate config variable
        config_line = next((p for p in parsed_lines if p and p['signature'] == 'config'), None)
        assert config_line is not None, "config variable not found"
        assert config_line['kind'] == 'var', "config should be a variable"
        assert validate_line_range(config_line, 2, 2), f"config line range incorrect: {config_line}"

        # Find and validate data variable
        data_line = next((p for p in parsed_lines if p and p['signature'] == 'data'), None)
        assert data_line is not None, "data variable not found"
        assert data_line['kind'] == 'var', "data should be a variable"
        assert validate_line_range(data_line, 3, 3), f"data line range incorrect: {data_line}"

        # Find and validate inner function
        inner_line = next((p for p in parsed_lines if p and p['signature'] == 'inner()'), None)
        assert inner_line is not None, "inner function not found"
        assert inner_line['kind'] == 'def', "inner should be a function"
        assert validate_line_range(inner_line, 5, 7), f"inner line range incorrect: {inner_line}"

        # Find and validate references
        config_ref = next((p for p in parsed_lines if p and p['kind'] == 'ref' and 'config' in p['signature']), None)
        assert config_ref is not None, "reference to config not found"
        assert config_ref['kind'] == 'ref', "config reference should be marked as ref"

        data_ref = next((p for p in parsed_lines if p and p['kind'] == 'ref' and 'data' in p['signature']), None)
        assert data_ref is not None, "reference to data not found"
        assert data_ref['kind'] == 'ref', "data reference should be marked as ref"

    def test_class_with_methods(self, emend_cmd, temp_py_file):
        """Test classes and methods with compact prefixes and line numbers."""
        temp_py_file.write(
            """\
class Calculator:
    def add(self, x: int, y: int) -> int:
        result: int = x + y
        return result

    def multiply(self, x: int, y: int) -> int:
        return x * y
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "search", temp_py_file.name, "--output", "summary", "--depth", "3"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Parse and validate output
        lines = result.stdout.strip().split('\n')
        parsed_lines = [parse_symbol_output(line) for line in lines if line.strip()]
        # Filter out None values (non-symbol lines like module header)
        parsed_lines = [p for p in parsed_lines if p is not None]

        # Find and validate Calculator class
        calc_line = next((p for p in parsed_lines if p and p['signature'] == 'Calculator'), None)
        assert calc_line is not None, "Calculator class not found"
        assert calc_line['kind'] == 'class', "Calculator should be a class"
        assert validate_line_range(calc_line, 1, 7), f"Calculator line range incorrect: {calc_line}"
        assert calc_line['indent_level'] == 1, "Calculator should have one indent level (top-level)"

        # Find and validate add method
        add_line = next((p for p in parsed_lines if p and 'add(self, x: int, y: int) -> int' in p['signature']), None)
        assert add_line is not None, "add method not found"
        assert add_line['kind'] == 'def', "add should be a function"
        assert validate_line_range(add_line, 2, 4), f"add line range incorrect: {add_line}"
        assert add_line['indent_level'] == 2, "add should be indented two levels (inside class)"

        # Find and validate result variable
        result_line = next((p for p in parsed_lines if p and p['signature'] == 'result: int'), None)
        assert result_line is not None, "result variable not found"
        assert result_line['kind'] == 'var', "result should be a variable"
        assert validate_line_range(result_line, 3, 3), f"result line range incorrect: {result_line}"
        assert result_line['indent_level'] == 3, "result should be indented three levels (inside method)"

        # Find and validate multiply method
        multiply_line = next((p for p in parsed_lines if p and 'multiply(self, x: int, y: int) -> int' in p['signature']), None)
        assert multiply_line is not None, "multiply method not found"
        assert multiply_line['kind'] == 'def', "multiply should be a function"
        assert validate_line_range(multiply_line, 6, 7), f"multiply line range incorrect: {multiply_line}"
        assert multiply_line['indent_level'] == 2, "multiply should be indented two levels (inside class)"

    def test_no_type_annotations(self, emend_cmd, temp_py_file):
        """Test functions without type annotations with new format."""
        temp_py_file.write(
            """\
def simple(x):
    y = "test"
    def nested():
        return y
    return nested
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "search", temp_py_file.name, "--output", "summary", "--depth", "3"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Parse and validate output
        lines = result.stdout.strip().split('\n')
        parsed_lines = [parse_symbol_output(line) for line in lines if line.strip()]
        # Filter out None values (non-symbol lines like module header)
        parsed_lines = [p for p in parsed_lines if p is not None]

        # Find and validate simple function
        simple_line = next((p for p in parsed_lines if p and p['signature'] == 'simple(x)'), None)
        assert simple_line is not None, "simple function not found"
        assert simple_line['kind'] == 'def', "simple should be a function"
        assert validate_line_range(simple_line, 1, 5), f"simple line range incorrect: {simple_line}"

        # Find and validate y variable
        y_line = next((p for p in parsed_lines if p and p['signature'] == 'y'), None)
        assert y_line is not None, "y variable not found"
        assert y_line['kind'] == 'var', "y should be a variable"
        assert validate_line_range(y_line, 2, 2), f"y line range incorrect: {y_line}"

        # Find and validate nested function
        nested_line = next((p for p in parsed_lines if p and p['signature'] == 'nested()'), None)
        assert nested_line is not None, "nested function not found"
        assert nested_line['kind'] == 'def', "nested should be a function"
        assert validate_line_range(nested_line, 3, 4), f"nested line range incorrect: {nested_line}"

        # Find and validate reference to y
        y_ref_line = next((p for p in parsed_lines if p and p['kind'] == 'ref' and 'y' in p['signature']), None)
        assert y_ref_line is not None, "reference to y not found"
        assert y_ref_line['kind'] == 'ref', "y reference should be marked as ref"

    def test_default_tree_depth(self, emend_cmd, temp_py_file):
        """Test that default tree-depth shows full nesting with line ranges."""
        temp_py_file.write(
            """\
def foo():
    def bar():
        pass
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "search", temp_py_file.name, "--output", "summary"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Parse and validate output
        lines = result.stdout.strip().split('\n')
        parsed_lines = [parse_symbol_output(line) for line in lines if line.strip()]
        # Filter out None values (non-symbol lines like module header)
        parsed_lines = [p for p in parsed_lines if p is not None]

        # Find and validate foo function
        foo_line = next((p for p in parsed_lines if p and p['signature'] == 'foo()'), None)
        assert foo_line is not None, "foo function not found"
        assert foo_line['kind'] == 'def', "foo should be a function"
        assert validate_line_range(foo_line, 1, 3), f"foo line range incorrect: {foo_line}"
        assert foo_line['indent_level'] == 1, "foo should have one indent level (top-level)"

        # Find and validate bar function (should appear with default depth)
        bar_line = next((p for p in parsed_lines if p and p['signature'] == 'bar()'), None)
        assert bar_line is not None, "bar function not found (default depth should show nested)"
        assert bar_line['kind'] == 'def', "bar should be a function"
        assert validate_line_range(bar_line, 2, 3), f"bar line range incorrect: {bar_line}"
        assert bar_line['indent_level'] == 2, "bar should be indented two levels (nested)"

    def test_flat_mode(self, emend_cmd, temp_py_file):
        """Test --flat mode shows full paths without indentation."""
        temp_py_file.write(
            """\
from typing import List

def foo(x: int) -> List[str]:
    y: str = "hello"
    z: List[str] = []
    def bar(a: float) -> None:
        return z + str(a)
    return [y] * x

class Calculator:
    def add(self, x: int, y: int) -> int:
        result: int = x + y
        return result
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "search", temp_py_file.name, "--output", "summary::flat", "--depth", "3"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Parse all output lines and validate structure
        lines = result.stdout.strip().split('\n')
        parsed_lines = [parse_symbol_output(line) for line in lines if line.strip()]
        # Filter out None values (non-symbol lines like module header)
        parsed_lines = [p for p in parsed_lines if p is not None]

        # Find specific symbols and validate them
        foo_line = next((p for p in parsed_lines if p and 'foo(x: int)' in p['signature']), None)
        assert foo_line is not None, "foo function not found"
        assert foo_line['kind'] == 'def', "foo should be a function"
        assert validate_line_range(foo_line, 3, 8), f"foo line range incorrect: {foo_line}"
        assert foo_line['indent_level'] == 0, "foo should have no indentation in flat mode"

        foo_bar_line = next((p for p in parsed_lines if p and 'foo.bar(a: float)' in p['signature']), None)
        assert foo_bar_line is not None, "foo.bar function not found"
        assert foo_bar_line['kind'] == 'def', "foo.bar should be a function"
        assert validate_line_range(foo_bar_line, 6, 7), f"foo.bar line range incorrect: {foo_bar_line}"
        assert foo_bar_line['indent_level'] == 0, "foo.bar should have no indentation in flat mode"

        calc_line = next((p for p in parsed_lines if p and p['signature'] == 'Calculator'), None)
        assert calc_line is not None, "Calculator class not found"
        assert calc_line['kind'] == 'class', "Calculator should be a class"
        assert validate_line_range(calc_line, 10, 13), f"Calculator line range incorrect: {calc_line}"
        assert calc_line['indent_level'] == 0, "Calculator should have no indentation in flat mode"

        calc_add_line = next((p for p in parsed_lines if p and 'Calculator.add(self, x: int, y: int)' in p['signature']), None)
        assert calc_add_line is not None, "Calculator.add method not found"
        assert calc_add_line['kind'] == 'def', "Calculator.add should be a function"
        assert validate_line_range(calc_add_line, 11, 13), f"Calculator.add line range incorrect: {calc_add_line}"
        assert calc_add_line['indent_level'] == 0, "Calculator.add should have no indentation in flat mode"

        # Variables should not appear in flat mode (only functions and classes)
        var_lines = [p for p in parsed_lines if p and p['kind'] == 'var']
        assert len(var_lines) == 0, f"Variables should not appear in flat mode, but found: {var_lines}"

    def test_selector_filters_to_class(self, emend_cmd, temp_py_file):
        """Test --selector filters to a specific class."""
        temp_py_file.write(
            """\
def foo():
    pass

class Calculator:
    def add(self, x: int, y: int) -> int:
        result: int = x + y
        return result

    def multiply(self, x: int, y: int) -> int:
        return x * y

class Processor:
    def process(self):
        pass
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "search", f"{temp_py_file.name}::Calculator", "--output", "summary", "--depth", "3"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Parse and validate output
        lines = result.stdout.strip().split('\n')
        parsed_lines = [parse_symbol_output(line) for line in lines if line.strip()]
        # Filter out None values (non-symbol lines like module header)
        parsed_lines = [p for p in parsed_lines if p is not None]

        # Find and validate Calculator class
        calc_line = next((p for p in parsed_lines if p and p['signature'] == 'Calculator'), None)
        assert calc_line is not None, "Calculator class not found"
        assert calc_line['kind'] == 'class', "Calculator should be a class"
        assert validate_line_range(calc_line, 4, 10), f"Calculator line range incorrect: {calc_line}"

        # Find and validate add method
        add_line = next((p for p in parsed_lines if p and 'add(self, x: int, y: int) -> int' in p['signature']), None)
        assert add_line is not None, "add method not found"
        assert add_line['kind'] == 'def', "add should be a function"
        assert validate_line_range(add_line, 5, 7), f"add line range incorrect: {add_line}"

        # Find and validate multiply method
        multiply_line = next((p for p in parsed_lines if p and 'multiply(self, x: int, y: int) -> int' in p['signature']), None)
        assert multiply_line is not None, "multiply method not found"
        assert multiply_line['kind'] == 'def', "multiply should be a function"
        assert validate_line_range(multiply_line, 9, 10), f"multiply line range incorrect: {multiply_line}"

        # Should not show foo or Processor
        assert not any('foo' in p['signature'] for p in parsed_lines if p), "foo should not appear with Calculator selector"
        assert not any(p and p['signature'] == 'Processor' for p in parsed_lines), "Processor should not appear with Calculator selector"

    def test_selector_filters_to_nested_function(self, emend_cmd, temp_py_file):
        """Test --selector filters to a nested function."""
        temp_py_file.write(
            """\
def outer():
    config = {"key": "value"}
    data = []

    def inner():
        print(config)
        data.append(1)

    return inner

def other():
    pass
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "search", f"{temp_py_file.name}::outer.inner", "--output", "summary", "--depth", "3"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Parse and validate output
        lines = result.stdout.strip().split('\n')
        parsed_lines = [parse_symbol_output(line) for line in lines if line.strip()]
        # Filter out None values (non-symbol lines like module header)
        parsed_lines = [p for p in parsed_lines if p is not None]

        # Find and validate outer function
        outer_line = next((p for p in parsed_lines if p and p['signature'] == 'outer()'), None)
        assert outer_line is not None, "outer function not found"
        assert outer_line['kind'] == 'def', "outer should be a function"
        assert validate_line_range(outer_line, 1, 9), f"outer line range incorrect: {outer_line}"

        # Find and validate inner function
        inner_line = next((p for p in parsed_lines if p and p['signature'] == 'inner()'), None)
        assert inner_line is not None, "inner function not found"
        assert inner_line['kind'] == 'def', "inner should be a function"
        assert validate_line_range(inner_line, 5, 7), f"inner line range incorrect: {inner_line}"

        # Should not show other function
        assert not any('other' in p['signature'] for p in parsed_lines if p), "other function should not appear with outer.inner selector"

    def test_selector_with_flat_mode(self, emend_cmd, temp_py_file):
        """Test --selector combined with --flat mode."""
        temp_py_file.write(
            """\
class Calculator:
    def add(self, x: int, y: int) -> int:
        result: int = x + y
        return result

    def multiply(self, x: int, y: int) -> int:
        return x * y

class Processor:
    def process(self):
        pass
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "search", f"{temp_py_file.name}::Calculator", "--output", "summary::flat", "--depth", "3"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Parse and validate output
        lines = result.stdout.strip().split('\n')
        parsed_lines = [parse_symbol_output(line) for line in lines if line.strip()]
        # Filter out None values (non-symbol lines like module header)
        parsed_lines = [p for p in parsed_lines if p is not None]

        # Find and validate Calculator class
        calc_line = next((p for p in parsed_lines if p and p['signature'] == 'Calculator'), None)
        assert calc_line is not None, "Calculator class not found"
        assert calc_line['kind'] == 'class', "Calculator should be a class"
        assert validate_line_range(calc_line, 1, 7), f"Calculator line range incorrect: {calc_line}"
        assert calc_line['indent_level'] == 0, "Calculator should have no indentation in flat mode"

        # Find and validate add method with full path
        add_line = next((p for p in parsed_lines if p and 'Calculator.add(self, x: int, y: int) -> int' in p['signature']), None)
        assert add_line is not None, "Calculator.add method not found"
        assert add_line['kind'] == 'def', "Calculator.add should be a function"
        assert validate_line_range(add_line, 2, 4), f"Calculator.add line range incorrect: {add_line}"
        assert add_line['indent_level'] == 0, "Calculator.add should have no indentation in flat mode"

        # Find and validate multiply method with full path
        multiply_line = next((p for p in parsed_lines if p and 'Calculator.multiply(self, x: int, y: int) -> int' in p['signature']), None)
        assert multiply_line is not None, "Calculator.multiply method not found"
        assert multiply_line['kind'] == 'def', "Calculator.multiply should be a function"
        assert validate_line_range(multiply_line, 6, 7), f"Calculator.multiply line range incorrect: {multiply_line}"
        assert multiply_line['indent_level'] == 0, "Calculator.multiply should have no indentation in flat mode"

        # Should not show Processor
        assert not any(p and p['signature'] == 'Processor' for p in parsed_lines), "Processor should not appear with Calculator selector"
