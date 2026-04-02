"""Phase 14: FactGraph.build_from_files() and intraprocedural trace on small file sets.

Tests that FactGraph can be constructed from an explicit list of source files
(not a project directory), populating symbol, CFG, and def-use facts correctly.
This is the prerequisite for making the Datalog intraprocedural trace engine
work on single-file fixtures.
"""

from __future__ import annotations

import pytest

from emend.fact_graph import FactGraph


class TestBuildFromFiles:
    """FactGraph.build_from_files() populates facts from explicit file lists."""

    def test_build_from_files_exists(self):
        """build_from_files is a callable classmethod."""
        assert callable(getattr(FactGraph, "build_from_files", None))

    def test_single_file_has_symbols(self, tmp_path):
        """A single Python file yields symbol facts."""
        src = tmp_path / "app.py"
        src.write_text("def handler():\n    pass\n")
        graph = FactGraph.build_from_files([str(src)])
        symbols = graph.symbols()
        names = [s.name for s in symbols]
        assert "handler" in names

    def test_single_file_has_cfg_blocks(self, tmp_path):
        """A single Python file yields CFG block facts."""
        src = tmp_path / "app.py"
        src.write_text("def handler():\n    x = 1\n    return x\n")
        graph = FactGraph.build_from_files([str(src)])
        blocks = graph.cfg_blocks()
        assert len(blocks) > 0, "Should have at least one CFG block"

    def test_single_file_has_def_use_facts(self, tmp_path):
        """A single Python file with assignment yields def-use facts."""
        src = tmp_path / "app.py"
        src.write_text("def handler():\n    x = 1\n    return x\n")
        graph = FactGraph.build_from_files([str(src)])
        du = graph.def_uses()
        var_names = [d.var_name for d in du]
        assert "x" in var_names, f"Expected 'x' in def-use facts, got {var_names}"

    def test_single_file_has_cfg_edges(self, tmp_path):
        """A single Python file with branching yields CFG edge facts."""
        src = tmp_path / "app.py"
        src.write_text(
            "def handler(cond):\n"
            "    if cond:\n"
            "        x = 1\n"
            "    else:\n"
            "        x = 2\n"
            "    return x\n"
        )
        graph = FactGraph.build_from_files([str(src)])
        edges = graph.cfg_edges()
        assert len(edges) > 0, "Should have CFG edges for branching code"

    def test_multiple_files(self, tmp_path):
        """Multiple files are all indexed."""
        f1 = tmp_path / "a.py"
        f1.write_text("def foo():\n    pass\n")
        f2 = tmp_path / "b.py"
        f2.write_text("def bar():\n    pass\n")
        graph = FactGraph.build_from_files([str(f1), str(f2)])
        names = [s.name for s in graph.symbols()]
        assert "foo" in names
        assert "bar" in names

    def test_paths_are_absolute(self, tmp_path):
        """File paths in facts are absolute so callers can look up by abs path."""
        src = tmp_path / "app.py"
        src.write_text("def handler():\n    pass\n")
        graph = FactGraph.build_from_files([str(src)])
        symbols = graph.symbols()
        file_paths = {s.file_path for s in symbols}
        # Should contain the resolved absolute path
        assert str(src.resolve()) in file_paths
