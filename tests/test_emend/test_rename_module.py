"""Tests for rename command (module mode)."""
import pytest


def test_rename_module_renames_file(tmp_path, run_emend_cmd):
    """Test that rename-module renames the file."""
    old_file = tmp_path / "old_name.py"
    old_file.write_text("""\
def helper():
    return 42
""")

    # Apply rename
    result = run_emend_cmd([
        "rename", str(old_file), "--to", "new_name",
        "--project", str(tmp_path), "--apply"
    ])
    assert result.returncode == 0

    # Old file should be gone
    assert not old_file.exists()

    # New file should exist
    new_file = tmp_path / "new_name.py"
    assert new_file.exists()
    assert "def helper():" in new_file.read_text()


def test_rename_module_updates_imports(tmp_path, run_emend_cmd):
    """Test that rename-module updates import statements."""
    module = tmp_path / "old_module.py"
    module.write_text("""\
def func():
    pass
""")

    user = tmp_path / "user.py"
    user.write_text("""\
from old_module import func

func()
""")

    # Apply rename
    result = run_emend_cmd([
        "rename", str(module), "--to", "new_module",
        "--project", str(tmp_path), "--apply"
    ])
    assert result.returncode == 0

    # Check new module exists
    new_module = tmp_path / "new_module.py"
    assert new_module.exists()
    assert not module.exists()

    # Check user file import was updated
    user_content = user.read_text()
    assert "from new_module import func" in user_content
    assert "old_module" not in user_content


def test_rename_module_dry_run(tmp_path, run_emend_cmd):
    """Test that dry-run doesn't modify files."""
    test_file = tmp_path / "old.py"
    test_file.write_text("""\
def test():
    pass
""")

    # Dry-run (no --apply)
    result = run_emend_cmd([
        "rename", str(test_file), "--to", "new",
        "--project", str(tmp_path)
    ])
    assert result.returncode == 0
    assert "CHANGES PREVIEW" in result.stdout

    # Old file should still exist
    assert test_file.exists()

    # New file should not exist
    new_file = tmp_path / "new.py"
    assert not new_file.exists()


class TestReplaceModuleInStrings:
    """``_replace_module_in_strings`` rewrites module references inside string
    literals.  Its offset math must be byte-based: when a line contains
    multi-byte UTF-8 characters before the target literal, char-vs-byte offset
    confusion previously left the original string intact and spliced the
    replacement in at the wrong location.
    """

    def test_multibyte_before_target_literal(self):
        from emend.transform.rename_move import _replace_module_in_strings

        content = 'x = "\U0001F389\U0001F389\U0001F389"; y = "models"\n'
        out = _replace_module_in_strings(content, "models", "schemas")
        assert out == 'x = "\U0001F389\U0001F389\U0001F389"; y = "schemas"\n'

    def test_ascii_only_unchanged_behaviour(self):
        from emend.transform.rename_move import _replace_module_in_strings

        content = 'y = "models"\n'
        out = _replace_module_in_strings(content, "models", "schemas")
        assert out == 'y = "schemas"\n'
