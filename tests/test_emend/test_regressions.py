"""Tests for known bugs documented in TODOS.md.

These tests were written TDD-style: added as failing tests first,
then fixed one at a time.
"""
import subprocess
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Bug 1: rename_symbol and find_references are name-based, not scope-aware
# ---------------------------------------------------------------------------

class TestScopeAwareRename:
    """rename_symbol should only rename symbols that refer to the target,
    not coincidental same-named symbols in other files."""

    def test_rename_does_not_touch_unrelated_same_named_symbol(self, tmp_path):
        """Renaming file_a::process should not rename file_b's own process."""
        from emend.transform import rename_symbol
        from emend.component_selector import ExtendedSelector

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # file_a defines process()
        file_a = project_dir / "file_a.py"
        file_a.write_text(
            "def process():\n"
            "    return 'A'\n"
        )

        # file_b has its own independent process() — should NOT be renamed
        file_b = project_dir / "file_b.py"
        file_b.write_text(
            "def process():\n"
            "    return 'B'\n"
            "\n"
            "result = process()\n"
        )

        # file_c imports from file_a — SHOULD be renamed
        file_c = project_dir / "file_c.py"
        file_c.write_text(
            "from file_a import process\n"
            "\n"
            "result = process()\n"
        )

        selector = ExtendedSelector(
            file_path=str(file_a),
            symbol_path=["process"],
            component=None,
            accessor=None,
        )

        diffs = rename_symbol(selector, "handle", project_path=str(project_dir), apply=True)

        # file_a: definition renamed
        content_a = file_a.read_text()
        assert "def handle():" in content_a
        assert "process" not in content_a

        # file_b: NOT renamed (independent symbol)
        content_b = file_b.read_text()
        assert "def process():" in content_b, "file_b's own process() should not be renamed"
        assert "result = process()" in content_b, "file_b's process() usage should not be renamed"

        # file_c: renamed (imports from file_a)
        content_c = file_c.read_text()
        assert "from file_a import handle" in content_c
        assert "result = handle()" in content_c

    def test_rename_does_not_touch_different_scope_in_same_file(self, tmp_path):
        """Renaming a module-level function should not rename
        a parameter with the same name in another function."""
        from emend.transform import rename_symbol
        from emend.component_selector import ExtendedSelector

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        file = project_dir / "module.py"
        file.write_text(
            "def process():\n"
            "    return 42\n"
            "\n"
            "def bar(process):\n"
            "    return process + 1\n"
            "\n"
            "result = process()\n"
        )

        selector = ExtendedSelector(
            file_path=str(file),
            symbol_path=["process"],
            component=None,
            accessor=None,
        )

        diffs = rename_symbol(selector, "handle", project_path=str(project_dir), apply=True)

        content = file.read_text()
        assert "def handle():" in content, "Module-level process should be renamed"
        assert "result = handle()" in content, "Module-level call should be renamed"
        assert "def bar(process):" in content, "Parameter 'process' should NOT be renamed"
        assert "return process + 1" in content, "Parameter usage should NOT be renamed"


class TestScopeAwareFindReferences:
    """find_references should only return references to the actual target symbol."""

    def test_find_references_ignores_unrelated_same_named_symbols(self, tmp_path):
        """find_references for file_a::process should not include
        file_b's independent process."""
        from emend.transform import find_references
        from emend.component_selector import ExtendedSelector

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        file_a = project_dir / "file_a.py"
        file_a.write_text(
            "def process():\n"
            "    return 'A'\n"
        )

        file_b = project_dir / "file_b.py"
        file_b.write_text(
            "def process():\n"
            "    return 'B'\n"
            "\n"
            "result = process()\n"
        )

        file_c = project_dir / "file_c.py"
        file_c.write_text(
            "from file_a import process\n"
            "\n"
            "result = process()\n"
        )

        selector = ExtendedSelector(
            file_path=str(file_a),
            symbol_path=["process"],
            component=None,
            accessor=None,
        )

        refs = find_references(selector, project_path=str(project_dir))

        ref_files = {Path(r.file_path).resolve() for r in refs}

        # Should include file_a (definition) and file_c (imports from file_a)
        assert file_a.resolve() in ref_files, "Definition file should have references"
        assert file_c.resolve() in ref_files, "Importing file should have references"

        # Should NOT include file_b (has its own independent process)
        assert file_b.resolve() not in ref_files, \
            "file_b has its own process() — should not appear in references"


# ---------------------------------------------------------------------------
# Bug 2: --docs flag is a no-op
# ---------------------------------------------------------------------------

class TestRenameDocsFlag:
    """The --docs flag should cause renaming inside docstrings."""

    def test_rename_with_docs_updates_docstrings(self, tmp_path):
        """When docs=True, occurrences of the old name in docstrings
        should be replaced."""
        from emend.transform import rename_symbol
        from emend.component_selector import ExtendedSelector

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        file = project_dir / "module.py"
        file.write_text(
            'def old_func():\n'
            '    """This is old_func. Call old_func() to do things."""\n'
            '    return 42\n'
        )

        selector = ExtendedSelector(
            file_path=str(file),
            symbol_path=["old_func"],
            component=None,
            accessor=None,
        )

        rename_symbol(selector, "new_func", project_path=str(project_dir),
                       docs=True, apply=True)

        content = file.read_text()
        assert "def new_func():" in content
        assert "This is new_func" in content, "Docstring should be updated"
        assert "Call new_func()" in content, "Docstring should be updated"
        assert "old_func" not in content

    def test_rename_without_docs_preserves_docstrings(self, tmp_path):
        """When docs=False (default), docstrings should NOT be modified."""
        from emend.transform import rename_symbol
        from emend.component_selector import ExtendedSelector

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        file = project_dir / "module.py"
        file.write_text(
            'def old_func():\n'
            '    """This is old_func documentation."""\n'
            '    return 42\n'
        )

        selector = ExtendedSelector(
            file_path=str(file),
            symbol_path=["old_func"],
            component=None,
            accessor=None,
        )

        rename_symbol(selector, "new_func", project_path=str(project_dir),
                       docs=False, apply=True)

        content = file.read_text()
        assert "def new_func():" in content
        # Docstring should still contain the old name
        assert "This is old_func documentation." in content

    def test_rename_docs_handles_class_docstrings(self, tmp_path):
        """--docs should also update class-level docstrings."""
        from emend.transform import rename_symbol
        from emend.component_selector import ExtendedSelector

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        file = project_dir / "module.py"
        file.write_text(
            'class OldClass:\n'
            '    """OldClass provides functionality.\n'
            '\n'
            '    Use OldClass() to create an instance.\n'
            '    """\n'
            '    pass\n'
        )

        selector = ExtendedSelector(
            file_path=str(file),
            symbol_path=["OldClass"],
            component=None,
            accessor=None,
        )

        rename_symbol(selector, "NewClass", project_path=str(project_dir),
                       docs=True, apply=True)

        content = file.read_text()
        assert "class NewClass:" in content
        assert "NewClass provides functionality." in content
        assert "Use NewClass() to create an instance." in content
        assert "OldClass" not in content

    def test_rename_docs_handles_module_docstring_in_other_file(self, tmp_path):
        """--docs should update docstrings in importing files too."""
        from emend.transform import rename_symbol
        from emend.component_selector import ExtendedSelector

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        file_a = project_dir / "helpers.py"
        file_a.write_text(
            "def compute():\n"
            "    return 42\n"
        )

        file_b = project_dir / "main.py"
        file_b.write_text(
            '"""Module that uses compute from helpers."""\n'
            "from helpers import compute\n"
            "\n"
            "def run():\n"
            '    """Calls compute to get the answer."""\n'
            "    return compute()\n"
        )

        selector = ExtendedSelector(
            file_path=str(file_a),
            symbol_path=["compute"],
            component=None,
            accessor=None,
        )

        rename_symbol(selector, "calculate", project_path=str(project_dir),
                       docs=True, apply=True)

        content_b = file_b.read_text()
        assert "from helpers import calculate" in content_b
        assert "Calls calculate to get the answer." in content_b
        assert "uses calculate from helpers." in content_b


# ---------------------------------------------------------------------------
# Bug 3: list-symbols shows incomplete signatures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_py_file():
    """Create a temporary Python file for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        yield f
    Path(f.name).unlink(missing_ok=True)


class TestListSymbolsCompleteSignatures:
    """list-symbols should show complete signatures including all param types."""

    def test_full_signature_all_param_types(self, emend_cmd, temp_py_file):
        """def f(a, /, b, *args, c=1, **kw) should show the complete signature."""
        temp_py_file.write(
            "def f(a, /, b, *args, c=1, **kw):\n"
            "    pass\n"
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "list-symbols", temp_py_file.name],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"

        # The signature should include all parameter types
        output = result.stdout
        assert "a, /" in output, f"Missing positional-only separator in: {output}"
        assert "*args" in output, f"Missing *args in: {output}"
        assert "c = 1" in output or "c=1" in output, f"Missing kwonly param in: {output}"
        assert "**kw" in output, f"Missing **kwargs in: {output}"

    def test_kwonly_params_with_star_separator(self, emend_cmd, temp_py_file):
        """def f(a, *, key=None, verbose=False) should show the * separator."""
        temp_py_file.write(
            "def f(a, *, key=None, verbose=False):\n"
            "    pass\n"
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "list-symbols", temp_py_file.name],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"
        output = result.stdout
        # Should show the * separator and keyword-only params
        assert "*" in output, f"Missing * separator in: {output}"
        assert "key" in output, f"Missing kwonly param 'key' in: {output}"
        assert "verbose" in output, f"Missing kwonly param 'verbose' in: {output}"

    def test_posonly_params(self, emend_cmd, temp_py_file):
        """def f(x, y, /) should show positional-only params with / separator."""
        temp_py_file.write(
            "def f(x, y, /):\n"
            "    pass\n"
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "list-symbols", temp_py_file.name],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"
        output = result.stdout
        assert "x" in output, f"Missing posonly param 'x' in: {output}"
        assert "y" in output, f"Missing posonly param 'y' in: {output}"
        assert "/" in output, f"Missing / separator in: {output}"

    def test_vararg_only(self, emend_cmd, temp_py_file):
        """def f(*args) should show *args in the signature."""
        temp_py_file.write(
            "def f(*args):\n"
            "    pass\n"
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "list-symbols", temp_py_file.name],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"
        output = result.stdout
        assert "*args" in output, f"Missing *args in: {output}"

    def test_kwarg_only(self, emend_cmd, temp_py_file):
        """def f(**kwargs) should show **kwargs in the signature."""
        temp_py_file.write(
            "def f(**kwargs):\n"
            "    pass\n"
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "list-symbols", temp_py_file.name],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"
        output = result.stdout
        assert "**kwargs" in output, f"Missing **kwargs in: {output}"

    def test_annotated_kwonly(self, emend_cmd, temp_py_file):
        """Keyword-only params with annotations should display correctly."""
        temp_py_file.write(
            "def f(a: int, *, key: str = 'default') -> bool:\n"
            "    pass\n"
        )
        temp_py_file.flush()

        result = subprocess.run(
            [emend_cmd, "list-symbols", temp_py_file.name],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Failed with stderr: {result.stderr}"
        output = result.stdout
        assert "a: int" in output, f"Missing annotated param in: {output}"
        assert "key: str" in output, f"Missing annotated kwonly param in: {output}"
        assert "-> bool" in output, f"Missing return type in: {output}"
