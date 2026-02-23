"""Tests for the callees command."""
import pytest

from emend.component_selector import ExtendedSelector


class TestFindCallees:
    """Tests for find_callees() in transform.py."""

    def test_callees_basic(self, tmp_path):
        """callees finds functions called by target."""
        from emend.transform import find_callees

        project = tmp_path / "project"
        project.mkdir()

        module = project / "module.py"
        module.write_text(
            "def helper():\n"
            "    return 42\n"
            "\n"
            "def utility():\n"
            "    return 99\n"
            "\n"
            "def main():\n"
            "    a = helper()\n"
            "    b = utility()\n"
            "    return a + b\n"
        )

        selector = ExtendedSelector(
            file_path=str(module),
            symbol_path=["main"],
            component=None,
            accessor=None,
        )

        callees = find_callees(selector, project_path=str(project))
        callee_names = {c.name for c in callees}
        assert "helper" in callee_names, f"Expected 'helper' in callees, got {callee_names}"
        assert "utility" in callee_names, f"Expected 'utility' in callees, got {callee_names}"

    def test_callees_cross_module(self, tmp_path):
        """callees finds imported functions called by the target."""
        from emend.transform import find_callees

        project = tmp_path / "project"
        project.mkdir()

        helpers = project / "helpers.py"
        helpers.write_text(
            "def compute(x):\n"
            "    return x * 2\n"
        )

        main = project / "main.py"
        main.write_text(
            "from helpers import compute\n"
            "\n"
            "def run():\n"
            "    return compute(21)\n"
        )

        selector = ExtendedSelector(
            file_path=str(main),
            symbol_path=["run"],
            component=None,
            accessor=None,
        )

        callees = find_callees(selector, project_path=str(project))
        callee_names = {c.name for c in callees}
        assert "compute" in callee_names, f"Expected 'compute' in callees, got {callee_names}"

    def test_callees_no_calls(self, tmp_path):
        """callees returns empty list for function with no calls."""
        from emend.transform import find_callees

        project = tmp_path / "project"
        project.mkdir()

        module = project / "module.py"
        module.write_text(
            "def pure():\n"
            "    return 42\n"
        )

        selector = ExtendedSelector(
            file_path=str(module),
            symbol_path=["pure"],
            component=None,
            accessor=None,
        )

        callees = find_callees(selector, project_path=str(project))
        assert len(callees) == 0

    def test_callees_method_calls(self, tmp_path):
        """callees finds method calls inside a function."""
        from emend.transform import find_callees

        project = tmp_path / "project"
        project.mkdir()

        module = project / "module.py"
        module.write_text(
            "def process(items):\n"
            "    result = items.copy()\n"
            "    result.append(42)\n"
            "    return result\n"
        )

        selector = ExtendedSelector(
            file_path=str(module),
            symbol_path=["process"],
            component=None,
            accessor=None,
        )

        callees = find_callees(selector, project_path=str(project))
        callee_names = {c.name for c in callees}
        assert "copy" in callee_names or "append" in callee_names, \
            f"Expected method calls in callees, got {callee_names}"


