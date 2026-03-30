"""Tests for the policy engine (Phase 8)."""

import json

import pytest
import yaml
from typer.testing import CliRunner

from emend.cli import app
from emend.policy import (
    FlowCheck,
    Policy,
    PolicyViolation,
    StructuralCheck,
    CustomCheck,
    DeadCodeCheck,
    TypeCheck,
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

    def test_load_unified_rules_via_legacy_policies_path(self, tmp_path):
        _write_rules(tmp_path, {
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
        policies = load_policies(str(tmp_path / ".emend" / "policies.yaml"))
        by_name = {p.name: p for p in policies}
        assert "no-print" in by_name
        assert "no-sqli" in by_name
        assert "deadcode-check" in by_name
        assert isinstance(by_name["no-print"].checks[0], StructuralCheck)
        assert isinstance(by_name["no-sqli"].checks[0], FlowCheck)
        assert by_name["no-sqli"].checks[0].flows_from == "request.args.get($X)"
        assert isinstance(by_name["deadcode-check"].checks[0], DeadCodeCheck)

    def test_load_unified_rules_top_level_deadcode(self, tmp_path):
        _write_rules(tmp_path, {
            "deadcode": {
                "entry-point-decorators": ["app.command"],
            },
            "rules": {
                "no-print": {"match": "print($X)", "message": "No print"},
            },
        })
        policies = load_policies(str(tmp_path / ".emend" / "policies.yaml"))
        names = {p.name for p in policies}
        assert "no-print" in names
        assert "deadcode" in names


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
