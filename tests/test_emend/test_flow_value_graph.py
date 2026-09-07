"""Behavioral gates for the compiled rule/value-flow evaluator.

These tests deliberately exercise occurrences (not just variable names): a
variable can have multiple generations, a sink can occur more than once on a
line, and two rules may share a label or pattern while remaining isolated.
The evaluator is the only runtime entry point used below; the old line-order
simulator is checked as a removed implementation detail at the end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from emend.checks.flow import (
    CompiledFlowConfig,
    FlowSanitizer,
    FlowScopeSanitizer,
    FlowSink,
    FlowSource,
    evaluate_compiled_flow,
    evaluate_flow_config,
)


def _config(
    *,
    source: str = "source()",
    sink: str = "sink($X)",
    label: str = "value",
    rule_id: str = "rule:value",
    effect: str = "",
    sanitizers: list[FlowSanitizer] | None = None,
    scope_sanitizers: list[FlowScopeSanitizer] | None = None,
) -> CompiledFlowConfig:
    return CompiledFlowConfig(
        labels=[label],
        sources=[FlowSource(source, label, rule_id=rule_id)],
        sinks=[FlowSink(
            sink if not effect else "",
            label,
            "tainted value reached sink",
            effect=effect,
            rule_id=rule_id,
            rule_name=rule_id,
            audiences=["lint", "policy", "trace"],
        )],
        sanitizers=list(sanitizers or []),
        scope_sanitizers=list(scope_sanitizers or []),
    )


def _run(tmp_path: Path, source: str, config: CompiledFlowConfig):
    path = tmp_path / "app.py"
    path.write_text(source)
    return evaluate_flow_config(
        config,
        [str(path)],
        language="python",
        project_path=str(tmp_path),
    )


@pytest.mark.parametrize(
    ("source", "reports"),
    [
        ("def f():\n    x = source()\n    x = 0\n    sink(x)\n", False),
        # Rebinding x does not erase the older value held by y.
        ("def f():\n    x = source()\n    y = x\n    x = 0\n    sink(y)\n", True),
        # A loop may execute zero times, so a loop-body overwrite is not a
        # dominating kill of the source generation.
        ("def f():\n    x = source()\n    while ready():\n        x = 0\n    sink(x)\n", True),
        # A back-edge carries the source generation to a later loop visit;
        # source-after-sink in the first visit is therefore still observable.
        ("def f(x):\n    while ready():\n        sink(x)\n        x = source()\n", True),
    ],
)
def test_value_generations_overwrite_and_alias_kills(tmp_path, source, reports):
    assert bool(_run(tmp_path, source, _config())) is reports


@pytest.mark.parametrize(
    ("source", "reports"),
    [
        (
            "def f():\n"
            "    x = source()\n"
            "    if ready():\n"
            "        x = 0\n"
            "    sink(x)\n",
            True,
        ),
        (
            "def f():\n"
            "    x = source()\n"
            "    if ready():\n"
            "        x = 0\n"
            "    else:\n"
            "        x = 0\n"
            "    sink(x)\n",
            False,
        ),
    ],
)
def test_branch_one_or_both_overwrites(tmp_path, source, reports):
    assert bool(_run(tmp_path, source, _config())) is reports


def test_sink_occurrences_and_nested_source_witness(tmp_path):
    source = "def f():\n    sink(source()); sink(source())\n"
    violations = _run(tmp_path, source, _config())

    # Both nested source-to-sink occurrences are real, even though they share
    # a line and a label.  Columns/occurrence identity must keep them apart.
    assert len(violations) == 2
    assert len({(v.line, v.col) for v in violations}) == 2
    for violation in violations:
        descriptions = [step.description for step in violation.trace]
        assert any(description.startswith("source:") for description in descriptions)
        assert any(description.startswith("sink:") for description in descriptions)


def test_repeated_identical_sinks_get_distinct_rule_ids(tmp_path):
    config = CompiledFlowConfig(
        labels=["same"],
        sources=[FlowSource("source()", "same", rule_id="rule:a")],
        sinks=[
            FlowSink("sink($X)", "same", "first", rule_id="rule:a", rule_name="a"),
            FlowSink("sink($X)", "same", "second", rule_id="rule:b", rule_name="b"),
        ],
    )
    violations = _run(tmp_path, "def f():\n    x = source()\n    sink(x)\n", config)

    assert {(v.rule_id, v.message) for v in violations} == {
        ("rule:a", "first"),
        ("rule:b", "second"),
    }


def test_identical_labels_and_rules_do_not_cross_contaminate(tmp_path):
    config = CompiledFlowConfig(
        labels=["same"],
        sources=[
            FlowSource("source_a()", "same", rule_id="rule:a"),
            FlowSource("source_b()", "same", rule_id="rule:b"),
        ],
        sinks=[
            FlowSink("sink_a($X)", "same", "a", rule_id="rule:a", rule_name="a"),
            FlowSink("sink_b($X)", "same", "b", rule_id="rule:b", rule_name="b"),
        ],
    )
    source = (
        "def f():\n"
        "    a = source_a()\n"
        "    b = source_b()\n"
        "    sink_a(a)\n"
        "    sink_b(b)\n"
    )
    violations = _run(tmp_path, source, config)
    assert {(v.rule_id, v.message) for v in violations} == {
        ("rule:a", "a"),
        ("rule:b", "b"),
    }


@pytest.mark.parametrize(
    ("source", "reports"),
    [
        ("def f():\n    x = source(); clean(x); sink(x)\n", False),
        ("def f():\n    x = source(); sink(x); clean(x)\n", True),
    ],
)
def test_sanitizer_byte_order_is_significant(tmp_path, source, reports):
    config = _config(sanitizers=[FlowSanitizer("clean($X)", "value")])
    assert bool(_run(tmp_path, source, config)) is reports


def test_sanitizer_quantifier_all_paths_and_some_path(tmp_path):
    source = (
        "def f():\n"
        "    x = source()\n"
        "    if ready():\n"
        "        clean(x)\n"
        "    sink(x)\n"
    )
    all_paths = _config(sanitizers=[FlowSanitizer("clean($X)", "value", "all_paths")])
    some_path = _config(sanitizers=[FlowSanitizer("clean($X)", "value", "some_path")])
    assert _run(tmp_path, source, all_paths)
    assert not _run(tmp_path, source, some_path)


def test_scope_sanitizer_kills_value_for_the_remaining_scope(tmp_path):
    source = "def f():\n    x = source()\n    commit()\n    sink(x)\n"
    config = _config(scope_sanitizers=[FlowScopeSanitizer("commit()", "value")])
    assert not _run(tmp_path, source, config)


@pytest.mark.parametrize(
    ("effect", "statement", "reports"),
    [
        ("writes($X)", "x.field = 1", True),
        ("writes($X)", "x.save()", True),
        ("writes($X)", "consume(x.field)", False),
        ("reads($X)", "consume(x.field)", True),
        ("reads($X)", "x.field = 1", False),
    ],
)
def test_effect_sinks_preserve_reads_writes_access_paths(
    tmp_path, effect, statement, reports,
):
    source = f"def f():\n    x = source()\n    {statement}\n"
    assert bool(_run(tmp_path, source, _config(effect=effect))) is reports


def test_nested_multihop_calls_are_isolated_by_call_site(tmp_path):
    source = (
        "def leaf(value):\n"
        "    sink(value)\n"
        "def middle(value):\n"
        "    leaf(value)\n"
        "def safe():\n"
        "    middle(0)\n"
        "def unsafe():\n"
        "    middle(source())\n"
    )
    violations = _run(tmp_path, source, _config())
    assert len(violations) == 1
    assert violations[0].line == 2


@pytest.mark.parametrize(
    ("source", "reports"),
    [
        (
            "def safe(value):\n"
            "    return 0\n"
            "def f():\n"
            "    sink(safe(source()))\n",
            False,
        ),
        ("def f():\n    sink(external_wrapper(source()))\n", True),
    ],
)
def test_resolved_calls_use_returns_but_opaque_calls_propagate(
    tmp_path, source, reports,
):
    assert bool(_run(tmp_path, source, _config())) is reports


def test_return_constraints_use_snapshot_type_facts(tmp_path):
    from emend.analysis_snapshot import TypeFact
    from emend.analysis_store import AnalysisStore

    path = tmp_path / "app.py"
    path.write_text("def f():\n    x = source()\n    sink(x)\n")
    graph = AnalysisStore.open(tmp_path).query_facts()
    graph.add_type(TypeFact("sink", "(object) -> str", "app.py", 3, "reference"))

    matching = _config(sink="$F:returns[str]($X)")
    rejected = _config(sink="$F:returns[int]($X)")
    assert evaluate_flow_config(matching, [str(path)], graph=graph)
    assert not evaluate_flow_config(rejected, [str(path)], graph=graph)


def test_recursive_call_graph_reaches_sink_and_terminates(tmp_path):
    source = (
        "def recurse(value, count):\n"
        "    if count:\n"
        "        return recurse(value, count - 1)\n"
        "    sink(value)\n"
        "def entry():\n"
        "    recurse(source(), 1)\n"
    )
    violations = _run(tmp_path, source, _config())
    assert len(violations) == 1
    assert violations[0].line == 4


def test_compiled_evaluator_matches_trace_lint_and_policy_adapters(tmp_path):
    import yaml
    from emend.checks.rules_config import compile_rules_document

    source = "def f():\n    x = source()\n    sink(x)\n"
    path = tmp_path / "app.py"
    path.write_text(source)
    config_path = tmp_path / "rules.yaml"
    config_path.write_text(yaml.safe_dump({
        "rules": {
            "value-flow": {
                "flow": {"from": "source()", "to": "sink($X)"},
                "message": "bad flow",
                "severity": "error",
            },
        },
    }))
    policy_config_path = tmp_path / "policy-rules.yaml"
    policy_document = {
        "policies": [{
            "name": "value-policy",
            "description": "bad flow",
            "severity": "error",
            "checks": [{
                "type": "flow",
                "flows-from": "source()",
                "flows-to": "sink($X)",
            }],
        }],
    }
    policy_config_path.write_text(yaml.safe_dump(policy_document))

    from emend.checks.engine import run_checks
    from emend.trace import load_trace_config, run_trace_analysis

    document = yaml.safe_load(config_path.read_text())
    direct = evaluate_compiled_flow(
        compile_rules_document(document).flow_rules, [str(path)],
        interprocedural=True, language="python", project_path=str(tmp_path),
    )
    trace = run_trace_analysis([str(path)], load_trace_config(str(config_path)))
    checks = run_checks(
        [str(path)], config=str(config_path), mode="all", project_path=str(tmp_path),
    )
    policy_direct = evaluate_compiled_flow(
        compile_rules_document(policy_document).policy_flow_rules, [str(path)],
        interprocedural=True, language="python", project_path=str(tmp_path),
    )
    policy_checks = run_checks(
        [str(path)], config=str(policy_config_path), mode="policy",
        project_path=str(tmp_path),
    )
    assert len(direct) == len(trace) == len(checks) == 1
    assert len(policy_direct) == len(policy_checks) == 1
    assert trace[0].message == checks[0].message == direct[0].message
    assert trace[0].line == checks[0].line == direct[0].line
    assert policy_direct[0].line == policy_checks[0].line
    assert policy_direct[0].message == policy_checks[0].message


def test_overlay_snapshot_is_coherent_for_value_graph(tmp_path):
    from emend.analysis_store import AnalysisStore

    path = tmp_path / "app.py"
    path.write_text("def f():\n    sink(0)\n")
    store = AnalysisStore(tmp_path)
    try:
        disk = store.query_facts()
        disk_id = disk.snapshot.snapshot_id
        owner = object()
        overlay = "def f():\n    x = source()\n    sink(x)\n"
        update = store.update_overlay(path, overlay, 1, owner=owner)
        assert update.accepted
        graph = store.query_facts()
        assert graph.snapshot.base_snapshot_id == disk_id
        revision = next(file for file in graph.snapshot.files if file.file_path == str(path.resolve()))
        assert revision.origin == "overlay"
        assert revision.version == 1
        assert graph.source_text(path) == overlay
        assert graph.snapshot.snapshot_id != disk_id
    finally:
        store.close()


def test_flow_legacy_line_order_adapter_is_removed_after_cutover():
    import emend.checks.flow as flow

    # Trace's legacy helpers are covered by test_phase17_remove_legacy_intra;
    # this guards the former flow-specific simulator boundary.
    assert not hasattr(flow, "_execute_via_python")
