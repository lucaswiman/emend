"""Tests for the visit_project() abstraction.

Verifies that find_references and rename_symbol continue to work
identically after refactoring to use visit_project().
"""
import pytest

from emend.component_selector import ExtendedSelector


class TestVisitProjectRefactorFindReferences:
    """Verify find_references still works after refactoring to use visit_project."""

    def test_find_references_basic(self, tmp_path):
        """Basic find_references still works."""
        from emend.transform import find_references

        project = tmp_path / "project"
        project.mkdir()

        target = project / "target.py"
        target.write_text(
            "def process():\n"
            "    return 42\n"
            "\n"
            "result = process()\n"
        )

        selector = ExtendedSelector(
            file_path=str(target),
            symbol_path=["process"],
            component=None,
            accessor=None,
        )

        refs = list(find_references(selector, project_path=str(project)))
        assert len(refs) >= 2  # definition + usage

    def test_find_references_cross_file(self, tmp_path):
        """Cross-file find_references still works."""
        from emend.transform import find_references

        project = tmp_path / "project"
        project.mkdir()

        file_a = project / "file_a.py"
        file_a.write_text(
            "def helper():\n"
            "    return 42\n"
        )

        file_b = project / "file_b.py"
        file_b.write_text(
            "from file_a import helper\n"
            "\n"
            "result = helper()\n"
        )

        selector = ExtendedSelector(
            file_path=str(file_a),
            symbol_path=["helper"],
            component=None,
            accessor=None,
        )

        refs = find_references(selector, project_path=str(project))
        ref_files = {r.file_path for r in refs}
        assert any("file_a.py" in f for f in ref_files)
        assert any("file_b.py" in f for f in ref_files)

    def test_find_references_scope_aware(self, tmp_path):
        """Scope-aware find_references still works after refactor."""
        from emend.transform import find_references

        project = tmp_path / "project"
        project.mkdir()

        file_a = project / "file_a.py"
        file_a.write_text(
            "def process():\n"
            "    return 'A'\n"
        )

        file_b = project / "file_b.py"
        file_b.write_text(
            "def process():\n"
            "    return 'B'\n"
        )

        selector = ExtendedSelector(
            file_path=str(file_a),
            symbol_path=["process"],
            component=None,
            accessor=None,
        )

        refs = find_references(selector, project_path=str(project))
        ref_files = {r.file_path for r in refs}
        assert not any("file_b.py" in f for f in ref_files)


class TestVisitProjectRefactorRenameSymbol:
    """Verify rename_symbol still works after refactoring to use visit_project."""

    def test_rename_basic(self, tmp_path):
        """Basic rename_symbol still works."""
        from emend.transform import rename_symbol

        project = tmp_path / "project"
        project.mkdir()

        target = project / "target.py"
        target.write_text(
            "def old_func():\n"
            "    return 42\n"
            "\n"
            "result = old_func()\n"
        )

        selector = ExtendedSelector(
            file_path=str(target),
            symbol_path=["old_func"],
            component=None,
            accessor=None,
        )

        diffs = rename_symbol(selector, "new_func", project_path=str(project), apply=True)
        content = target.read_text()
        assert "def new_func():" in content
        assert "result = new_func()" in content

    def test_rename_cross_file(self, tmp_path):
        """Cross-file rename still works."""
        from emend.transform import rename_symbol

        project = tmp_path / "project"
        project.mkdir()

        file_a = project / "file_a.py"
        file_a.write_text(
            "def helper():\n"
            "    return 42\n"
        )

        file_b = project / "file_b.py"
        file_b.write_text(
            "from file_a import helper\n"
            "\n"
            "result = helper()\n"
        )

        selector = ExtendedSelector(
            file_path=str(file_a),
            symbol_path=["helper"],
            component=None,
            accessor=None,
        )

        rename_symbol(selector, "utility", project_path=str(project), apply=True)

        content_a = file_a.read_text()
        assert "def utility():" in content_a

        content_b = file_b.read_text()
        assert "from file_a import utility" in content_b
        assert "result = utility()" in content_b

    def test_rename_scope_aware(self, tmp_path):
        """Scope-aware rename still works after refactor."""
        from emend.transform import rename_symbol

        project = tmp_path / "project"
        project.mkdir()

        file_a = project / "file_a.py"
        file_a.write_text(
            "def process():\n"
            "    return 'A'\n"
        )

        file_b = project / "file_b.py"
        file_b.write_text(
            "def process():\n"
            "    return 'B'\n"
        )

        selector = ExtendedSelector(
            file_path=str(file_a),
            symbol_path=["process"],
            component=None,
            accessor=None,
        )

        rename_symbol(selector, "handle", project_path=str(project), apply=True)

        content_a = file_a.read_text()
        assert "def handle():" in content_a

        content_b = file_b.read_text()
        assert "def process():" in content_b, "file_b should be unchanged"


class TestVisitProjectHelper:
    """Test the visit_project_ts() helper directly."""

    def test_visit_project_ts_yields_results(self, tmp_path):
        """visit_project_ts iterates over matching files."""
        from emend.transform import visit_project_ts

        project = tmp_path / "project"
        project.mkdir()

        file_a = project / "file_a.py"
        file_a.write_text("x = 1\n")

        file_b = project / "file_b.py"
        file_b.write_text("y = 2\n")

        results = list(visit_project_ts(
            name_hint="x",
            project_path=str(project),
        ))
        # Should find file_a (contains "x") but not file_b
        assert len(results) == 1
        assert "file_a.py" in results[0][0]

    def test_visit_project_ts_skips_non_matching(self, tmp_path):
        """visit_project_ts skips files that don't contain name_hint."""
        from emend.transform import visit_project_ts

        project = tmp_path / "project"
        project.mkdir()

        file_a = project / "file_a.py"
        file_a.write_text("def foo(): pass\n")

        file_b = project / "file_b.py"
        file_b.write_text("def bar(): pass\n")

        results = list(visit_project_ts(
            name_hint="nonexistent",
            project_path=str(project),
        ))
        assert len(results) == 0


class TestFileToModule:
    """Tests for _file_to_module helper."""

    def test_init_py_maps_to_package_name(self, tmp_path):
        """__init__.py should map to the package name, not pkg.__init__."""
        from emend.transform.project_iter import _file_to_module
        project = tmp_path / "project"
        pkg = project / "mypkg"
        pkg.mkdir(parents=True)
        init_file = pkg / "__init__.py"
        init_file.write_text("# package init\n")

        result = _file_to_module(str(init_file), str(project))
        assert result == "mypkg", f"Expected 'mypkg', got '{result}'"

    def test_init_py_nested_package(self, tmp_path):
        """Nested __init__.py should map to dotted package name."""
        from emend.transform.project_iter import _file_to_module
        project = tmp_path / "project"
        pkg = project / "mypkg" / "sub"
        pkg.mkdir(parents=True)
        init_file = pkg / "__init__.py"
        init_file.write_text("# sub package init\n")

        result = _file_to_module(str(init_file), str(project))
        assert result == "mypkg.sub", f"Expected 'mypkg.sub', got '{result}'"

    def test_regular_module_unchanged(self, tmp_path):
        """Regular .py files should still work normally."""
        from emend.transform.project_iter import _file_to_module
        project = tmp_path / "project"
        pkg = project / "mypkg"
        pkg.mkdir(parents=True)
        mod_file = pkg / "utils.py"
        mod_file.write_text("# utils\n")

        result = _file_to_module(str(mod_file), str(project))
        assert result == "mypkg.utils"
