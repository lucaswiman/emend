"""Tests for copy-imports functionality using get + add primitives."""

import ast
import subprocess
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_py_files():
    """Create two temporary Python files for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as source_file:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as dest_file:
            yield source_file, dest_file
    Path(source_file.name).unlink(missing_ok=True)
    Path(dest_file.name).unlink(missing_ok=True)


class TestCopyImportsPrimitives:
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


class TestResolveRelativeModuleInPackageInit:
    """Relative imports inside ``__init__.py`` must resolve to the package.

    ``_file_to_module`` already collapses ``pkg/__init__.py`` to ``pkg``, so
    the module name *is* the package. The old ``.__init__`` suffix check was
    dead code and the generic branch stripped one component too many,
    resolving ``from . import helpers`` in ``pkg/__init__.py`` to ``helpers``
    instead of ``pkg.helpers`` — an unimportable name.
    """

    def _resolve(self, level, module, source_file):
        from emend.transform.patterns import _resolve_relative_module

        return _resolve_relative_module(level, module, source_file, ".")

    def test_single_dot_in_package_init(self):
        assert self._resolve(1, "helpers", "pkg/__init__.py") == "pkg.helpers"

    def test_single_dot_in_nested_package_init(self):
        assert self._resolve(1, "helpers", "pkg/sub/__init__.py") == "pkg.sub.helpers"

    def test_single_dot_in_module_unchanged(self):
        assert self._resolve(1, "helpers", "pkg/mod.py") == "pkg.helpers"

    def test_single_dot_in_nested_module_unchanged(self):
        assert self._resolve(1, "helpers", "pkg/sub/mod.py") == "pkg.sub.helpers"

    def test_double_dot_from_nested_module(self):
        assert self._resolve(2, "helpers", "pkg/sub/mod.py") == "pkg.helpers"

    def test_double_dot_from_nested_package_init(self):
        assert self._resolve(2, "helpers", "pkg/sub/__init__.py") == "pkg.helpers"

    def test_bare_from_dot_import(self):
        assert self._resolve(1, "", "pkg/mod.py") == "pkg"

    def test_too_many_dots_falls_back(self):
        assert self._resolve(5, "helpers", "pkg/mod.py") == "helpers"
