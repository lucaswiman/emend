"""Tests for Rust CFG construction."""

from __future__ import annotations

import json
import textwrap

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build(source: str):
    """Build CFGs for the given Rust source string."""
    from emend import emend_core
    return emend_core.build_cfgs(textwrap.dedent(source), ext="rs")


# ---------------------------------------------------------------------------
# Basic construction
# ---------------------------------------------------------------------------


class TestRsBasicCfg:
    """Smoke tests for Rust CFG construction."""

    def test_empty_function(self):
        cfgs = _build("""\
            fn f() {}
        """)
        assert len(cfgs) == 1
        cfg = cfgs[0]
        assert cfg.func_name == "f"
        assert cfg.block_count() >= 2  # entry + exit

    def test_simple_sequential(self):
        cfgs = _build("""\
            fn f() {
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
            fn f() {}
            fn g() { let x = 1; }
        """)
        assert len(cfgs) == 2
        names = {c.func_name for c in cfgs}
        assert names == {"f", "g"}

    def test_function_in_impl(self):
        cfgs = _build("""\
            struct Foo;
            impl Foo {
                fn bar(&self) -> i32 {
                    42
                }
            }
        """)
        assert len(cfgs) == 1
        assert cfgs[0].func_name == "bar"

    def test_function_in_trait(self):
        cfgs = _build("""\
            trait MyTrait {
                fn do_thing(&self) -> bool {
                    true
                }
            }
        """)
        assert len(cfgs) == 1
        assert cfgs[0].func_name == "do_thing"


# ---------------------------------------------------------------------------
# Branching (alternative-style if)
# ---------------------------------------------------------------------------


class TestRsBranching:
    """Tests for if/else if/else CFG construction (alternative-style)."""

    def test_if_else(self):
        cfgs = _build("""\
            fn f(x: i32) -> i32 {
                if x > 0 {
                    1
                } else {
                    2
                }
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 3
        edges = cfg.get_edges()
        edge_kinds = {e["kind"] for e in edges}
        assert "true_branch" in edge_kinds

    def test_if_no_else(self):
        cfgs = _build("""\
            fn f(x: i32) {
                if x > 0 {
                    println!("positive");
                }
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 3
        edges = cfg.get_edges()
        edge_kinds = {e["kind"] for e in edges}
        assert "false_branch" in edge_kinds

    def test_if_else_if_else(self):
        cfgs = _build("""\
            fn f(x: i32) -> i32 {
                if x > 0 {
                    1
                } else if x == 0 {
                    0
                } else {
                    -1
                }
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 4


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------


class TestRsLoops:
    """Tests for loop CFG construction."""

    def test_while_loop(self):
        cfgs = _build("""\
            fn f() {
                let mut x = 0;
                while x < 10 {
                    x += 1;
                }
            }
        """)
        cfg = cfgs[0]
        edges = cfg.get_edges()
        edge_kinds = {e["kind"] for e in edges}
        assert "back_edge" in edge_kinds

    def test_for_loop(self):
        cfgs = _build("""\
            fn f() {
                let mut total = 0;
                for item in [1, 2, 3] {
                    total += item;
                }
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 3

    def test_infinite_loop(self):
        cfgs = _build("""\
            fn f() {
                loop {
                    break;
                }
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 3
        edges = cfg.get_edges()
        edge_kinds = {e["kind"] for e in edges}
        # break creates a jump edge
        assert "jump" in edge_kinds

    def test_loop_with_break_continue(self):
        cfgs = _build("""\
            fn f() {
                let mut i = 0;
                loop {
                    i += 1;
                    if i > 10 {
                        break;
                    }
                    if i % 2 == 0 {
                        continue;
                    }
                }
            }
        """)
        cfg = cfgs[0]
        edges = cfg.get_edges()
        edge_kinds = {e["kind"] for e in edges}
        assert "jump" in edge_kinds  # break
        assert "back_edge" in edge_kinds  # continue


# ---------------------------------------------------------------------------
# Match expressions
# ---------------------------------------------------------------------------


class TestRsMatch:
    """Tests for match expression CFG construction."""

    def test_match_basic(self):
        cfgs = _build("""\
            fn f(x: i32) -> &'static str {
                match x {
                    1 => "one",
                    2 => "two",
                    _ => "other",
                }
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 4  # entry + 3 arms + exit

    def test_match_with_block(self):
        cfgs = _build("""\
            fn f(x: i32) -> i32 {
                match x {
                    1 => {
                        let y = 10;
                        y + 1
                    }
                    _ => 0,
                }
            }
        """)
        cfg = cfgs[0]
        assert cfg.block_count() >= 3


# ---------------------------------------------------------------------------
# Return
# ---------------------------------------------------------------------------


class TestRsJumps:
    """Tests for return statements."""

    def test_early_return(self):
        cfgs = _build("""\
            fn f(x: i32) -> i32 {
                if x < 0 {
                    return -1;
                }
                x
            }
        """)
        cfg = cfgs[0]
        edges = cfg.get_edges()
        jump_edges = [e for e in edges if e["kind"] == "jump"]
        assert len(jump_edges) >= 1

    def test_unreachable_after_return(self):
        from emend.cfg import find_unreachable_blocks

        cfgs = _build("""\
            fn f() -> i32 {
                return 1;
                let x = 2;
                x
            }
        """)
        cfg = cfgs[0]
        unreachable = find_unreachable_blocks(cfg)
        assert len(unreachable) >= 1


# ---------------------------------------------------------------------------
# Def/use extraction
# ---------------------------------------------------------------------------


class TestRsDefUse:
    """Tests for variable definition and use extraction in Rust."""

    def test_let_binding(self):
        cfgs = _build("""\
            fn f() {
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

    def test_assignment(self):
        cfgs = _build("""\
            fn f() {
                let mut x = 0;
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


class TestRsOutput:
    """Tests for CFG output formatting with Rust."""

    def test_to_dot(self):
        cfgs = _build("fn f() { let x = 1; }")
        dot = cfgs[0].to_dot()
        assert "digraph" in dot
        assert "f" in dot

    def test_to_json(self):
        cfgs = _build("fn f() { let x = 1; }")
        data = json.loads(cfgs[0].to_json())
        assert "blocks" in data
        assert "edges" in data
        assert data["func_name"] == "f"

    def test_text_format(self):
        from emend.cfg import format_cfg_text

        cfgs = _build("fn f() { let x = 1; }")
        text = format_cfg_text(cfgs[0])
        assert "function f" in text
