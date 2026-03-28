"""Tests for TypeScript/JavaScript CFG construction."""

from __future__ import annotations

import json
import textwrap

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build(source: str, ext: str = "ts"):
    """Build CFGs for the given TypeScript source string."""
    from emend import emend_core
    return emend_core.build_cfgs(textwrap.dedent(source), ext=ext)


# ---------------------------------------------------------------------------
# Basic construction
# ---------------------------------------------------------------------------


class TestTsBasicCfg:
    """Smoke tests for TypeScript CFG construction."""

    def test_empty_function(self):
        cfgs = _build("""\
            function f() {}
        """)
        assert len(cfgs) == 1
        cfg = cfgs[0]
        assert cfg.func_name == "f"
        assert cfg.block_count() >= 2  # entry + exit

    def test_simple_sequential(self):
        cfgs = _build("""\
            function f() {
                let x = 1;
                let y = 2;
                let z = x + y;
            }
        """)
        cfg = cfgs[0]
        assert cfg.func_name == "f"
        assert cfg.block_count() >= 2

    def test_multiple_functions(self):
        cfgs = _build("""\
            function f() {}
            function g() { let x = 1; }
        """)
        assert len(cfgs) == 2
        names = {c.func_name for c in cfgs}
        assert names == {"f", "g"}

    def test_arrow_function(self):
        cfgs = _build("""\
            const f = () => {
                return 1;
            };
        """)
        assert len(cfgs) == 1
        assert cfgs[0].func_name == "<anonymous>"

    def test_method_definition(self):
        cfgs = _build("""\
            class Foo {
                bar() {
                    return 42;
                }
            }
        """)
        assert len(cfgs) == 1
        assert cfgs[0].func_name == "bar"

    def test_js_extension(self):
        """Ensure .js files use the same TS grammar."""
        cfgs = _build("function f() { return 1; }", ext="js")
        assert len(cfgs) == 1
        assert cfgs[0].func_name == "f"


# ---------------------------------------------------------------------------
# Branching (alternative-style if)
# ---------------------------------------------------------------------------


class TestTsBranching:
    """Tests for if/else if/else CFG construction (alternative-style)."""

    def test_if_else(self):
        cfgs = _build("""\
            function f(x) {
                if (x > 0) {
                    let y = 1;
                } else {
                    let y = 2;
                }
                return 0;
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 3
        edges = cfg.get_edges()
        edge_kinds = {e["kind"] for e in edges}
        assert "true_branch" in edge_kinds

    def test_if_no_else(self):
        cfgs = _build("""\
            function f(x) {
                if (x > 0) {
                    let y = 1;
                }
                return 0;
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 3

    def test_if_else_if_else(self):
        cfgs = _build("""\
            function f(x) {
                if (x > 0) {
                    return 1;
                } else if (x === 0) {
                    return 0;
                } else {
                    return -1;
                }
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 4


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------


class TestTsLoops:
    """Tests for loop CFG construction."""

    def test_while_loop(self):
        cfgs = _build("""\
            function f() {
                let x = 0;
                while (x < 10) {
                    x += 1;
                }
                return x;
            }
        """)
        cfg = cfgs[0]
        edges = cfg.get_edges()
        edge_kinds = {e["kind"] for e in edges}
        assert "back_edge" in edge_kinds or cfg.block_count() >= 3

    def test_for_in_loop(self):
        cfgs = _build("""\
            function f(items) {
                let total = 0;
                for (let item of items) {
                    total += item;
                }
                return total;
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 3

    def test_c_style_for_loop(self):
        cfgs = _build("""\
            function f() {
                let sum = 0;
                for (let i = 0; i < 10; i++) {
                    sum += i;
                }
                return sum;
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 3
        edges = cfg.get_edges()
        edge_kinds = {e["kind"] for e in edges}
        assert "back_edge" in edge_kinds

    def test_break_continue(self):
        cfgs = _build("""\
            function f(items) {
                for (let item of items) {
                    if (item < 0) {
                        continue;
                    }
                    if (item > 100) {
                        break;
                    }
                }
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 4


# ---------------------------------------------------------------------------
# Try/catch/finally (fields-style)
# ---------------------------------------------------------------------------


class TestTsTryCatch:
    """Tests for exception handling CFG construction (fields-style)."""

    def test_try_catch(self):
        cfgs = _build("""\
            function f() {
                try {
                    let x = risky();
                } catch (e) {
                    let x = fallback();
                }
                return 0;
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 3

    def test_try_catch_finally(self):
        cfgs = _build("""\
            function f() {
                try {
                    let x = risky();
                } catch (e) {
                    let x = 0;
                } finally {
                    cleanup();
                }
                return 0;
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 4


# ---------------------------------------------------------------------------
# Switch/case (with fallthrough)
# ---------------------------------------------------------------------------


class TestTsSwitch:
    """Tests for switch/case CFG construction."""

    def test_switch_basic(self):
        cfgs = _build("""\
            function f(x) {
                switch (x) {
                    case 1:
                        return "one";
                    case 2:
                        return "two";
                    default:
                        return "other";
                }
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 4

    def test_switch_with_break(self):
        cfgs = _build("""\
            function f(x) {
                let result;
                switch (x) {
                    case 1:
                        result = "one";
                        break;
                    case 2:
                        result = "two";
                        break;
                    default:
                        result = "other";
                }
                return result;
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 4


# ---------------------------------------------------------------------------
# Return / throw
# ---------------------------------------------------------------------------


class TestTsJumps:
    """Tests for return and throw statements."""

    def test_early_return(self):
        cfgs = _build("""\
            function f(x) {
                if (x < 0) {
                    return -1;
                }
                return x;
            }
        """)
        cfg = cfgs[0]
        edges = cfg.get_edges()
        jump_edges = [e for e in edges if e["kind"] == "jump"]
        assert len(jump_edges) >= 1

    def test_throw(self):
        cfgs = _build("""\
            function f(x) {
                if (x === null) {
                    throw new Error("null");
                }
                return x;
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 3


# ---------------------------------------------------------------------------
# Def/use extraction
# ---------------------------------------------------------------------------


class TestTsDefUse:
    """Tests for variable definition and use extraction in TypeScript."""

    def test_variable_declarator(self):
        cfgs = _build("""\
            function f() {
                let x = 1;
                let y = x + 2;
            }
        """)
        cfg = cfgs[0]
        blocks = cfg.get_blocks()
        all_defs = []
        for b in blocks:
            all_defs.extend(d[0] for d in b["defs"])
        assert "x" in all_defs or "y" in all_defs

    def test_assignment_expression(self):
        cfgs = _build("""\
            function f() {
                let x;
                x = 42;
            }
        """)
        cfg = cfgs[0]
        blocks = cfg.get_blocks()
        all_defs = []
        for b in blocks:
            all_defs.extend(d[0] for d in b["defs"])
        assert "x" in all_defs


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------


class TestTsOutput:
    """Tests for CFG output formatting with TypeScript."""

    def test_to_dot(self):
        cfgs = _build("function f() { let x = 1; }")
        dot = cfgs[0].to_dot()
        assert "digraph" in dot
        assert "f" in dot

    def test_to_json(self):
        cfgs = _build("function f() { let x = 1; }")
        data = json.loads(cfgs[0].to_json())
        assert "blocks" in data
        assert "edges" in data
        assert data["func_name"] == "f"

    def test_text_format(self):
        from emend.cfg import format_cfg_text

        cfgs = _build("function f() { let x = 1; }")
        text = format_cfg_text(cfgs[0])
        assert "function f" in text

    def test_json_format(self):
        from emend.cfg import format_cfgs_json

        cfgs = _build("function f() { let x = 1; }")
        data = json.loads(format_cfgs_json(cfgs))
        assert len(data) == 1
        assert data[0]["func_name"] == "f"
