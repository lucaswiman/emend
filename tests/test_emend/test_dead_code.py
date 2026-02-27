"""Tests for the dead-code detection command."""
import json
from pathlib import Path

import pytest

from emend.component_selector import ExtendedSelector


class TestFindDeadCode:
    """Tests for find_dead_code() in transform.py."""

    def test_finds_unreferenced_function(self, tmp_path):
        """A function with no references is flagged as dead."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def used_func():\n"
            "    return 1\n"
            "\n"
            "def unused_func():\n"
            "    return 2\n"
            "\n"
            "result = used_func()\n"
        )

        dead = find_dead_code(str(project))
        dead_names = {d.name for d in dead}
        assert "unused_func" in dead_names
        assert "used_func" not in dead_names

    def test_finds_unreferenced_class(self, tmp_path):
        """A class with no references is flagged as dead."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "class UsedClass:\n"
            "    pass\n"
            "\n"
            "class UnusedClass:\n"
            "    pass\n"
            "\n"
            "obj = UsedClass()\n"
        )

        dead = find_dead_code(str(project))
        dead_names = {d.name for d in dead}
        assert "UnusedClass" in dead_names
        assert "UsedClass" not in dead_names

    def test_cross_file_reference_not_dead(self, tmp_path):
        """A function referenced from another file is not dead."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        lib_file = project / "lib.py"
        lib_file.write_text(
            "def helper():\n"
            "    return 42\n"
            "\n"
            "def orphan():\n"
            "    return 0\n"
        )

        user_file = project / "user.py"
        user_file.write_text(
            "from lib import helper\n"
            "\n"
            "result = helper()\n"
        )

        dead = find_dead_code(str(project))
        dead_names = {d.name for d in dead}
        assert "orphan" in dead_names
        assert "helper" not in dead_names

    def test_skips_dunder_methods(self, tmp_path):
        """Dunder methods like __init__ are never flagged."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def __init__():\n"
            "    pass\n"
        )

        dead = find_dead_code(str(project))
        dead_names = {d.name for d in dead}
        assert "__init__" not in dead_names

    def test_skips_test_functions(self, tmp_path):
        """Functions starting with test_ are never flagged."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        test_file = project / "test_example.py"
        test_file.write_text(
            "def test_something():\n"
            "    assert True\n"
            "\n"
            "class TestSuite:\n"
            "    pass\n"
        )

        dead = find_dead_code(str(project))
        dead_names = {d.name for d in dead}
        assert "test_something" not in dead_names
        assert "TestSuite" not in dead_names

    def test_skips_main(self, tmp_path):
        """The 'main' function is never flagged."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def main():\n"
            "    pass\n"
        )

        dead = find_dead_code(str(project))
        dead_names = {d.name for d in dead}
        assert "main" not in dead_names

    def test_skips_private_by_default(self, tmp_path):
        """Private symbols (_name) are skipped by default."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def _private_helper():\n"
            "    return 1\n"
            "\n"
            "def public_unused():\n"
            "    return 2\n"
        )

        dead = find_dead_code(str(project))
        dead_names = {d.name for d in dead}
        assert "_private_helper" not in dead_names
        assert "public_unused" in dead_names

    def test_include_private(self, tmp_path):
        """With include_private=True, _private symbols are checked."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def _private_helper():\n"
            "    return 1\n"
        )

        dead = find_dead_code(str(project), include_private=True)
        dead_names = {d.name for d in dead}
        assert "_private_helper" in dead_names

    def test_skips_all_exports(self, tmp_path):
        """Symbols listed in __all__ are not flagged."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "__all__ = ['exported_func']\n"
            "\n"
            "def exported_func():\n"
            "    return 1\n"
            "\n"
            "def not_exported():\n"
            "    return 2\n"
        )

        dead = find_dead_code(str(project))
        dead_names = {d.name for d in dead}
        assert "exported_func" not in dead_names
        assert "not_exported" in dead_names

    def test_kind_filter_function(self, tmp_path):
        """With kind='function', only functions are checked."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def unused_func():\n"
            "    return 1\n"
            "\n"
            "class UnusedClass:\n"
            "    pass\n"
        )

        dead = find_dead_code(str(project), kind="function")
        dead_names = {d.name for d in dead}
        assert "unused_func" in dead_names
        assert "UnusedClass" not in dead_names

    def test_kind_filter_class(self, tmp_path):
        """With kind='class', only classes are checked."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def unused_func():\n"
            "    return 1\n"
            "\n"
            "class UnusedClass:\n"
            "    pass\n"
        )

        dead = find_dead_code(str(project), kind="class")
        dead_names = {d.name for d in dead}
        assert "UnusedClass" in dead_names
        assert "unused_func" not in dead_names

    def test_returns_correct_fields(self, tmp_path):
        """DeadSymbol has correct file_path, name, kind, line, selector."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def orphan():\n"
            "    return 42\n"
        )

        dead = find_dead_code(str(project))
        assert len(dead) == 1
        d = dead[0]
        assert d.name == "orphan"
        assert d.kind == "function"
        assert d.line == 1
        assert "main.py" in d.file_path
        assert "orphan" in d.selector
        assert d.reason == "no references found"

    def test_empty_project(self, tmp_path):
        """An empty project returns no dead code."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        dead = find_dead_code(str(project))
        assert dead == []

    def test_skips_decorated_entry_points(self, tmp_path):
        """Functions decorated with framework decorators are skipped."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def route(f):\n"
            "    return f\n"
            "\n"
            "@route\n"
            "def my_handler():\n"
            "    return 'hello'\n"
            "\n"
            "def truly_unused():\n"
            "    return 'bye'\n"
        )

        dead = find_dead_code(str(project))
        dead_names = {d.name for d in dead}
        assert "my_handler" not in dead_names
        assert "truly_unused" in dead_names

    def test_sorted_output(self, tmp_path):
        """Results are sorted by file path then line number."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        file_b = project / "b.py"
        file_b.write_text(
            "def zeta():\n"
            "    pass\n"
            "\n"
            "def alpha():\n"
            "    pass\n"
        )

        file_a = project / "a.py"
        file_a.write_text(
            "def gamma():\n"
            "    pass\n"
        )

        dead = find_dead_code(str(project))
        # Should be sorted: a.py first, then b.py, then by line within file
        assert len(dead) >= 3
        file_paths = [d.file_path for d in dead]
        assert file_paths == sorted(file_paths) or all(
            (file_paths[i], dead[i].line) <= (file_paths[i + 1], dead[i + 1].line)
            for i in range(len(dead) - 1)
        )

    def test_only_top_level_symbols(self, tmp_path):
        """Only top-level symbols are checked, not nested methods."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "class MyClass:\n"
            "    def unused_method(self):\n"
            "        pass\n"
            "\n"
            "obj = MyClass()\n"
        )

        dead = find_dead_code(str(project))
        dead_names = {d.name for d in dead}
        # Methods are nested (depth > 1), so should not be checked
        assert "unused_method" not in dead_names
        # MyClass is used, so not dead
        assert "MyClass" not in dead_names


class TestDeadCodeCLI:
    """Tests for the dead-code CLI command."""

    def test_cli_finds_dead_code(self, tmp_path, run_emend_cmd):
        """CLI command reports dead code."""
        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def used_func():\n"
            "    return 1\n"
            "\n"
            "def dead_func():\n"
            "    return 2\n"
            "\n"
            "result = used_func()\n"
        )

        result = run_emend_cmd(["dead-code", str(project)])
        assert "dead_func" in result.stdout
        assert "used_func" not in result.stdout

    def test_cli_json_output(self, tmp_path, run_emend_cmd):
        """CLI --json produces valid JSON output."""
        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def orphan():\n"
            "    return 42\n"
        )

        result = run_emend_cmd(["dead-code", str(project), "--json"])
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "orphan"
        assert data[0]["kind"] == "function"
        assert data[0]["line"] == 1

    def test_cli_kind_filter(self, tmp_path, run_emend_cmd):
        """CLI --kind filters to specific symbol types."""
        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def unused_func():\n"
            "    pass\n"
            "\n"
            "class UnusedClass:\n"
            "    pass\n"
        )

        result = run_emend_cmd(["dead-code", str(project), "--kind", "function"])
        assert "unused_func" in result.stdout
        assert "UnusedClass" not in result.stdout

    def test_cli_no_dead_code(self, tmp_path, run_emend_cmd):
        """CLI reports 'No dead code found.' when everything is used."""
        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def helper():\n"
            "    return 1\n"
            "\n"
            "result = helper()\n"
        )

        result = run_emend_cmd(["dead-code", str(project)])
        assert "No dead code found" in result.stdout

    def test_cli_include_private(self, tmp_path, run_emend_cmd):
        """CLI --include-private flag includes private symbols."""
        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def _private_unused():\n"
            "    return 1\n"
        )

        # Without --include-private
        result = run_emend_cmd(["dead-code", str(project)])
        assert "_private_unused" not in result.stdout

        # With --include-private
        result = run_emend_cmd(["dead-code", str(project), "--include-private"])
        assert "_private_unused" in result.stdout
