"""Tests for project file iteration helpers in ``emend.transform.project_iter``."""


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
