"""Tests for CLI transform commands (find, replace).

Component operation CLI tests (search/edit/add/rm) are in test_component_operations.py.
"""

import pytest
from typer.testing import CliRunner

from emend.cli import app

runner = CliRunner()


class TestFindCommand:
    """Tests for 'search' command (pattern mode)."""

    def test_find_simple_pattern(self, tmp_path):
        """Find a pattern with metavariable."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "x = 5\n"
            "print('world')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file)])

        assert result.exit_code == 0
        # Should show file:line format
        assert f"{str(test_file)}:1" in result.stdout

    def test_find_no_matches(self, tmp_path):
        """Find pattern that doesn't match anything."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "x = 5\n"
            "y = 10\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file)])

        assert result.exit_code == 0
        # Should indicate no matches (empty output)
        assert result.stdout.strip() == ""

    def test_find_multiple_matches(self, tmp_path):
        """Find pattern that matches multiple times."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "x = 5\n"
            "print('world')\n"
            "print('test')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file)])

        assert result.exit_code == 0
        # Should show 3 matches (3 lines of output)
        lines = result.stdout.strip().split('\n')
        assert len(lines) == 3
        # Each line should reference the file in file:line format
        for i, line in enumerate(lines):
            assert f"{str(test_file)}:" in line

    def test_find_count(self, tmp_path):
        """Find with --count shows only count."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "x = 5\n"
            "print('world')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file), "--output", "count"])

        assert result.exit_code == 0
        assert "2" in result.stdout

    def test_find_nonexistent_file(self, tmp_path):
        """Error when file doesn't exist."""
        result = runner.invoke(app, ["search", "print($X)", f"{tmp_path}/nonexistent.py"])

        assert result.exit_code != 0
        assert "does not exist" in result.stderr.lower() or "cannot find" in result.stderr.lower() or "error" in result.stderr.lower()

    def test_find_json_includes_matches(self, tmp_path):
        """Find with --json should include match details with line numbers and captures."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "x = 5\n"
            "print('world')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file), "--output", "json"])

        assert result.exit_code == 0
        # Parse JSON output
        import json
        data = json.loads(result.stdout)
        # Should have matches array
        assert "matches" in data
        assert len(data["matches"]) == 2
        # Each match should have line, code, and captures
        for match in data["matches"]:
            assert "line" in match
            assert match["line"] is not None
            assert "code" in match
            assert "captures" in match
        # Verify specific matches
        assert data["matches"][0]["line"] == 1
        assert "print" in data["matches"][0]["code"]
        assert data["matches"][1]["line"] == 3

    def test_find_with_scope(self, tmp_path):
        """Find only inside a named function using --in scope."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('global')\n"
            "def my_func():\n"
            "    print('inside')\n"
            "def other_func():\n"
            "    print('other')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file), "--where", "my_func"])

        assert result.exit_code == 0
        # Should find only the print inside my_func (line 3)
        lines = result.stdout.strip().split('\n')
        assert len(lines) == 1
        assert ":3" in lines[0]

    def test_find_with_dotted_scope(self, tmp_path):
        """Find only inside a nested scope using --in with dotted path."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "class MyClass:\n"
            "    def method(self):\n"
            "        print('inside method')\n"
            "    def other_method(self):\n"
            "        print('other')\n"
            "def func():\n"
            "    print('in func')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file), "--where", "MyClass.method"])

        assert result.exit_code == 0
        # Should find only the print inside MyClass.method (line 3)
        lines = result.stdout.strip().split('\n')
        assert len(lines) == 1
        assert ":3" in lines[0]


class TestReplaceCommand:
    """Tests for 'replace' command."""

    def test_replace_dry_run(self, tmp_path):
        """Replace without --apply shows diff without modifying file."""
        test_file = tmp_path / "test.py"
        original = "print('hello')\nx = 5\n"
        test_file.write_text(original)

        result = runner.invoke(app, ["replace", "print('hello')", "logger.info('hello')", str(test_file)])

        assert result.exit_code == 0
        # Should show diff
        assert "print('hello')" in result.stdout
        assert "logger.info('hello')" in result.stdout
        # File should not be modified
        assert test_file.read_text() == original

    def test_replace_apply(self, tmp_path):
        """Replace with --apply modifies file."""
        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')\nprint('world')\n")

        result = runner.invoke(app, ["replace", "print($X)", "logger.info($X)", str(test_file), "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "logger.info('hello')" in content
        assert "logger.info('world')" in content
        assert "print(" not in content

    def test_replace_with_metavar(self, tmp_path):
        """Replace pattern with metavariable."""
        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')\nprint('world')\n")

        result = runner.invoke(app, ["replace", "print($X)", "logger.info($X)", str(test_file), "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "logger.info('hello')" in content
        assert "logger.info('world')" in content

    def test_replace_multiple_metavars(self, tmp_path):
        """Replace pattern with multiple metavariables."""
        test_file = tmp_path / "test.py"
        test_file.write_text("assertEqual(x, 5)\nassertEqual(y, 10)\n")

        result = runner.invoke(app, ["replace", "assertEqual($A, $B)", "assert $A == $B", str(test_file), "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "assert x == 5" in content
        assert "assert y == 10" in content

    def test_replace_no_matches(self, tmp_path):
        """Replace pattern that doesn't match returns empty diff."""
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 5\ny = 10\n")

        result = runner.invoke(app, ["replace", "print($X)", "logger.info($X)", str(test_file)])

        assert result.exit_code == 0
        # Empty output when no matches
        assert result.stdout.strip() == ""

    def test_replace_nonexistent_file(self, tmp_path):
        """Error when file doesn't exist."""
        result = runner.invoke(app, ["replace", "print($X)", "logger.info($X)", f"{tmp_path}/nonexistent.py"])

        assert result.exit_code != 0
        assert "Error" in result.stdout or "Error" in result.stderr

    def test_replace_dry_run_output_format(self, tmp_path):
        """Replace dry-run should output diff format, not tuple repr."""
        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')\n")

        result = runner.invoke(app, ["replace", "print('hello')", "logger.info('hello')", str(test_file)])

        assert result.exit_code == 0
        # Should contain diff markers
        assert "---" in result.stdout or "+++" in result.stdout
        # Should NOT start with tuple opening paren or contain tuple closing with count
        assert not result.stdout.startswith("(")
        assert not result.stdout.endswith(", 1)")
        assert not result.stdout.endswith(", 1)\n")

    def test_replace_with_scope(self, tmp_path):
        """Replace only inside a named function using --in scope."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('global')\n"
            "def my_func():\n"
            "    print('inside')\n"
            "def other_func():\n"
            "    print('other')\n"
        )

        result = runner.invoke(app, ["replace", "print($X)", "logger.info($X)", str(test_file), "--where", "my_func", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        # Should replace only inside my_func
        assert "print('global')" in content  # Global not replaced
        assert "logger.info('inside')" in content  # Inside my_func replaced
        assert "print('other')" in content  # Inside other_func not replaced

    def test_replace_with_dotted_scope(self, tmp_path):
        """Replace only inside a nested scope using --in with dotted path."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "class MyClass:\n"
            "    def method(self):\n"
            "        print('inside method')\n"
            "    def other_method(self):\n"
            "        print('other')\n"
            "def func():\n"
            "    print('in func')\n"
        )

        result = runner.invoke(app, ["replace", "print($X)", "logger.info($X)", str(test_file), "--where", "MyClass.method", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        # Should replace only inside MyClass.method
        assert "logger.info('inside method')" in content  # Inside MyClass.method replaced
        assert "print('other')" in content  # Inside other_method not replaced
        assert "print('in func')" in content  # Inside func not replaced


class TestFindReplaceConstraints:
    """Tests for search/replace with --inside/--not-inside constraints."""

    def test_find_inside_cli(self, tmp_path):
        """Test find with --inside constraint."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('module')\n"
            "def func():\n"
            "    print('inside func')\n"
            "class MyClass:\n"
            "    def method(self):\n"
            "        print('inside method')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file), "--where", "def"])

        assert result.exit_code == 0
        # Should find prints inside functions only (2 matches)
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 2
        # Each match should be in file:line format
        assert all(f"{str(test_file)}:" in line for line in lines)

    def test_find_not_inside_cli(self, tmp_path):
        """Test find with --not-inside constraint."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('before')\n"
            "if condition:\n"
            "    print('inside if')\n"
            "print('after')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file), "--where", "not if"])

        assert result.exit_code == 0
        # Should find prints outside if blocks only (2 matches)
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 2

    def test_replace_inside_cli(self, tmp_path):
        """Test replace with --inside constraint."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('module')\n"
            "def func():\n"
            "    print('inside func')\n"
        )

        result = runner.invoke(app, ["replace", "print($X)", "logger.info($X)", str(test_file), "--where", "def", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        # Should replace only inside function
        assert "print('module')" in content  # Module level not replaced
        assert "logger.info('inside func')" in content  # Inside func replaced

    def test_replace_not_inside_cli(self, tmp_path):
        """Test replace with --not-inside constraint."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "x = 1\n"
            "class MyClass:\n"
            "    y = 2\n"
            "z = 3\n"
        )

        result = runner.invoke(app, ["replace", "$NAME = $VALUE", "$NAME: int = $VALUE", str(test_file), "--where", "not class", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        # Should replace only outside class
        assert "x: int = 1" in content  # Outside class replaced
        assert "z: int = 3" in content  # Outside class replaced
        assert "y = 2" in content  # Inside class not replaced

    def test_find_inside_and_not_inside_conflict(self, tmp_path):
        """Test that --inside and --not-inside cannot be used together."""
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")

        result = runner.invoke(app, ["search", "$X = $Y", str(test_file), "--where", "def", "--where", "not class"])

        assert result.exit_code == 2
        assert "Cannot specify both" in result.stdout or "Cannot specify both" in result.stderr


class TestFindReplaceMultiFile:
    """Tests for search/replace across multiple files using globs."""

    def test_find_glob_pattern(self, tmp_path):
        """Test find with glob pattern matching multiple files."""
        # Create multiple test files
        file1 = tmp_path / "test1.py"
        file1.write_text("print('file1')\n")
        file2 = tmp_path / "test2.py"
        file2.write_text("print('file2')\n")
        file3 = tmp_path / "other.py"
        file3.write_text("print('other')\n")

        # Use glob pattern to match test*.py
        result = runner.invoke(app, ["search", "print($X)", str(tmp_path / "test*.py")])

        assert result.exit_code == 0
        output = result.stdout
        # Should find matches in both test1.py and test2.py
        assert "test1.py" in output
        assert "test2.py" in output
        # Should NOT find match in other.py (doesn't match pattern)
        assert "other.py" not in output

    def test_find_directory(self, tmp_path):
        """Test find with directory path searches all .py files recursively."""
        # Create nested directory structure
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        file1 = tmp_path / "file1.py"
        file1.write_text("print('top')\n")
        file2 = subdir / "file2.py"
        file2.write_text("print('nested')\n")
        non_py = tmp_path / "file3.txt"
        non_py.write_text("print('not py')\n")

        result = runner.invoke(app, ["search", "print($X)", str(tmp_path)])

        assert result.exit_code == 0
        output = result.stdout
        # Should find matches in both .py files
        assert "file1.py" in output
        assert "file2.py" in output
        # Should NOT search non-.py files
        assert "file3.txt" not in output

    def test_replace_glob_pattern(self, tmp_path):
        """Test replace with glob pattern across multiple files."""
        file1 = tmp_path / "test1.py"
        file1.write_text("old('file1')\n")
        file2 = tmp_path / "test2.py"
        file2.write_text("old('file2')\n")
        file3 = tmp_path / "other.py"
        file3.write_text("old('other')\n")

        result = runner.invoke(app, ["replace", "old($X)", "new($X)", str(tmp_path / "test*.py"), "--apply"])

        assert result.exit_code == 0
        # Check files were modified
        assert "new('file1')" in file1.read_text()
        assert "new('file2')" in file2.read_text()
        # other.py should NOT be modified (doesn't match glob)
        assert "old('other')" in file3.read_text()

    def test_replace_directory(self, tmp_path):
        """Test replace with directory path modifies all .py files."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        file1 = tmp_path / "file1.py"
        file1.write_text("x = 1\n")
        file2 = subdir / "file2.py"
        file2.write_text("x = 2\n")

        result = runner.invoke(app, ["replace", "$NAME = $VALUE", "$NAME: int = $VALUE", str(tmp_path), "--apply"])

        assert result.exit_code == 0
        # Both files should be modified
        assert "x: int = 1" in file1.read_text()
        assert "x: int = 2" in file2.read_text()
