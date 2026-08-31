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

    def test_try_except_handler_body_reachable(self):
        """Except handler body blocks must be reachable from the exception edge.

        Regression: the CFG builder created a separate block for the exception
        edge target and a second block for walking the handler body, but never
        connected them — leaving the entire handler body unreachable.
        """
        cfgs = _build("""\
            def f():
                for attempt in range(3):
                    try:
                        return risky()
                    except Exception as e:
                        error_str = str(e)
                        if "409" in error_str:
                            continue
                        elif "429" in error_str:
                            continue
                        elif "timeout" in error_str:
                            continue
                        else:
                            raise
        """)
        cfg = cfgs[0]
        blocks = cfg.get_blocks()
        edges = cfg.get_edges()

        # Build reachable set via BFS from entry
        adj: dict[int, list[int]] = {}
        for e in edges:
            adj.setdefault(e["from"], []).append(e["to"])
        reachable: set[int] = set()
        stack = [cfg.entry]
        while stack:
            b = stack.pop()
            if b in reachable:
                continue
            reachable.add(b)
            for n in adj.get(b, []):
                stack.append(n)

        # Every non-exit block WITH content (statements, defs, or uses)
        # should be reachable. Empty structural join blocks may be
        # unreachable when all paths through a try/except terminate.
        unreachable_with_content = [
            b for b in blocks
            if b["id"] not in reachable
            and b["id"] != cfg.exit
            and (b.get("statements") or b.get("defs") or b.get("uses"))
        ]
        assert unreachable_with_content == [], (
            f"Blocks {[b['id'] for b in unreachable_with_content]} are unreachable but should not be"
        )

    def test_try_except_multiple_handlers_reachable(self):
        """Multiple except clauses should all be reachable."""
        cfgs = _build("""\
            def f():
                try:
                    risky()
                except ValueError:
                    handle_value()
                except TypeError:
                    handle_type()
                except RuntimeError:
                    handle_runtime()
        """)
        cfg = cfgs[0]
        blocks = cfg.get_blocks()
        edges = cfg.get_edges()

        adj: dict[int, list[int]] = {}
        for e in edges:
            adj.setdefault(e["from"], []).append(e["to"])
        reachable: set[int] = set()
        stack = [cfg.entry]
        while stack:
            b = stack.pop()
            if b in reachable:
                continue
            reachable.add(b)
            for n in adj.get(b, []):
                stack.append(n)

        unreachable = [
            b for b in blocks
            if b["id"] not in reachable and b["id"] != cfg.exit
        ]
        assert unreachable == [], (
            f"Blocks {[b['id'] for b in unreachable]} are unreachable but should not be"
        )

    def test_continue_with_inline_comment_reachable(self):
        """A comment on the same line as continue must not create a false unreachable block.

        Regression: tree-sitter parses inline comments as named sibling nodes.
        walk_body treated them as statements, so after `continue` (which
        terminates), the comment triggered creation of an unreachable block
        whose byte range overlapped real code.
        """
        cfgs = _build("""\
            def f():
                for x in items:
                    if cond:
                        continue  # skip this
                    process(x)
        """)
        cfg = cfgs[0]
        blocks = cfg.get_blocks()
        edges = cfg.get_edges()

        adj: dict[int, list[int]] = {}
        for e in edges:
            adj.setdefault(e["from"], []).append(e["to"])
        reachable: set[int] = set()
        stack = [cfg.entry]
        while stack:
            b = stack.pop()
            if b in reachable:
                continue
            reachable.add(b)
            for n in adj.get(b, []):
                stack.append(n)

        unreachable_with_content = [
            b for b in blocks
            if b["id"] not in reachable
            and b["id"] != cfg.exit
            and (b.get("statements") or b.get("defs") or b.get("uses"))
        ]
        assert unreachable_with_content == [], (
            f"Blocks {[b['id'] for b in unreachable_with_content]} are unreachable but should not be"
        )

    def test_finally_reachable_when_try_terminates(self):
        """Finally block must be reachable even when all try paths terminate.

        Regression: when the try body ended with return/raise and there were
        no except clauses, the finally block had no incoming edges because
        try_end was None and except_target was None.
        """
        cfgs = _build("""\
            def f():
                try:
                    for item in entries:
                        if item == target:
                            return item
                    raise ValueError("not found")
                finally:
                    cleanup()
        """)
        cfg = cfgs[0]
        blocks = cfg.get_blocks()
        edges = cfg.get_edges()

        adj: dict[int, list[int]] = {}
        for e in edges:
            adj.setdefault(e["from"], []).append(e["to"])
        reachable: set[int] = set()
        stack = [cfg.entry]
        while stack:
            b = stack.pop()
            if b in reachable:
                continue
            reachable.add(b)
            for n in adj.get(b, []):
                stack.append(n)

        unreachable_with_content = [
            b for b in blocks
            if b["id"] not in reachable
            and b["id"] != cfg.exit
            and (b.get("statements") or b.get("defs") or b.get("uses"))
        ]
        assert unreachable_with_content == [], (
            f"Blocks {[b['id'] for b in unreachable_with_content]} are unreachable but should not be"
        )

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

    def test_text_format_lines_are_1_based(self):
        """format_cfg_text must display 1-based line numbers.

        Regression: Rust returns 0-based tree-sitter rows but the formatter
        displayed them without conversion, producing confusing 0-based output.
        """
        from emend.cfg import format_cfg_text

        cfgs = _build("""\
            def f():
                x = 1
        """)
        text = format_cfg_text(cfgs[0])
        assert "lines 1-" in text
        assert "lines 0-" not in text

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


class TestCliUnreachable:
    """CLI `analyze cfg --unreachable` line-number reporting (Datalog path)."""

    def test_unreachable_reports_real_line_numbers(self, tmp_path):
        """The Datalog path must report real block line spans, not 0-0.

        Regression: the Datalog branch hardcoded start_line/end_line to 0,
        producing bogus ``:1`` locations and ``lines 0-0`` text.
        """
        from typer.testing import CliRunner
        from emend.cli import app

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.py").write_text(textwrap.dedent("""\
            def f(x):
                if x:
                    return 1
                else:
                    return 2
                print("dead")
                y = 3
                return y
        """))

        runner = CliRunner()
        result = runner.invoke(
            app, ["analyze", "cfg", str(project), "--unreachable"]
        )
        assert result.exit_code == 0, result.output
        out = result.output
        assert "unreachable code" in out, out
        # Must not contain the bogus placeholder span.
        assert "lines 0-0" not in out, out
        assert ":1:" not in out, out
        # The dead statements live on source lines 6-8; the block covering
        # them must be reported with a real (non-zero) start line.
        import re
        spans = re.findall(r"lines (\d+)-(\d+)", out)
        assert spans, out
        assert any(int(s) > 0 and int(e) > 0 for s, e in spans), out
        # And at least one reported location line must be > 1.
        locs = re.findall(r"mod\.py:(\d+):", out)
        assert any(int(l) > 1 for l in locs), out

    def test_unreachable_json_has_real_line_numbers(self, tmp_path):
        """JSON output from the Datalog path must carry real line spans."""
        from typer.testing import CliRunner
        from emend.cli import app

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.py").write_text(textwrap.dedent("""\
            def f(x):
                if x:
                    return 1
                else:
                    return 2
                print("dead")
                y = 3
                return y
        """))

        runner = CliRunner()
        result = runner.invoke(
            app, ["analyze", "cfg", str(project), "--unreachable", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        blocks = [b for r in data for b in r["unreachable_blocks"]]
        assert blocks, result.output
        assert any(
            b["start_line"] > 0 or b["end_line"] > 0 for b in blocks
        ), result.output

    def test_unreachable_scopes_same_basename_by_relative_path(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from emend.cli import app
        from emend.fact_graph import CfgBlockFact
        import emend.transform

        project = tmp_path / "project"
        project.mkdir()
        (project / "pyproject.toml").touch()
        (project / "left").mkdir(parents=True)
        (project / "right").mkdir()
        (project / "left" / "mod.py").write_text(
            "def f(x):\n    return 1\n    print('left')\n"
        )
        (project / "right" / "mod.py").write_text(
            "\n\n\n\n\ndef f(x):\n    return 1\n    print('right')\n"
        )
        monkeypatch.setattr(
            emend.transform,
            "_get_or_build_fact_graph",
            lambda _path: type("Graph", (), {
                "unreachable_blocks_datalog": lambda _self, func_qn=None: [
                    CfgBlockFact("left/mod.py", "left.mod.f", 2, False, False),
                    CfgBlockFact("right/mod.py", "right.mod.f", 2, False, False),
                ],
            })(),
        )

        result = CliRunner().invoke(
            app, ["analyze", "cfg", str(project), "--unreachable", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        data = {entry["file"]: entry for entry in json.loads(result.output)}
        assert set(data) == {"left/mod.py", "right/mod.py"}
        assert data["left/mod.py"]["unreachable_blocks"][0]["start_line"] < 6
        assert data["right/mod.py"]["unreachable_blocks"][0]["start_line"] >= 8
