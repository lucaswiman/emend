"""Tests for the MCP server mode."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Skip the entire module if the mcp package isn't usable (e.g. on Python
# 3.14t where Pydantic has compatibility issues with the mcp SDK).
try:
    from emend.mcp_server import (
        mcp_app,
        search,
        replace,
        modify,
        refs,
        rename,
        move,
        graph,
        deadcode,
        grammar_and_cookbook,
        datalog_query,
    )
except Exception:
    pytest.skip("mcp SDK not usable in this environment", allow_module_level=True)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def test_all_tools_registered():
    tool_names = {t.name for t in mcp_app._tool_manager.list_tools()}
    expected = {
        "search", "replace", "modify", "refs", "rename",
        "move", "graph", "deadcode", "lint",
        "impact", "semantic_context", "taint",
        "query_facts", "datalog_query", "check_policies",
        "map_read", "map_write",
        "grammar_and_cookbook",
    }
    assert expected == tool_names


# ---------------------------------------------------------------------------
# search tool
# ---------------------------------------------------------------------------


def test_search_summary(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet(name: str) -> str:\n    return f'Hello, {name}'\n")

    result = search(query=str(p), output="summary")
    assert "greet" in result


def test_search_lookup(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet(name: str) -> str:\n    return f'Hello, {name}'\n")

    result = search(query=f"{p}::greet", output="code")
    assert "def greet" in result


def test_search_pattern(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("print('hello')\nprint('world')\n")

    result = search(query="print($X)", files=str(p), output="json")
    data = json.loads(result)
    assert data["count"] == 2
    assert len(data["matches"]) == 2


def test_search_pattern_location(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("x = 1\ny = 2\n")

    result = search(query="$X = $Y", files=str(p), output="location")
    assert str(p) in result


def test_search_pattern_count(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("a = 1\nb = 2\nc = 3\n")

    result = search(query="$X = $Y", files=str(p), output="count")
    assert result.strip() == "3"


def test_search_kind_filter(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("class Foo:\n    pass\n\ndef bar():\n    pass\n")

    result = search(query=str(p), kind="function", output="selector")
    assert "bar" in result
    assert "Foo" not in result


# ---------------------------------------------------------------------------
# replace tool
# ---------------------------------------------------------------------------


def test_replace_dry_run(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("print('hello')\nprint('world')\n")

    result = replace(
        pattern="print($X)",
        replacement="log($X)",
        path=str(p),
        apply=False,
    )
    assert "log(" in result
    assert "dry-run" in result.lower()
    # File should NOT be modified
    assert p.read_text() == "print('hello')\nprint('world')\n"


def test_replace_apply(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("print('hello')\n")

    result = replace(
        pattern="print($X)",
        replacement="log($X)",
        path=str(p),
        apply=True,
    )
    assert "applied" in result.lower()
    assert "log('hello')" in p.read_text()


def test_replace_no_matches(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("x = 1\n")

    result = replace(
        pattern="print($X)",
        replacement="log($X)",
        path=str(p),
    )
    assert "no matches" in result.lower()


# ---------------------------------------------------------------------------
# modify tool (unified edit + add + remove)
# ---------------------------------------------------------------------------


def test_modify_set_returns(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet(name: str) -> str:\n    return f'Hello, {name}'\n")

    result = modify(
        selector=f"{p}::greet[returns]",
        value="str | None",
        mode="set",
        apply=False,
    )
    assert "str | None" in result


def test_modify_set_apply(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet(name: str) -> str:\n    return f'Hello, {name}'\n")

    modify(
        selector=f"{p}::greet[returns]",
        value="str | None",
        mode="set",
        apply=True,
    )
    assert "str | None" in p.read_text()


def test_modify_remove(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet(name: str, unused: int) -> str:\n    return f'Hello, {name}'\n")

    result = modify(
        selector=f"{p}::greet[params][unused]",
        mode="remove",
        apply=False,
    )
    assert "unused" in result  # shows in the diff


def test_modify_add_parameter(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet(name: str) -> str:\n    return f'Hello, {name}'\n")

    result = modify(
        selector=f"{p}::greet[params]",
        value="greeting: str = 'Hello'",
        mode="add",
        apply=False,
    )
    assert "greeting" in result


def test_modify_add_parameter_apply(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet(name: str) -> str:\n    return f'Hello, {name}'\n")

    modify(
        selector=f"{p}::greet[params]",
        value="greeting: str = 'Hello'",
        mode="add",
        apply=True,
    )
    assert "greeting" in p.read_text()


def test_modify_add_at_position(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet(a: int, b: int) -> int:\n    return a + b\n")

    modify(
        selector=f"{p}::greet[params]",
        value="c: int",
        mode="add",
        at=1,
        apply=True,
    )
    content = p.read_text()
    # c should appear between a and b
    assert "a: int, c: int, b: int" in content


def test_modify_add_missing_value(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet(name: str) -> str:\n    return f'Hello, {name}'\n")

    result = modify(
        selector=f"{p}::greet[params]",
        mode="add",
        apply=False,
    )
    assert "Error" in result


def test_modify_unknown_mode(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet(name: str) -> str:\n    return f'Hello, {name}'\n")

    result = modify(
        selector=f"{p}::greet[returns]",
        value="int",
        mode="bogus",
        apply=False,
    )
    assert "Error" in result


# ---------------------------------------------------------------------------
# refs tool
# ---------------------------------------------------------------------------


def test_refs_basic(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet():\n    pass\n\ngreet()\n")

    result = refs(selector=f"{p}::greet")
    data = json.loads(result)
    assert len(data) >= 2  # definition + call


def test_refs_exclude_definition(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet():\n    pass\n\ngreet()\n")

    result = refs(selector=f"{p}::greet", exclude_definition=True)
    data = json.loads(result)
    assert all(not r["is_definition"] for r in data)


# ---------------------------------------------------------------------------
# rename tool
# ---------------------------------------------------------------------------


def test_rename_dry_run(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def old_func():\n    pass\n\nold_func()\n")

    result = rename(selector=f"{p}::old_func", to="new_func", apply=False)
    assert "new_func" in result
    assert "Dry-run" in result
    # File should NOT be modified
    assert "old_func" in p.read_text()


def test_rename_apply(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def old_func():\n    pass\n\nold_func()\n")

    rename(selector=f"{p}::old_func", to="new_func", apply=True)
    content = p.read_text()
    assert "new_func" in content
    assert "old_func" not in content


# ---------------------------------------------------------------------------
# move tool
# ---------------------------------------------------------------------------


def test_move_dry_run(tmp_path):
    src = tmp_path / "source.py"
    src.write_text("def helper():\n    return 42\n")
    dest = tmp_path / "dest.py"
    dest.write_text("")

    result = move(
        selector=f"{src}::helper",
        destination=str(dest),
        apply=False,
    )
    assert "helper" in result
    assert "Dry-run" in result


# ---------------------------------------------------------------------------
# graph tool
# ---------------------------------------------------------------------------


def test_graph_json(tmp_path):
    p = tmp_path / "example.py"
    p.write_text(
        "def foo():\n    bar()\n\ndef bar():\n    pass\n"
    )

    result = graph(file_path=str(p), format="json")
    data = json.loads(result)
    assert isinstance(data, dict)


def test_graph_plain(tmp_path):
    p = tmp_path / "example.py"
    p.write_text(
        "def foo():\n    bar()\n\ndef bar():\n    pass\n"
    )

    result = graph(file_path=str(p), format="plain")
    assert "foo" in result


# ---------------------------------------------------------------------------
# deadcode tool
# ---------------------------------------------------------------------------


def test_deadcode_basic(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def _used():\n    pass\n\ndef unused_func():\n    _used()\n")
    # Initialize a git repo so deadcode can work
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path, capture_output=True,
        env={"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t",
             "HOME": str(tmp_path)},
    )

    result = deadcode(
        path=str(tmp_path),
        no_last_reference=True,
    )
    data = json.loads(result)
    assert isinstance(data, list)


# ---------------------------------------------------------------------------
# move copy_only (was copy_to)
# ---------------------------------------------------------------------------


def test_move_copy_only_dry_run(tmp_path):
    src = tmp_path / "source.py"
    src.write_text("def helper():\n    return 42\n")
    dest = tmp_path / "dest.py"

    result = move(
        selector=f"{src}::helper",
        destination=str(dest),
        copy_only=True,
        apply=False,
    )
    assert "helper" in result


# ---------------------------------------------------------------------------
# grammar_and_cookbook
# ---------------------------------------------------------------------------


def test_grammar_and_cookbook():
    """The grammar_and_cookbook tool returns the RST reference document."""
    result = grammar_and_cookbook()
    assert "Selector syntax" in result
    assert "Pattern syntax" in result
    assert "Cookbook recipes" in result
    assert "emend search" in result
    assert "emend replace" in result


# ---------------------------------------------------------------------------
# datalog_query tool
# ---------------------------------------------------------------------------


def test_datalog_query_basic(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def foo():\n    pass\n\ndef bar():\n    foo()\n")

    result = datalog_query(
        query='?[name, kind] := *symbol[_, _, name, kind, _, _, _]',
        project=str(tmp_path),
    )
    data = json.loads(result)
    assert "headers" in data
    assert "rows" in data
    assert "count" in data
    names = [row[0] for row in data["rows"]]
    assert "foo" in names
    assert "bar" in names


def test_datalog_query_transitive_callers(tmp_path):
    p = tmp_path / "example.py"
    p.write_text(
        "def a():\n    b()\n\ndef b():\n    c()\n\ndef c():\n    pass\n"
    )

    # Find all callers of c (directly or transitively)
    result = datalog_query(
        query=(
            'reaches[caller] := *call[caller, callee, _, _, _], callee == "example.c"\n'
            'reaches[caller] := *call[caller, mid, _, _, _], reaches[mid]\n'
            '?[caller] := reaches[caller]'
        ),
        project=str(tmp_path),
    )
    data = json.loads(result)
    assert "rows" in data


def test_datalog_query_limit(tmp_path):
    p = tmp_path / "example.py"
    # Create many symbols
    lines = [f"def func_{i}():\n    pass\n" for i in range(20)]
    p.write_text("\n".join(lines))

    result = datalog_query(
        query='?[name] := *symbol[_, _, name, "function", _, _, _]',
        project=str(tmp_path),
        limit=5,
    )
    data = json.loads(result)
    assert data["count"] <= 5


def test_datalog_query_error(tmp_path):
    result = datalog_query(
        query="this is not valid cozoscript !!!",
        project=str(tmp_path),
    )
    data = json.loads(result)
    assert "error" in data


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_mcp_help():
    """The mcp command --help should work."""
    result = subprocess.run(
        [sys.executable, "-m", "emend.cli", "mcp", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "MCP" in result.stdout or "mcp" in result.stdout.lower()
