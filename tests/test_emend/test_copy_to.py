"""Tests for emend copy-to command."""

import ast
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from conftest import assert_valid_python, get_import_statements


@pytest.fixture
def temp_py_file():
    """Create a temporary Python file for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        yield f
    Path(f.name).unlink(missing_ok=True)


@pytest.fixture
def temp_dest_file():
    """Create a temporary destination file for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        yield f
    Path(f.name).unlink(missing_ok=True)


class TestCopyTo:
    def test_copy_simple_function(self, emend_cmd_list, temp_py_file, temp_dest_file):
        """Test copying a simple function to another file."""
        temp_py_file.write(
            """\
def my_func(x, y):
    return x + y
"""
        )
        temp_py_file.flush()

        # Close dest file so emend can write to it
        dest_path = temp_dest_file.name
        temp_dest_file.close()

        result = subprocess.run(
            emend_cmd_list + [
                "copy-to",
                "--apply",
                f"{temp_py_file.name}::my_func",
                dest_path,
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Check destination file contains the function
        dest_content = Path(dest_path).read_text()
        assert "def my_func(x, y):" in dest_content
        assert "return x + y" in dest_content

    def test_copy_nested_function(self, emend_cmd_list, temp_py_file, temp_dest_file):
        """Test copying a nested function."""
        temp_py_file.write(
            """\
class Builder:
    def _build(self):
        def nested(a, b):
            return a + b
        return nested
"""
        )
        temp_py_file.flush()

        dest_path = temp_dest_file.name
        temp_dest_file.close()

        result = subprocess.run(
            emend_cmd_list + [
                "copy-to",
                "--apply",
                f"{temp_py_file.name}::Builder._build.nested",
                dest_path,
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        dest_content = Path(dest_path).read_text()
        assert "def nested(a, b):" in dest_content
        assert "return a + b" in dest_content

    def test_copy_with_dedent(self, emend_cmd_list, temp_py_file, temp_dest_file):
        """Test copying with dedent option."""
        temp_py_file.write(
            """\
class MyClass:
    def method(self):
        x = 1
        return x + 2
"""
        )
        temp_py_file.flush()

        dest_path = temp_dest_file.name
        temp_dest_file.close()

        result = subprocess.run(
            emend_cmd_list + [
                "copy-to",
                "--dedent",
                "--apply",
                f"{temp_py_file.name}::MyClass.method",
                dest_path,
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        dest_content = Path(dest_path).read_text()
        # Validate syntax of copied code
        assert_valid_python(dest_content)

        # Parse as AST to verify structure and indentation
        tree = ast.parse(dest_content)
        func_defs = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        assert len(func_defs) > 0, "Expected at least one function definition"

        method_func = next((f for f in func_defs if f.name == "method"), None)
        assert method_func is not None, "Expected 'method' function to be defined"

        # Verify the function has the expected body
        assert len(method_func.body) > 0, "Expected function body to have statements"

        # When dedented, the function definition should start at column 0
        assert dest_content.startswith("def method(self):")

        # Verify correct relative indentation via AST parsing
        # Function body should be properly indented relative to def
        lines = dest_content.split("\n")
        first_line = lines[0]
        assert not first_line.startswith((" ", "\t")), f"Function definition should not be indented: {first_line!r}"

        # Body lines should have consistent indentation greater than the function def
        body_lines = [line for line in lines[1:] if line.strip()]
        if body_lines:
            # Get the indentation of the first body line
            first_body_indent = len(body_lines[0]) - len(body_lines[0].lstrip())
            assert first_body_indent > 0, f"Body should be indented, got: {body_lines[0]!r}"

            # All non-empty body lines should be indented
            for body_line in body_lines:
                body_indent = len(body_line) - len(body_line.lstrip())
                assert body_indent > 0, f"Body line should be indented: {body_line!r}"

    def test_copy_with_append(self, emend_cmd_list, temp_py_file, temp_dest_file):
        """Test copying with append option."""
        temp_py_file.write(
            """\
def func1():
    return 1

def func2():
    return 2
"""
        )
        temp_py_file.flush()

        dest_path = temp_dest_file.name
        temp_dest_file.write("# Existing content\n")
        temp_dest_file.flush()
        temp_dest_file.close()

        result = subprocess.run(
            emend_cmd_list + [
                "copy-to",
                "--append",
                "--apply",
                f"{temp_py_file.name}::func1",
                dest_path,
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        dest_content = Path(dest_path).read_text()
        assert "# Existing content" in dest_content
        assert "def func1():" in dest_content

    def test_copy_without_apply_shows_preview(self, emend_cmd_list, temp_py_file, temp_dest_file):
        """Test that without --apply, the command shows a preview."""
        temp_py_file.write(
            """\
def test_func():
    pass
"""
        )
        temp_py_file.flush()

        dest_path = temp_dest_file.name
        temp_dest_file.close()

        result = subprocess.run(
            emend_cmd_list + [
                "copy-to",
                f"{temp_py_file.name}::test_func",
                dest_path,
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"
        # Should show preview in stdout with function definition
        assert "def test_func():" in result.stdout

    def test_copy_nonexistent_symbol(self, emend_cmd_list, temp_py_file, temp_dest_file):
        """Test error when copying nonexistent symbol."""
        temp_py_file.write("def foo(): pass\n")
        temp_py_file.flush()

        dest_path = temp_dest_file.name
        temp_dest_file.close()

        result = subprocess.run(
            emend_cmd_list + [
                "copy-to",
                "--apply",
                f"{temp_py_file.name}::nonexistent",
                dest_path,
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0

    def test_copy_class_method(self, emend_cmd_list, temp_py_file, temp_dest_file):
        """Test copying a class method."""
        temp_py_file.write(
            """\
class Calculator:
    def add(self, a, b):
        return a + b

    def subtract(self, a, b):
        return a - b
"""
        )
        temp_py_file.flush()

        dest_path = temp_dest_file.name
        temp_dest_file.close()

        result = subprocess.run(
            emend_cmd_list + [
                "copy-to",
                "--apply",
                f"{temp_py_file.name}::Calculator.add",
                dest_path,
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        dest_content = Path(dest_path).read_text()
        assert "def add(self, a, b):" in dest_content
        assert "return a + b" in dest_content

    def test_copy_function_with_module_imports(self, emend_cmd_list, temp_py_file, temp_dest_file):
        """Test that copying a function includes necessary imports from stdlib (emend only)."""
        temp_py_file.write(
            """\
import ast
import json

def outer():
    def parse_code(code):
        tree = ast.parse(code)
        return json.dumps({"nodes": len(tree.body)})
    return parse_code
"""
        )
        temp_py_file.flush()

        dest_path = temp_dest_file.name
        temp_dest_file.close()

        result = subprocess.run(
            emend_cmd_list + [
                "copy-to",
                "--apply",
                f"{temp_py_file.name}::outer.parse_code",
                dest_path,
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        dest_content = Path(dest_path).read_text()
        # Validate syntax of copied code
        assert_valid_python(dest_content)

        # Should include the function definition
        assert "def parse_code(code):" in dest_content

        # Verify imports using AST analysis
        tree = ast.parse(dest_content)
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)

        assert "ast" in imported_modules, f"Expected 'ast' in imports, got {imported_modules}"
        assert "json" in imported_modules, f"Expected 'json' in imports, got {imported_modules}"

    def test_copy_function_with_from_imports(self, emend_cmd_list, temp_py_file, temp_dest_file):
        """Test that copying a function includes necessary from...import statements (emend only)."""
        temp_py_file.write(
            """\
from typing import List, Dict
from pathlib import Path

def outer():
    def process_files(paths: List[str]) -> Dict[str, int]:
        result = {}
        for p in paths:
            path_obj = Path(p)
            result[p] = path_obj.stat().st_size
        return result
    return process_files
"""
        )
        temp_py_file.flush()

        dest_path = temp_dest_file.name
        temp_dest_file.close()

        result = subprocess.run(
            emend_cmd_list + [
                "copy-to",
                "--apply",
                f"{temp_py_file.name}::outer.process_files",
                dest_path,
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        dest_content = Path(dest_path).read_text()
        # Validate syntax of copied code
        assert_valid_python(dest_content)

        # Should include the function definition
        assert "def process_files(paths: List[str]) -> Dict[str, int]:" in dest_content

        # Verify imports using AST analysis
        imports = get_import_statements(dest_content)
        imports_text = " ".join(imports)
        assert "List" in imports_text, f"Expected 'List' in imports, got {imports}"
        assert "Dict" in imports_text, f"Expected 'Dict' in imports, got {imports}"
        assert "Path" in imports_text, f"Expected 'Path' in imports, got {imports}"

    def test_copy_function_with_third_party_imports(self, emend_cmd_list, temp_py_file, temp_dest_file):
        """Test that copying a function includes third-party library imports (emend only)."""
        temp_py_file.write(
            """\
import pytest
import subprocess

def wrapper():
    def run_test_command(cmd: str):
        result = subprocess.run(cmd.split(), capture_output=True)
        return result.returncode
    return run_test_command
"""
        )
        temp_py_file.flush()

        dest_path = temp_dest_file.name
        temp_dest_file.close()

        result = subprocess.run(
            emend_cmd_list + [
                "copy-to",
                "--apply",
                f"{temp_py_file.name}::wrapper.run_test_command",
                dest_path,
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        dest_content = Path(dest_path).read_text()
        # Validate syntax of copied code
        assert_valid_python(dest_content)

        # Should include the function definition
        assert "def run_test_command(cmd: str):" in dest_content

        # Should include necessary import (subprocess is used, pytest is not)
        imports = get_import_statements(dest_content)
        imports_text = " ".join(imports)
        assert "subprocess" in imports_text, f"Expected 'subprocess' in imports, got {imports}"

    def test_copy_nested_function_with_simple_decorator(
        self, emend_cmd_list, temp_py_file, temp_dest_file
    ):
        """Test that decorators on nested functions are copied correctly."""
        temp_py_file.write(
            """\
def outer():
    @decorator_one
    def inner():
        return 42
    return inner
"""
        )
        temp_py_file.flush()

        dest_path = temp_dest_file.name
        temp_dest_file.close()

        result = subprocess.run(
            emend_cmd_list + [
                "copy-to",
                "--apply",
                f"{temp_py_file.name}::outer.inner",
                dest_path,
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        dest_content = Path(dest_path).read_text()
        # Validate syntax of copied code
        assert_valid_python(dest_content)

        # Should include the function definition and decorator via AST
        tree = ast.parse(dest_content)
        func_defs = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        assert len(func_defs) > 0, "Expected at least one function definition"

        inner_func = next((f for f in func_defs if f.name == "inner"), None)
        assert inner_func is not None, "Expected 'inner' function to be defined"
        assert len(inner_func.decorator_list) > 0, "Expected at least one decorator"

        # Verify decorator name via AST
        decorator_names = [
            d.id if isinstance(d, ast.Name) else str(d) for d in inner_func.decorator_list
        ]
        assert "decorator_one" in decorator_names, f"Expected decorator_one in {decorator_names}"

        # Verify function body content
        assert "return 42" in dest_content

    def test_copy_nested_function_with_multiline_decorator(
        self, emend_cmd_list, temp_py_file, temp_dest_file
    ):
        """Test that multi-line decorators on nested functions are copied."""
        temp_py_file.write(
            """\
def wrapper_function():
    @decorator_one(
        param=42,
        another_param="value"
    )
    def inner_function():
        pass
    return inner_function
"""
        )
        temp_py_file.flush()

        dest_path = temp_dest_file.name
        temp_dest_file.close()

        result = subprocess.run(
            emend_cmd_list + [
                "copy-to",
                "--apply",
                f"{temp_py_file.name}::wrapper_function.inner_function",
                dest_path,
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        dest_content = Path(dest_path).read_text()
        # Should include the complete multi-line decorator
        assert "@decorator_one(" in dest_content
        assert "param=42" in dest_content
        assert 'another_param="value"' in dest_content
        assert "def inner_function():" in dest_content

    def test_copy_nested_function_with_multiple_decorators(
        self, emend_cmd_list, temp_py_file, temp_dest_file
    ):
        """Test that multiple decorators on nested functions are copied."""
        temp_py_file.write(
            """\
def wrapper_function():
    @decorator_one(
        param=42
    )
    @decorator_two
    @decorator_three(x=1, y=2)
    def inner_function():
        pass
    return inner_function
"""
        )
        temp_py_file.flush()

        dest_path = temp_dest_file.name
        temp_dest_file.close()

        result = subprocess.run(
            emend_cmd_list + [
                "copy-to",
                "--apply",
                f"{temp_py_file.name}::wrapper_function.inner_function",
                dest_path,
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        dest_content = Path(dest_path).read_text()
        # Validate syntax of copied code
        assert_valid_python(dest_content)

        # Verify all decorators are present via AST
        tree = ast.parse(dest_content)
        func_defs = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        assert len(func_defs) > 0, "Expected at least one function definition"

        inner_func = next((f for f in func_defs if f.name == "inner_function"), None)
        assert inner_func is not None, "Expected 'inner_function' to be defined"
        assert len(inner_func.decorator_list) == 3, (
            f"Expected 3 decorators, got {len(inner_func.decorator_list)}"
        )

        # Verify decorator names via AST
        decorator_names = [
            d.id if isinstance(d, ast.Name) else (
                d.func.id if isinstance(d, ast.Call) and isinstance(d.func, ast.Name)
                else str(d)
            )
            for d in inner_func.decorator_list
        ]
        assert decorator_names[0] == "decorator_one", f"First decorator should be decorator_one, got {decorator_names[0]}"
        assert decorator_names[1] == "decorator_two", f"Second decorator should be decorator_two, got {decorator_names[1]}"
        assert decorator_names[2] == "decorator_three", f"Third decorator should be decorator_three, got {decorator_names[2]}"
