"""YAML-parameterized tests for component operations (get/set/add/remove).

Each YAML file in data/ defines test cases that run at both the raw API level
(get_component, set_component, add_to_component, remove_component) and the
CLI level (emend search, emend edit, emend add).
"""

import ast
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from emend.cli import app
from emend.component_selector import ExtendedSelector
from emend.transform import (
    add_to_component,
    cmd_add,
    get_component,
    remove_component,
    set_component,
)

runner = CliRunner()
DATA_DIR = Path(__file__).parent / "data"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_cases(filename: str) -> list[dict]:
    """Load test cases from a YAML file in the data/ directory."""
    with open(DATA_DIR / filename) as f:
        return yaml.safe_load(f)


def parse_selector(selector_str: str, file_path: str) -> ExtendedSelector:
    """Parse a simplified selector string into an ExtendedSelector.

    Examples:
        "func[params]"             -> symbol_path=["func"], component="params"
        "func[params][x]"          -> ..., accessor="x"
        "func[params][1]"          -> ..., accessor=1
        "func[params][-1]"         -> ..., accessor=-1
        "MyClass.method[params]"   -> symbol_path=["MyClass", "method"], ...
        "func[params]:KEYWORD_ONLY" -> ..., pseudo_class="KEYWORD_ONLY"
        "func"                     -> symbol_path=["func"], component=None
    """
    pseudo_class = None
    remaining = selector_str

    # Extract pseudo_class suffix (e.g. ":KEYWORD_ONLY")
    pseudo_match = re.search(r":([A-Z_]+)$", remaining)
    if pseudo_match:
        pseudo_class = pseudo_match.group(1)
        remaining = remaining[: pseudo_match.start()]

    # Extract bracket contents
    brackets = re.findall(r"\[([^\]]*)\]", remaining)
    component = brackets[0] if len(brackets) >= 1 else None

    accessor = None
    if len(brackets) >= 2:
        accessor_str = brackets[1]
        try:
            accessor = int(accessor_str)
        except ValueError:
            accessor = accessor_str

    # Symbol path is everything before the first '['
    symbol_part = remaining.split("[")[0]
    symbol_path = symbol_part.split(".")

    return ExtendedSelector(
        file_path=file_path,
        symbol_path=symbol_path,
        component=component,
        accessor=accessor,
        pseudo_class=pseudo_class,
    )


def write_source(tmp_path, case):
    """Write case source to a temp file, return path string."""
    test_file = tmp_path / "test.py"
    test_file.write_text(case["source"])
    return str(test_file)


def case_id(case):
    """Generate a readable test ID from a case dict."""
    return case["name"]


# ---------------------------------------------------------------------------
# GET tests
# ---------------------------------------------------------------------------

get_cases = load_cases("component_get.yaml")


@pytest.mark.parametrize("case", get_cases, ids=case_id)
def test_get_api(case, tmp_path):
    """get_component() returns the expected string."""
    file_path = write_source(tmp_path, case)
    selector = parse_selector(case["selector"], file_path)
    result = get_component(selector)
    assert result == case["expected"]


@pytest.mark.parametrize("case", get_cases, ids=case_id)
def test_get_cli(case, tmp_path):
    """'emend search' stdout matches expected output."""
    file_path = write_source(tmp_path, case)
    result = runner.invoke(app, ["search", f"{file_path}::{case['selector']}"])
    assert result.exit_code == 0
    if "cli_contains" in case:
        for expected in case["cli_contains"]:
            assert expected in result.stdout
    else:
        assert result.stdout.strip() == case["expected"]


# ---------------------------------------------------------------------------
# SET tests
# ---------------------------------------------------------------------------

set_cases = load_cases("component_set.yaml")


@pytest.mark.parametrize("case", set_cases, ids=case_id)
def test_set_api(case, tmp_path):
    """set_component() diff contains expected strings."""
    file_path = write_source(tmp_path, case)
    selector = parse_selector(case["selector"], file_path)
    diff = set_component(selector, case["value"], apply=False)
    for expected in case.get("expected_in_diff", []):
        assert expected in diff, f"Expected {expected!r} in diff:\n{diff}"
    assert Path(file_path).read_text() == case["source"]


@pytest.mark.parametrize("case", set_cases, ids=case_id)
def test_set_cli(case, tmp_path):
    """'emend edit' modifies file correctly."""
    file_path = write_source(tmp_path, case)
    result = runner.invoke(
        app, ["edit", f"{file_path}::{case['selector']}", case["value"], "--apply"]
    )
    assert result.exit_code == 0, f"exit_code={result.exit_code}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    content = Path(file_path).read_text()
    for expected in case.get("expected_in_file", []):
        assert expected in content, f"Expected {expected!r} in file:\n{content}"


# ---------------------------------------------------------------------------
# ADD tests
# ---------------------------------------------------------------------------

add_cases = load_cases("component_add.yaml")


def _has_before_after(case):
    return "before" in case or "after" in case


@pytest.mark.parametrize("case", add_cases, ids=case_id)
def test_add_api(case, tmp_path):
    """add_to_component() diff contains expected strings."""
    file_path = write_source(tmp_path, case)

    if _has_before_after(case):
        # before/after require cmd_add (higher-level wrapper)
        diff = cmd_add(
            selector_str=f"{file_path}::{case['selector']}",
            value=case["value"],
            before=case.get("before"),
            after=case.get("after"),
            apply=False,
        )
    else:
        selector = parse_selector(case["selector"], file_path)
        position = case.get("position", -1)
        diff = add_to_component(selector, case["value"], position=position, apply=False)

    for expected in case.get("expected_in_diff", []):
        assert expected in diff, f"Expected {expected!r} in diff:\n{diff}"
    assert Path(file_path).read_text() == case["source"]


@pytest.mark.parametrize("case", add_cases, ids=case_id)
def test_add_cli(case, tmp_path):
    """'emend add' modifies file correctly."""
    file_path = write_source(tmp_path, case)
    args = ["add", f"{file_path}::{case['selector']}", case["value"]]

    if "before" in case:
        args += ["--before", case["before"]]
    elif "after" in case:
        args += ["--after", case["after"]]
    elif "position" in case and case["position"] >= 0:
        args += ["--at", str(case["position"])]

    args.append("--apply")
    result = runner.invoke(app, args)
    assert result.exit_code == 0, f"exit_code={result.exit_code}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    content = Path(file_path).read_text()
    for expected in case.get("expected_in_file", []):
        assert expected in content, f"Expected {expected!r} in file:\n{content}"


# ---------------------------------------------------------------------------
# REMOVE tests
# ---------------------------------------------------------------------------

remove_cases = load_cases("component_remove.yaml")


@pytest.mark.parametrize("case", remove_cases, ids=case_id)
def test_remove_api(case, tmp_path):
    """remove_component() diff contains expected strings."""
    file_path = write_source(tmp_path, case)
    selector = parse_selector(case["selector"], file_path)
    diff = remove_component(selector, apply=False)
    for expected in case.get("expected_in_diff", []):
        assert expected in diff, f"Expected {expected!r} in diff:\n{diff}"
    assert Path(file_path).read_text() == case["source"]


def test_add_rejects_position_below_append_sentinel(tmp_path):
    test_file = tmp_path / "test.py"
    source = "def func(a, b):\n    pass\n"
    test_file.write_text(source)
    selector = ExtendedSelector(
        file_path=str(test_file), symbol_path=["func"], component="params"
    )

    with pytest.raises(ValueError, match="position must be -1"):
        add_to_component(selector, "x", position=-2)
    assert test_file.read_text() == source


def test_diff_preserves_selector_path_spelling(tmp_path, monkeypatch):
    nested = tmp_path / "nested"
    nested.mkdir()
    test_file = nested / "test.py"
    test_file.write_text("def func() -> int:\n    pass\n")
    monkeypatch.chdir(tmp_path)
    selector = ExtendedSelector(
        file_path="./nested/test.py", symbol_path=["func"], component="returns"
    )

    diff = set_component(selector, "str")

    assert diff.startswith("--- ./nested/test.py\n+++ ./nested/test.py\n")


@pytest.mark.parametrize("case", remove_cases, ids=case_id)
def test_remove_cli(case, tmp_path):
    """'emend edit --rm' modifies file correctly."""
    file_path = write_source(tmp_path, case)
    selector_str = case["selector"]

    # For remove, split selector to determine CLI args
    # If there's an accessor, format is: edit <file>::sym[comp][acc] --rm --apply
    # If no accessor, format is: edit <file>::sym[comp] --rm --apply
    result = runner.invoke(
        app, ["edit", f"{file_path}::{selector_str}", "--rm", "--apply"]
    )
    assert result.exit_code == 0, f"exit_code={result.exit_code}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    content = Path(file_path).read_text()
    for expected in case.get("expected_in_file", []):
        assert expected in content, f"Expected {expected!r} in file:\n{content}"
    for not_expected in case.get("not_in_file", []):
        assert not_expected not in content, f"Did NOT expect {not_expected!r} in file:\n{content}"


# ---------------------------------------------------------------------------
# ERROR tests
# ---------------------------------------------------------------------------

error_cases = load_cases("component_errors.yaml")

EXCEPTION_MAP = {
    "ValueError": ValueError,
    "FileNotFoundError": FileNotFoundError,
}


@pytest.mark.parametrize(
    "case",
    [c for c in error_cases if not c.get("cli_only")],
    ids=lambda c: c["name"],
)
def test_error_api(case, tmp_path):
    """Operations raise expected exceptions."""
    if case.get("source") is not None:
        file_path = write_source(tmp_path, case)
    else:
        file_path = str(tmp_path / "nonexistent.py")

    selector = parse_selector(case["selector"], file_path)
    exc_type = EXCEPTION_MAP[case["error_type"]]
    match = case.get("error_match") or None

    with pytest.raises(exc_type, match=match):
        op = case["operation"]
        if op == "get":
            get_component(selector)
        elif op == "set":
            set_component(selector, case.get("value", ""), apply=False)
        elif op == "add":
            add_to_component(selector, case.get("value", ""), position=-1, apply=False)
        elif op == "remove":
            remove_component(selector, apply=False)


@pytest.mark.parametrize("case", error_cases, ids=lambda c: c["name"])
def test_error_cli(case, tmp_path):
    """CLI returns non-zero exit code with error message."""
    if case.get("source") is not None:
        file_path = write_source(tmp_path, case)
    else:
        file_path = str(tmp_path / "nonexistent.py")

    selector_str = f"{file_path}::{case['selector']}"
    op = case["operation"]

    if op == "get":
        args = ["search", selector_str]
    elif op == "set":
        args = ["edit", selector_str, case.get("value", "")]
    elif op == "add":
        args = ["add", selector_str, case.get("value", "")]
        if "before" in case:
            args += ["--before", case["before"]]
        if "after" in case:
            args += ["--after", case["after"]]
    elif op == "remove":
        args = ["edit", selector_str, "--rm"]

    result = runner.invoke(app, args)
    assert result.exit_code != 0, (
        f"Expected non-zero exit for {case['name']}, got 0\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Behavioral / non-parameterized tests
# ---------------------------------------------------------------------------


def test_set_apply_writes_file(tmp_path):
    """set_component with apply=True writes changes to file."""
    test_file = tmp_path / "test.py"
    test_file.write_text("def func() -> int:\n    pass\n")

    selector = ExtendedSelector(
        file_path=str(test_file), symbol_path=["func"], component="returns"
    )
    diff = set_component(selector, "str", apply=True)

    content = test_file.read_text()
    assert "-> str:" in content
    assert "-> int:" not in content
    # Diff should still be returned
    assert "-> int:" in diff
    assert "-> str:" in diff


def test_set_dry_run_no_write(tmp_path):
    """set_component with apply=False does not modify the file."""
    test_file = tmp_path / "test.py"
    original = "def func() -> int:\n    pass\n"
    test_file.write_text(original)

    selector = ExtendedSelector(
        file_path=str(test_file), symbol_path=["func"], component="returns"
    )
    diff = set_component(selector, "str", apply=False)

    assert test_file.read_text() == original
    assert "-> int:" in diff
    assert "-> str:" in diff


def test_diff_format(tmp_path):
    """Verify set_component returns unified diff format."""
    test_file = tmp_path / "test.py"
    test_file.write_text("def func() -> int:\n    pass\n")

    selector = ExtendedSelector(
        file_path=str(test_file), symbol_path=["func"], component="returns"
    )
    diff = set_component(selector, "str", apply=False)

    assert diff.startswith("---")
    assert "+++" in diff
    assert "@@" in diff


def test_remove_symbol_dry_run(tmp_path):
    """Removing an entire symbol with apply=False shows diff."""
    from emend.transform import cmd_edit

    test_file = tmp_path / "test.py"
    test_file.write_text("def foo():\n    pass\n\ndef bar():\n    pass\n")

    result = cmd_edit(selector_str=f"{test_file}::foo", rm=True, apply=False)

    assert "def foo():" in test_file.read_text()
    assert "-def foo():" in result
    assert "def bar():" in result


def test_remove_symbol_with_apply(tmp_path):
    """Removing an entire symbol with apply=True deletes it from file."""
    from emend.transform import cmd_edit

    test_file = tmp_path / "test.py"
    test_file.write_text("def foo():\n    pass\n\ndef bar():\n    pass\n")

    cmd_edit(selector_str=f"{test_file}::foo", rm=True, apply=True)

    content = test_file.read_text()
    assert "def foo():" not in content
    assert "def bar():" in content


def test_remove_class_with_apply(tmp_path):
    """Removing an entire class with apply=True deletes it from file."""
    from emend.transform import cmd_edit

    test_file = tmp_path / "test.py"
    test_file.write_text("class Foo:\n    pass\n\nclass Bar:\n    pass\n")

    cmd_edit(selector_str=f"{test_file}::Foo", rm=True, apply=True)

    content = test_file.read_text()
    assert "class Foo:" not in content
    assert "class Bar:" in content


def test_edit_no_operation(tmp_path):
    """cmd_edit raises ValueError when no operation is specified."""
    from emend.transform import cmd_edit

    test_file = tmp_path / "test.py"
    test_file.write_text("def foo(): pass\n")

    with pytest.raises(ValueError, match="No operation"):
        cmd_edit(selector_str=f"{test_file}::foo[params]")


def test_edit_explicit_language_overrides_file_extension(tmp_path):
    test_file = tmp_path / "python_source.ts"
    test_file.write_text("def foo():\n    return 1\n")

    result = runner.invoke(
        app,
        [
            "--language", "python", "edit", "set",
            f"{test_file}::foo[body]", "return 2", "--apply",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "return 2" in test_file.read_text()
    assert "return 1" not in test_file.read_text()


def test_remove_last_param_multiline_no_trailing_comma(tmp_path):
    """Removing the last parameter in a multi-line list must also remove
    the preceding comma on the previous line."""
    test_file = tmp_path / "test.py"
    test_file.write_text("def func(\n    x: int,\n    y: str\n):\n    pass\n")

    selector = ExtendedSelector(
        file_path=str(test_file),
        symbol_path=["func"],
        component="params",
        accessor="y",
    )
    remove_component(selector, apply=True)

    content = test_file.read_text()
    assert "y" not in content
    assert "x: int," not in content, (
        "Trailing comma left after removing last param in multi-line list"
    )
    assert "x: int" in content


def test_cli_edit_selector_with_brackets(tmp_path):
    """Selectors containing brackets (e.g. [params]) must not be
    misinterpreted as Typer subcommand names (regression for typer._click
    vs click.UsageError mismatch)."""
    test_file = tmp_path / "test.py"
    test_file.write_text("def func(x: int) -> str:\n    return ''\n")

    result = runner.invoke(
        app,
        ["edit", f"{test_file}::func[returns]", "int", "--apply"],
    )
    assert result.exit_code == 0, (
        f"exit_code={result.exit_code}\nstdout: {result.stdout}\n"
        f"stderr: {getattr(result, 'stderr', '')}"
    )
    assert "-> int:" in test_file.read_text()


# ---------------------------------------------------------------------------
# [imports] component: get + add through the installed CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_py_files():
    """Create two temporary Python files for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as source_file:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as dest_file:
            yield source_file, dest_file
    Path(source_file.name).unlink(missing_ok=True)
    Path(dest_file.name).unlink(missing_ok=True)


class TestCopyImportsViaComponents:
    """Copy imports between files with ``search ::[imports]`` + ``add ::[imports]``."""

    def test_copy_imports_to_empty_file(self, emend_cmd, temp_py_files):
        """Test copying imports to an empty file using get + add."""
        source_file, dest_file = temp_py_files
        source_file.write(
            """\
import os
import sys
from pathlib import Path

def foo():
    pass
"""
        )
        source_file.flush()

        dest_file.write("")
        dest_file.flush()

        # Get imports from source
        result = subprocess.run(
            [emend_cmd, "search", f"{source_file.name}::[imports]"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Failed to get imports: {result.stderr}"
        imports = result.stdout.strip().split('\n')

        # Add each import to destination
        for import_line in imports:
            if import_line:
                result = subprocess.run(
                    [emend_cmd, "add", f"{dest_file.name}::[imports]", import_line, "--apply"],
                    capture_output=True,
                    text=True,
                )
                assert result.returncode == 0, f"Failed to add import: {result.stderr}"

        content = Path(dest_file.name).read_text()

        # Validate that the generated file is syntactically valid Python
        try:
            ast.parse(content)
        except SyntaxError as e:
            pytest.fail(f"Generated code is not valid Python:\n{content}\n\nError: {e!r}")

        # Verify all imports are present and correctly formatted
        assert "import os" in content
        assert "import sys" in content
        assert "from pathlib import Path" in content

    def test_copy_imports_prepend_to_file_with_imports(self, emend_cmd, temp_py_files):
        """Test copying imports prepends before existing imports using position control."""
        source_file, dest_file = temp_py_files
        source_file.write(
            """\
import json
from typing import Dict
"""
        )
        source_file.flush()

        dest_file.write(
            """\
import os

def bar():
    pass
"""
        )
        dest_file.flush()

        # Get imports from source
        result = subprocess.run(
            [emend_cmd, "search", f"{source_file.name}::[imports]"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Failed to get imports: {result.stderr}"
        imports = result.stdout.strip().split('\n')

        # Add imports at position 0 (prepend)
        for import_line in imports:
            if import_line:
                result = subprocess.run(
                    [emend_cmd, "add", f"{dest_file.name}::[imports]", import_line, "--at", "0", "--apply"],
                    capture_output=True,
                    text=True,
                )
                assert result.returncode == 0, f"Failed to add import: {result.stderr}"

        content = Path(dest_file.name).read_text()

        # Validate that the generated file is syntactically valid Python
        try:
            ast.parse(content)
        except SyntaxError as e:
            pytest.fail(f"Generated code is not valid Python:\n{content}\n\nError: {e!r}")

        # Collect all imports in order
        tree = ast.parse(content)
        import_statements = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    import_statements.append(f"import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = ", ".join(alias.name for alias in node.names)
                import_statements.append(f"from {module} import {names}")

        # Verify all required imports are present
        assert any("json" in imp for imp in import_statements), f"'import json' not found in {import_statements}"
        assert any("typing" in imp and "Dict" in imp for imp in import_statements), f"'from typing import Dict' not found in {import_statements}"
        assert any("os" in imp for imp in import_statements), f"'import os' not found in {import_statements}"

        # Verify import order: source imports (json, typing) should come before existing imports (os)
        json_idx = next(i for i, imp in enumerate(import_statements) if "json" in imp)
        os_idx = next(i for i, imp in enumerate(import_statements) if "import os" in imp)
        typing_idx = next(i for i, imp in enumerate(import_statements) if "typing" in imp)
        assert json_idx < os_idx, f"'import json' at index {json_idx} should come before 'import os' at index {os_idx}"
        assert typing_idx < os_idx, f"'from typing import Dict' at index {typing_idx} should come before 'import os' at index {os_idx}"

    def test_copy_imports_append_to_file_with_imports(self, emend_cmd, temp_py_files):
        """Test copying imports with append adds after existing imports."""
        source_file, dest_file = temp_py_files
        source_file.write(
            """\
import json
from typing import Dict
"""
        )
        source_file.flush()

        dest_file.write(
            """\
import os

def bar():
    pass
"""
        )
        dest_file.flush()

        # Get imports from source
        result = subprocess.run(
            [emend_cmd, "search", f"{source_file.name}::[imports]"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Failed to get imports: {result.stderr}"
        imports = result.stdout.strip().split('\n')

        # Add imports at position -1 (append)
        for import_line in imports:
            if import_line:
                result = subprocess.run(
                    [emend_cmd, "add", f"{dest_file.name}::[imports]", import_line, "--apply"],
                    capture_output=True,
                    text=True,
                )
                assert result.returncode == 0, f"Failed to add import: {result.stderr}"

        content = Path(dest_file.name).read_text()
        assert "import json" in content
        assert "from typing import Dict" in content
        assert "import os" in content
        # Existing imports should come before source imports
        assert content.index("import os") < content.index("import json")

    def test_copy_imports_preserves_future_imports_at_top(self, emend_cmd, temp_py_files):
        """Test that __future__ imports stay at the top."""
        source_file, dest_file = temp_py_files
        source_file.write(
            """\
import os
import sys
"""
        )
        source_file.flush()

        dest_file.write(
            """\
from __future__ import annotations

import json
"""
        )
        dest_file.flush()

        # Get imports from source
        result = subprocess.run(
            [emend_cmd, "search", f"{source_file.name}::[imports]"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Failed to get imports: {result.stderr}"
        imports = result.stdout.strip().split('\n')

        # Add imports - should respect __future__ ordering
        for import_line in imports:
            if import_line:
                result = subprocess.run(
                    [emend_cmd, "add", f"{dest_file.name}::[imports]", import_line, "--apply"],
                    capture_output=True,
                    text=True,
                )
                assert result.returncode == 0, f"Failed to add import: {result.stderr}"

        content = Path(dest_file.name).read_text()
        lines = content.split('\n')
        # __future__ import must be first
        assert lines[0] == "from __future__ import annotations"
        assert "import os" in content
        assert "import sys" in content

    def test_copy_imports_mixed_styles(self, emend_cmd, temp_py_files):
        """Test copying mixed import styles."""
        source_file, dest_file = temp_py_files
        source_file.write(
            """\
import os
from pathlib import Path
import sys
from typing import List, Dict
"""
        )
        source_file.flush()

        dest_file.write(
            """\
def main():
    pass
"""
        )
        dest_file.flush()

        # Get imports from source
        result = subprocess.run(
            [emend_cmd, "search", f"{source_file.name}::[imports]"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Failed to get imports: {result.stderr}"
        imports = result.stdout.strip().split('\n')

        # Add each import
        for import_line in imports:
            if import_line:
                result = subprocess.run(
                    [emend_cmd, "add", f"{dest_file.name}::[imports]", import_line, "--apply"],
                    capture_output=True,
                    text=True,
                )
                assert result.returncode == 0, f"Failed to add import: {result.stderr}"

        content = Path(dest_file.name).read_text()
        assert "import os" in content
        assert "from pathlib import Path" in content
        assert "import sys" in content
        assert "from typing import List, Dict" in content

    def test_copy_imports_without_apply_shows_preview(self, emend_cmd, temp_py_files):
        """Test that without --apply, the command shows a preview."""
        source_file, dest_file = temp_py_files
        source_file.write(
            """\
import os
import sys
"""
        )
        source_file.flush()

        dest_file.write("")
        dest_file.flush()

        # Get imports from source
        result = subprocess.run(
            [emend_cmd, "search", f"{source_file.name}::[imports]"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Failed to get imports: {result.stderr}"
        imports = result.stdout.strip().split('\n')

        # Add first import without --apply to see preview
        result = subprocess.run(
            [emend_cmd, "add", f"{dest_file.name}::[imports]", imports[0]],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Failed to preview add: {result.stderr}"

        # File should not be modified
        content = Path(dest_file.name).read_text()
        assert content == ""

        # Preview should be in stdout
        assert imports[0] in result.stdout or "import os" in result.stdout

    def test_copy_imports_from_file_with_no_imports(self, emend_cmd, temp_py_files):
        """Test copying from a file with no imports."""
        source_file, dest_file = temp_py_files
        source_file.write(
            """\
def foo():
    pass
"""
        )
        source_file.flush()

        dest_file.write(
            """\
def bar():
    pass
"""
        )
        dest_file.flush()

        # Get imports from source (should be empty)
        result = subprocess.run(
            [emend_cmd, "search", f"{source_file.name}::[imports]"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Failed to get imports: {result.stderr}"
        imports = result.stdout.strip()

        # If no imports, destination should be unchanged
        if not imports or not imports.split('\n')[0]:
            content = Path(dest_file.name).read_text()
            assert "def bar():" in content
