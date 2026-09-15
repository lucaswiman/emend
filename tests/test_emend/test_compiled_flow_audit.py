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


@pytest.mark.parametrize("extension", ["py", "ts"])
@pytest.mark.parametrize("statement,effect,reports", [
    ("clean(x)", "", True),
    ("x = clean(x)", "returns", True),
    ("x = risky()", "", True),
    ("x = 0; risky()", "", False),
    ("mapping[key]", "", True),
    ("obj.attribute", "", True),
    ("1 / divisor", "", True),
    ("1 / 0", "", True),
    ("x = mapping[key]", "", True),
    ("throw", "", True),
])
@pytest.mark.parametrize("boundary", ["except", "finally"])
def test_exception_paths_keep_pre_call_values(tmp_path, extension, statement, effect, reports, boundary):
    if extension == "py":
        statement = "raise ValueError()" if statement == "throw" else statement
        tail = "except Exception:\n        pass\n    sink(x)" if boundary == "except" else "finally:\n        sink(x)"
        source = f"def f():\n    x = source()\n    try:\n        {statement}\n    {tail}\n"
    else:
        statement = "throw new Error()" if statement == "throw" else statement
        tail = "catch(e) {} sink(x);" if boundary == "except" else "finally { sink(x); }"
        source = f"function f() {{ let x = source(); try {{ {statement}; }} {tail} }}\n"
    path = tmp_path / f"app.{extension}"
    path.write_text(source)
    config = _rule(sanitizers=[FlowSanitizer("clean($X)", "value", effect=effect)])
    assert bool(evaluate_flow_config(config, [str(path)], project_path=str(tmp_path))) is reports


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
    tmp_path, request, endpoint, constraint, type_name, expected,
):
    source_text = "def f():\n    y = source(x)\n    clean(y)\n    sink(y)\n"
    path = tmp_path / "app.py"
    path.write_text(source_text)
    # Endpoint filtering is conservative only when no binding is available;
    # provide the exact local bindings needed for this matrix.
    from emend.analysis_store import AnalysisStore
    store = AnalysisStore.open(tmp_path)
    graph = store.detached_facts(store.query_facts())
    request.addfinalizer(graph.close)
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


def test_type_filtered_rules_remain_isolated(tmp_path, request):
    path = tmp_path / "app.py"
    path.write_text(
        "def f():\n"
        "    a = source_a(x)\n"
        "    b = source_b(y)\n"
        "    sink_a(a)\n"
        "    sink_b(b)\n"
    )
    from emend.analysis_store import AnalysisStore
    store = AnalysisStore.open(tmp_path)
    graph = store.detached_facts(store.query_facts())
    request.addfinalizer(graph.close)
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


@pytest.mark.parametrize("extension", ["py", "ts"])
@pytest.mark.parametrize("alias,handler,reports", [
    (False, "x = 0", False),
    (True, "x = 0", True),
    (True, "y = 0", False),
    (False, "x = risky()", False),
])
def test_sanitizer_bypass_must_carry_sink_generation(tmp_path, extension, alias, handler, reports):
    if extension == "py":
        setup = "\n    y = x" if alias else ""
        text = f"def f():\n    x = source(){setup}\n    try:\n        clean(x)\n    except Exception:\n        {handler}\n    sink({'y' if alias else 'x'})\n"
    else:
        setup = "let y = x;" if alias else ""
        text = f"function f() {{ let x = source(); {setup} try {{ clean(x); }} catch(e) {{ {handler}; }} sink({'y' if alias else 'x'}); }}"
    path = tmp_path / f"app.{extension}"
    path.write_text(text)
    violations = evaluate_flow_config(_rule(sanitizers=[FlowSanitizer("clean($X)", "value")]), [str(path)], project_path=str(tmp_path))
    assert bool(violations) is reports
    if reports:
        assert violations[0].trace[0].description.startswith("source:")
        assert violations[0].trace[-1].description.startswith("sink:")


@pytest.mark.parametrize("body,reports", [
    ("y = x\n    if flag:\n        x = 0\n    clean(x)\n    sink(y)", True),
    ("y = transform(x)\n    clean(y)\n    sink(x)", True),
    ("y = x\n    clean(y)\n    sink(x)", False),
    ("if flag:\n        x = escape(x)\n    else:\n        clean(x)\n    sink(x)", False),
])
def test_sanitizer_tracks_aliases_and_derived_values(tmp_path, body, reports):
    violations = _run(tmp_path, "def f():\n    x = source()\n    " + body + "\n", _rule(sanitizers=[
        FlowSanitizer("clean($X)", "value"),
        FlowSanitizer("escape($X)", "value", effect="returns"),
    ]))
    assert bool(violations) is reports


@pytest.mark.parametrize("callee,body,reports", [
    ("def consume(v):\n    sink(v)\n", "try:\n        clean(x)\n    except Exception:\n        x = 0\n    consume(x)", False),
    ("def consume(v):\n    sink(v)\n", "y = x\n    try:\n        clean(x)\n    except Exception:\n        x = 0\n    consume(y)", True),
    ("def clean(v):\n    pass\n", "try:\n        clean(x)\n    except Exception:\n        x = 0\n    sink(x)", False),
    ("def noop(v):\n    pass\n", "noop(0)\n    noop(x)\n    sink(x)", True),
    ("def identity(v):\n    return v\n", "y = identity(x)\n    sink(y)", True),
    ("def identity(v):\n    return v\n", "y = identity(x)\n    clean(y)\n    sink(x)", False),
    ("def pair(a, b):\n    clean(a)\n    sink(b)\n", "pair(0, x)", True),
    ("def pair(a, b):\n    clean(a)\n    sink(b)\n", "pair(x, x)", False),
    ("def pair(a, b):\n    clean(a)\n    sink(b)\n", "pair(transform(x), x)", True),
    ("def choose(a, b):\n    return b\n", "sink(choose(0, x))", True),
    ("def choose(a, b):\n    return b\n", "sink(choose(x, 0))", False),
    ("def identity(v):\n    return v\n", "y = identity(x)\n    clean(y)\n    sink(identity(0))", False),
])
def test_correlated_sanitizers_across_calls(tmp_path, callee, body, reports):
    assert bool(_run(tmp_path, callee + "def f():\n    clean(0)\n    x = source()\n    " + body + "\n",
                     _rule(sanitizers=[FlowSanitizer("clean($X)", "value")]))) is reports


def test_loop_keeps_old_alias_distinct_from_new_source_generation(tmp_path):
    rows = _run(tmp_path, """def f():
    x = 0
    while flag:
        y = x
        x = source()
        clean(x)
        sink(y)
""", _rule(sanitizers=[FlowSanitizer("clean($X)", "value")]))
    # Every generation was validated before becoming the next iteration's y.
    assert rows == []
    rows = _run(tmp_path, """def f():
    x = 0
    while flag:
        y = x
        x = source()
        if flag:
            clean(x)
            sink(y)
""", _rule(sanitizers=[FlowSanitizer("clean($X)", "value")]))
    assert rows and rows[0].trace


def test_correlation_budget_retains_warning_without_inventing_witness(tmp_path, monkeypatch):
    import emend.checks.flow as flow
    monkeypatch.setattr(flow, "_VALUE_STATE_LIMIT", 0)
    rows = _run(tmp_path, "def f():\n    x = source()\n    if flag:\n        clean(x)\n    sink(x)\n",
                _rule(sanitizers=[FlowSanitizer("clean($X)", "value")]))
    assert rows[0].trace == []
    assert rows[0].engine == "occurrence-budget"
    assert "correlation limit" in rows[0].message


@pytest.mark.parametrize("extension,head,condition", [
    ("ts", "function f()", "(flag)"),
    ("rs", "fn f()", "flag"),
])
@pytest.mark.parametrize("body,reports", [
    ("let y = x; clean(y); sink(x);", False),
    ("let y = transform(x); clean(y); sink(x);", True),
    ("let y = x; if CONDITION { x = 0; } clean(x); sink(y);", True),
])
def test_generation_validation_other_languages(tmp_path, extension, head, condition, body, reports):
    path = tmp_path / f"app.{extension}"
    mutable = "mut " if extension == "rs" else ""
    path.write_text(f"{head} {{ let {mutable}x = source(); {body.replace('CONDITION', condition)} }}")
    assert bool(evaluate_flow_config(_rule(sanitizers=[FlowSanitizer("clean($X)", "value")]),
                                    [str(path)], project_path=str(tmp_path))) is reports


@pytest.mark.parametrize("replacement,reports", [("0", False), ("risky()", True)])
def test_outer_handler_preserves_failed_inner_handler_assignment(tmp_path, replacement, reports):
    rows = _run(tmp_path, f"""def f():
    x = source()
    try:
        try:
            clean(x)
        except Exception:
            x = {replacement}
    except Exception:
        pass
    finally:
        sink(x)
""", _rule(sanitizers=[FlowSanitizer("clean($X)", "value")]))
    assert bool(rows) is reports


@pytest.mark.parametrize("handler,reports", [("Exception", False), ("ValueError", True)])
def test_narrow_inner_handler_can_let_exception_escape(tmp_path, handler, reports):
    rows = _run(tmp_path, f"""def f():
    x = source()
    try:
        try:
            clean(x)
        except {handler}:
            x = 0
    except Exception:
        pass
    sink(x)
""", _rule(sanitizers=[FlowSanitizer("clean($X)", "value")]))
    assert bool(rows) is reports


@pytest.mark.parametrize("extension", ["py", "ts"])
def test_mandatory_finalizer_overwrite_precedes_outer_handler(tmp_path, extension):
    source = """def f():
    x = source()
    try:
        try:
            clean(x)
        finally:
            x = 0
    except Exception:
        pass
    sink(x)
""" if extension == "py" else """function f() {
    let x = source();
    try { try { clean(x); } finally { x = 0; } } catch(e) {}
    sink(x);
}"""
    path = tmp_path / f"app.{extension}"
    path.write_text(source)
    assert evaluate_flow_config(_rule(sanitizers=[FlowSanitizer("clean($X)", "value")]),
                                [str(path)], project_path=str(tmp_path)) == []


@pytest.mark.parametrize("finalizer,reports", [("pass", True), ("x = 0", False)])
def test_unmatched_inner_exception_executes_finalizer(tmp_path, finalizer, reports):
    rows = _run(tmp_path, f"""def f():
    x = source()
    try:
        try:
            clean(x)
        except ValueError:
            x = 0
        finally:
            {finalizer}
    except Exception:
        pass
    sink(x)
""", _rule(sanitizers=[FlowSanitizer("clean($X)", "value")]))
    assert bool(rows) is reports


def test_exception_in_else_executes_finalizer(tmp_path):
    rows = _run(tmp_path, """def f():
    x = source()
    try:
        harmless()
    except Exception:
        x = 0
    else:
        x = clean(x)
    finally:
        sink(x)
""", _rule(sanitizers=[FlowSanitizer("clean($X)", "value", effect="returns")]))
    assert rows


@pytest.mark.parametrize("first,second,reports", [
    ("x = 0", "pass", True),
    ("pass", "x = 0", True),
    ("x = 0", "x = 0", False),
])
@pytest.mark.parametrize("finalizer", [False, True])
def test_except_dispatch_does_not_execute_mismatched_handler(tmp_path, first, second, reports, finalizer):
    tail = "finally:\n        sink(x)" if finalizer else "sink(x)"
    rows = _run(tmp_path, f"""def f():
    x = source()
    try:
        clean(x)
    except ValueError:
        {first}
    except Exception:
        {second}
    {tail}
""", _rule(sanitizers=[FlowSanitizer("clean($X)", "value")]))
    assert bool(rows) is reports


@pytest.mark.parametrize("extension", ["py", "ts"])
@pytest.mark.parametrize("termination", ["return", "break", "continue"])
@pytest.mark.parametrize("overwrite", [False, True])
def test_terminating_handler_runs_finalizer(tmp_path, extension, termination, overwrite):
    if extension == "py":
        assignment = "x = 0\n            " if overwrite else ""
        source = f"""def f():
    x = source()
    while flag:
        try:
            clean(x)
        except Exception:
            {assignment}{termination}
        finally:
            sink(x)
"""
    else:
        assignment = "x = 0;" if overwrite else ""
        source = f"function f() {{ let x = source(); while(flag) {{ try {{ clean(x); }} catch(e) {{ {assignment} {termination}; }} finally {{ sink(x); }} }} }}"
    path = tmp_path / f"app.{extension}"
    path.write_text(source)
    rows = evaluate_flow_config(_rule(sanitizers=[FlowSanitizer("clean($X)", "value")]),
                                [str(path)], project_path=str(tmp_path))
    assert bool(rows) is not overwrite
    if rows:
        assert rows[0].trace


@pytest.mark.parametrize("handler", ["Handler", "get_type()"])
def test_custom_handler_type_evaluation_can_escape(tmp_path, handler):
    rows = _run(tmp_path, f"""def f():
    x = source()
    try:
        try:
            clean(x)
        except {handler}:
            x = 0
        except Exception:
            x = 0
    except Exception:
        pass
    sink(x)
""", _rule(sanitizers=[FlowSanitizer("clean($X)", "value")]))
    assert rows
