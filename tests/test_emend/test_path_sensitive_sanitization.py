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
