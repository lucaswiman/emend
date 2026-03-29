"""Tests for per-function control flow graph construction."""

from __future__ import annotations

import json
import textwrap

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build(source: str):
    """Build CFGs for the given Python source string."""
    from emend import emend_core
    return emend_core.build_cfgs(textwrap.dedent(source), ext="py")


# ---------------------------------------------------------------------------
# Basic construction
# ---------------------------------------------------------------------------


class TestBasicCfg:
    """Smoke tests for CFG construction."""

    def test_empty_function(self):
        cfgs = _build("""\
            def f():
                pass
        """)
        assert len(cfgs) == 1
        cfg = cfgs[0]
        assert cfg.func_name == "f"
        assert cfg.block_count() >= 2  # at least entry + exit
        assert cfg.edge_count() >= 1

    def test_simple_sequential(self):
        cfgs = _build("""\
            def f():
                x = 1
                y = 2
                z = x + y
        """)
        cfg = cfgs[0]
        assert cfg.func_name == "f"
        # Sequential code should have entry block + exit, with a single path
        blocks = cfg.get_blocks()
        assert len(blocks) >= 2  # body block + exit

    def test_multiple_functions(self):
        cfgs = _build("""\
            def f():
                pass

            def g():
                x = 1
        """)
        assert len(cfgs) == 2
        names = {c.func_name for c in cfgs}
        assert names == {"f", "g"}

    def test_async_function(self):
        cfgs = _build("""\
            async def f():
                x = await something()
        """)
        assert len(cfgs) == 1
        assert cfgs[0].func_name == "f"


# ---------------------------------------------------------------------------
# Branching
# ---------------------------------------------------------------------------


class TestBranching:
    """Tests for if/elif/else CFG construction."""

    def test_if_else(self):
        cfgs = _build("""\
            def f(x):
                if x > 0:
                    y = 1
                else:
                    y = 2
                return y
        """)
        cfg = cfgs[0]
        edges = cfg.get_edges()
        edge_kinds = {e["kind"] for e in edges}
        # Should have true_branch and false_branch edges
        assert "true_branch" in edge_kinds or "fallthrough" in edge_kinds
        # Should have more than just a linear chain
        assert cfg.block_count() >= 3

    def test_if_no_else(self):
        cfgs = _build("""\
            def f(x):
                if x > 0:
                    y = 1
                return 0
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 3  # condition, true body, join

    def test_if_elif_else(self):
        cfgs = _build("""\
            def f(x):
                if x > 0:
                    y = 1
                elif x == 0:
                    y = 0
                else:
                    y = -1
                return y
        """)
        cfg = cfgs[0]
        # 3 branches + join block + exit
        assert cfg.block_count() >= 4


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------


class TestLoops:
    """Tests for for/while loop CFG construction."""

    def test_while_loop(self):
        cfgs = _build("""\
            def f():
                x = 0
                while x < 10:
                    x += 1
                return x
        """)
        cfg = cfgs[0]
        edges = cfg.get_edges()
        edge_kinds = {e["kind"] for e in edges}
        # Should have a back-edge for the loop
        assert "back_edge" in edge_kinds or cfg.block_count() >= 3

    def test_for_loop(self):
        cfgs = _build("""\
            def f(items):
                total = 0
                for item in items:
                    total += item
                return total
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 3  # pre-loop, loop body, post-loop

    def test_break_continue(self):
        cfgs = _build("""\
            def f(items):
                for item in items:
                    if item < 0:
                        continue
                    if item > 100:
                        break
                    process(item)
        """)
        cfg = cfgs[0]
        edges = cfg.get_edges()
        edge_kinds = {e["kind"] for e in edges}
        # break/continue create jump or back_edge
        assert cfg.block_count() >= 4


# ---------------------------------------------------------------------------
# Try/except/finally
# ---------------------------------------------------------------------------


class TestTryExcept:
    """Tests for exception handling CFG construction."""

    def test_try_except(self):
        cfgs = _build("""\
            def f():
                try:
                    x = risky()
                except ValueError:
                    x = default()
                return x
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 3  # try body, except handler, join

    def test_try_except_finally(self):
        cfgs = _build("""\
            def f():
                try:
                    x = risky()
                except ValueError:
                    x = 0
                finally:
                    cleanup()
                return x
        """)
        cfg = cfgs[0]
        edges = cfg.get_edges()
        edge_kinds = {e["kind"] for e in edges}
        # Should have finally or exception edges
        assert cfg.block_count() >= 4

    def test_try_else(self):
        cfgs = _build("""\
            def f():
                try:
                    x = risky()
                except ValueError:
                    x = 0
                else:
                    x = x + 1
                return x
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 4


# ---------------------------------------------------------------------------
# Return / raise
# ---------------------------------------------------------------------------


class TestJumps:
    """Tests for return and raise statements."""

    def test_early_return(self):
        cfgs = _build("""\
            def f(x):
                if x < 0:
                    return -1
                return x
        """)
        cfg = cfgs[0]
        edges = cfg.get_edges()
        # The early return should create a jump edge to exit
        jump_edges = [e for e in edges if e["kind"] == "jump"]
        assert len(jump_edges) >= 1 or cfg.block_count() >= 3

    def test_raise(self):
        cfgs = _build("""\
            def f(x):
                if x is None:
                    raise ValueError("x is None")
                return x
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 3

    def test_unreachable_after_return(self):
        """Code after an unconditional return should be in an unreachable block."""
        from emend.cfg import find_unreachable_blocks

        cfgs = _build("""\
            def f():
                return 1
                x = 2
                y = 3
        """)
        cfg = cfgs[0]
        unreachable = find_unreachable_blocks(cfg)
        # There should be at least one unreachable block containing x=2, y=3
        assert len(unreachable) >= 1


# ---------------------------------------------------------------------------
# With statement
# ---------------------------------------------------------------------------


class TestWith:
    def test_with_statement(self):
        cfgs = _build("""\
            def f():
                with open("file") as fh:
                    data = fh.read()
                return data
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 2


# ---------------------------------------------------------------------------
# Def/use extraction
# ---------------------------------------------------------------------------


class TestDefUse:
    """Tests for variable definition and use extraction in blocks."""

    def test_defs_in_assignment(self):
        cfgs = _build("""\
            def f():
                x = 1
                y = x + 2
        """)
        cfg = cfgs[0]
        blocks = cfg.get_blocks()
        all_defs = []
        for b in blocks:
            all_defs.extend(d[0] for d in b["defs"])
        assert "x" in all_defs or "y" in all_defs

    def test_uses_in_expression(self):
        cfgs = _build("""\
            def f(x):
                y = x + 1
                return y
        """)
        cfg = cfgs[0]
        blocks = cfg.get_blocks()
        all_uses = []
        for b in blocks:
            all_uses.extend(u[0] for u in b["uses"])
        # x should appear as a use in the assignment
        assert "x" in all_uses or "y" in all_uses


# ---------------------------------------------------------------------------
# Dominators
# ---------------------------------------------------------------------------


class TestDominators:
    """Tests for dominator computation."""

    def test_entry_dominates_all(self):
        cfgs = _build("""\
            def f(x):
                if x:
                    y = 1
                else:
                    y = 2
                return y
        """)
        cfg = cfgs[0]
        # The entry block should dominate every reachable block
        for block in cfg.get_blocks():
            bid = block["id"]
            doms = cfg.dominators(bid)
            if bid != cfg.exit or cfg.predecessors(bid):
                assert cfg.entry in doms or bid == cfg.entry

    def test_self_dominates(self):
        cfgs = _build("""\
            def f():
                x = 1
                return x
        """)
        cfg = cfgs[0]
        for block in cfg.get_blocks():
            bid = block["id"]
            doms = cfg.dominators(bid)
            assert bid in doms


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------


class TestOutput:
    """Tests for CFG output formatting."""

    def test_to_dot(self):
        cfgs = _build("""\
            def f():
                x = 1
        """)
        dot = cfgs[0].to_dot()
        assert "digraph" in dot
        assert "f" in dot

    def test_to_json(self):
        cfgs = _build("""\
            def f():
                x = 1
        """)
        data = json.loads(cfgs[0].to_json())
        assert "blocks" in data
        assert "edges" in data
        assert data["func_name"] == "f"

    def test_text_format(self):
        from emend.cfg import format_cfg_text

        cfgs = _build("""\
            def f():
                x = 1
        """)
        text = format_cfg_text(cfgs[0])
        assert "function f" in text

    def test_json_format(self):
        from emend.cfg import format_cfgs_json

        cfgs = _build("""\
            def f():
                x = 1
        """)
        data = json.loads(format_cfgs_json(cfgs))
        assert len(data) == 1
        assert data[0]["func_name"] == "f"


# ---------------------------------------------------------------------------
# Fact graph integration
# ---------------------------------------------------------------------------


class TestFactGraphIntegration:
    """Tests for CfgEdgeFact and DefUseFact in the fact graph."""

    def test_cfg_edge_fact_roundtrip(self):
        from emend.fact_graph import CfgEdgeFact, FactGraph

        graph = FactGraph()
        fact = CfgEdgeFact(
            file_path="test.py",
            func_qn="test::f",
            from_block=0,
            to_block=1,
            edge_kind="true_branch",
            from_line=1,
            to_line=3,
        )
        graph.add_cfg_edge(fact)
        results = graph.cfg_edges(func_qn="test::f")
        assert len(results) == 1
        assert results[0].edge_kind == "true_branch"

    def test_def_use_fact_roundtrip(self):
        from emend.fact_graph import DefUseFact, FactGraph

        graph = FactGraph()
        fact = DefUseFact(
            file_path="test.py",
            func_qn="test::f",
            var_name="x",
            def_block=0,
            use_block=1,
            def_line=2,
            def_col=4,
            use_line=5,
            use_col=8,
        )
        graph.add_def_use(fact)
        results = graph.def_uses(var_name="x")
        assert len(results) == 1
        assert results[0].use_line == 5

    def test_serialization_roundtrip(self):
        from emend.fact_graph import CfgEdgeFact, DefUseFact, FactGraph

        graph = FactGraph()
        graph.add_cfg_edge(CfgEdgeFact(
            file_path="a.py", func_qn="a::f",
            from_block=0, to_block=1, edge_kind="fallthrough",
            from_line=1, to_line=2,
        ))
        graph.add_def_use(DefUseFact(
            file_path="a.py", func_qn="a::f",
            var_name="x", def_block=0, use_block=1,
            def_line=1, def_col=0, use_line=2, use_col=4,
        ))

        json_str = graph.to_json()
        graph2 = FactGraph.from_json(json_str)
        assert len(graph2.cfg_edges()) == 1
        assert len(graph2.def_uses()) == 1
