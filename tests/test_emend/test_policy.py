"""Tests for the policy engine (Phase 8)."""

import json

import pytest
import yaml
from typer.testing import CliRunner

from emend.cli import app
from emend.policy import (
    DatalogCheck,
    FlowCheck,
    Policy,
    PolicyViolation,
    StructuralCheck,
    CustomCheck,
    DeadCodeCheck,
    TypeCheck,
    _run_datalog_check,
    format_policy_violations,
    load_policies,
    run_policy_checks,
    validate_policies,
)

runner = CliRunner()


def _write_policies(tmp_path, policies_dict):
    """Write a YAML policy config file."""
    config_dir = tmp_path / ".emend"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "policies.yaml"
    config_file.write_text(yaml.dump(policies_dict))
    return str(config_file)


def _write_rules(tmp_path, rules_dict):
    """Write a unified rules config file."""
    config_dir = tmp_path / ".emend"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "rules.yaml"
    config_file.write_text(yaml.dump(rules_dict))
    return str(config_file)


class TestLoadPolicies:
    def test_load_structural_policy(self, tmp_path):
        config = _write_policies(tmp_path, {
            "policies": [{
                "name": "no-print",
                "description": "No print() calls",
                "severity": "warning",
                "checks": [{
                    "type": "structural",
                    "pattern": "print($X)",
                }],
            }],
        })
        policies = load_policies(config)
        assert len(policies) == 1
        assert policies[0].name == "no-print"
        assert len(policies[0].checks) == 1
        assert isinstance(policies[0].checks[0], StructuralCheck)

    def test_load_flow_policy(self, tmp_path):
        config = _write_policies(tmp_path, {
            "policies": [{
                "name": "no-sqli",
                "description": "No SQL injection",
                "severity": "error",
                "checks": [{
                    "type": "flow",
                    "flows-from": "request.args.get($X)",
                    "flows-to": "cursor.execute($Q)",
                    "label": "user_input",
                }],
            }],
        })
        policies = load_policies(config)
        assert len(policies) == 1
        check = policies[0].checks[0]
        assert isinstance(check, FlowCheck)
        assert check.flows_from == "request.args.get($X)"
        assert check.label == "user_input"

    def test_load_multiple_policies(self, tmp_path):
        config = _write_policies(tmp_path, {
            "policies": [
                {"name": "p1", "severity": "error", "checks": [{"type": "structural", "pattern": "eval($X)"}]},
                {"name": "p2", "severity": "warning", "checks": [{"type": "structural", "pattern": "exec($X)"}]},
            ],
        })
        policies = load_policies(config)
        assert len(policies) == 2

    def test_load_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_policies(str(tmp_path / "nonexistent.yaml"))

    def test_load_invalid_yaml(self, tmp_path):
        config = _write_policies(tmp_path, {"not_policies": []})
        with pytest.raises(ValueError, match="policies"):
            load_policies(config)

    def test_load_hyphenated_keys(self, tmp_path):
        """Hyphenated YAML keys (flows-from) should be accepted."""
        config = _write_policies(tmp_path, {
            "policies": [{
                "name": "test",
                "severity": "error",
                "checks": [{
                    "type": "flow",
                    "flows-from": "source($X)",
                    "flows-to": "sink($X)",
                    "not-through": "sanitize($X)",
                }],
            }],
        })
        policies = load_policies(config)
        check = policies[0].checks[0]
        assert isinstance(check, FlowCheck)
        assert check.not_through == "sanitize($X)"

    def test_load_unified_rules_yaml_as_policies(self, tmp_path):
        config_dir = tmp_path / ".emend"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "rules.yaml"
        config_file.write_text(yaml.dump({
            "rules": {
                "no-print": {
                    "match": "print($X)",
                    "message": "No print calls",
                    "severity": "warning",
                },
                "no-sqli": {
                    "flow": {
                        "from": "request.args.get($X)",
                        "to": "cursor.execute($Q)",
                    },
                    "message": "No SQL injection",
                    "severity": "error",
                },
            },
        }))

        policies = load_policies(str(config_file))
        assert {p.name for p in policies} == {"no-print", "no-sqli"}
        structural = next(p for p in policies if p.name == "no-print")
        flow = next(p for p in policies if p.name == "no-sqli")
        assert isinstance(structural.checks[0], StructuralCheck)
        assert isinstance(flow.checks[0], FlowCheck)

    def test_load_unified_rules_loads_rules_and_policies(self, tmp_path):
        config_path = _write_rules(tmp_path, {
            "macros": {"input": "request.args.get($X)"},
            "rules": {
                "no-print": {
                    "match": "print($X)",
                    "message": "No print",
                    "severity": "warning",
                },
                "no-sqli": {
                    "flow": {
                        "from": "{input}",
                        "to": "cursor.execute($Q)",
                        "not-through": "escape($X)",
                    },
                    "message": "No SQL injection",
                    "severity": "error",
                },
                "deadcode-check": {
                    "deadcode": {
                        "entry-point-names": ["main"],
                    },
                    "message": "Dead code policy",
                },
            },
        })
        policies = load_policies(config_path)
        by_name = {p.name: p for p in policies}
        assert "no-print" in by_name
        assert "no-sqli" in by_name
        assert "deadcode-check" in by_name
        assert isinstance(by_name["no-print"].checks[0], StructuralCheck)
        assert isinstance(by_name["no-sqli"].checks[0], FlowCheck)
        assert by_name["no-sqli"].checks[0].flows_from == "request.args.get($X)"
        assert isinstance(by_name["deadcode-check"].checks[0], DeadCodeCheck)

    def test_load_unified_rules_flow_dict_form_pattern(self, tmp_path):
        """Dict-form ``from: {pattern: ...}`` / ``to: {pattern: ...}`` must be
        unwrapped to the pattern string, matching the lint engine
        (checks/pattern_rules.py)."""
        config_path = _write_rules(tmp_path, {
            "rules": {
                "no-sqli": {
                    "flow": {
                        "from": {"pattern": "request.args.get($X)"},
                        "to": {"pattern": "cursor.execute($Q)"},
                    },
                    "message": "No SQL injection",
                    "severity": "error",
                },
            },
        })
        policies = load_policies(config_path)
        check = next(p for p in policies if p.name == "no-sqli").checks[0]
        assert isinstance(check, FlowCheck)
        assert check.flows_from == "request.args.get($X)"
        assert check.flows_to == "cursor.execute($Q)"

    def test_load_unified_rules_flow_list_not_through(self, tmp_path):
        """List-valued ``not-through`` must be pipe-joined, not stringified."""
        config_path = _write_rules(tmp_path, {
            "rules": {
                "no-sqli": {
                    "flow": {
                        "from": "request.args.get($X)",
                        "to": "cursor.execute($Q)",
                        "not-through": ["escape($X)", "sanitize($X)"],
                    },
                    "message": "No SQL injection",
                    "severity": "error",
                },
            },
        })
        policies = load_policies(config_path)
        check = next(p for p in policies if p.name == "no-sqli").checks[0]
        assert isinstance(check, FlowCheck)
        # Must mirror checks/pattern_rules.py expand_not_through(): pipe-joined,
        # never the python list repr "['escape($X)', 'sanitize($X)']".
        assert check.not_through == "escape($X) | sanitize($X)"

    def test_load_unified_rules_top_level_deadcode(self, tmp_path):
        config_path = _write_rules(tmp_path, {
            "deadcode": {
                "entry-point-decorators": ["app.command"],
            },
            "rules": {
                "no-print": {"match": "print($X)", "message": "No print"},
            },
        })
        policies = load_policies(config_path)
        names = {p.name for p in policies}
        assert "no-print" in names
        assert "deadcode" in names


    def test_load_policies_null_value(self, tmp_path):
        """YAML with `policies:` (null value) should not crash."""
        config_path = _write_rules(tmp_path, {"policies": None})
        policies = load_policies(config_path)
        assert policies == []

    def test_load_sequence_null_path(self, tmp_path):
        """Sequence check with `path:` (null value) should not crash."""
        config_path = _write_policies(tmp_path, {
            "policies": [{
                "name": "test-seq",
                "severity": "error",
                "description": "test",
                "checks": [{
                    "type": "sequence",
                    "name": "my-seq",
                    "message": "seq violation",
                    "sequence": [
                        {"bind": "a", "pattern": "$X = input()"},
                        {"bind": "b", "pattern": "eval($X)"},
                    ],
                    "path": None,
                }],
            }],
        })
        policies = load_policies(config_path)
        assert len(policies) == 1


class TestValidatePolicies:
    def test_valid_policy(self):
        p = Policy("test", "desc", "error", [StructuralCheck("print($X)")])
        assert validate_policies([p]) == []

    def test_invalid_severity(self):
        p = Policy("test", "desc", "critical", [StructuralCheck("print($X)")])
        errors = validate_policies([p])
        assert len(errors) == 1
        assert "severity" in errors[0]

    def test_no_checks(self):
        p = Policy("test", "desc", "error", [])
        errors = validate_policies([p])
        assert any("at least one check" in e for e in errors)

    def test_duplicate_names(self):
        p1 = Policy("same", "desc1", "error", [StructuralCheck("x")])
        p2 = Policy("same", "desc2", "error", [StructuralCheck("y")])
        errors = validate_policies([p1, p2])
        assert any("duplicate" in e for e in errors)


class TestStructuralCheck:
    def test_detects_pattern(self, tmp_path):
        test_file = tmp_path / "app.py"
        test_file.write_text("print('hello')\nprint('world')\n")

        policies = [Policy(
            name="no-print",
            description="No print calls",
            severity="warning",
            checks=[StructuralCheck(pattern="print($X)")],
        )]

        violations = run_policy_checks([str(test_file)], policies)
        assert len(violations) >= 1
        assert violations[0].policy_name == "no-print"
        assert violations[0].severity == "warning"

    def test_structural_check_via_run_checks(self, tmp_path):
        """StructuralCheck policies should not be filtered out in unified checks."""
        from emend.checks.engine import run_checks
        config_file = _write_rules(tmp_path, {
            "policies": [{
                "name": "no-eval",
                "severity": "error",
                "description": "No eval calls",
                "checks": [{"type": "structural", "pattern": "eval($X)"}],
            }],
        })
        test_file = tmp_path / "app.py"
        test_file.write_text("result = eval('1+1')\n")

        violations = run_checks(
            [str(test_file)],
            config=config_file,
            mode="policy",
        )
        assert len(violations) >= 1
        assert any(v.rule_name == "no-eval" for v in violations)


class TestFormatViolations:
    def test_text_format(self):
        violations = [
            PolicyViolation("app.py", 10, 0, "no-print", "structural", "warning", "No print"),
        ]
        output = format_policy_violations(violations)
        assert "no-print" in output
        assert "app.py:10" in output
        assert "WARNING" in output

    def test_json_format(self):
        violations = [
            PolicyViolation("app.py", 10, 0, "no-print", "structural", "warning", "No print"),
        ]
        output = format_policy_violations(violations, json_output=True)
        data = json.loads(output)
        assert len(data) == 1
        assert data[0]["policy"] == "no-print"

    def test_empty_violations_text(self):
        output = format_policy_violations([])
        assert "No policy violations" in output

    def test_empty_violations_json(self):
        output = format_policy_violations([], json_output=True)
        assert json.loads(output) == []

    def test_witness_in_output(self):
        violations = [
            PolicyViolation("app.py", 10, 0, "test", "flow", "error", "msg", ["source L1: x", "sink L5: y"]),
        ]
        output = format_policy_violations(violations)
        assert "source L1: x" in output


def test_check_cli_uses_rules_yaml(tmp_path, monkeypatch):
    config_dir = tmp_path / ".emend"
    config_dir.mkdir()
    (config_dir / "rules.yaml").write_text(yaml.dump({
        "rules": {
            "no-print": {
                "match": "print($X)",
                "message": "Use logging",
            }
        }
    }))
    test_file = tmp_path / "app.py"
    test_file.write_text("print('hello')\n")

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["check", str(tmp_path)])

    assert result.exit_code == 1
    assert "no-print" in result.stdout


class TestDatalogCheckColumnIndexing:
    """Regression tests for _run_datalog_check column index extraction.

    _col_idx() returns int|None.  Using ``or`` to chain fallbacks treats
    index **0** as falsy, silently skipping the first column.
    """

    def _make_policy(self):
        check = DatalogCheck(cozoscript="?[file_path, line, message] <- ...")
        return Policy(
            name="test-policy",
            description="test description",
            severity="error",
            checks=[check],
        )

    def _run_with_mock_result(self, monkeypatch, headers, rows):
        """Call _run_datalog_check with a mocked FactGraph."""
        import emend.fact_graph

        class _MockGraph:
            def run_query(self, query):
                return {"headers": headers, "rows": rows}

        class _MockFactGraphCls:
            @staticmethod
            def build_from_project(path):
                return _MockGraph()

        monkeypatch.setattr(emend.fact_graph, "FactGraph", _MockFactGraphCls)
        policy = self._make_policy()
        return _run_datalog_check(policy.checks[0], policy, "/tmp/fake")

    def test_file_path_at_column_zero(self, monkeypatch):
        """file_path at index 0 must not be skipped by the or-chain."""
        violations = self._run_with_mock_result(
            monkeypatch,
            headers=["file_path", "line", "message"],
            rows=[["app.py", 42, "problem"]],
        )
        assert len(violations) == 1
        assert violations[0].file_path == "app.py"
        assert violations[0].line == 42
        assert violations[0].message == "problem"

    def test_line_at_column_zero(self, monkeypatch):
        """line at index 0 must not be skipped by the or-chain."""
        violations = self._run_with_mock_result(
            monkeypatch,
            headers=["line", "file_path", "message"],
            rows=[[10, "foo.py", "issue"]],
        )
        assert len(violations) == 1
        assert violations[0].line == 10
        assert violations[0].file_path == "foo.py"

    def test_file_path_at_zero_with_file_column_present(self, monkeypatch):
        """file_path at 0 must take priority over a later 'file' column."""
        violations = self._run_with_mock_result(
            monkeypatch,
            headers=["file_path", "line", "file", "message"],
            rows=[["correct.py", 1, "wrong.py", "msg"]],
        )
        assert len(violations) == 1
        assert violations[0].file_path == "correct.py"

    def test_no_file_or_line_columns_uses_sentinel(self, monkeypatch):
        """A query without file/line columns must not misinterpret data.

        e.g. ``?[count, name]`` should not stringify the count as a file path
        nor try to coerce the name into an int. Instead, a sentinel file path
        and line 0 are used, and the full row is preserved in the witness.
        """
        violations = self._run_with_mock_result(
            monkeypatch,
            headers=["count", "name"],
            rows=[[3, "alpha"]],
        )
        assert len(violations) == 1
        v = violations[0]
        # Must NOT guess column 0 (count=3) as the file path.
        assert v.file_path == "<project>"
        # Must NOT attempt int("alpha") from column 1 as the line.
        assert v.line == 0
        # The full row stays available in the witness so nothing is lost.
        assert v.witness == ["count=3", "name=alpha"]

    def test_non_numeric_line_value_does_not_crash(self, monkeypatch):
        """A named 'line' column with a non-numeric value must not crash."""
        violations = self._run_with_mock_result(
            monkeypatch,
            headers=["file_path", "line", "message"],
            rows=[["app.py", "not-a-number", "boom"]],
        )
        assert len(violations) == 1
        v = violations[0]
        assert v.file_path == "app.py"
        # Falls back to 0 rather than raising ValueError on int("not-a-number").
        assert v.line == 0
        assert v.message == "boom"
        assert v.witness == ["file_path=app.py", "line=not-a-number", "message=boom"]

    def test_single_unnamed_column_not_treated_as_file(self, monkeypatch):
        """A single unnamed column must not be guessed as the file path."""
        violations = self._run_with_mock_result(
            monkeypatch,
            headers=["symbol"],
            rows=[["my_func"]],
        )
        assert len(violations) == 1
        v = violations[0]
        assert v.file_path == "<project>"
        assert v.line == 0
        assert v.witness == ["symbol=my_func"]
