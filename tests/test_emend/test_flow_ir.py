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
    _flow_witness_to_steps,
)
from emend.transform import PatternMatch


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


class TestFlowWitnessToSteps:
    def test_conversion(self):
        from emend.lint import FlowWitness

        fw = FlowWitness(
            source_line=2,
            source_text="request.args.get('q')",
            sink_line=5,
            sink_text="cursor.execute(raw)",
            taint_chain=[(2, "raw"), (3, "data")],
        )
        steps = _flow_witness_to_steps(fw, "app.py")
        assert len(steps) == 3  # source + 1 propagation (line 3) + sink
        assert steps[0].kind == "source"
        assert steps[0].line == 2
        assert steps[1].kind == "propagation"
        assert steps[1].line == 3
        assert steps[2].kind == "sink"
        assert steps[2].line == 5


# ---------------------------------------------------------------------------
# execute_flow_spec (Python fallback, no FactGraph)
# ---------------------------------------------------------------------------


class TestExecuteFlowSpecPython:
    def test_basic_detection(self, tmp_path):
        source = (
            "def handle():\n"
            "    user_input = request.args.get('name')\n"
            "    cursor.execute(user_input)\n"
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
        assert len(v.witness) >= 2  # at least source + sink

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
    def test_preserves_exact_match_locations(self, monkeypatch, tmp_path):
        class _FakeGraph:
            def symbols(self, **kwargs):
                return []

            def cfg_blocks(self, **kwargs):
                return []

            def source_locs(self, **kwargs):
                return []

            def flow_rule_check_datalog(self, **kwargs):
                assert kwargs["include_locations"] is True
                return [(str(src_file), "<module>", "raw", "raw", 0, 0)]

        src_file = tmp_path / "app.py"
        source = (
            "raw = request.args.get('name')\n"
            "cursor.execute(raw)\n"
        )
        src_file.write_text(source)

        spec = FlowSpec(
            name="sql-injection",
            message="SQL injection risk",
            sources="request.args.get($X)",
            sinks="cursor.execute($X)",
        )

        def _fake_find_pattern(pattern, file_path, **kwargs):
            if pattern == "request.args.get($X)":
                return [
                    PatternMatch(
                        node_text="request.args.get(raw)",
                        captures={"X": "raw"},
                        line=1,
                        matched_text="request.args.get(raw)",
                        col=7,
                    )
                ]
            if pattern == "cursor.execute($X)":
                return [
                    PatternMatch(
                        node_text="cursor.execute(raw)",
                        captures={"X": "raw"},
                        line=2,
                        matched_text="cursor.execute(raw)",
                        col=1,
                    )
                ]
            return []

        monkeypatch.setattr("emend.transform.find_pattern", _fake_find_pattern)

        violations = execute_flow_spec(
            spec,
            str(src_file),
            source,
            "python",
            fact_graph=_FakeGraph(),
        )

        assert len(violations) == 1
        violation = violations[0]
        assert violation.line == 2
        assert violation.source_line == 1
        assert violation.sink_text == "cursor.execute(raw)"
        assert len(violation.witness) == 2
        assert violation.witness[0].line == 1
        assert violation.witness[1].line == 2
