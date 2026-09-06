"""Tests for flow_ir.py — shared IR, witness model, and execute_flow_spec."""

import pytest

from emend.flow_ir import (
    FlowSpec,
    FlowViolation,
    WitnessStep,
    execute_flow_spec,
    format_witness,
    from_flow_check,
    from_lint_rule,
)


# ---------------------------------------------------------------------------
# FlowSpec construction
# ---------------------------------------------------------------------------


class TestFlowSpec:
    def test_from_lint_rule(self):
        from emend.lint import LintRule

        rule = LintRule(
            name="sql-injection",
            find="",
            message="SQL injection risk",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($Q)",
            not_through="sanitize($X)",
        )
        spec = from_lint_rule(rule)
        assert spec.name == "sql-injection"
        assert spec.message == "SQL injection risk"
        assert spec.sources == "request.args.get($X)"
        assert spec.sinks == "cursor.execute($Q)"
        assert spec.sanitizers == "sanitize($X)"
        assert spec.severity == "warning"

    def test_from_flow_check(self):
        from emend.policy import FlowCheck

        check = FlowCheck(
            flows_from="get_input($X)",
            flows_to="execute($Q)",
            not_through="validate($X)",
            label="input-validation",
        )
        spec = from_flow_check(check, "my-policy", "Must validate input", "error")
        assert spec.name == "my-policy"
        assert spec.message == "Must validate input"
        assert spec.sources == "get_input($X)"
        assert spec.sinks == "execute($Q)"
        assert spec.sanitizers == "validate($X)"
        assert spec.severity == "error"
        assert spec.label == "input-validation"

    def test_from_flow_check_no_label(self):
        from emend.policy import FlowCheck

        check = FlowCheck(flows_from="src($X)", flows_to="sink($X)")
        spec = from_flow_check(check, "pol", "desc")
        assert spec.label == "pol"


# ---------------------------------------------------------------------------
# Witness formatting
# ---------------------------------------------------------------------------


class TestFormatWitness:
    def test_empty(self):
        assert format_witness([]) == []

    def test_basic_steps(self):
        steps = [
            WitnessStep(file_path="a.py", func_qn="f", block_id=0,
                        line=5, var_name="user_input", kind="source"),
            WitnessStep(file_path="a.py", func_qn="f", block_id=0,
                        line=7, var_name="data", kind="propagation"),
            WitnessStep(file_path="a.py", func_qn="f", block_id=0,
                        line=10, var_name="cursor.execute(data)", kind="sink"),
        ]
        lines = format_witness(steps)
        assert len(lines) == 3
        assert "source L5" in lines[0]
        assert "user_input" in lines[0]
        assert "propagation L7" in lines[1]
        assert "sink L10" in lines[2]

    def test_step_without_var(self):
        steps = [
            WitnessStep(file_path="a.py", func_qn="f", block_id=0,
                        line=1, kind="step"),
        ]
        lines = format_witness(steps)
        assert lines == ["step L1"]




# ---------------------------------------------------------------------------
# execute_flow_spec (Python fallback, no FactGraph)
# ---------------------------------------------------------------------------


class TestExecuteFlowSpecPython:
    def test_basic_detection(self, tmp_path):
        source = (
            "def handle():\n"
            "    user_input = request.args.get('name')\n"
            "    data = user_input\n"
            "    cursor.execute(data)\n"
        )
        f = tmp_path / "app.py"
        f.write_text(source)

        spec = FlowSpec(
            name="sql-injection",
            message="SQL injection risk",
            sources="request.args.get($X)",
            sinks="cursor.execute($Q)",
        )
        violations = execute_flow_spec(spec, str(f), source, "python")
        assert len(violations) >= 1
        v = violations[0]
        assert v.spec_name == "sql-injection"
        assert v.file_path == str(f)
        assert v.source_text
        assert v.sink_text
        assert [(step.kind, step.line) for step in v.witness] == [
            ("source", 2), ("propagation", 3), ("sink", 4),
        ]

    def test_sanitizer_blocks(self, tmp_path):
        source = (
            "def handle():\n"
            "    user_input = request.args.get('name')\n"
            "    clean = sanitize(user_input)\n"
            "    cursor.execute(clean)\n"
        )
        f = tmp_path / "app.py"
        f.write_text(source)

        spec = FlowSpec(
            name="sql-injection",
            message="SQL injection risk",
            sources="request.args.get($X)",
            sinks="cursor.execute($Q)",
            sanitizers="sanitize($X)",
        )
        violations = execute_flow_spec(spec, str(f), source, "python")
        assert len(violations) == 0

    def test_no_sources_no_violations(self, tmp_path):
        source = "def handle():\n    cursor.execute('SELECT 1')\n"
        f = tmp_path / "app.py"
        f.write_text(source)

        spec = FlowSpec(
            name="test",
            message="test",
            sources="request.args.get($X)",
            sinks="cursor.execute($Q)",
        )
        violations = execute_flow_spec(spec, str(f), source, "python")
        assert violations == []


# ---------------------------------------------------------------------------
# FlowViolation model
# ---------------------------------------------------------------------------


class TestFlowViolation:
    def test_defaults(self):
        v = FlowViolation(
            spec_name="test",
            message="msg",
            severity="warning",
            file_path="a.py",
            line=10,
        )
        assert v.witness == []
        assert v.col == 0
        assert v.source_text == ""
        assert v.sink_text == ""


class TestExecuteFlowSpecDatalog:
    def test_preserves_exact_match_locations(self, tmp_path):
        from emend.fact_graph import FactGraph

        src_file = tmp_path / "app.py"
        source = "def handle(raw):\n    read(raw)\n    sink(raw)\n"
        src_file.write_text(source)
        graph = FactGraph.build_from_files([str(src_file)])
        spec = FlowSpec(name="unsafe", message="Unsafe input",
                        sources="read($X)", sinks="sink($X)")
        violations = execute_flow_spec(spec, str(src_file), source, "python", graph)

        assert len(violations) == 1
        violation = violations[0]
        assert (violation.source_line, violation.line, violation.col) == (2, 3, 4)
        assert violation.source_text == "read(raw)"
        assert violation.sink_text == "sink(raw)"
        assert [(s.kind, s.line, s.col) for s in violation.witness] == [
            ("source", 2, 4), ("sink", 3, 4),
        ]
