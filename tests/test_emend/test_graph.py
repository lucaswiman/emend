"""Tests for the call graph (``emend analyze graph``) on Python sources."""

import json

from emend.transform import generate_graph


class TestGraphCommand:
    """Tests for the graph command with multiple output formats."""

    def test_graph_plain_format(self, tmp_path):
        """Graph in plain text format shows caller -> callees."""
        f = tmp_path / "example.py"
        f.write_text(
            "def helper():\n"
            "    pass\n"
            "\n"
            "def process():\n"
            "    helper()\n"
            "\n"
            "def main():\n"
            "    process()\n"
            "    helper()\n"
        )
        result = generate_graph(str(f), format="plain")
        assert "helper (no calls)" in result or "helper ->" in result
        assert "process -> helper" in result
        assert "main -> process" in result or "main -> helper" in result

    def test_graph_json_format(self, tmp_path):
        """Graph in JSON format is valid JSON with adjacency list."""
        f = tmp_path / "example.py"
        f.write_text(
            "def a():\n"
            "    b()\n"
            "\n"
            "def b():\n"
            "    pass\n"
        )
        result = generate_graph(str(f), format="json")
        data = json.loads(result)
        assert isinstance(data, dict)
        assert "a" in data
        assert "b" in data
        assert "b" in data["a"]
        assert data["b"] == []

    def test_graph_dot_format(self, tmp_path):
        """Graph in DOT format has correct structure."""
        f = tmp_path / "example.py"
        f.write_text(
            "def a():\n"
            "    b()\n"
            "\n"
            "def b():\n"
            "    pass\n"
        )
        result = generate_graph(str(f), format="dot")
        assert result.startswith("digraph callgraph {")
        assert result.strip().endswith("}")
        assert '"a" -> "b"' in result

    def test_graph_no_functions(self, tmp_path):
        """Graph with no functions produces empty output."""
        f = tmp_path / "example.py"
        f.write_text("x = 1\ny = 2\n")
        result = generate_graph(str(f), format="plain")
        assert result == ""

    def test_graph_with_class(self, tmp_path):
        """Graph includes class-level functions."""
        f = tmp_path / "example.py"
        f.write_text(
            "def standalone():\n"
            "    pass\n"
            "\n"
            "class MyClass:\n"
            "    def method(self):\n"
            "        standalone()\n"
        )
        result = generate_graph(str(f), format="json")
        data = json.loads(result)
        # Top-level: standalone and MyClass
        assert "standalone" in data
        assert "MyClass" in data

    def test_graph_circular_calls(self, tmp_path):
        """Graph handles functions that call each other."""
        f = tmp_path / "example.py"
        f.write_text(
            "def ping():\n"
            "    pong()\n"
            "\n"
            "def pong():\n"
            "    ping()\n"
        )
        result = generate_graph(str(f), format="json")
        data = json.loads(result)
        assert "pong" in data["ping"]
        assert "ping" in data["pong"]

    def test_graph_json_format_multiple_callees(self, tmp_path):
        """Graph JSON with multiple callees per function."""
        f = tmp_path / "example.py"
        f.write_text(
            "def step1():\n"
            "    pass\n"
            "\n"
            "def step2():\n"
            "    pass\n"
            "\n"
            "def orchestrate():\n"
            "    step1()\n"
            "    step2()\n"
        )
        result = generate_graph(str(f), format="json")
        data = json.loads(result)
        assert set(data["orchestrate"]) == {"step1", "step2"}

    def test_graph_dot_format_edges(self, tmp_path):
        """Graph DOT format includes all edges."""
        f = tmp_path / "example.py"
        f.write_text(
            "def a():\n"
            "    b()\n"
            "    c()\n"
            "\n"
            "def b():\n"
            "    c()\n"
            "\n"
            "def c():\n"
            "    pass\n"
        )
        result = generate_graph(str(f), format="dot")
        assert '"a" -> "b"' in result
        assert '"a" -> "c"' in result
        assert '"b" -> "c"' in result

    def test_graph_cli_plain(self, tmp_path, run_emend_cmd):
        """CLI graph command works with plain format."""
        f = tmp_path / "example.py"
        f.write_text(
            "def a():\n"
            "    b()\n"
            "\n"
            "def b():\n"
            "    pass\n"
        )
        result = run_emend_cmd(["graph", str(f)])
        assert "a -> b" in result.stdout

    def test_graph_cli_json(self, tmp_path, run_emend_cmd):
        """CLI graph command works with JSON format."""
        f = tmp_path / "example.py"
        f.write_text(
            "def a():\n"
            "    b()\n"
            "\n"
            "def b():\n"
            "    pass\n"
        )
        result = run_emend_cmd(["graph", str(f), "--format", "json"])
        data = json.loads(result.stdout)
        assert "b" in data["a"]

    def test_graph_cli_dot(self, tmp_path, run_emend_cmd):
        """CLI graph command works with DOT format."""
        f = tmp_path / "example.py"
        f.write_text(
            "def a():\n"
            "    b()\n"
            "\n"
            "def b():\n"
            "    pass\n"
        )
        result = run_emend_cmd(["graph", str(f), "--format", "dot"])
        assert "digraph callgraph" in result.stdout
        assert '"a" -> "b"' in result.stdout
