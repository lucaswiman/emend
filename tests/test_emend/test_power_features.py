"""Tests for Part 2 power features: scope-aware matching, enhanced inside/not-inside, graph."""

import json

import pytest
import libcst as cst

from emend.transform import find_pattern, replace_pattern, generate_graph


# ============================================================================
# 2.1 Scope-Aware Pattern Matching: --where flag
# ============================================================================


class TestWhereFlag:
    """Tests for --where flag on find_pattern."""

    def test_where_class_name(self, tmp_path):
        """--where 'class MyClass' limits matches to inside MyClass."""
        f = tmp_path / "test.py"
        f.write_text(
            "x = 1\n"
            "class MyClass:\n"
            "    y = 2\n"
            "    def method(self):\n"
            "        z = 3\n"
            "class Other:\n"
            "    w = 4\n"
        )
        matches = find_pattern("$NAME = $VALUE", str(f), where="class MyClass")
        names = {cst.Module([]).code_for_node(m.captures["NAME"]) for m in matches}
        assert names == {"y", "z"}

    def test_where_def_pattern(self, tmp_path):
        """--where 'def test_*' limits matches to test functions."""
        f = tmp_path / "test.py"
        f.write_text(
            "def helper():\n"
            "    print('helper')\n"
            "def test_one():\n"
            "    print('test one')\n"
            "def test_two():\n"
            "    print('test two')\n"
        )
        matches = find_pattern("print($X)", str(f), where="def test_*")
        captured = {cst.Module([]).code_for_node(m.captures["X"]) for m in matches}
        assert captured == {"'test one'", "'test two'"}

    def test_where_conflicts_with_inside(self, tmp_path):
        """--where and --inside cannot both be specified."""
        f = tmp_path / "test.py"
        f.write_text("x = 1\n")
        with pytest.raises(ValueError, match="Cannot specify both.*where.*inside"):
            find_pattern("$X = $Y", str(f), where="class Foo", inside="def")

    def test_where_cli(self, tmp_path, run_emend_cmd):
        """CLI --where flag works correctly."""
        f = tmp_path / "test.py"
        f.write_text(
            "def helper():\n"
            "    print('helper')\n"
            "def test_foo():\n"
            "    print('test foo')\n"
        )
        result = run_emend_cmd(["find", "print($X)", str(f), "--where", "def test_*"])
        assert ":4" in result.stdout


# ============================================================================
# 2.3 Enhanced --inside / --not-inside with patterns
# ============================================================================


class TestEnhancedInside:
    """Tests for pattern-based --inside constraints."""

    def test_inside_def_with_glob_name(self, tmp_path):
        """--inside 'def test_*' matches only inside test functions."""
        f = tmp_path / "test.py"
        f.write_text(
            "def helper():\n"
            "    x = 1\n"
            "def test_something():\n"
            "    y = 2\n"
            "def test_other():\n"
            "    z = 3\n"
            "w = 4\n"
        )
        matches = find_pattern("$NAME = $VALUE", str(f), inside="def test_*")
        names = {cst.Module([]).code_for_node(m.captures["NAME"]) for m in matches}
        assert names == {"y", "z"}

    def test_inside_class_with_name(self, tmp_path):
        """--inside 'class MyClass' matches only inside that specific class."""
        f = tmp_path / "test.py"
        f.write_text(
            "class MyClass:\n"
            "    x = 1\n"
            "class Other:\n"
            "    y = 2\n"
            "z = 3\n"
        )
        matches = find_pattern("$NAME = $VALUE", str(f), inside="class MyClass")
        names = {cst.Module([]).code_for_node(m.captures["NAME"]) for m in matches}
        assert names == {"x"}

    def test_inside_class_wildcard(self, tmp_path):
        """--inside 'class Test*' matches inside any class starting with Test."""
        f = tmp_path / "test.py"
        f.write_text(
            "class TestFoo:\n"
            "    a = 1\n"
            "class TestBar:\n"
            "    b = 2\n"
            "class Helper:\n"
            "    c = 3\n"
        )
        matches = find_pattern("$NAME = $VALUE", str(f), inside="class Test*")
        names = {cst.Module([]).code_for_node(m.captures["NAME"]) for m in matches}
        assert names == {"a", "b"}

    def test_inside_try_colon(self, tmp_path):
        """--inside 'try:' works as pattern form of keyword 'try'."""
        f = tmp_path / "test.py"
        f.write_text(
            "x = 1\n"
            "try:\n"
            "    y = 2\n"
            "except:\n"
            "    z = 3\n"
            "w = 4\n"
        )
        matches = find_pattern("$NAME = $VALUE", str(f), inside="try:")
        names = {cst.Module([]).code_for_node(m.captures["NAME"]) for m in matches}
        assert names == {"y", "z"}

    def test_inside_async_def_name(self, tmp_path):
        """--inside 'async def fetch_*' matches inside async functions with matching name."""
        f = tmp_path / "test.py"
        f.write_text(
            "async def fetch_data():\n"
            "    x = 1\n"
            "async def process_data():\n"
            "    y = 2\n"
            "def fetch_sync():\n"
            "    z = 3\n"
        )
        matches = find_pattern("$NAME = $VALUE", str(f), inside="async def fetch_*")
        names = {cst.Module([]).code_for_node(m.captures["NAME"]) for m in matches}
        assert names == {"x"}


class TestEnhancedNotInside:
    """Tests for pattern-based --not-inside constraints."""

    def test_not_inside_def_glob(self, tmp_path):
        """--not-inside 'def test_*' excludes matches from test functions."""
        f = tmp_path / "test.py"
        f.write_text(
            "def helper():\n"
            "    x = 1\n"
            "def test_something():\n"
            "    y = 2\n"
            "z = 3\n"
        )
        matches = find_pattern("$NAME = $VALUE", str(f), not_inside="def test_*")
        names = {cst.Module([]).code_for_node(m.captures["NAME"]) for m in matches}
        assert names == {"x", "z"}

    def test_not_inside_try_colon(self, tmp_path):
        """--not-inside 'try:' skips matches inside try blocks."""
        f = tmp_path / "test.py"
        f.write_text(
            "x = 1\n"
            "try:\n"
            "    y = 2\n"
            "except:\n"
            "    z = 3\n"
            "w = 4\n"
        )
        matches = find_pattern("$NAME = $VALUE", str(f), not_inside="try:")
        names = {cst.Module([]).code_for_node(m.captures["NAME"]) for m in matches}
        assert names == {"x", "w"}

    def test_not_inside_class_name(self, tmp_path):
        """--not-inside 'class MyClass' excludes matches from MyClass but keeps others."""
        f = tmp_path / "test.py"
        f.write_text(
            "class MyClass:\n"
            "    x = 1\n"
            "class Other:\n"
            "    y = 2\n"
            "z = 3\n"
        )
        matches = find_pattern("$NAME = $VALUE", str(f), not_inside="class MyClass")
        names = {cst.Module([]).code_for_node(m.captures["NAME"]) for m in matches}
        assert names == {"y", "z"}

    def test_not_inside_except_pattern(self, tmp_path):
        """--not-inside 'except ValueError:' excludes ValueError handler only."""
        f = tmp_path / "test.py"
        f.write_text(
            "try:\n"
            "    x = risky()\n"
            "except ValueError:\n"
            "    y = fallback_val()\n"
            "except TypeError:\n"
            "    z = fallback_type()\n"
        )
        matches = find_pattern("$NAME = $VALUE", str(f), not_inside="except ValueError:")
        names = {cst.Module([]).code_for_node(m.captures["NAME"]) for m in matches}
        # x and z are kept, y is excluded (inside except ValueError)
        assert "y" not in names
        assert "z" in names

    def test_invalid_constraint_raises(self, tmp_path):
        """Invalid constraint raises ValueError with helpful message."""
        f = tmp_path / "test.py"
        f.write_text("x = 1\n")
        with pytest.raises(ValueError, match="Unknown inside/not_inside constraint"):
            find_pattern("$X = $Y", str(f), inside="bogus_keyword")


class TestEnhancedInsideReplace:
    """Tests for replace_pattern with enhanced inside/not_inside."""

    def test_replace_not_inside_test_functions(self, tmp_path):
        """replace with --not-inside 'def test_*' only replaces outside test funcs."""
        f = tmp_path / "test.py"
        f.write_text(
            "def helper():\n"
            "    print('helper')\n"
            "def test_something():\n"
            "    print('test')\n"
        )
        diff, count = replace_pattern(
            "print($X)", "logger.info($X)", str(f), not_inside="def test_*"
        )
        assert count == 1
        assert "logger.info('helper')" in diff
        assert "logger.info('test')" not in diff

    def test_replace_inside_class_name(self, tmp_path):
        """replace with --inside 'class MyClass' only replaces inside that class."""
        f = tmp_path / "test.py"
        f.write_text(
            "class MyClass:\n"
            "    def method(self):\n"
            "        print('my')\n"
            "class Other:\n"
            "    def method(self):\n"
            "        print('other')\n"
        )
        diff, count = replace_pattern(
            "print($X)", "log($X)", str(f), inside="class MyClass"
        )
        assert count == 1
        assert "log('my')" in diff
        assert "log('other')" not in diff


# ============================================================================
# 2.3 Keyword-based inside/not_inside
# ============================================================================


class TestInsideKeywords:
    """Ensure keyword-based inside/not_inside works."""

    def test_inside_def_keyword(self, tmp_path):
        """Simple 'def' keyword still works."""
        f = tmp_path / "test.py"
        f.write_text(
            "print('module')\n"
            "def func():\n"
            "    print('func')\n"
        )
        matches = find_pattern("print($X)", str(f), inside="def")
        assert len(matches) == 1
        assert cst.Module([]).code_for_node(matches[0].captures["X"]) == "'func'"

    def test_not_inside_class_keyword(self, tmp_path):
        """Simple 'class' keyword still works for not_inside."""
        f = tmp_path / "test.py"
        f.write_text(
            "x = 1\n"
            "class MyClass:\n"
            "    y = 2\n"
            "z = 3\n"
        )
        matches = find_pattern("$NAME = $VALUE", str(f), not_inside="class")
        names = {cst.Module([]).code_for_node(m.captures["NAME"]) for m in matches}
        assert names == {"x", "z"}


# ============================================================================
# 2.1 Scope-Local Filtering: --scope-local flag
# ============================================================================


class TestScopeLocal:
    """Tests for --scope-local flag that filters to locally-defined names."""

    def test_scope_local_excludes_imported(self, tmp_path):
        """--scope-local excludes matches where the name is imported."""
        f = tmp_path / "test.py"
        f.write_text(
            "from os.path import join\n"
            "\n"
            "def join_local(a, b):\n"
            "    return a + b\n"
            "\n"
            "result1 = join('a', 'b')\n"
            "result2 = join_local('a', 'b')\n"
        )
        # Without scope_local: both calls match
        all_matches = find_pattern("$FUNC($A, $B)", str(f))
        all_names = {cst.Module([]).code_for_node(m.captures["FUNC"]) for m in all_matches}
        assert "join" in all_names
        assert "join_local" in all_names

        # With scope_local: only locally-defined join_local matches
        local_matches = find_pattern("$FUNC($A, $B)", str(f), scope_local=True)
        local_names = {cst.Module([]).code_for_node(m.captures["FUNC"]) for m in local_matches}
        assert "join_local" in local_names
        assert "join" not in local_names

    def test_scope_local_keeps_local_definitions(self, tmp_path):
        """--scope-local keeps matches for locally-defined names."""
        f = tmp_path / "test.py"
        f.write_text(
            "def process(x):\n"
            "    return x + 1\n"
            "\n"
            "result = process(42)\n"
        )
        matches = find_pattern("process($X)", str(f), scope_local=True)
        assert len(matches) == 1

    def test_scope_local_cli(self, tmp_path, run_emend_cmd):
        """CLI --scope-local flag works."""
        f = tmp_path / "test.py"
        f.write_text(
            "from os.path import join\n"
            "\n"
            "def my_join(a, b):\n"
            "    return a + b\n"
            "\n"
            "x = join('a', 'b')\n"
            "y = my_join('a', 'b')\n"
        )
        result = run_emend_cmd([
            "find", "$FUNC($A, $B)", str(f), "--scope-local", "--count"
        ])
        # Should only count locally-defined calls (my_join), not join
        count = int(result.stdout.strip())
        assert count >= 1


# ============================================================================
# 2.4 Cross-Reference Graph Tests
# ============================================================================


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
