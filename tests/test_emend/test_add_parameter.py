"""Tests for emend add-parameter command."""

import ast
import subprocess
import tempfile
from pathlib import Path

import pytest

from conftest import assert_valid_python


def assert_function_signature(content: str, func_name: str, expected_signature: str):
    """Assert that a function has the expected signature.

    Args:
        content: The source code
        func_name: Name of the function to find
        expected_signature: Expected signature substring (e.g., "def foo(x, y):")
    """
    assert_valid_python(content)
    tree = ast.parse(content)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                lines = content.split('\n')
                # Find the function definition line(s)
                for i in range(node.lineno - 1, min(node.end_lineno or node.lineno, len(lines))):
                    line_content = lines[i]
                    if expected_signature in line_content:
                        return
                # If we get here, the signature wasn't found in the exact form
                func_lines = '\n'.join(lines[node.lineno - 1:node.end_lineno or node.lineno])
                pytest.fail(
                    f"Function '{func_name}' signature not found.\n"
                    f"Expected substring: {expected_signature}\n"
                    f"Actual function:\n{func_lines}"
                )
                return

    pytest.fail(f"Function '{func_name}' not found in content")


def get_function_parameters(content: str, func_name: str) -> ast.arguments:
    """Extract the parameters of a function using AST.

    Args:
        content: The source code
        func_name: Name of the function
    """
    tree = ast.parse(content)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                return node.args

    pytest.fail(f"Function '{func_name}' not found")


@pytest.fixture
def emend_cmd():
    """Return the path to emend executable."""
    return ".venv/bin/emend"


@pytest.fixture
def temp_py_file():
    """Create a temporary Python file."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        yield f
    Path(f.name).unlink()



class TestAddCommandPseudoClass:
    """Test the 'add' command with pseudo-class syntax support."""

    def test_add_pseudo_class_keyword_only_with_existing_star(self, emend_cmd, temp_py_file):
        """Test adding keyword-only parameter with existing * separator using pseudo-class."""
        temp_py_file.write(
            """\
def foo(a, *, b):
    pass
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "add",
                f"{temp_py_file.name}::foo[params]:KEYWORD_ONLY",
                "c",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}\nstdout: {result.stdout}"

        content = Path(temp_py_file.name).read_text()
        # Should add c after * (keyword-only section)
        assert "def foo(a, *, b, c):" in content

    def test_add_pseudo_class_keyword_only_without_star(self, emend_cmd, temp_py_file):
        """Test adding keyword-only parameter without existing * separator using pseudo-class."""
        temp_py_file.write(
            """\
def foo(a, b):
    pass
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "add",
                f"{temp_py_file.name}::foo[params]:KEYWORD_ONLY",
                "c",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}\nstdout: {result.stdout}"

        content = Path(temp_py_file.name).read_text()
        # Should add * separator and c after it
        assert "def foo(a, b, *, c):" in content

    def test_add_pseudo_class_positional_or_keyword(self, emend_cmd, temp_py_file):
        """Test adding positional-or-keyword parameter using pseudo-class."""
        temp_py_file.write(
            """\
def foo(a, *, b):
    pass
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "add",
                f"{temp_py_file.name}::foo[params]:POSITIONAL_OR_KEYWORD",
                "c",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}\nstdout: {result.stdout}"

        content = Path(temp_py_file.name).read_text()
        # Should add c before * (positional section)
        assert "def foo(a, c, *, b):" in content

    def test_add_pseudo_class_positional_only(self, emend_cmd, temp_py_file):
        """Test adding positional-only parameter using pseudo-class."""
        temp_py_file.write(
            """\
def foo(a, b):
    pass
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "add",
                f"{temp_py_file.name}::foo[params]:POSITIONAL_ONLY",
                "c",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}\nstdout: {result.stdout}"

        content = Path(temp_py_file.name).read_text()
        # Should add c to regular params section (at the end by default)
        assert "def foo(a, b, c):" in content

    def test_add_pseudo_class_keyword_only_no_args(self, emend_cmd, temp_py_file):
        """Test adding keyword-only parameter to function with no arguments using pseudo-class."""
        temp_py_file.write(
            """\
def foo():
    pass
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "add",
                f"{temp_py_file.name}::foo[params]:KEYWORD_ONLY",
                "bar",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}\nstdout: {result.stdout}"

        content = Path(temp_py_file.name).read_text()
        # Should add * separator and bar after it
        assert "def foo(*, bar):" in content

    def test_add_pseudo_class_keyword_only_before_kwargs(self, emend_cmd, temp_py_file):
        """Test adding keyword-only parameter before **kwargs using pseudo-class."""
        temp_py_file.write(
            """\
def foo(a, **kw):
    pass
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "add",
                f"{temp_py_file.name}::foo[params]:KEYWORD_ONLY",
                "b",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}\nstdout: {result.stdout}"

        content = Path(temp_py_file.name).read_text()
        # Should add * separator and b after it, before **kw
        assert "def foo(a, *, b, **kw):" in content

    def test_add_pseudo_class_invalid_syntax(self, emend_cmd, temp_py_file):
        """Test that invalid pseudo-class raises error."""
        temp_py_file.write(
            """\
def foo(a):
    pass
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "add",
                f"{temp_py_file.name}::foo[params]:INVALID",
                "b",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        # Should fail with non-zero exit code
        assert result.returncode != 0
        # Grammar rejection produces UnexpectedCharacters error
        assert "Error" in result.stderr
