"""Tests for the show command."""

import re
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import NamedTuple

import pytest

from emend.transform import cmd_lookup


def test_show_simple_function(emend_cmd, tmp_path: Path):
    """Show a simple function by symbol path."""
    test_file = tmp_path / "sample.py"
    test_file.write_text("""def greet(name):
    return f"Hello, {name}!"
""")

    result = subprocess.run(
        [emend_cmd, "search", f"{test_file}::greet"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

    expected = """def greet(name):
    return f"Hello, {name}!"
"""
    assert textwrap.dedent(result.stdout) == textwrap.dedent(expected)


def test_show_with_dedent(emend_cmd, tmp_path: Path):
    """Show a nested function with dedent flag."""
    test_file = tmp_path / "sample.py"
    test_file.write_text("""class Calculator:
    def add(self, a, b):
        def validate(x):
            return isinstance(x, (int, float))
        return a + b
""")

    result = subprocess.run(
        [emend_cmd, "search", "--output", "code::dedent", f"{test_file}::Calculator.add.validate"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

    expected = """def validate(x):
    return isinstance(x, (int, float))
"""
    assert textwrap.dedent(result.stdout) == textwrap.dedent(expected)


def test_show_line_range(emend_cmd, tmp_path: Path):
    """Show a line range selector."""
    test_file = tmp_path / "sample.py"
    test_file.write_text("""def foo():
    x = 1
    y = 2
    return x + y

def bar():
    pass
""")

    result = subprocess.run(
        [emend_cmd, "search", f"{test_file}:2-4"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

    expected = "    x = 1\n    y = 2\n    return x + y\n"
    assert result.stdout == expected, f"Expected:\n{expected!r}\n\nGot:\n{result.stdout!r}"


def test_show_line_range_with_dedent(emend_cmd, tmp_path: Path):
    """Show a line range with dedent flag."""
    test_file = tmp_path / "sample.py"
    test_file.write_text("""def foo():
    x = 1
    y = 2
    return x + y

def bar():
    pass
""")

    result = subprocess.run(
        [emend_cmd, "search", "--output", "code::dedent", f"{test_file}:2-4"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

    expected = """x = 1
y = 2
return x + y
"""
    assert textwrap.dedent(result.stdout) == textwrap.dedent(expected)


def test_show_symbol_not_found(emend_cmd, tmp_path: Path):
    """Show returns non-zero exit code when symbol not found."""
    test_file = tmp_path / "sample.py"
    test_file.write_text("""def foo():
    pass
""")

    result = subprocess.run(
        [emend_cmd, "search", f"{test_file}::nonexistent"],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, "Expected non-zero exit code for missing symbol"
    assert "not found" in result.stderr.lower(), f"Expected 'not found' in stderr, got: {result.stderr}"


def test_show_function_with_decorators(emend_cmd, tmp_path: Path):
    """Show a function with decorators includes all decorators."""
    test_file = tmp_path / "sample.py"
    test_file.write_text("""class MyClass:
    @property
    @lru_cache(maxsize=128)
    def expensive_property(self):
        return self._compute()
""")

    result = subprocess.run(
        [emend_cmd, "search", f"{test_file}::MyClass.expensive_property"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

    expected = """@property
@lru_cache(maxsize=128)
def expensive_property(self):
    return self._compute()
"""
    assert textwrap.dedent(result.stdout) == textwrap.dedent(expected)


# ---------------------------------------------------------------------------
# --output metadata
# ---------------------------------------------------------------------------


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
            [emend_cmd, "search", "--output", "metadata", f"{temp_py_file.name}::my_func"],
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
            [emend_cmd, "search", "--output", "metadata", f"{temp_py_file.name}:4"],
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
            [emend_cmd, "search", "--output", "metadata", f"{temp_py_file.name}::decorated"],
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
            [emend_cmd, "search", "--output", "metadata", f"{temp_py_file.name}::Builder._build.nested"],
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
            [emend_cmd, "search", "--output", "metadata", f"{temp_py_file.name}::MyClass"],
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
            [emend_cmd, "search", "--output", "metadata", f"{temp_py_file.name}::Builder.nested_method"],
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
            [emend_cmd, "search", "--output", "metadata", f"{temp_py_file.name}::nonexistent"],
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0, "Expected non-zero exit code for missing symbol"
        assert "not found" in result.stderr.lower(), f"Expected 'not found' in stderr, got: {result.stderr}"


# ---------------------------------------------------------------------------
# Line selectors (file.py:42, file.py:42-100)
# ---------------------------------------------------------------------------


def test_show_location_single_line(run_emend_cmd):
    """Test show --metadata with single line selector (file.py:42)."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write("""
class Builder:
    def __init__(self):
        pass

    def build(self):
        # This is line 7
        result = self.process()
        return result

    def process(self):
        return "done"
""")
        f.flush()
        temp_file = f.name

    try:
        # show --metadata for line 7 should find the build method
        result = run_emend_cmd(["search", "--output", "metadata", f"{temp_file}:7"])

        assert result.returncode == 0, f"Command failed: {result.stderr}"
        output = result.stdout

        # Should show the build method since line 7 is inside it
        assert "build" in output, f"Expected 'build' in output, got: {output}"
        assert "Lines:" in output, f"Expected line info in output, got: {output}"
    finally:
        Path(temp_file).unlink()


def test_show_location_line_range(run_emend_cmd):
    """Test show --metadata with line range selector (file.py:42-100)."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write("""
class Builder:
    def __init__(self):
        pass

    def build(self):
        result = self.process()
        return result

    def process(self):
        # Line 11
        step1 = "start"
        step2 = "middle"
        step3 = "end"
        return step3
""")
        f.flush()
        temp_file = f.name

    try:
        # show --metadata for lines 11-14 should find the process method
        result = run_emend_cmd(["search", "--output", "metadata", f"{temp_file}:11-14"])

        assert result.returncode == 0, f"Command failed: {result.stderr}"
        output = result.stdout

        # Should show the process method since lines 11-14 are inside it
        assert "process" in output, f"Expected 'process' in output, got: {output}"
        assert "Lines:" in output, f"Expected line info in output, got: {output}"
    finally:
        Path(temp_file).unlink()


def test_line_selector_outside_any_function_error(run_emend_cmd):
    """Test line selector on module-level code returns error when no symbol found."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write("""
# Line 2: module-level code
x = 42

def func():
    return x
""")
        f.flush()
        temp_file = f.name

    try:
        # show --metadata for line 3 (module-level assignment) should fail
        result = run_emend_cmd(["search", "--output", "metadata", f"{temp_file}:3"], check=False)

        # Should exit with error code for module-level code
        assert result.returncode != 0, \
            f"Expected non-zero exit code, got {result.returncode}"
        combined = (result.stdout + result.stderr).lower()
        assert "no symbol found" in combined, \
            f"Expected 'no symbol found' message, got stdout: {result.stdout}, stderr: {result.stderr}"
    finally:
        Path(temp_file).unlink()


# ---------------------------------------------------------------------------
# Lookup dispatch (emend.transform.dispatch)
# ---------------------------------------------------------------------------


class TestDispatchAnnotations:
    """Runtime-evaluation of module annotations (guards against missing imports)."""

    def test_single_fn_annotation_resolves(self):
        """`_dispatch_with_returns_filter`'s `single_fn` param is annotated
        `Callable[[ExtendedSelector], str]`. With `from __future__ import
        annotations` the annotation is stored as a string and only fails when
        evaluated. Evaluating it against the module namespace raises NameError
        if `Callable` was never imported into the module.
        """
        from emend.transform import dispatch

        ann = dispatch._dispatch_with_returns_filter.__annotations__["single_fn"]
        # Stored as a string due to `from __future__ import annotations`.
        assert isinstance(ann, str)
        # Evaluating against the module globals must not raise NameError.
        eval(ann, vars(dispatch))  # noqa: S307


class TestCmdLookupLineBasedMetadata:
    """Tests for cmd_lookup with line-based selectors and metadata=True."""

    def test_line_with_no_symbol_raises_value_error(self, tmp_path):
        """When a line-based selector points to an empty line (no symbol),
        cmd_lookup should raise ValueError, not SystemExit."""
        source = tmp_path / "example.py"
        source.write_text(
            "def foo():\n"
            "    pass\n"
            "\n"  # line 3: empty line, no symbol
            "def bar():\n"
            "    pass\n"
        )
        # Line 3 is an empty line with no symbol.
        selector_str = f"{source}:3"
        with pytest.raises(ValueError, match="No symbol found at line 3"):
            cmd_lookup(str(source), selector_str=selector_str, metadata=True)
