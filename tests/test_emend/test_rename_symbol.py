"""Tests for rename-symbol command."""
import pytest


def test_rename_function(tmp_path, run_emend_cmd):
    """Test renaming a function."""
    test_file = tmp_path / "test.py"
    test_file.write_text("""\
def old_func():
    return 42

result = old_func()
""")

    # Dry-run
    result = run_emend_cmd([
        "rename-symbol", str(test_file), "old_func", "new_func",
        "--project", str(tmp_path)
    ])
    assert result.returncode == 0
    assert "-def old_func():" in result.stdout
    assert "+def new_func():" in result.stdout

    # Apply
    result = run_emend_cmd([
        "rename-symbol", str(test_file), "old_func", "new_func",
        "--project", str(tmp_path), "--apply"
    ])
    assert result.returncode == 0

    content = test_file.read_text()
    assert "def new_func():" in content
    assert "result = new_func()" in content
    assert "old_func" not in content


def test_rename_class(tmp_path, run_emend_cmd):
    """Test renaming a class across project."""
    main_file = tmp_path / "main.py"
    main_file.write_text("""\
class OldClass:
    pass

obj = OldClass()
""")

    user_file = tmp_path / "user.py"
    user_file.write_text("""\
from main import OldClass

obj = OldClass()
""")

    # Apply
    result = run_emend_cmd([
        "rename-symbol", str(main_file), "OldClass", "NewClass",
        "--project", str(tmp_path), "--apply"
    ])
    assert result.returncode == 0

    # Check main file
    content = main_file.read_text()
    assert "class NewClass:" in content
    assert "obj = NewClass()" in content
    assert "OldClass" not in content

    # Check user file
    content = user_file.read_text()
    assert "from main import NewClass" in content
    assert "obj = NewClass()" in content
    assert "OldClass" not in content


def test_rename_class_dry_run(tmp_path, run_emend_cmd):
    """Test dry-run for renaming a class doesn't modify files."""
    main_file = tmp_path / "main.py"
    original_content = """\
class OldClass:
    pass

obj = OldClass()
"""
    main_file.write_text(original_content)

    user_file = tmp_path / "user.py"
    user_original = """\
from main import OldClass

obj = OldClass()
"""
    user_file.write_text(user_original)

    # Dry-run
    result = run_emend_cmd([
        "rename-symbol", str(main_file), "OldClass", "NewClass",
        "--project", str(tmp_path)
    ])
    assert result.returncode == 0
    assert "-class OldClass:" in result.stdout
    assert "+class NewClass:" in result.stdout

    # Files should be unchanged
    assert main_file.read_text() == original_content
    assert user_file.read_text() == user_original


def test_rename_updates_imports(tmp_path, run_emend_cmd):
    """Test that rename updates import statements."""
    module = tmp_path / "module.py"
    module.write_text("""\
def helper():
    return 123
""")

    user = tmp_path / "user.py"
    user.write_text("""\
from module import helper

result = helper()
""")

    # Apply rename
    result = run_emend_cmd([
        "rename-symbol", str(module), "helper", "utility",
        "--project", str(tmp_path), "--apply"
    ])
    assert result.returncode == 0

    # Check module
    assert "def utility():" in module.read_text()

    # Check user file import was updated
    user_content = user.read_text()
    assert "from module import utility" in user_content
    assert "result = utility()" in user_content


def test_rename_updates_imports_dry_run(tmp_path, run_emend_cmd):
    """Test dry-run for renaming with imports doesn't modify files."""
    module = tmp_path / "module.py"
    module_original = """\
def helper():
    return 123
"""
    module.write_text(module_original)

    user = tmp_path / "user.py"
    user_original = """\
from module import helper

result = helper()
"""
    user.write_text(user_original)

    # Dry-run
    result = run_emend_cmd([
        "rename-symbol", str(module), "helper", "utility",
        "--project", str(tmp_path)
    ])
    assert result.returncode == 0
    assert "-def helper():" in result.stdout
    assert "+def utility():" in result.stdout
    assert "-from module import helper" in result.stdout
    assert "+from module import utility" in result.stdout

    # Files should be unchanged
    assert module.read_text() == module_original
    assert user.read_text() == user_original


def test_rename_dry_run(tmp_path, run_emend_cmd):
    """Test that dry-run doesn't modify files."""
    test_file = tmp_path / "test.py"
    original_content = """\
def old_name():
    pass
"""
    test_file.write_text(original_content)

    # Dry-run (no --apply)
    result = run_emend_cmd([
        "rename-symbol", str(test_file), "old_name", "new_name",
        "--project", str(tmp_path)
    ])
    assert result.returncode == 0
    assert "-def old_name():" in result.stdout
    assert "+def new_name():" in result.stdout

    # File should be unchanged
    assert test_file.read_text() == original_content
