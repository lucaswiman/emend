"""Tests for the show command."""

import subprocess
import textwrap
from pathlib import Path


def test_show_simple_function(emend_cmd, tmp_path: Path):
    """Show a simple function by symbol path."""
    test_file = tmp_path / "sample.py"
    test_file.write_text("""def greet(name):
    return f"Hello, {name}!"
""")

    result = subprocess.run(
        [emend_cmd, "lookup", f"{test_file}::greet"],
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
        [emend_cmd, "lookup", "--dedent", f"{test_file}::Calculator.add.validate"],
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
        [emend_cmd, "lookup", f"{test_file}:2-4"],
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
        [emend_cmd, "lookup", "--dedent", f"{test_file}:2-4"],
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
        [emend_cmd, "lookup", f"{test_file}::nonexistent"],
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
        [emend_cmd, "lookup", f"{test_file}::MyClass.expensive_property"],
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
