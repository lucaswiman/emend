"""Tests for CLI transform commands (find, replace).

Component operation CLI tests (search/edit/add/rm) are in test_component_operations.py.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from emend.cli import app

from conftest import assert_valid_python

runner = CliRunner()


@pytest.mark.parametrize('extensions', [('tsx', 'tsx'), ('tsx', 'ts')])
def test_multifile_replace_preserves_file_dialects(tmp_path, extensions):
    from emend.transform.project_iter import find_pattern_in_project

    paths = [tmp_path / f'file{i}.{ext}' for i, ext in enumerate(extensions)]
    for path in paths:
        path.write_text('const f = () => <div>{print(1)}</div>;\n' if path.suffix == '.tsx'
                        else 'function f() { print(1); }\n')
    assert len(find_pattern_in_project('print($X)', [str(p) for p in paths], language='typescript')) == 2
    result = runner.invoke(app, ['--language', 'typescript', 'replace', 'print($X)', 'log($X)', str(tmp_path), '--apply'])
    assert result.exit_code == 0, result.output
    assert all('log(1)' in path.read_text() and 'print(1)' not in path.read_text() for path in paths)


def test_parse_where_rejects_empty_value():
    from emend.cli_base import parse_where_clause

    with pytest.raises(ValueError, match="--where cannot be empty"):
        parse_where_clause(["  "])


class TestFindCommand:
    """Tests for canonical 'find' pattern mode."""

    def test_find_simple_pattern(self, tmp_path):
        """Find a pattern with metavariable."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "x = 5\n"
            "print('world')\n"
        )

        result = runner.invoke(app, ["find", "print($X)", str(test_file)])

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

        result = runner.invoke(app, ["find", "print($X)", str(test_file)])

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

        result = runner.invoke(app, ["find", "print($X)", str(test_file)])

        assert result.exit_code == 0
        # Should show 3 matches (header + code line per match = 6 lines)
        lines = result.stdout.strip().split('\n')
        assert len(lines) == 6
        # Odd lines (0, 2, 4) are headers with file:line format
        for i in range(0, 6, 2):
            assert f"{str(test_file)}:" in lines[i]

    def test_find_count(self, tmp_path):
        """Find with --count shows only count."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "x = 5\n"
            "print('world')\n"
        )

        result = runner.invoke(app, ["find", "print($X)", str(test_file), "--output", "count"])

        assert result.exit_code == 0
        assert "2" in result.stdout

    def test_find_nonexistent_file(self, tmp_path):
        """Error when file doesn't exist."""
        result = runner.invoke(app, ["find", "print($X)", f"{tmp_path}/nonexistent.py"])

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

        result = runner.invoke(app, ["find", "print($X)", str(test_file), "--output", "json"])

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

        result = runner.invoke(app, ["find", "print($X)", str(test_file), "--where", "my_func"])

        assert result.exit_code == 0
        # Should find only the print inside my_func (line 3): header + code = 2 lines
        lines = result.stdout.strip().split('\n')
        assert len(lines) == 2
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

        result = runner.invoke(app, ["find", "print($X)", str(test_file), "--where", "MyClass.method"])

        assert result.exit_code == 0
        # Should find only the print inside MyClass.method (line 3): header + code = 2 lines
        lines = result.stdout.strip().split('\n')
        assert len(lines) == 2
        assert ":3" in lines[0]

    def test_find_multiple_files(self, tmp_path):
        """Canonical find accepts multiple file scopes."""
        file1 = tmp_path / "a.py"
        file1.write_text("print('a')\n")
        file2 = tmp_path / "b.py"
        file2.write_text("print('b')\n")

        result = runner.invoke(app, ["find", "print($X)", str(file1), str(file2)])

        assert result.exit_code == 0
        assert f"{file1}:1" in result.stdout
        assert f"{file2}:1" in result.stdout

    def test_find_within_flag(self, tmp_path):
        """Canonical find exposes explicit --within."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def helper():\n"
            "    print('helper')\n"
            "def test_case():\n"
            "    print('match')\n"
        )

        result = runner.invoke(
            app,
            ["find", "--within", "def test_*", "print($X)", str(test_file)],
        )

        assert result.exit_code == 0
        assert ":4" in result.stdout
        assert ":2" not in result.stdout

    def test_find_not_within_flag(self, tmp_path):
        """Canonical find exposes explicit --not-within."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def helper():\n"
            "    print('helper')\n"
            "class TestCase:\n"
            "    def case(self):\n"
            "        print('skip')\n"
        )

        result = runner.invoke(
            app,
            ["find", "--not-within", "class Test*", "print($X)", str(test_file)],
        )

        assert result.exit_code == 0
        assert ":2" in result.stdout
        assert ":5" not in result.stdout


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
        # Should find prints inside functions only (2 matches): header + code per match = 4 lines
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 4
        # Header lines (0, 2) should reference the file in file:line format
        assert f"{str(test_file)}:" in lines[0]
        assert f"{str(test_file)}:" in lines[2]

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
        # Should find prints outside if blocks only (2 matches): header + code per match = 4 lines
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 4

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

    def test_replace_glob_honors_language_override(self, tmp_path):
        files = [tmp_path / name for name in ("a.py", "b.py")]
        for path in files:
            path.write_text("const result = first == second;\n")

        result = runner.invoke(app, [
            "--language", "typescript", "replace", "$X == $Y", "$X === $Y",
            *(str(path) for path in files), "--apply",
        ])

        assert result.exit_code == 0, result.output
        assert all("first === second" in path.read_text() for path in files)

        python_file = tmp_path / "default.py"
        python_file.write_text("old(1)\n")
        result = runner.invoke(app, [
            "replace", "old($X)", "new($X)", str(python_file), "--apply",
        ])
        assert result.exit_code == 0, result.output
        assert python_file.read_text() == "new(1)\n"

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


@pytest.fixture
def temp_py_file():
    """Create a temporary Python file for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        yield f
    Path(f.name).unlink(missing_ok=True)


class TestReplaceWhereScope:
    """``replace --where <scope>`` through the installed CLI (identifier-aware, literal-safe)."""

    def test_simple_text_replacement_without_apply_shows_preview(self, emend_cmd, temp_py_file):
        """Test that simple text replacement shows preview without --apply.

        Migrated from: test_transform_without_apply_shows_preview
        Old: transform-references --from "old_name" --to "new_name"
        New: replace "old_name" "new_name" --in wrapper
        """
        temp_py_file.write(
            """\
def wrapper():
    x = old_name
    y = old_name + 1
    return x + y
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "replace",
                "old_name",
                "new_name",
                temp_py_file.name,
                "--where",
                "wrapper",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # File should not be modified (no --apply)
        content = Path(temp_py_file.name).read_text()
        assert "old_name" in content
        assert "new_name" not in content

        # Preview should be in stdout
        assert "Changes: 2" in result.stdout or "old_name" in result.stdout

    def test_capture_groups_with_metavariables(self, emend_cmd, temp_py_file):
        r"""Test replacement with capture groups using metavariables.

        Migrated from: test_transform_with_regex_groups
        Old: transform-references --from "config\.get_field(\d+)" --to "config.field\1"
        New: replace "helper($X)" "helper_new_$X" --in process

        Note: This test uses a different example because replace's metavariable system
        works with arbitrary expressions rather than regex groups. The pattern captures
        entire arguments as metavariables, not individual numeric suffixes.
        """
        temp_py_file.write(
            """\
def process():
    result1 = helper(1)
    result2 = helper(2)
    result3 = helper(3)
    return [result1, result2, result3]
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "replace",
                "helper($X)",
                "helper_new_$X",
                temp_py_file.name,
                "--where",
                "process",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        content = Path(temp_py_file.name).read_text()
        assert "result1 = helper_new_1" in content
        assert "result2 = helper_new_2" in content
        assert "result3 = helper_new_3" in content

    def test_syntax_aware_replacement_respects_literals(self, emend_cmd, temp_py_file):
        """Test that replacement respects string literals and comments.

        Migrated from: test_transform_identifier_not_string_literal
        Old: transform-references --from "old_name" --to "new_name"
        New: replace "old_name" "new_name" --in foo

        The key difference is that transform-references (AST-based) skips
        string literals and comments, while replace needs to be syntax-aware.
        """
        temp_py_file.write(
            """\
def foo():
    old_name = 1
    msg = "old_name is the variable"
    return old_name
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "replace",
                "old_name",
                "new_name",
                temp_py_file.name,
                "--where",
                "foo",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        content = Path(temp_py_file.name).read_text()
        # Identifiers should be transformed
        assert "new_name = 1" in content
        assert "return new_name" in content
        # String literals should NOT be transformed
        assert 'msg = "old_name is the variable"' in content
        # The rewritten file is still valid Python
        assert_valid_python(content)

    def test_replacement_with_multiple_occurrences_same_line(self, emend_cmd, temp_py_file):
        """Test replacement of multiple occurrences on the same line.

        Migrated from: test_transform_multiple_occurrences_same_line
        Old: transform-references --from "\bval\b" --to "value"
        New: replace "val" "value" --in compute

        Note: Word boundary regex \b is implicit in AST-based transforms,
        but replace works with simpler pattern matching.
        """
        temp_py_file.write(
            """\
def compute():
    result = val + val + val
    return result
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "replace",
                "val",
                "value",
                temp_py_file.name,
                "--where",
                "compute",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        content = Path(temp_py_file.name).read_text()
        assert "result = value + value + value" in content

    def test_replacement_in_method_scope(self, emend_cmd, temp_py_file):
        r"""Test replacement within a method scope.

        Migrated from: test_transform_nested_function_scope
        Old: transform-references --from "\bitems\b" --to "data_items" --apply
             file.py::Builder.build
        New: replace "items" "data_items" file.py --in Builder.build --apply

        Note: Method scopes must use dotted path (ClassName.method_name).
        """
        temp_py_file.write(
            """\
class Builder:
    def build(self):
        items = [1, 2, 3]
        total = sum(items)
        count = len(items)
        return total / count
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "replace",
                "items",
                "data_items",
                temp_py_file.name,
                "--where",
                "Builder.build",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        content = Path(temp_py_file.name).read_text()
        # Verify positive assertions: replacements occurred
        assert "data_items = [1, 2, 3]" in content
        assert "sum(data_items)" in content
        assert "len(data_items)" in content
        # Verify AST validity: modified code is syntactically correct
        assert_valid_python(content)

    def test_replacement_no_matches(self, emend_cmd, temp_py_file):
        """Test replacement when pattern matches nothing.

        Migrated from: test_transform_no_matches
        """
        temp_py_file.write(
            """\
def func():
    x = 42
    return x
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "replace",
                "nonexistent_pattern",
                "something_else",
                temp_py_file.name,
                "--where",
                "func",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        # Should succeed but not change anything
        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"
        # When there are no matches, the output is typically empty (no diff)
        # The command still succeeds with exit code 0

        content = Path(temp_py_file.name).read_text()
        assert "x = 42" in content

    def test_replacement_in_method_call(self, emend_cmd, temp_py_file):
        r"""Test replacement of method calls within a class method.

        Migrated from: test_transform_method_calls_within_class
        Old: transform-references --from "self\.add" --to "self.new_add" --apply
        New: replace "self.add" "self.new_add" --in Calculator.process --apply

        Note: Method scopes must use dotted path (ClassName.method_name).
        """
        temp_py_file.write(
            """\
class Calculator:
    def add(self, x, y):
        return x + y

    def process(self):
        result1 = self.add(1, 2)
        result2 = self.add(3, 4)
        return result1 + result2
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "replace",
                "self.add",
                "self.new_add",
                temp_py_file.name,
                "--where",
                "Calculator.process",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        content = Path(temp_py_file.name).read_text()
        assert "self.new_add(1, 2)" in content
        assert "self.new_add(3, 4)" in content

    def test_replacement_comment_not_modified(self, emend_cmd, temp_py_file):
        """Test that comments are not modified by replacement.

        Migrated from: test_transform_identifier_not_comment
        """
        temp_py_file.write(
            """\
def foo():
    old_name = 1  # old_name is the variable
    return old_name
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "replace",
                "old_name",
                "new_name",
                temp_py_file.name,
                "--where",
                "foo",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        content = Path(temp_py_file.name).read_text()
        # Identifiers should be transformed
        assert "new_name = 1" in content
        assert "return new_name" in content
        # Comments should NOT be transformed
        assert "# old_name is the variable" in content
