"""Tests for decorator transformation using the edit command.

The transform-decorators command has been removed. Use the edit command instead:
    emend edit file.py::func[decorators] --rm --apply  # remove all
    emend edit file.py::func[decorators] "@new_decorator" --apply  # replace all
    emend find and replace with --in scope for pattern-based changes
"""

import subprocess
import tempfile
from pathlib import Path

import pytest

from conftest import assert_valid_python, get_decorator_names


@pytest.fixture
def temp_py_file():
    """Create a temporary Python file for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        yield f
    Path(f.name).unlink(missing_ok=True)


class TestDecoratorEditing:
    def test_edit_replace_all_decorators_single(self, emend_cmd, temp_py_file):
        """Test replacing all decorators on a function using edit."""
        temp_py_file.write(
            """\
@old_decorator
def my_func():
    return 42
"""
        )
        temp_py_file.flush()

        # Replace the entire decorators component
        result = subprocess.run(
            [
                emend_cmd,
                "edit",
                f"{temp_py_file.name}::my_func[decorators]",
                "@new_decorator",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        content = Path(temp_py_file.name).read_text()
        assert "@new_decorator" in content
        assert "@old_decorator" not in content

    def test_edit_remove_all_decorators(self, emend_cmd, temp_py_file):
        """Test removing all decorators from a function."""
        temp_py_file.write(
            """\
@decorator1
@decorator2
def func():
    pass
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "edit",
                f"{temp_py_file.name}::func[decorators]",
                "--rm",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        content = Path(temp_py_file.name).read_text()
        assert "@decorator1" not in content
        assert "@decorator2" not in content
        assert "def func():" in content

    def test_edit_replace_multiple_decorators(self, emend_cmd, temp_py_file):
        """Test replacing all decorators when multiple are present."""
        temp_py_file.write(
            """\
@first_decorator
@old_decorator
@another_decorator
def complex_func():
    pass
"""
        )
        temp_py_file.flush()

        # Replace all decorators with a single new one
        result = subprocess.run(
            [
                emend_cmd,
                "edit",
                f"{temp_py_file.name}::complex_func[decorators]",
                "@new_decorator",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        content = Path(temp_py_file.name).read_text()
        assert_valid_python(content)

        # All old decorators should be gone
        assert "@first_decorator" not in content
        assert "@old_decorator" not in content
        assert "@another_decorator" not in content
        # New one should be there
        assert "@new_decorator" in content

    def test_edit_add_decorator(self, emend_cmd, temp_py_file):
        """Test adding a decorator to a function."""
        temp_py_file.write(
            """\
@existing_decorator
def func():
    pass
"""
        )
        temp_py_file.flush()

        # Add a decorator at the beginning
        result = subprocess.run(
            [
                emend_cmd,
                "add",
                f"{temp_py_file.name}::func[decorators]",
                "@new_decorator",
                "--at",
                "0",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        content = Path(temp_py_file.name).read_text()
        assert_valid_python(content)

        # Both decorators should be present
        decorators = get_decorator_names(content, "func")
        assert "@new_decorator" in content
        assert "@existing_decorator" in content
        # New one should be first
        assert decorators[0] == "new_decorator"

    def test_edit_replace_all_decorators_on_method(self, emend_cmd, temp_py_file):
        """Test replacing all decorators on a class method."""
        temp_py_file.write(
            """\
class MyClass:
    @old_decorator
    def method(self):
        return "result"
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "edit",
                f"{temp_py_file.name}::MyClass.method[decorators]",
                "@new_decorator",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        content = Path(temp_py_file.name).read_text()
        assert_valid_python(content)
        assert "@new_decorator" in content
        assert "@old_decorator" not in content

    def test_edit_remove_single_decorator_by_name(self, emend_cmd, temp_py_file):
        """Test removing a specific decorator by name using accessor."""
        temp_py_file.write(
            """\
@first
@second
@third
def func():
    pass
"""
        )
        temp_py_file.flush()

        # Remove the "second" decorator specifically
        result = subprocess.run(
            [
                emend_cmd,
                "edit",
                f"{temp_py_file.name}::func[decorators][second]",
                "--rm",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        content = Path(temp_py_file.name).read_text()
        assert_valid_python(content)

        decorators = get_decorator_names(content, "func")
        assert "first" in decorators
        assert "second" not in decorators
        assert "third" in decorators

    def test_edit_dry_run_shows_preview(self, emend_cmd, temp_py_file):
        """Test that without --apply, edit shows a preview."""
        temp_py_file.write(
            """\
@old_decorator
def func():
    pass
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "edit",
                f"{temp_py_file.name}::func[decorators]",
                "@new_decorator",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # File should not be modified
        content = Path(temp_py_file.name).read_text()
        assert "@old_decorator" in content
        assert "@new_decorator" not in content

        # Preview should show the diff
        output = result.stdout
        assert "---" in output or "+++" in output or "new_decorator" in output
