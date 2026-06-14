"""Tests for nested symbol qualified name construction in refs.py.

Regression tests for the bug where find_references / find_callers / find_callees
constructed qualified names using only the last element of symbol_path, e.g.
"module.method" instead of "module.Class.method" for selector Class.method.
"""
from pathlib import Path

import pytest

from emend.component_selector import ExtendedSelector


def _make_nested_selector(file_path, *symbol_parts):
    """Create a selector with a multi-part symbol path (e.g. Class.method)."""
    return ExtendedSelector(
        file_path=str(file_path),
        symbol_path=list(symbol_parts),
        component=None,
        accessor=None,
    )


class TestNestedSymbolReferences:
    """find_references should use the full qualified name for nested symbols."""

    def test_refs_distinguishes_same_named_methods(self, tmp_path):
        """Searching for ClassA.process should not return ClassB.process refs."""
        from emend.transform import find_references

        project = tmp_path / "project"
        project.mkdir()

        module = project / "module.py"
        module.write_text(
            "class ClassA:\n"
            "    def process(self):\n"
            "        return 'A'\n"
            "\n"
            "class ClassB:\n"
            "    def process(self):\n"
            "        return 'B'\n"
            "\n"
            "a = ClassA()\n"
            "a.process()\n"
            "b = ClassB()\n"
            "b.process()\n"
        )

        selector = _make_nested_selector(module, "ClassA", "process")
        refs = list(find_references(selector, project_path=str(project)))

        ref_lines = {r.line for r in refs}

        assert 2 in ref_lines, (
            f"Expected ClassA.process definition on line 2, got lines {ref_lines}"
        )
        assert 6 not in ref_lines, (
            f"ClassB.process definition (line 6) should not appear when searching "
            f"for ClassA.process, got lines {ref_lines}"
        )


class TestNestedSymbolCallers:
    """find_callers should use the full qualified name for nested symbols."""

    def test_callers_distinguishes_same_named_methods(self, tmp_path):
        """Searching callers of ClassA.process should not return ClassB.process callers."""
        from emend.transform import find_callers

        project = tmp_path / "project"
        project.mkdir()

        module = project / "module.py"
        module.write_text(
            "class ClassA:\n"
            "    def process(self):\n"
            "        return 'A'\n"
            "\n"
            "class ClassB:\n"
            "    def process(self):\n"
            "        return 'B'\n"
            "\n"
            "def call_a():\n"
            "    a = ClassA()\n"
            "    a.process()\n"
            "\n"
            "def call_b():\n"
            "    b = ClassB()\n"
            "    b.process()\n"
        )

        selector = _make_nested_selector(module, "ClassA", "process")
        callers = list(find_callers(selector, project_path=str(project)))

        caller_lines = {r.line for r in callers}

        assert 15 not in caller_lines, (
            f"ClassB.process call (line 15) should not appear as a caller of "
            f"ClassA.process, got lines {caller_lines}"
        )


class TestNestedSymbolCallees:
    """find_callees should use the full qualified name for nested symbols."""

    def test_callees_distinguishes_same_named_methods(self, tmp_path):
        """Searching callees of ClassA.run should use full qualified name."""
        from emend.transform import find_callees

        project = tmp_path / "project"
        project.mkdir()

        module = project / "module.py"
        module.write_text(
            "def helper():\n"
            "    return 1\n"
            "\n"
            "class ClassA:\n"
            "    def run(self):\n"
            "        return helper()\n"
            "\n"
            "class ClassB:\n"
            "    def run(self):\n"
            "        return 999\n"
        )

        selector = _make_nested_selector(module, "ClassA", "run")
        callees = find_callees(selector, project_path=str(project))

        callee_names = {c.name for c in callees}
        assert "helper" in callee_names, (
            f"Expected 'helper' in callees of ClassA.run, got {callee_names}"
        )
