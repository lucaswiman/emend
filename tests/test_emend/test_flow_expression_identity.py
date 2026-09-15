"""Expression values must not accidentally validate their input aliases."""

import pytest

from emend.checks.flow import (
    CompiledFlowConfig, FlowSanitizer, FlowSink, FlowSource, evaluate_flow_config,
)


@pytest.mark.parametrize("extension,expression", [
    ("py", "x if flag else 0"),
    ("py", "x or 0"),
    ("py", "x and 0"),
    ("py", "[x]"),
    ("py", "(x,)"),
    ("py", "{'value': x}"),
    ("py", "{x}"),
    ("py", "[x for item in items]"),
    ("py", "{x for item in items}"),
    ("py", "(x for item in items)"),
    ("ts", "flag ? x : 0"),
    ("ts", "x || 0"),
    ("ts", "[x]"),
    ("ts", "{value: x}"),
    ("ts", "`${x}`"),
    ("rs", "[x]"),
    ("rs", "(x,)"),
    ("rs", "Wrapper { value: x }"),
    ("py", "x"),
    ("py", "(x)"),
    ("ts", "x"),
    ("rs", "x"),
])
def test_validating_expression_output_preserves_input_taint(tmp_path, extension, expression):
    path = tmp_path / f"app.{extension}"
    if extension == "py":
        source = f"def f():\n    x = source()\n    y = {expression}\n    clean(y)\n    sink(x)\n"
    else:
        head = "function f()" if extension == "ts" else "fn f()"
        source = f"{head} {{ let x = source(); let y = {expression}; clean(y); sink(x); }}"
    path.write_text(source)
    config = CompiledFlowConfig(
        labels=["value"], sources=[FlowSource("source()", "value")],
        sinks=[FlowSink("sink($X)", "value", "unsafe")],
        sanitizers=[FlowSanitizer("clean($X)", "value")],
    )
    rows = evaluate_flow_config(config, [str(path)], project_path=str(tmp_path))
    assert bool(rows) is (expression not in {"x", "(x)"})
    if rows:
        assert rows[0].trace


@pytest.mark.parametrize("body,reports", [
    ("y = [x]\n    clean(y)\n    sink(y)", False),
    ("clean(x)\n    y = [x]\n    sink(y)", False),
    ("y = [x]\n    clean(0)\n    sink(y)", True),
    ("y = x\n    clean(x)\n    sink(y)", False),
])
def test_expression_outputs_and_aliases_retain_validation_semantics(tmp_path, body, reports):
    path = tmp_path / "app.py"
    path.write_text("def f():\n    x = source()\n    " + body + "\n")
    config = CompiledFlowConfig(
        labels=["value"], sources=[FlowSource("source()", "value")],
        sinks=[FlowSink("sink($X)", "value", "unsafe")],
        sanitizers=[FlowSanitizer("clean($X)", "value")],
    )
    assert bool(evaluate_flow_config(config, [str(path)], project_path=str(tmp_path))) is reports


def test_source_wrapping_resolved_sanitizer_starts_clean(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("def clean(v):\n    return v\ndef f():\n    x = clean(raw)\n    sink(x)\n")
    config = CompiledFlowConfig(
        labels=["value"], sources=[FlowSource("x = clean($X)", "value")],
        sinks=[FlowSink("sink($X)", "value", "unsafe")],
        sanitizers=[FlowSanitizer("clean($X)", "value")],
    )
    assert evaluate_flow_config(config, [str(path)], project_path=str(tmp_path)) == []
