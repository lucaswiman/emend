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
        trace_analysis,
        check_duplicates,
        check,
        facts_query,
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
        "facts_query",
        "grammar_and_cookbook",
    }


def test_check_duplicates_preserves_involves_file_scope(monkeypatch):
    import emend.duplicate

    captured = {}

    def fake_query_duplicates(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(emend.duplicate, "query_duplicates", fake_query_duplicates)

    assert json.loads(check_duplicates("changed.py", project="repo")) == []
    assert captured["project_path"] == "repo"
    assert captured["involves_file"] == "changed.py"
    assert captured["file_scope"] is None


def test_expert_profile_includes_mappings_and_facts_query():
    configure_profile(profile="expert")
    tool_names = {t.name for t in mcp_app._tool_manager.list_tools()}
    assert "mappings" in tool_names
    assert "facts_query" in tool_names


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
        "facts_query",
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

    result = search(mode="code", query="print($X)", files=[str(p)], output="json")
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

    result = search(mode="summary", files=[str(p)], output="summary")
    assert "greet" in result


def test_search_summary_directory(tmp_path):
    # Regression: summary mode over a directory (or glob) hit the batch
    # branch which called dicts_to_tree_symbols() without the required
    # module_path argument, raising TypeError.
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    (tmp_path / "b.py").write_text("def beta():\n    return 2\n")

    result = search(mode="summary", files=[str(tmp_path)], output="summary")
    assert "alpha" in result
    assert "beta" in result


def test_search_summary_glob(tmp_path):
    # Regression: glob path also routes through the batch branch.
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    result = search(mode="summary", files=[str(tmp_path / "*.py")], output="summary")
    assert "alpha" in result


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

    result = references(kind="all", selector=f"{p}::greet")
    data = json.loads(result)
    assert len(data) >= 2


def test_references_calls_mode(tmp_path):
    p = tmp_path / "example.py"
    p.write_text("def greet():\n    pass\n\ndef caller():\n    greet()\n")

    result = references(kind="calls", selector=f"{p}::greet")
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


def test_trace_analysis_interprocedural_includes_engine(tmp_path):
    p = tmp_path / "example.py"
    p.write_text(
        "def run_query(cursor, query):\n"
        "    cursor.execute(query)\n"
        "\n"
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    run_query(cursor, name)\n"
    )

    result = trace_analysis(
        path=str(p),
        from_pattern="request.args.get($X)",
        to_pattern="cursor.execute($Q)",
        interprocedural=True,
    )
    data = json.loads(result)

    assert data["violations"]
    assert all(v["engine"] == "datalog" for v in data["violations"])


def test_analyze_trace_mode_interprocedural_includes_engine(tmp_path):
    p = tmp_path / "example.py"
    p.write_text(
        "def run_query(cursor, query):\n"
        "    cursor.execute(query)\n"
        "\n"
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    run_query(cursor, name)\n"
    )

    result = analyze(
        mode="trace",
        path=str(p),
        from_pattern="request.args.get($X)",
        to_pattern="cursor.execute($Q)",
        interprocedural=True,
    )
    data = json.loads(result)

    assert data["violations"]
    assert all(v["engine"] == "datalog" for v in data["violations"])


def test_check_unified_rules(tmp_path):
    config_dir = tmp_path / ".emend"
    config_dir.mkdir()
    (config_dir / "rules.yaml").write_text(
        "rules:\n"
        "  no-print:\n"
        "    match: \"print($X)\"\n"
        "    message: \"Use logging\"\n"
    )
    p = tmp_path / "example.py"
    p.write_text("print('x')\n")

    result = check(paths=[str(tmp_path)], config=str(config_dir / "rules.yaml"))
    data = json.loads(result)
    assert data[0]["rule"] == "no-print"


def test_check_missing_rules_returns_json_error(monkeypatch, tmp_path):
    # No .emend/rules.yaml exists: the tool must return a clean JSON error
    # instead of letting FileNotFoundError propagate to the MCP client.
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "example.py"
    p.write_text("print('x')\n")

    result = check(paths=[str(tmp_path)], config=None)
    data = json.loads(result)
    assert isinstance(data, dict)
    assert "error" in data


def test_check_file_scopes_use_common_parent_as_project(monkeypatch, tmp_path):
    import emend.checks

    left = tmp_path / "pkg" / "left.py"
    right = tmp_path / "pkg" / "nested" / "right.py"
    right.parent.mkdir(parents=True)
    left.write_text("x = 1\n")
    right.write_text("y = 2\n")
    captured = {}

    def fake_run_checks(file_paths, **kwargs):
        captured["project_path"] = kwargs["project_path"]
        return []

    monkeypatch.setattr(emend.checks, "run_checks", fake_run_checks)
    assert json.loads(check(paths=[str(left), str(right)])) == []
    assert captured["project_path"] == str(left.parent)


def test_facts_query_guided_symbols(tmp_path):
    configure_profile(profile="expert")
    p = tmp_path / "example.py"
    p.write_text("def foo():\n    pass\n\ndef bar():\n    foo()\n")

    result = facts_query(
        fact_type="symbols",
        project=str(tmp_path),
    )
    data = json.loads(result)
    assert isinstance(data, list)
    names = {entry["name"] for entry in data}
    assert "foo" in names
    assert "bar" in names


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
    assert "emend find" in result
    assert "emend edit replace" in result


def test_grammar_and_cookbook_facts_section():
    """section='facts' must return the Fact graph section, not 'Unknown section'."""
    result = grammar_and_cookbook(section="facts")
    assert "Unknown section" not in result
    assert "facts_query" in result.lower() or "fact graph" in result.lower()


def test_trace_analysis_unknown_preset_returns_json_error(tmp_path):
    """Unknown preset should return a JSON error, not raise ValueError."""
    p = tmp_path / "example.py"
    p.write_text("x = 1\n")
    result = trace_analysis(path=str(p), preset="nonexistent_preset_xyz")
    data = json.loads(result)
    assert "error" in data
    assert "nonexistent_preset_xyz" in data["error"].lower() or "unknown" in data["error"].lower()


def test_trace_analysis_intraprocedural_no_crash(tmp_path):
    """trace_analysis without interprocedural should not crash with TypeError."""
    p = tmp_path / "example.py"
    p.write_text(
        "def handler(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )
    result = trace_analysis(
        path=str(p),
        from_pattern="request.args.get($X)",
        to_pattern="cursor.execute($Q)",
        interprocedural=False,
    )
    data = json.loads(result)
    assert isinstance(data, dict) or isinstance(data, list)


def test_analyze_duplicates_uses_correct_limit(tmp_path):
    """analyze(mode='duplicates') should not use max_depth as the limit."""
    p = tmp_path / "example.py"
    p.write_text("def foo():\n    pass\n")
    result = analyze(mode="duplicates", path=str(tmp_path))
    data = json.loads(result)
    assert isinstance(data, dict) or isinstance(data, list)


def test_mcp_help():
    """The mcp command --help should work."""
    result = subprocess.run(
        [sys.executable, "-m", "emend.cli", "mcp", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "MCP" in result.stdout or "mcp" in result.stdout.lower()
