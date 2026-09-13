"""Identically spelled names in different language namespaces stay distinct."""

import json
from pathlib import Path

from emend.analysis_store import AnalysisStore
from emend.fact_graph import (
    CallFact, DecoratorOnFact, FactGraph, FuncSummaryFact, ReferenceFact, SymbolFact, TypeFact,
)
import pytest


def test_same_module_names_survive_incremental_updates(tmp_path):
    python = tmp_path / "api.py"
    typescript = tmp_path / "api.ts"
    python.write_text("def common():\n    return 1\n")
    typescript.write_text("export function common() { return 2; }\n")
    store = AnalysisStore(tmp_path)

    def symbols():
        return {(s.qualified_name, s.file_path) for s in store.query_facts().symbols()}

    expected = {("api.common", "api.py"), ("api.common", "api.ts")}
    assert symbols() == expected
    python.write_text("def common():\n    return 3\n")
    assert symbols() == expected
    store.close()
    assert symbols() == expected
    python.unlink()
    assert symbols() == {("api.common", "api.ts")}
    store.close()


def test_namespace_scopes_recursion_metadata_and_removal():
    graph = FactGraph()
    for path in ("api.py", "api.ts"):
        graph.add_symbol(SymbolFact(path, "common", "api.common", "function", 1, 2))
        graph.add_decorator_on(DecoratorOnFact("api.common", path, path))
        graph.add_func_summary(FuncSummaryFact("api.common", "x", file_path=path))
    graph.add_call(CallFact("python.only", "api.common", "api.py", 3, 0))
    graph.add_call(CallFact("api.common", "ts.leaf", "api.ts", 3, 0))
    graph.add_reference(ReferenceFact("api.common", "api.py", 3, 0, "call"))

    assert graph.transitive_callers("ts.leaf") == {"api.common"}
    assert [(s.qualified_name, s.file_path) for s in graph.dead_code()] == [("api.common", "api.ts")]
    assert [c.caller_qn for c in graph.calls_to("api.common", namespace="python")] == ["python.only"]
    assert graph.calls_to("api.common", namespace="typescript") == []
    with pytest.raises(ValueError, match="Ambiguous"):
        graph.add_decorator_on(DecoratorOnFact("api.common", "unowned"))
    with pytest.raises(ValueError, match="Ambiguous"):
        graph.cascade_dead({"api.common"})
    assert graph.cascade_dead({"api.common"}, namespace="python") == []

    restored = FactGraph.from_json(graph.to_json())
    assert restored.decorators_on("api.common") == graph.decorators_on("api.common")
    assert restored.func_summaries() == graph.func_summaries()
    restored.remove_files(["api.py"])
    assert restored.decorators_on("api.common") == [DecoratorOnFact("api.common", "api.ts", "api.ts")]
    assert restored.func_summaries() == [FuncSummaryFact("api.common", "x", file_path="api.ts")]


@pytest.mark.parametrize("suffix, other_source", [
    ("ts", "function root() { helper(); }\nfunction helper() {}\n"),
    ("rs", "fn root() { helper(); }\nfn helper() {}\n"),
])
def test_selector_calls_deadcode_and_cascade_are_language_scoped(tmp_path, suffix, other_source):
    from emend.component_selector import parse_extended_selector
    from emend.transform import find_callers, find_dead_code, safe_delete

    python = tmp_path / "api.py"
    other = tmp_path / f"api.{suffix}"
    python.write_text("def root():\n    helper()\ndef helper():\n    return 1\n")
    other.write_text(other_source)
    target = parse_extended_selector(f"{python}::helper")
    assert {r.file_path for r in find_callers(target, str(tmp_path))} == {str(python)}
    dead = list(find_dead_code(str(tmp_path), strings_count_as_references=False,
                               unused_modules=False, include_transitive=True))
    assert {(s.file_path, s.name) for s in dead} == {
        (str(path), name) for path in (python, other) for name in ("root", "helper")
    }
    plan = safe_delete(parse_extended_selector(f"{python}::root"),
                       project_path=str(tmp_path), cascade=True, apply=True)
    assert {item["file_path"] for item in plan.deletions} == {str(python)}
    assert python.read_text() == ""
    assert other.read_text() == other_source


@pytest.mark.parametrize("reverse", [False, True])
def test_configured_namespace_survives_serialization_and_later_facts(tmp_path, reverse):
    config = tmp_path / "languages/python/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text((Path(__file__).parents[2] / "languages/python/config.toml").read_text().replace(
        'file_extensions = ["py", "pyi"]', 'file_extensions = ["py", "pyi", "special"]'))
    (tmp_path / "api.special").write_text("def common():\n    return 1\n")
    (tmp_path / "main.py").write_text("from api import common\nvalue = common()\n")
    graph = AnalysisStore(tmp_path).query_facts()
    data = json.loads(graph.to_json())
    restored = FactGraph.from_json(json.dumps(data[::-1] if reverse else data))
    for current in (graph, restored):
        current.add_type(TypeFact("api.common", "int", "api.special", 1, "definition"))
        assert current.namespace_for_file("api.special") == "python"
        assert current.dead_code_unified()[0] == []
