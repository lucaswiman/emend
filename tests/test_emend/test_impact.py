"""Tests for the impact command."""
import json
from pathlib import Path

import pytest

from emend.component_selector import ExtendedSelector


class TestFindImpact:
    """Tests for find_impact() in transform.py."""

    def test_impact_from_selector_direct_caller(self, tmp_path):
        """Impact analysis finds direct callers of a changed symbol."""
        from emend.transform import find_impact

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text(
            "def helper(x):\n"
            "    return x + 1\n"
        )

        app = project / "app.py"
        app.write_text(
            "from lib import helper\n"
            "\n"
            "def main():\n"
            "    return helper(42)\n"
        )

        selector = ExtendedSelector(
            file_path=str(lib),
            symbol_path=["helper"],
            component=None,
            accessor=None,
        )

        result = find_impact(selectors=[selector], project_path=str(project))

        assert len(result.changed_symbols) == 1
        assert "helper" in result.changed_symbols[0]

        # app.py::main should be impacted because it calls helper
        impacted_names = [s.split("::")[-1] for s in result.impacted_symbols]
        assert "main" in impacted_names

    def test_impact_transitive_closure(self, tmp_path):
        """Impact analysis computes transitive closure: A->B->C, changing C impacts A."""
        from emend.transform import find_impact

        project = tmp_path / "project"
        project.mkdir()

        c_file = project / "c_mod.py"
        c_file.write_text(
            "def deep_func():\n"
            "    return 42\n"
        )

        b_file = project / "b_mod.py"
        b_file.write_text(
            "from c_mod import deep_func\n"
            "\n"
            "def middle_func():\n"
            "    return deep_func()\n"
        )

        a_file = project / "a_mod.py"
        a_file.write_text(
            "from b_mod import middle_func\n"
            "\n"
            "def top_func():\n"
            "    return middle_func()\n"
        )

        selector = ExtendedSelector(
            file_path=str(c_file),
            symbol_path=["deep_func"],
            component=None,
            accessor=None,
        )

        result = find_impact(selectors=[selector], project_path=str(project))

        impacted_names = [s.split("::")[-1] for s in result.impacted_symbols]
        assert "middle_func" in impacted_names
        assert "top_func" in impacted_names

    def test_impact_detects_test_files(self, tmp_path):
        """Impact analysis identifies impacted test files."""
        from emend.transform import find_impact

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text(
            "def compute(x):\n"
            "    return x * 2\n"
        )

        test_file = project / "test_lib.py"
        test_file.write_text(
            "from lib import compute\n"
            "\n"
            "def test_compute():\n"
            "    assert compute(3) == 6\n"
        )

        selector = ExtendedSelector(
            file_path=str(lib),
            symbol_path=["compute"],
            component=None,
            accessor=None,
        )

        result = find_impact(selectors=[selector], project_path=str(project))

        # test_compute should be in impacted_tests
        assert len(result.impacted_tests) > 0
        test_selectors = " ".join(result.impacted_tests)
        assert "test_compute" in test_selectors or "test_lib" in test_selectors

    def test_impact_witness_edges(self, tmp_path):
        """Impact analysis returns witness edges explaining impact."""
        from emend.transform import find_impact

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text(
            "def target():\n"
            "    return 1\n"
        )

        caller = project / "caller.py"
        caller.write_text(
            "from lib import target\n"
            "\n"
            "def use_target():\n"
            "    return target()\n"
        )

        selector = ExtendedSelector(
            file_path=str(lib),
            symbol_path=["target"],
            component=None,
            accessor=None,
        )

        result = find_impact(selectors=[selector], project_path=str(project))

        # Should have at least one edge
        assert len(result.edges) > 0
        edge_kinds = {e.kind for e in result.edges}
        assert "calls" in edge_kinds

    def test_impact_no_callers(self, tmp_path):
        """Impact analysis returns empty impacted set when no callers exist."""
        from emend.transform import find_impact

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text(
            "def isolated_func():\n"
            "    return 42\n"
        )

        selector = ExtendedSelector(
            file_path=str(lib),
            symbol_path=["isolated_func"],
            component=None,
            accessor=None,
        )

        result = find_impact(selectors=[selector], project_path=str(project))

        assert len(result.changed_symbols) == 1
        assert len(result.impacted_symbols) == 0
        assert len(result.impacted_tests) == 0

    def test_impact_requires_input(self):
        """Impact analysis raises ValueError without selectors or diff_spec."""
        from emend.transform import find_impact

        with pytest.raises(ValueError, match="Either selectors or diff_spec"):
            find_impact()

    def test_impact_max_depth_limits_traversal(self, tmp_path):
        """Impact analysis respects max_depth limit."""
        from emend.transform import find_impact

        project = tmp_path / "project"
        project.mkdir()

        # Create a 3-level chain: c -> b -> a
        c = project / "c_mod.py"
        c.write_text("def level_c():\n    return 1\n")

        b = project / "b_mod.py"
        b.write_text(
            "from c_mod import level_c\n\n"
            "def level_b():\n    return level_c()\n"
        )

        a = project / "a_mod.py"
        a.write_text(
            "from b_mod import level_b\n\n"
            "def level_a():\n    return level_b()\n"
        )

        selector = ExtendedSelector(
            file_path=str(c),
            symbol_path=["level_c"],
            component=None,
            accessor=None,
        )

        # With max_depth=1, should find level_b but not level_a
        result = find_impact(
            selectors=[selector], project_path=str(project), max_depth=1
        )

        impacted_names = [s.split("::")[-1] for s in result.impacted_symbols]
        assert "level_b" in impacted_names
        assert "level_a" not in impacted_names

    def test_impact_json_output(self, tmp_path):
        """Impact result can be serialized to JSON via dataclass fields."""
        from emend.transform import find_impact, ImpactResult, ImpactEdge

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text(
            "def func():\n"
            "    return 1\n"
        )

        selector = ExtendedSelector(
            file_path=str(lib),
            symbol_path=["func"],
            component=None,
            accessor=None,
        )

        result = find_impact(selectors=[selector], project_path=str(project))

        # Verify the result can be serialized to JSON
        data = {
            "changed_symbols": result.changed_symbols,
            "impacted_symbols": result.impacted_symbols,
            "impacted_tests": result.impacted_tests,
            "edges": [
                {"source": e.source, "target": e.target, "kind": e.kind}
                for e in result.edges
            ],
        }
        json_str = json.dumps(data, indent=2)
        parsed = json.loads(json_str)
        assert "changed_symbols" in parsed
        assert "impacted_symbols" in parsed
        assert "impacted_tests" in parsed
        assert "edges" in parsed


class TestParseDiffToChangedFiles:
    """Tests for diff parsing helper."""

    def test_parse_simple_diff(self):
        """Parse a simple unified diff to extract file and lines."""
        from emend.transform import _parse_diff_to_changed_files

        diff_text = (
            "diff --git a/lib.py b/lib.py\n"
            "index abc..def 100644\n"
            "--- a/lib.py\n"
            "+++ b/lib.py\n"
            "@@ -1,3 +1,4 @@\n"
            " def foo():\n"
            "-    return 1\n"
            "+    x = 1\n"
            "+    return x\n"
        )

        result = _parse_diff_to_changed_files(diff_text)

        assert len(result) == 1
        file_path, lines = result[0]
        assert file_path == "lib.py"
        assert 1 in lines  # Lines in the hunk range

    def test_parse_multi_file_diff(self):
        """Parse a diff with multiple files."""
        from emend.transform import _parse_diff_to_changed_files

        diff_text = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -5,2 +5,3 @@\n"
            " some code\n"
            "+new line\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -10,1 +10,2 @@\n"
            "+another line\n"
        )

        result = _parse_diff_to_changed_files(diff_text)

        assert len(result) == 2
        files = {r[0] for r in result}
        assert files == {"a.py", "b.py"}

    def test_parse_empty_diff(self):
        """Parse an empty diff."""
        from emend.transform import _parse_diff_to_changed_files

        result = _parse_diff_to_changed_files("")
        assert result == []


class TestImpactHelpers:
    """Tests for _is_test_file and _is_test_symbol helpers."""

    def test_is_test_file_by_prefix(self):
        from emend.transform import _is_test_file
        assert _is_test_file("test_foo.py")
        assert _is_test_file("/path/to/test_bar.py")

    def test_is_test_file_by_suffix(self):
        from emend.transform import _is_test_file
        assert _is_test_file("foo_test.py")

    def test_is_test_file_by_directory(self):
        from emend.transform import _is_test_file
        assert _is_test_file("tests/conftest.py")
        assert _is_test_file("/project/tests/helpers.py")

    def test_is_not_test_file(self):
        from emend.transform import _is_test_file
        assert not _is_test_file("lib.py")
        assert not _is_test_file("/src/app.py")

    def test_is_test_symbol(self):
        from emend.transform import _is_test_symbol
        assert _is_test_symbol("test_foo.py::test_something")
        assert _is_test_symbol("foo.py::TestClass")

    def test_is_not_test_symbol(self):
        from emend.transform import _is_test_symbol
        assert not _is_test_symbol("foo.py::helper")
        assert not _is_test_symbol("lib.py::MyClass")


class TestImpactWithDiff:
    """Tests for impact analysis via git diff input."""

    def test_impact_diff_with_git_repo(self, tmp_path):
        """Impact analysis can parse a diff and find changed symbols."""
        import subprocess

        project = tmp_path / "project"
        project.mkdir()

        # Initialize git repo
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=str(project),
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(project), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(project), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=str(project), capture_output=True, check=True,
        )

        lib = project / "lib.py"
        lib.write_text("def greet():\n    return 'hello'\n")

        subprocess.run(
            ["git", "add", "."], cwd=str(project),
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(project), capture_output=True, check=True,
        )

        # Modify the file (unstaged change -- git diff HEAD will pick it up)
        lib.write_text("def greet():\n    return 'hi there'\n")

        from emend.transform import find_impact

        result = find_impact(diff_spec="HEAD", project_path=str(project))

        # Should detect greet as changed
        assert len(result.changed_symbols) >= 1
        changed_names = [s.split("::")[-1] for s in result.changed_symbols]
        assert "greet" in changed_names
