"""Finalizers preserve the continuation and values of their actual entry path."""

import pytest

from emend.checks.flow import (
    CompiledFlowConfig, FlowSanitizer, FlowSink, FlowSource, evaluate_flow_config,
)


@pytest.mark.parametrize("extension", ["py", "ts"])
@pytest.mark.parametrize("python,typescript,reports", [
    ("try:\n    if True:\n        pass\n    x = 0\n    return\nfinally:\n    sink(x)",
     "try { if (true) {} x = 0; return; } finally { sink(x); }", False),
    ("try:\n    return\nfinally:\n    pass\nsink(x)",
     "try { return; } finally {} sink(x);", False),
    ("try:\n    if flag:\n        return\n    x = 0\nfinally:\n    pass\nsink(x)",
     "try { if (flag) return; x = 0; } finally {} sink(x);", False),
    ("try:\n    if flag:\n        return\nfinally:\n    pass\nsink(x)",
     "try { if (flag) return; } finally {} sink(x);", True),
    ("try:\n    pass\nfinally:\n    return\nsink(x)",
     "try {} finally { return; } sink(x);", False),
    ("try:\n    try:\n        return\n    finally:\n        x = 0\nfinally:\n    sink(x)",
     "try { try { return; } finally { x = 0; } } finally { sink(x); }", False),
    ("try:\n    try:\n        return\n    finally:\n        pass\nfinally:\n    sink(x)",
     "try { try { return; } finally {} } finally { sink(x); }", True),
])
def test_finalizer_preserves_return_path(tmp_path, extension, python, typescript, reports):
    _assert_flow(tmp_path, extension, python, typescript, reports)


@pytest.mark.parametrize("extension", ["py", "ts"])
@pytest.mark.parametrize("jump", ["break", "continue"])
def test_finalizer_does_not_mix_loop_exit_with_normal_completion(tmp_path, extension, jump):
    python = f"while flag:\n    try:\n        if ready:\n            {jump}\n        x = 0\n    finally:\n        pass\n    sink(x)"
    typescript = f"while(flag) {{ try {{ if(ready) {jump}; x = 0; }} finally {{}} sink(x); }}"
    _assert_flow(tmp_path, extension, python, typescript, False)


def _assert_flow(tmp_path, extension, python, typescript, reports):
    source = ("def f():\n    clean(0)\n    x = source()\n"
              + "".join("    " + line + "\n" for line in python.splitlines())) if extension == "py" else (
                  "function f() { clean(0); let x = source(); " + typescript + " }")
    path = tmp_path / f"app.{extension}"
    path.write_text(source)
    config = CompiledFlowConfig(
        sources=[FlowSource("source()", "value")],
        sinks=[FlowSink("sink($X)", "value", "unsafe")],
        sanitizers=[FlowSanitizer("clean($X)", "value")],
    )
    rows = evaluate_flow_config(config, [str(path)], project_path=str(tmp_path))
    assert bool(rows) is reports
    if rows:
        assert rows[0].trace


@pytest.mark.parametrize("sanitizers", [[], [FlowSanitizer("unmatched($X)", "value")]])
def test_finalizer_continuation_without_matching_sanitizer(tmp_path, sanitizers):
    path = tmp_path / "app.py"
    path.write_text("def f():\n    x = source()\n    try:\n        if flag: return\n        x = 0\n    finally: pass\n    sink(x)\n")
    config = CompiledFlowConfig(sources=[FlowSource("source()", "value")],
        sinks=[FlowSink("sink($X)", "value", "unsafe")], sanitizers=sanitizers)
    assert not evaluate_flow_config(config, [str(path)], project_path=str(tmp_path))


@pytest.mark.parametrize("returns,reports", [(False, True), (True, False)])
def test_source_created_inside_finalizer(tmp_path, returns, reports):
    path = tmp_path / "app.py"
    path.write_text("def f():\n    try:\n        " + ("return" if returns else "pass")
                    + "\n    finally: x = source()\n    sink(x)\n")
    config = CompiledFlowConfig(sources=[FlowSource("source()", "value")],
        sinks=[FlowSink("sink($X)", "value", "unsafe")])
    assert bool(evaluate_flow_config(config, [str(path)], project_path=str(tmp_path))) is reports


@pytest.mark.parametrize("finalizer,reports", [
    ("return 0", False), ("clean(x)", False), ("pass", True), ("x = 0", True),
])
def test_call_returns_after_finalizer(tmp_path, finalizer, reports):
    path = tmp_path / "app.py"
    path.write_text("def g():\n    x = source()\n    try: return x\n    finally: " + finalizer
                    + "\ndef f(): sink(g())\n")
    config = CompiledFlowConfig(sources=[FlowSource("source()", "value")],
        sinks=[FlowSink("sink($X)", "value", "unsafe")],
        sanitizers=[FlowSanitizer("clean($X)", "value")])
    assert bool(evaluate_flow_config(config, [str(path)], project_path=str(tmp_path))) is reports


def test_eventless_finalizer_loop_extraction_terminates():
    from emend import emend_core
    # No occurrence inside the loop can truncate empty-block closure.
    emend_core.build_flow_facts("def f():\n    source()\n    while True:\n        try: continue\n        finally: pass\n", "py")


@pytest.mark.parametrize("callee", [
    "raise ValueError()",
    "try: return source()\nfinally: raise ValueError()",
])
def test_throw_does_not_complete_a_call(tmp_path, callee):
    path = tmp_path / "app.py"
    path.write_text("def g():\n" + "".join("    " + line + "\n" for line in callee.splitlines())
                    + "def f():\n    clean(0)\n    x = source()\n    sink(g())\n    sink(x)\n")
    config = CompiledFlowConfig(sources=[FlowSource("source()", "value")],
        sinks=[FlowSink("sink($X)", "value", "unsafe")],
        sanitizers=[FlowSanitizer("clean($X)", "value")])
    assert not evaluate_flow_config(config, [str(path)], project_path=str(tmp_path))


@pytest.mark.parametrize("outer_return,reports", [(False, False), (True, True)])
def test_canceled_nested_return_restores_pending_outer_return(tmp_path, outer_return, reports):
    nested = "try:\n    try: return 0\n    finally: raise ValueError()\nexcept Exception: pass"
    if outer_return:
        body = "try: return source()\nfinally:\n" + "".join("    " + line + "\n" for line in nested.splitlines())
    else:
        body = nested.replace("return 0", "return source()")
    path = tmp_path / "app.py"
    path.write_text("def g():\n" + "".join("    " + line + "\n" for line in body.splitlines())
                    + "def f(): sink(g())\n")
    config = CompiledFlowConfig(sources=[FlowSource("source()", "value")],
        sinks=[FlowSink("sink($X)", "value", "unsafe")])
    assert bool(evaluate_flow_config(config, [str(path)], project_path=str(tmp_path))) is reports
