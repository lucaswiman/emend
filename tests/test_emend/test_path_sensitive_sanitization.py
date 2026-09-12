"""Regression tests for path-sensitive sanitizer semantics."""

from pathlib import Path

import pytest

from emend.trace import TraceConfig, TraceSanitizer, TraceSink, TraceSource, run_trace_analysis


def _run(path: Path, source: str, sink: str = "sink", sanitizer: str = "validate"):
    path.write_text(source)
    return run_trace_analysis(
        [str(path)],
        TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(f"get_user_input()", "sqli")],
            sinks=[TraceSink(f"{sink}($X)", "sqli", "SQL injection")],
            sanitizers=[TraceSanitizer(f"{sanitizer}($X)", "sqli")],
        ),
    )


@pytest.mark.parametrize(
    ("body", "sink", "expected"),
    [
        ("if should_validate:\n        x = validate(x)\n    sink(x)", "sink", True),
        ("if should_validate:\n        x = validate(x)\n    else:\n        x = validate(x)\n    sink(x)", "sink", False),
        ("if flag:\n        y = validate(x)\n    sink(x)", "sink", True),
        ("validate(x)\n    sink(x)", "sink", False),
        ("execute(x)\n    x = sanitize(x)", "execute", True),
        ("x = sanitize(x)\n    execute(x)", "execute", False),
    ],
)
def test_sanitizer_path_and_source_order(tmp_path, body, sink, expected):
    source = "def handler():\n    x = get_user_input()\n    " + body + "\n"
    assert bool(_run(tmp_path / "app.py", source, sink, "sanitize" if sink == "execute" else "validate")) is expected


def test_cfg_free_flow_is_fail_closed(tmp_path):
    violations = _run(
        tmp_path / "app.py",
        "def handler():\n    x = get_user_input()\n    execute(x)\n",
        "execute",
        "sanitize",
    )
    assert violations


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Markup(x)", True),
        ("Markup(escape(x))", False),
        ("Markup(x + escape(x))", True),
        ("escaped = escape(x)\n    Markup(x)", True),
        ("escape(x)\n    Markup(x)", True),
        ("escaped = escape(x)\n    Markup(escaped)", False),
        ("alias = x\n    x = escape(x)\n    Markup(alias)", True),
        ("x = escape(x)\n    Markup(x)", False),
        ("if flag:\n        x = escape(x)\n    Markup(x)", True),
        ("if flag:\n        x = escape(x)\n    else:\n        x = escape(x)\n    Markup(x)", False),
    ],
)
def test_preset_sanitizer_cleans_only_returned_value(tmp_path, body, expected):
    from emend.trace_presets import get_preset

    path = tmp_path / "app.py"
    path.write_text('def handler():\n    x = request.args.get("q")\n    ' + body + '\n')
    assert bool(run_trace_analysis([str(path)], get_preset("flask"))) is expected


@pytest.mark.parametrize("quantifier", ["all_paths", "some_path"])
def test_return_sanitizer_quantifier(tmp_path, quantifier):
    path = tmp_path / "app.py"
    path.write_text("def handler():\n    x = source()\n    if flag:\n        x = clean(x)\n    sink(x)\n")
    config = TraceConfig(
        sources=[TraceSource("source()", "value")],
        sinks=[TraceSink("sink($X)", "value", "unsafe")],
        sanitizers=[TraceSanitizer("clean($X)", "value", quantifier, effect="returns")],
    )
    assert bool(run_trace_analysis([str(path)], config)) is (quantifier == "all_paths")


def test_unknown_sanitizer_effect_rejected(tmp_path):
    config = TraceConfig(
        sources=[TraceSource("source()", "value")],
        sinks=[TraceSink("sink($X)", "value", "unsafe")],
        sanitizers=[TraceSanitizer("clean($X)", "value", effect="return")],
    )
    with pytest.raises(ValueError, match="Unknown sanitizer effect"):
        run_trace_analysis([str(tmp_path / "app.py")], config)
