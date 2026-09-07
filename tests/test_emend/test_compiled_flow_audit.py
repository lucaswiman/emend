"""Small cross-product audit for the compiled value/effect flow evaluator."""

from __future__ import annotations

from pathlib import Path

import pytest

from emend.analysis_snapshot import TypeFact
from emend.checks.flow import (
    CompiledFlowConfig,
    FlowSanitizer,
    FlowScopeSanitizer,
    FlowSink,
    FlowSource,
    evaluate_flow_config,
)


def _rule(
    *,
    source="source()",
    sinks=None,
    sanitizers=(),
    scope_sanitizers=(),
    source_type="",
):
    return CompiledFlowConfig(
        labels=["value"],
        sources=[FlowSource(source, "value", type_constraint=source_type)],
        sinks=list(sinks or [FlowSink("sink($X)", "value", "bad")]),
        sanitizers=list(sanitizers),
        scope_sanitizers=list(scope_sanitizers),
    )


def _run(tmp_path: Path, text: str, config, graph=None):
    path = tmp_path / "app.py"
    path.write_text(text)
    return evaluate_flow_config(
        config, [str(path)], language="python", project_path=str(tmp_path), graph=graph,
    )


@pytest.mark.parametrize(
    ("statement", "reports"),
    [
        ("x += 1", True),
        ("del x", False),
    ],
)
def test_write_effect_includes_augmented_assignment_but_not_delete(
    tmp_path, statement, reports,
):
    source = f"def f():\n    x = source()\n    {statement}\n"
    config = _rule(
        sinks=[FlowSink("", "value", "write", effect="writes($X)")],
    )
    assert bool(_run(tmp_path, source, config)) is reports


@pytest.mark.parametrize(
    ("source", "statement", "reports"),
    [
        ("obj = source()", "obj.field = 1", True),
        ("obj = source()", "obj.child.field = 1", True),
        ("x = source()", "xx.field = 1", False),
        ("x = source()", "obj.field = 1", False),
    ],
)
def test_write_effects_are_nested_and_prefix_safe(tmp_path, source, statement, reports):
    assert bool(_run(
        tmp_path,
        f"def f():\n    {source}\n    {statement}\n",
        _rule(sinks=[FlowSink("", "value", "write", effect="writes($X)")]),
    )) is reports


def test_pattern_and_effect_sinks_coexist(tmp_path):
    config = _rule(sinks=[
        FlowSink("sink($X)", "value", "pattern"),
        FlowSink("", "value", "effect", effect="writes($X)"),
    ])
    violations = _run(
        tmp_path,
        "def f():\n    x = source()\n    sink(x)\n    x.field = 1\n",
        config,
    )
    assert {(row.message, row.sink_pattern) for row in violations} == {
        ("pattern", "sink($X)"), ("effect", "writes($X)"),
    }


@pytest.mark.parametrize("kind", ["regular", "scope"])
def test_sanitizers_suppress_effect_sinks(tmp_path, kind):
    kwargs = (
        {"sanitizers": [FlowSanitizer("clean($X)", "value")]}
        if kind == "regular" else
        {"scope_sanitizers": [FlowScopeSanitizer("commit()", "value")]}
    )
    clean = "clean(x)" if kind == "regular" else "commit()"
    assert not _run(
        tmp_path,
        f"def f():\n    x = source()\n    {clean}\n    x.field = 1\n",
        _rule(sinks=[FlowSink("", "value", "effect", effect="writes($X)")], **kwargs),
    )


def test_sanitizer_before_sink_survives_unrelated_raw_use_after_sink(tmp_path):
    config = _rule(sanitizers=[FlowSanitizer("clean($X)", "value")])
    assert not _run(
        tmp_path,
        "def f():\n    x = source()\n    clean(x)\n    sink(x)\n    observe(x)\n",
        config,
    )


def test_scope_sanitizer_requires_both_branches_and_covers_multiple_vars(tmp_path):
    config = _rule(scope_sanitizers=[FlowScopeSanitizer("commit()", "value")])
    source = (
        "def f():\n"
        "    x = source()\n"
        "    y = source()\n"
        "    if ready():\n"
        "        commit()\n"
        "    else:\n"
        "        commit()\n"
        "    sink(x)\n"
        "    sink(y)\n"
    )
    assert not _run(tmp_path, source, config)


def test_regular_and_scope_sanitizers_compose(tmp_path):
    config = _rule(
        sanitizers=[FlowSanitizer("clean($X)", "value")],
        scope_sanitizers=[FlowScopeSanitizer("commit()", "value")],
    )
    assert not _run(
        tmp_path,
        "def f():\n    x = source()\n    clean(x)\n    commit()\n    sink(x)\n",
        config,
    )


@pytest.mark.parametrize(
    ("endpoint", "constraint", "type_name", "expected"),
    [
        ("source", "int", "int", True),
        ("source", "str", "int", False),
        ("sink", "int", "int", True),
        ("sink", "str", "int", False),
        ("sanitizer", "int", "int", False),
        ("sanitizer", "str", "int", True),
    ],
)
def test_source_sink_and_sanitizer_type_constraints(
    tmp_path, endpoint, constraint, type_name, expected,
):
    source_text = "def f():\n    y = source(x)\n    clean(y)\n    sink(y)\n"
    path = tmp_path / "app.py"
    path.write_text(source_text)
    # Endpoint filtering is conservative only when no binding is available;
    # provide the exact local bindings needed for this matrix.
    from emend.analysis_store import AnalysisStore
    graph = AnalysisStore.open(tmp_path).query_facts()
    graph.add_type(TypeFact("x", type_name, "app.py", 2, "definition"))
    graph.add_type(TypeFact("y", type_name, "app.py", 2, "definition"))
    source_constraint = constraint if endpoint == "source" else ""
    sink_constraint = constraint if endpoint == "sink" else ""
    sanitizer_constraint = constraint if endpoint == "sanitizer" else ""
    config = _rule(
        source="source($X)",
        source_type=source_constraint,
        sinks=[FlowSink("sink($X)", "value", "bad", type_constraint=sink_constraint)],
        sanitizers=([] if endpoint != "sanitizer" else [FlowSanitizer(
            "clean($X)", "value", type_constraint=sanitizer_constraint,
        )]),
    )
    assert bool(evaluate_flow_config(
        config, [str(path)], language="python", project_path=str(tmp_path), graph=graph,
    )) is expected


@pytest.mark.parametrize("endpoint", ["source", "sink", "sanitizer"])
def test_type_constraints_keep_unknown_bindings_conservatively(tmp_path, endpoint):
    path = tmp_path / "app.py"
    path.write_text("def f():\n    y = source(x)\n    clean(y)\n    sink(y)\n")
    from emend.analysis_store import AnalysisStore
    graph = AnalysisStore.open(tmp_path).query_facts()
    config = _rule(
        source="source($X)",
        source_type="int" if endpoint == "source" else "",
        sinks=[FlowSink(
            "sink($X)", "value", "bad",
            type_constraint="int" if endpoint == "sink" else "",
        )],
        sanitizers=[] if endpoint != "sanitizer" else [FlowSanitizer(
            "clean($X)", "value",
            type_constraint="int" if endpoint == "sanitizer" else "",
        )],
    )
    # Missing bindings retain the endpoint conservatively.  For a source or
    # sink that means the flow remains; for a sanitizer it means it applies.
    assert bool(_run(tmp_path, path.read_text(), config, graph)) is (endpoint != "sanitizer")


def test_type_filtered_rules_remain_isolated(tmp_path):
    path = tmp_path / "app.py"
    path.write_text(
        "def f():\n"
        "    a = source_a(x)\n"
        "    b = source_b(y)\n"
        "    sink_a(a)\n"
        "    sink_b(b)\n"
    )
    from emend.analysis_store import AnalysisStore
    graph = AnalysisStore.open(tmp_path).query_facts()
    graph.add_type(TypeFact("x", "int", "app.py", 2, "definition"))
    graph.add_type(TypeFact("y", "str", "app.py", 3, "definition"))
    config = CompiledFlowConfig(
        labels=["same"],
        sources=[
            FlowSource("source_a($X)", "same", "int", rule_id="a"),
            FlowSource("source_b($X)", "same", "int", rule_id="b"),
        ],
        sinks=[
            FlowSink("sink_a($X)", "same", "a", rule_id="a"),
            FlowSink("sink_b($X)", "same", "b", rule_id="b"),
        ],
    )
    violations = evaluate_flow_config(
        config, [str(path)], language="python", project_path=str(tmp_path), graph=graph,
    )
    assert {(row.rule_id, row.message) for row in violations} == {("a", "a")}


def test_repeated_rules_share_pattern_matches_within_one_query(tmp_path, monkeypatch):
    import emend.checks.flow as flow

    path = tmp_path / "app.py"
    path.write_text("def f():\n    x = source()\n    sink(x)\n")
    calls = []
    original = flow._pattern_spans

    def counted(pattern, files, language):
        calls.append(pattern)
        return original(pattern, files, language)

    monkeypatch.setattr(flow, "_pattern_spans", counted)
    config = CompiledFlowConfig(
        sources=[FlowSource("source()", "value", rule_id=rule_id)
                 for rule_id in ("a", "b")],
        sinks=[FlowSink("sink($X)", "value", rule_id, rule_id=rule_id)
               for rule_id in ("a", "b")],
    )
    assert len(evaluate_flow_config(config, [str(path)], project_path=str(tmp_path))) == 2
    assert calls == ["source()", "sink($X)"]
