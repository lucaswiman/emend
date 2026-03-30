"""Tests for the MCP server mode."""

import json
import subprocess
import sys

import pytest

# Skip the entire module if the mcp package isn't usable (e.g. on Python
# 3.14t where Pydantic has compatibility issues with the mcp SDK).
try:
    from emend.mcp_server import (
        mcp_app,
        dump_schema,
        configure_profile,
        search,
        transform,
        references,
        analyze,
        check,
        datalog,
        mappings,
        grammar_and_cookbook,
    )
except Exception:
    pytest.skip("mcp SDK not usable in this environment", allow_module_level=True)


@pytest.fixture(autouse=True)
def _core_profile():
    configure_profile(profile="core")
    yield
    configure_profile(profile="core")


def test_all_tools_registered_core():
    tool_names = {t.name for t in mcp_app._tool_manager.list_tools()}
    assert tool_names == {
        "search",
        "transform",
        "references",
        "analyze",
        "check",
        "grammar_and_cookbook",
    }


def test_expert_profile_includes_mappings_and_datalog():
    configure_profile(profile="expert")
    tool_names = {t.name for t in mcp_app._tool_manager.list_tools()}
    assert "mappings" in tool_names
    assert "datalog" in tool_names


def test_dump_schema():
    """dump_schema() returns valid JSON with all tools and their inputSchema."""
    result = dump_schema()
    data = json.loads(result)
    assert "tools" in data
    tool_names = {t["name"] for t in data["tools"]}
    assert tool_names == {
        "search",
        "transform",
        "references",
        "analyze",
        "check",
        "grammar_and_cookbook",
    }
    for tool in data["tools"]:
        assert "description" in tool
        assert "inputSchema" in tool
        assert tool["inputSchema"]["type"] == "object"


def test_dump_schema_no_anyof_null():
    """Schema compression should collapse anyOf-with-null into plain type."""
    result = dump_schema()
    data = json.loads(result)
    for tool in data["tools"]:
        props = tool["inputSchema"].get("properties", {})
        for param_name, param_schema in props.items():
            if "anyOf" in param_schema:
                entries = param_schema["anyOf"]
                null_entries = [e for e in entries if isinstance(e, dict) and e.get("type") == "null"]
                assert not null_entries, (
                    f"Tool {tool['name']}, param {param_name}: anyOf still has null branch"
                )


def test_dump_schema_no_title_keys():
    """Schema compression should strip all title keys."""
    result = dump_schema()
    data = json.loads(result)

    def _check_no_title(obj, path=""):
        if isinstance(obj, dict):
            assert "title" not in obj, f"Found 'title' key at {path}"
            for k, v in obj.items():
                _check_no_title(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _check_no_title(item, f"{path}[{i}]")

    _check_no_title(data)


def test_search_code_pattern(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("print('hello')\nprint('world')\n")

    result = search(mode="code", query="print($X)", files=str(p), output="json")
    data = json.loads(result)
    assert data["count"] == 2
    assert len(data["matches"]) == 2


def test_search_symbol_lookup(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet(name: str) -> str:\n    return f'Hello, {name}'\n")

    result = search(mode="symbol", query=f"{p}::greet", output="code")
    assert "def greet" in result


def test_search_summary(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet(name: str) -> str:\n    return f'Hello, {name}'\n")

    result = search(mode="summary", files=str(p), output="summary")
    assert "greet" in result


def test_transform_replace_dry_run(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("print('hello')\n")

    result = transform(
        operation="replace",
        pattern="print($X)",
        replacement="log($X)",
        path=str(p),
        apply=False,
    )
    assert "dry-run" in result.lower()
    assert "print('hello')" in p.read_text()


def test_transform_edit_apply(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet(name: str) -> str:\n    return f'Hello, {name}'\n")

    transform(
        operation="edit",
        selector=f"{p}::greet[returns]",
        value="str | None",
        apply=True,
    )
    assert "str | None" in p.read_text()


def test_references_refs_mode(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet():\n    pass\n\ngreet()\n")

    result = references(mode="refs", selector=f"{p}::greet")
    data = json.loads(result)
    assert len(data) >= 2


def test_references_callers_mode(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet():\n    pass\n\ndef caller():\n    greet()\n")

    result = references(mode="callers", selector=f"{p}::greet")
    data = json.loads(result)
    assert isinstance(data, list)


def test_analyze_graph_mode(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def foo():\n    bar()\n\ndef bar():\n    pass\n")

    result = analyze(mode="graph", file_path=str(p), format="json")
    data = json.loads(result)
    assert isinstance(data, dict)


def test_analyze_deadcode_mode(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def used():\n    pass\n")

    result = analyze(
        mode="deadcode",
        path=str(tmp_path),
        no_last_reference=True,
    )
    data = json.loads(result)
    assert isinstance(data, list)


def test_check_lint_missing_config(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("print('x')\n")

    result = check(mode="lint", path=str(tmp_path))
    assert "Error: Config file not found" in result


def test_datalog_raw_query(tmp_path):
    configure_profile(profile="expert")
    p = tmp_path / "example.py"
    p.write_text("def foo():\n    pass\n\ndef bar():\n    foo()\n")

    result = datalog(
        mode="raw",
        query='?[name, kind] := *symbol[_, _, name, kind, _, _, _]',
        project=str(tmp_path),
    )
    data = json.loads(result)
    assert "headers" in data
    assert "rows" in data
    assert "count" in data


def test_datalog_guided_symbols(tmp_path):
    configure_profile(profile="expert")
    p = tmp_path / "example.py"
    p.write_text("def foo():\n    pass\n")

    result = datalog(mode="guided", fact_type="symbols", project=str(tmp_path))
    data = json.loads(result)
    assert isinstance(data, list)


def test_mappings_read_write(monkeypatch, tmp_path):
    configure_profile(profile="expert")
    monkeypatch.chdir(tmp_path)
    entry = {
        "source_project": "svc-a",
        "source_identifier": "UserService",
        "target_project": "svc-b",
        "target_identifier": "AccountService",
    }
    write_result = mappings(operation="write", kind="mapping", op="add", entry=entry)
    write_data = json.loads(write_result)
    assert write_data["source_identifier"] == "UserService"

    read_result = mappings(operation="read", kind="mapping", query="UserService")
    read_data = json.loads(read_result)
    assert len(read_data) >= 1


def test_grammar_and_cookbook_default_returns_toc():
    result = grammar_and_cookbook()
    assert 'section="<name>"' in result
    assert "selectors" in result
    assert "patterns" in result
    assert "commands" in result
    assert "recipes" in result


def test_grammar_and_cookbook_all_returns_full_document():
    result = grammar_and_cookbook(section="all")
    assert "Selector syntax" in result
    assert "Pattern syntax" in result
    assert "Cookbook recipes" in result
    assert "emend grep" in result or "emend search" in result
    assert "emend replace" in result


def test_mcp_help():
    """The mcp command --help should work."""
    result = subprocess.run(
        [sys.executable, "-m", "emend.cli", "mcp", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "MCP" in result.stdout or "mcp" in result.stdout.lower()
