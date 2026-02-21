"""Shared test fixtures and utilities for emend tests."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

# Use emend from the venv
EMEND = str(Path(sys.executable).parent / "emend")


@pytest.fixture
def emend_cmd():
    """Fixture for tests that use emend path (str)."""
    return str(Path(sys.executable).parent / "emend")


@pytest.fixture
def emend_cmd_list():
    """Fixture for tests that use emend command list."""
    return [sys.executable, "-m", "emend.cli"]


@pytest.fixture
def run_emend_cmd():
    """Fixture for tests that use run_emend() helper."""
    cmd_path = str(Path(sys.executable).parent / "emend")

    def _run(args, check=True):
        full_cmd = [cmd_path] + args
        result = subprocess.run(full_cmd, capture_output=True, text=True, check=False)
        if check and result.returncode != 0:
            print(f"STDOUT:\n{result.stdout}")
            print(f"STDERR:\n{result.stderr}")
            raise subprocess.CalledProcessError(
                result.returncode, full_cmd, result.stdout, result.stderr
            )
        return result

    return _run


def assert_valid_python(content: str):
    """Assert that content is syntactically valid Python."""
    try:
        ast.parse(content)
    except SyntaxError as e:
        pytest.fail(f"Generated code is not valid Python:\n{content}\n\nError: {e!r}")


def get_import_statements(code: str) -> list[str]:
    """Extract import statements from Python code.

    Args:
        code: Python source code as a string.

    Returns:
        List of import statements found in the code (as strings).
    """
    imports = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return imports

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = ", ".join(alias.name for alias in node.names)
            imports.append(f"from {module} import {names}")

    return imports


def get_decorator_names(code: str, target_path: str = None) -> list[str]:
    """Extract decorator names from a function/class in order.

    Args:
        code: Python source code as a string.
        target_path: Optional dot-separated path to target function/class (e.g., "ClassName.method_name").
                    If None, returns decorators from the first function/class found.

    Returns:
        List of decorator names in order (without @ symbol).
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    def find_target(nodes, path_parts):
        """Recursively find the target node by path."""
        if not path_parts:
            return None
        target_name = path_parts[0]
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == target_name:
                    if len(path_parts) == 1:
                        return node
                    return find_target(node.body, path_parts[1:])
        return None

    if target_path:
        path_parts = target_path.split(".")
        target = find_target(tree.body, path_parts)
    else:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                target = node
                break
        else:
            target = None

    if not target:
        return []

    decorator_names = []
    for decorator in target.decorator_list:
        if isinstance(decorator, ast.Name):
            decorator_names.append(decorator.id)
        elif isinstance(decorator, ast.Call):
            if isinstance(decorator.func, ast.Name):
                decorator_names.append(decorator.func.id)
            elif isinstance(decorator.func, ast.Attribute):
                decorator_names.append(ast.unparse(decorator.func))
        elif isinstance(decorator, ast.Attribute):
            decorator_names.append(ast.unparse(decorator))

    return decorator_names
