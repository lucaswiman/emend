r"""Tests for transform-references functionality using find + replace primitives.

This test suite migrates transform-references tests to use the lower-level
find + replace --in primitives instead of the higher-level transform-references
command.

Key translation guide:
- Simple text: old_name → old_name (no regex needed)
- Word boundaries: \bname\b → name (AST respects boundaries automatically)
- Capture groups: func\((\w+)\) → func($arg) with metavariables
- Backreferences: \1, \2 → $VAR1, $VAR2 (or named vars)
"""

import ast
import subprocess
import tempfile
from pathlib import Path

import pytest

from conftest import assert_valid_python


@pytest.fixture
def temp_py_file():
    """Create a temporary Python file for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        yield f
    Path(f.name).unlink(missing_ok=True)


class TestPrimitivesTransform:
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

    def test_replacement_with_function_call_pattern(self, emend_cmd, temp_py_file):
        r"""Test replacement of function calls with capture groups.

        Migrated from: test_transform_function_calls_within_scope
        Old: transform-references --from "helper\((\d+)\)" --to "helper_v2_\1"
        New: replace "helper($N)" "helper_v2_$N" --in main
        """
        temp_py_file.write(
            """\
def helper(x):
    return x + 1

def main():
    result1 = helper(5)
    result2 = helper(10)
    return result1 + result2
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "replace",
                "helper($N)",
                "helper_v2_$N",
                temp_py_file.name,
                "--where",
                "main",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # Check the file was modified
        content = Path(temp_py_file.name).read_text()
        assert "helper_v2_5" in content
        assert "helper_v2_10" in content

    def test_replacement_preserves_string_literals(self, emend_cmd, temp_py_file):
        """Test that string literals are preserved and not transformed.

        Migrated from: test_transform_preserves_string_literals

        This test verifies that replace respects string literal boundaries:
        - Identifiers like 'prefix' get replaced
        - The same text within string literals is NOT replaced
        """
        temp_py_file.write(
            """\
def config():
    prefix = "value1"
    config_key = "prefix_in_string"
    return [prefix, config_key]
"""
        )
        temp_py_file.flush()

        result = subprocess.run(
            [
                emend_cmd,
                "replace",
                "prefix",
                "settings",
                temp_py_file.name,
                "--where",
                "config",
                "--apply",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        content = Path(temp_py_file.name).read_text()
        # Verify positive assertion: identifier replacement occurred
        assert "settings = " in content
        assert "return [settings, config_key]" in content
        # String literals should NOT be transformed (negative assertion)
        assert '"prefix_in_string"' in content
        # Verify AST validity: modified code is syntactically correct
        assert_valid_python(content)

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
