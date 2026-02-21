"""Tests for unified show command with --metadata flag."""

import re
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

import pytest


class LocationInfo(NamedTuple):
    """Parsed output from show --metadata command."""
    selector: str
    line_start: int
    line_end: int
    total_lines: int
    offset_start: int
    offset_end: int
    decorators: list[str]
    parameters: list[str]
    kind: str


def parse_show_metadata_output(output: str) -> LocationInfo:
    """Parse structured output from show --metadata command.

    Expected format:
        {selector}
        --------------------------------------------------
          Lines: X-Y (N lines)
          Offset: start-end
          Decorators: @dec1, @dec2  (optional)
          Parameters: N (param1, param2, ...)  (optional)
          Kind: function|class|method|etc
    """
    lines = output.strip().split('\n')

    if len(lines) < 2:
        raise ValueError(f"Invalid output format: {output}")

    selector = lines[0].strip()

    # Find separator line
    separator_idx = None
    for i, line in enumerate(lines):
        if line.startswith('--'):
            separator_idx = i
            break

    if separator_idx is None:
        raise ValueError(f"No separator line found in output: {output}")

    # Parse info lines
    info_lines = lines[separator_idx + 1:]

    parsed = {
        'selector': selector,
        'decorators': [],
        'parameters': [],
    }

    for line in info_lines:
        line = line.strip()
        if not line:
            continue

        # Parse Lines: X-Y (N lines)
        if line.startswith('Lines:'):
            match = re.search(r'Lines:\s+(\d+)-(\d+)\s+\((\d+)\s+lines\)', line)
            if match:
                parsed['line_start'] = int(match.group(1))
                parsed['line_end'] = int(match.group(2))
                parsed['total_lines'] = int(match.group(3))
                # Validate coherence: end - start + 1 == total_lines
                if parsed['line_end'] - parsed['line_start'] + 1 != parsed['total_lines']:
                    raise ValueError(f"Incoherent line count: {line}")

        # Parse Offset: start-end
        elif line.startswith('Offset:'):
            match = re.search(r'Offset:\s+(\d+)-(\d+)', line)
            if match:
                parsed['offset_start'] = int(match.group(1))
                parsed['offset_end'] = int(match.group(2))
                # Validate coherence: start offset should be < end offset
                if parsed['offset_start'] >= parsed['offset_end']:
                    raise ValueError(f"Invalid offset range: {line}")

        # Parse Decorators: @dec1, @dec2
        elif line.startswith('Decorators:'):
            decs_part = line.replace('Decorators:', '').strip()
            if decs_part:
                parsed['decorators'] = [d.strip() for d in decs_part.split(',')]

        # Parse Parameters: N (param1, param2, ...)
        elif line.startswith('Parameters:'):
            # Extract parameter names from the output
            match = re.search(r'Parameters:\s+(\d+)\s+\((.*?)\)', line)
            if match:
                params_str = match.group(2)
                if params_str:
                    parsed['parameters'] = [p.strip() for p in params_str.split(',')]

        # Parse Kind: function|class|method|etc
        elif line.startswith('Kind:'):
            parsed['kind'] = line.replace('Kind:', '').strip()

    # Validate all required fields are present
    required = ['line_start', 'line_end', 'total_lines', 'offset_start', 'offset_end', 'kind']
    for field in required:
        if field not in parsed:
            raise ValueError(f"Missing required field '{field}' in output: {output}")

    return LocationInfo(**parsed)


@pytest.fixture
def temp_py_file():
    """Create a temporary Python file for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        yield f
    Path(f.name).unlink(missing_ok=True)


class TestShowUnified:
    def test_show_metadata_simple_function(self, emend_cmd, temp_py_file):
        """Test show --metadata for a simple function."""
        temp_py_file.write(
            """\
def my_func(x, y):
    return x + y
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "lookup", "--metadata", f"{temp_py_file.name}::my_func"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Parse and validate structured output
        info = parse_show_metadata_output(result.stdout)

        # Validate selector points to correct symbol
        assert info.selector.endswith("::my_func"), f"Selector mismatch: {info.selector}"

        # Validate line coherence
        assert info.line_start == 1, f"Expected start line 1, got {info.line_start}"
        assert info.line_end == 2, f"Expected end line 2, got {info.line_end}"
        assert info.total_lines == 2, f"Expected total lines 2, got {info.total_lines}"
        assert info.line_end - info.line_start + 1 == info.total_lines, "Line math doesn't add up"

        # Validate offset coherence
        assert info.offset_start < info.offset_end, "Offset start should be < end"
        assert info.offset_start >= 0, "Offset should be non-negative"

        # Validate parameters
        assert info.parameters == ['x', 'y'], f"Expected params ['x', 'y'], got {info.parameters}"

        # Validate kind
        assert info.kind == 'function', f"Expected kind 'function', got {info.kind}"

        # Validate no decorators for this function
        assert info.decorators == [], f"Expected no decorators, got {info.decorators}"

    def test_show_metadata_line_based_selector(self, emend_cmd, temp_py_file):
        """Test show --metadata with line-based selector."""
        temp_py_file.write(
            """\
def foo():
    pass

def bar(a, b):
    return a + b
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "lookup", "--metadata", f"{temp_py_file.name}:4"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Parse and validate structured output
        info = parse_show_metadata_output(result.stdout)

        # Validate selector points to bar function
        assert "bar" in info.selector, f"Expected 'bar' in selector: {info.selector}"

        # Validate line coherence
        assert info.line_start == 4, f"Expected start line 4, got {info.line_start}"
        assert info.line_end == 5, f"Expected end line 5, got {info.line_end}"
        assert info.total_lines == 2, f"Expected total lines 2, got {info.total_lines}"

        # Validate parameters
        assert info.parameters == ['a', 'b'], f"Expected params ['a', 'b'], got {info.parameters}"

        # Validate kind
        assert info.kind == 'function', f"Expected kind 'function', got {info.kind}"

    def test_show_metadata_with_decorators(self, emend_cmd, temp_py_file):
        """Test show --metadata with decorators."""
        temp_py_file.write(
            """\
@decorator
@another(arg=1)
def decorated():
    pass
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "lookup", "--metadata", f"{temp_py_file.name}::decorated"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Parse and validate structured output
        info = parse_show_metadata_output(result.stdout)

        # Validate decorators are shown
        assert info.decorators == ['@decorator', '@another(arg=1)'], f"Expected decorators, got {info.decorators}"
        assert info.kind == 'function'

    def test_show_metadata_nested_function(self, emend_cmd, temp_py_file):
        """Test show --metadata for a nested function."""
        temp_py_file.write(
            """\
class Builder:
    def _build(self):
        def nested(a, b, c):
            return a + b + c
        return nested
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "lookup", "--metadata", f"{temp_py_file.name}::Builder._build.nested"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Parse and validate structured output
        info = parse_show_metadata_output(result.stdout)

        # Validate selector contains nested path
        assert "Builder._build.nested" in info.selector, f"Expected nested path in selector: {info.selector}"

        # Validate line coherence
        assert info.line_start == 3, f"Expected start line 3, got {info.line_start}"
        assert info.line_end == 4, f"Expected end line 4, got {info.line_end}"
        assert info.total_lines == 2, f"Expected total lines 2, got {info.total_lines}"

        # Validate parameters
        assert info.parameters == ['a', 'b', 'c'], f"Expected params ['a', 'b', 'c'], got {info.parameters}"

    def test_show_metadata_class(self, emend_cmd, temp_py_file):
        """Test show --metadata for a class."""
        temp_py_file.write(
            """\
class MyClass:
    def method(self):
        pass
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "lookup", "--metadata", f"{temp_py_file.name}::MyClass"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Parse and validate structured output
        info = parse_show_metadata_output(result.stdout)

        # Validate class kind
        assert info.kind == 'class', f"Expected kind 'class', got {info.kind}"
        assert "MyClass" in info.selector

        # Validate line coherence
        assert info.line_start == 1, f"Expected start line 1, got {info.line_start}"
        assert info.line_end == 3, f"Expected end line 3, got {info.line_end}"
        assert info.total_lines == 3, f"Expected total lines 3, got {info.total_lines}"

    def test_show_metadata_dedent_ignored(self, emend_cmd, temp_py_file):
        """Test show --metadata ignores --dedent flag (both flags together)."""
        temp_py_file.write(
            """\
class Builder:
    def nested_method(self):
        pass
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "lookup", "--metadata", "--dedent", f"{temp_py_file.name}::Builder.nested_method"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Should still output metadata, not source code
        info = parse_show_metadata_output(result.stdout)
        assert info.kind == 'method'
        assert "nested_method" in info.selector

    def test_show_metadata_symbol_not_found(self, emend_cmd, temp_py_file):
        """Test show --metadata error handling for invalid symbol."""
        temp_py_file.write("def foo(): pass\n")
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "lookup", "--metadata", f"{temp_py_file.name}::nonexistent"],
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0, "Expected non-zero exit code for missing symbol"
        assert "not found" in result.stderr.lower(), f"Expected 'not found' in stderr, got: {result.stderr}"

    def test_show_without_metadata_unchanged(self, emend_cmd, temp_py_file):
        """Test show without --metadata flag continues to show source code."""
        temp_py_file.write(
            """\
def greet(name):
    return f"Hello, {name}!"
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "lookup", f"{temp_py_file.name}::greet"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Should output source code, not metadata
        assert "def greet(name):" in result.stdout
        assert 'return f"Hello, {name}!"' in result.stdout
        # Should NOT contain metadata format
        assert "Lines:" not in result.stdout
        assert "Offset:" not in result.stdout
        assert "Kind:" not in result.stdout
