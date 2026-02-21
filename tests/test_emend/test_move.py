"""Tests for the unified move command."""

import ast
import subprocess
import sys
from pathlib import Path


def test_move_dedent(tmp_path, emend_cmd):
    """Test that --dedent flag works for nested symbols."""
    # Create source file with indented nested function
    source_file = tmp_path / "source.py"
    source_file.write_text("""class MyClass:
    def outer_method(self):
        def nested_func():
            return 42
        return nested_func()
""")

    # Create destination file
    dest_file = tmp_path / "dest.py"
    dest_file.write_text("")

    # Run move command with --dedent and --apply
    result = subprocess.run(
        [
            emend_cmd,
            "move",
            f"{source_file}::MyClass.outer_method.nested_func",
            str(dest_file),
            "--dedent",
            "--apply",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Command failed: {result.stderr}"

    # Verify function was dedented (no leading spaces) - works for both implementations
    dest_content = dest_file.read_text()
    assert dest_content.strip().startswith("def nested_func():")
    assert "    return 42" in dest_content  # Still has internal indentation

    # Verify indentation is correct with AST parsing
    tree = ast.parse(dest_content)
    func_def = tree.body[0]
    assert isinstance(func_def, ast.FunctionDef)
    assert func_def.col_offset == 0, f"Function should be at column 0, got {func_def.col_offset}"


def test_move_basic(tmp_path, emend_cmd):
    """Test basic move functionality."""
    # Create source file
    source_file = tmp_path / "source.py"
    source_file.write_text("""def helper_func():
    return "hello"
""")

    # Create destination file
    dest_file = tmp_path / "dest.py"
    dest_file.write_text("")

    # Run move with --apply
    result = subprocess.run(
        [
            emend_cmd,
            "move",
            f"{source_file}::helper_func",
            str(dest_file),
            "--apply",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Command failed: {result.stderr}"

    # Verify function was moved to destination
    dest_content = dest_file.read_text()
    assert "def helper_func():" in dest_content
    assert 'return "hello"' in dest_content

    # Verify function was removed from source using AST
    source_content = source_file.read_text()
    if source_content.strip():  # Only parse if file has content
        source_tree = ast.parse(source_content)
        func_names = [node.name for node in ast.walk(source_tree) if isinstance(node, ast.FunctionDef)]
        assert "helper_func" not in func_names, "helper_func should not exist in source after move"
    else:
        assert source_content.strip() == "", "Source file should be empty after moving all functions"


def test_move_updates_imports(tmp_path, emend_cmd):
    """Test that move updates imports automatically."""
    # Create source file with a function
    source_file = tmp_path / "source.py"
    source_file.write_text("""def my_func():
    return 42
""")

    # Create destination file
    dest_file = tmp_path / "dest.py"
    dest_file.write_text("")

    # Create a file that imports the function
    user_file = tmp_path / "user.py"
    user_file.write_text("""from source import my_func

result = my_func()
""")

    # Run move with --apply
    result = subprocess.run(
        [
            emend_cmd,
            "move",
            f"{source_file}::my_func",
            str(dest_file),
            "--apply",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Command failed: {result.stderr}"

    # Verify imports were updated in user.py using AST parsing
    user_content = user_file.read_text()
    user_tree = ast.parse(user_content)

    # Extract all import sources from ImportFrom statements
    import_sources = []
    for node in ast.walk(user_tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            import_sources.append(node.module)

    assert "dest" in import_sources, "dest should be imported"
    assert "source" not in import_sources, "source should not be imported after move"

    # Verify the old import string is gone
    assert "from source import my_func" not in user_content
    # Verify new import exists
    assert "from dest import my_func" in user_content


def test_move_nested_dedent(tmp_path, emend_cmd):
    """Test move with --dedent flag for nested symbols."""
    # Create source file with nested function
    source_file = tmp_path / "source.py"
    source_file.write_text("""class MyClass:
    def method(self):
        def inner():
            return 10
        return inner()
""")

    # Create destination file
    dest_file = tmp_path / "dest.py"
    dest_file.write_text("")

    # Run move with --dedent and --apply
    result = subprocess.run(
        [
            emend_cmd,
            "move",
            f"{source_file}::MyClass.method.inner",
            str(dest_file),
            "--dedent",
            "--apply",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Command failed: {result.stderr}"

    # Verify function was dedented in destination
    dest_content = dest_file.read_text()
    assert dest_content.strip().startswith("def inner():")
    assert "    return 10" in dest_content  # Internal indentation preserved

    # Verify indentation is correct with AST parsing
    tree = ast.parse(dest_content)
    func_def = tree.body[0]
    assert isinstance(func_def, ast.FunctionDef)
    assert func_def.col_offset == 0, f"Function should be at column 0, got {func_def.col_offset}"


def test_move_no_update_imports(tmp_path, emend_cmd):
    """Test move with --no-update-imports flag."""
    # Create source file
    source_file = tmp_path / "source.py"
    source_file.write_text("""def util_func():
    return True
""")

    # Create destination file
    dest_file = tmp_path / "dest.py"
    dest_file.write_text("")

    # Create a file that imports the function
    user_file = tmp_path / "user.py"
    user_file.write_text("""from source import util_func

x = util_func()
""")

    # Run move with --no-update-imports and --apply
    result = subprocess.run(
        [
            emend_cmd,
            "move",
            f"{source_file}::util_func",
            str(dest_file),
            "--no-update-imports",
            "--apply",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Command failed: {result.stderr}"

    # Verify imports were NOT updated
    user_content = user_file.read_text()
    assert "from source import util_func" in user_content  # Still old import


def test_move_dry_run(tmp_path, emend_cmd):
    """Test move without --apply (dry-run mode)."""
    # Create source file
    source_file = tmp_path / "source.py"
    source_file.write_text("""def test_func():
    pass
""")

    # Create destination file
    dest_file = tmp_path / "dest.py"
    dest_file.write_text("# original\n")

    # Run move without --apply (dry-run)
    result = subprocess.run(
        [
            emend_cmd,
            "move",
            f"{source_file}::test_func",
            str(dest_file),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Command failed: {result.stderr}"

    # Verify stdout shows unified diff format
    assert "---" in result.stdout or "+++" in result.stdout or "@@" in result.stdout, \
        "Dry-run output should show unified diff format"

    # Verify files were NOT modified
    source_content = source_file.read_text()
    assert "def test_func():" in source_content  # Still in source

    dest_content = dest_file.read_text()
    assert dest_content == "# original\n"  # Not modified
