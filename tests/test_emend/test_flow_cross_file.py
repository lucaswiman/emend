"""Cross-file occurrence links use resolved call identities and call spans."""

from pathlib import Path

from emend.checks.flow import (
    CompiledFlowConfig,
    FlowSink,
    FlowSource,
    evaluate_flow_config,
)


def test_cross_file_call_links_are_exact_and_same_line_calls_are_isolated(tmp_path: Path):
    (tmp_path / "lib.py").write_text(
        "def helper(value):\n"
        "    sink(value)\n"
    )
    (tmp_path / "app.py").write_text(
        "from lib import helper\n"
        "def run():\n"
        "    helper(source()); helper(0)\n"
    )
    config = CompiledFlowConfig(
        labels=["value"],
        sources=[FlowSource("source()", "value")],
        sinks=[FlowSink("sink($X)", "value", "tainted")],
    )

    violations = evaluate_flow_config(
        config,
        [str(tmp_path / "app.py"), str(tmp_path / "lib.py")],
        language="python",
        project_path=str(tmp_path),
    )

    assert [(violation.file_path, violation.line) for violation in violations] == [
        (str(tmp_path / "lib.py"), 2),
    ]
    assert any(step.file_path == str(tmp_path / "app.py") for step in violations[0].trace)


def test_same_named_functions_in_different_files_do_not_cross_contaminate(tmp_path: Path):
    (tmp_path / "safe.py").write_text(
        "def helper(value):\n"
        "    sink(value)\n"
    )
    (tmp_path / "other.py").write_text(
        "def helper(value):\n"
        "    sink(value)\n"
    )
    (tmp_path / "app.py").write_text(
        "from safe import helper\n"
        "def run():\n"
        "    helper(source())\n"
    )
    config = CompiledFlowConfig(
        labels=["value"],
        sources=[FlowSource("source()", "value")],
        sinks=[FlowSink("sink($X)", "value", "tainted")],
    )

    violations = evaluate_flow_config(
        config,
        [str(tmp_path / name) for name in ("app.py", "safe.py", "other.py")],
        language="python",
        project_path=str(tmp_path),
    )

    assert [(violation.file_path, violation.line) for violation in violations] == [
        (str(tmp_path / "safe.py"), 2),
    ]
