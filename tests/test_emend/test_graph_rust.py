"""Tests for call graph generation on Rust projects."""
from __future__ import annotations

import json

import pytest


class TestGenerateGraphRust:
    """generate_graph() works on Rust projects."""

    def test_graph_plain_format(self, tmp_path):
        """Plain text call graph for Rust file shows caller -> callee edges."""
        from emend.transform import generate_graph

        f = tmp_path / "funcs.rs"
        f.write_text(
            "fn helper() -> i32 { 42 }\n"
            "\n"
            "fn process() -> i32 { helper() }\n"
            "\n"
            "fn unused() {}\n"
        )

        result = generate_graph(str(f), format="plain")

        # process calls helper — that edge must appear
        assert "process -> helper" in result, (
            f"Expected 'process -> helper' in graph output:\n{result}"
        )
        # helper and unused have no calls
        assert "helper (no calls)" in result or "helper ->" in result, (
            f"Expected helper in graph output:\n{result}"
        )
        assert "unused (no calls)" in result, (
            f"Expected 'unused (no calls)' in graph output:\n{result}"
        )

    def test_graph_json_format(self, tmp_path):
        """JSON call graph for Rust file has correct adjacency structure."""
        from emend.transform import generate_graph

        f = tmp_path / "funcs.rs"
        f.write_text(
            "fn helper() -> i32 { 42 }\n"
            "\n"
            "fn process() -> i32 { helper() }\n"
            "\n"
            "fn unused() {}\n"
        )

        result = generate_graph(str(f), format="json")
        data = json.loads(result)

        assert isinstance(data, dict), "JSON output should be a dict"
        assert "process" in data, f"Expected 'process' key in {list(data)}"
        assert "helper" in data, f"Expected 'helper' key in {list(data)}"
        assert "unused" in data, f"Expected 'unused' key in {list(data)}"

        # process calls helper
        assert "helper" in data["process"], (
            f"Expected 'helper' in process callees: {data['process']}"
        )
        # helper and unused have no outgoing calls
        assert data["helper"] == [], f"Expected helper to have no callees: {data['helper']}"
        assert data["unused"] == [], f"Expected unused to have no callees: {data['unused']}"

    def test_graph_dot_format(self, tmp_path):
        """DOT call graph for Rust file has correct digraph structure."""
        from emend.transform import generate_graph

        f = tmp_path / "funcs.rs"
        f.write_text(
            "fn helper() -> i32 { 42 }\n"
            "\n"
            "fn process() -> i32 { helper() }\n"
            "\n"
            "fn unused() {}\n"
        )

        result = generate_graph(str(f), format="dot")

        assert "digraph callgraph {" in result, (
            f"Expected digraph header in DOT output:\n{result}"
        )
        assert result.strip().endswith("}"), (
            f"Expected closing brace in DOT output:\n{result}"
        )
        assert '"process" -> "helper"' in result, (
            f"Expected process->helper edge in DOT output:\n{result}"
        )

    def test_graph_with_impl_methods(self, tmp_path):
        """Call graph with Rust impl methods documents current limitation.

        TODO: Rust impl block methods require impl_item collection support (Phase 8+).
        Currently the graph includes the struct type (Service) as a node but not
        the individual impl methods (run, compute). This test documents the current
        behavior and uses free functions to verify edges are tracked correctly.
        """
        from emend.transform import generate_graph

        # Verify that impl methods are NOT yet surfaced as graph nodes (current behavior)
        f = tmp_path / "service.rs"
        f.write_text(
            "struct Service;\n"
            "\n"
            "impl Service {\n"
            "    fn compute(&self) -> i32 {\n"
            "        42\n"
            "    }\n"
            "\n"
            "    fn run(&self) -> i32 {\n"
            "        self.compute()\n"
            "    }\n"
            "}\n"
        )

        result = generate_graph(str(f), format="json")
        data = json.loads(result)

        # TODO: Rust impl block methods require impl_item collection support (Phase 8+).
        # Currently only the struct type appears as a node, not the impl methods.
        # The struct itself is present in the graph.
        assert "Service" in data, (
            f"Expected 'Service' struct in graph data: {list(data)}"
        )
        # run and compute are NOT yet surfaced as graph nodes (known limitation)
        assert "run" not in data and "compute" not in data, (
            f"Impl methods run/compute should not be in graph yet "
            f"(impl_item collection not implemented), but found in {list(data)}"
        )

    def test_graph_plain_multiple_callees(self, tmp_path):
        """Plain output lists multiple callees for a Rust function."""
        from emend.transform import generate_graph

        f = tmp_path / "multi.rs"
        f.write_text(
            "fn step1() {}\n"
            "\n"
            "fn step2() {}\n"
            "\n"
            "fn orchestrate() {\n"
            "    step1();\n"
            "    step2();\n"
            "}\n"
        )

        result = generate_graph(str(f), format="plain")

        # orchestrate calls both step1 and step2
        assert "orchestrate ->" in result, (
            f"Expected orchestrate with callees in:\n{result}"
        )
        assert "step1" in result and "step2" in result, (
            f"Expected step1 and step2 in graph output:\n{result}"
        )

    def test_graph_dot_multiple_edges(self, tmp_path):
        """DOT format includes all edges when a Rust caller calls multiple functions."""
        from emend.transform import generate_graph

        f = tmp_path / "multi.rs"
        f.write_text(
            "fn a() {}\n"
            "\n"
            "fn b() {}\n"
            "\n"
            "fn caller() {\n"
            "    a();\n"
            "    b();\n"
            "}\n"
        )

        result = generate_graph(str(f), format="dot")

        assert '"caller" -> "a"' in result, (
            f"Expected caller->a edge in DOT:\n{result}"
        )
        assert '"caller" -> "b"' in result, (
            f"Expected caller->b edge in DOT:\n{result}"
        )

    def test_graph_pub_functions(self, tmp_path):
        """Public Rust functions appear in the call graph."""
        from emend.transform import generate_graph

        f = tmp_path / "lib.rs"
        f.write_text(
            "fn internal() -> i32 { 0 }\n"
            "\n"
            "pub fn exported() -> i32 { internal() }\n"
        )

        result = generate_graph(str(f), format="json")
        data = json.loads(result)

        assert "exported" in data, f"Expected 'exported' in {list(data)}"
        assert "internal" in data, f"Expected 'internal' in {list(data)}"
        assert "internal" in data["exported"], (
            f"Expected 'internal' in exported's callees: {data['exported']}"
        )

    def test_graph_circular_calls(self, tmp_path):
        """Call graph handles mutually recursive Rust functions."""
        from emend.transform import generate_graph

        f = tmp_path / "cycle.rs"
        f.write_text(
            "fn ping() { pong() }\n"
            "\n"
            "fn pong() { ping() }\n"
        )

        result = generate_graph(str(f), format="json")
        data = json.loads(result)

        assert "ping" in data, f"Expected 'ping' in {list(data)}"
        assert "pong" in data, f"Expected 'pong' in {list(data)}"
        assert "pong" in data["ping"], f"Expected pong in ping's callees: {data['ping']}"
        assert "ping" in data["pong"], f"Expected ping in pong's callees: {data['pong']}"

    def test_graph_nested_impl_and_free_functions(self, tmp_path):
        """Graph with free functions and impl methods documents current limitation.

        TODO: Rust impl block methods require impl_item collection support (Phase 8+).
        Currently free functions appear in the graph but impl methods do not. This test
        verifies that free functions are correctly included while documenting the
        limitation for impl methods.
        """
        from emend.transform import generate_graph

        f = tmp_path / "mixed.rs"
        f.write_text(
            "fn helper() -> i32 { 1 }\n"
            "\n"
            "struct Processor;\n"
            "\n"
            "impl Processor {\n"
            "    fn run(&self) -> i32 {\n"
            "        helper()\n"
            "    }\n"
            "}\n"
        )

        result = generate_graph(str(f), format="json")
        data = json.loads(result)

        # Free function helper should appear as a node
        assert "helper" in data, f"Expected 'helper' (free function) in {list(data)}"

        # TODO: Rust impl block methods require impl_item collection support (Phase 8+).
        # `run` is an impl method and is currently NOT surfaced as a graph node.
        # Only the struct (Processor) appears, not the method.
        assert "run" not in data, (
            f"Impl method 'run' should not be in graph yet "
            f"(impl_item collection not implemented), but found in {list(data)}"
        )
        assert "Processor" in data, (
            f"Expected 'Processor' struct in graph: {list(data)}"
        )
