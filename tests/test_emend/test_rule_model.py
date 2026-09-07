"""Canonical rule-document compilation contracts."""

from dataclasses import FrozenInstanceError

import pytest

from emend.checks.rule_model import (
    CompiledFlowRule,
    CompiledStructuralCheck,
)
from emend.checks.rules_config import compile_rules_document


def test_compile_rules_document_expands_once_into_immutable_payloads():
    compiled = compile_rules_document({
        "macros": {"input": "request.args.get($X)"},
        "exclude-paths": ["vendor/**"],
        "rules": {
            "no-print": {
                "find": "print($X)",
                "not-within": "debug_mode()",
                "files": "src/**/*.py",
            },
            "sql": {
                "flow": {
                    "from": {"pattern": "{input}", "type_constraint": "str"},
                    "to": {"pattern": "cursor.execute($Q)", "type_constraint": "Cursor"},
                    "not-through": ["quote($X)", {"pattern": "escape($X)"}],
                    "scope-sanitizers": "session.commit()",
                    "quantifier": "some_path",
                    "interprocedural": True,
                },
                "label": "untrusted",
                "language": ["python", "typescript"],
                "enabled": False,
            },
        },
        "deadcode": {"kind": "function", "exclude-paths": "generated/**"},
        "duplicate-code": {"min-lines": 8, "cross-file-only": False},
    })

    assert dict(compiled.macros) == {"input": "request.args.get($X)"}
    assert compiled.pattern_rules[0].pattern == "print($X)"
    assert compiled.pattern_rules[0].files == ("src/**/*.py",)
    assert compiled.deadcode and compiled.deadcode.exclude_paths == ("generated/**",)
    assert compiled.duplicate and compiled.duplicate.min_lines == 8

    flow = compiled.flow_rules[0]
    assert flow.rule_id == "rule:sql:flow"
    assert flow.label == "untrusted"
    assert flow.sources[0].pattern == "request.args.get($X)"
    assert flow.sources[0].type_constraint == "str"
    assert flow.sinks[0].type_constraint == "Cursor"
    assert tuple(endpoint.pattern for endpoint in flow.sanitizers) == (
        "quote($X)", "escape($X)",
    )
    assert flow.scope_sanitizers[0].pattern == "session.commit()"
    assert flow.quantifier == "some_path"
    assert flow.languages == ("python", "typescript")
    assert flow.exclude_paths == ("vendor/**",)
    assert flow.enabled is False
    assert flow.options["interprocedural"] is True

    with pytest.raises(FrozenInstanceError):
        flow.label = "changed"
    with pytest.raises(TypeError):
        compiled.macros["input"] = "changed"


def test_compiled_flow_ids_are_distinct_when_labels_are_shared():
    compiled = compile_rules_document({
        "rules": {
            "first": {"flow": {"from": "a($X)", "to": "sink($X)"}, "label": "same"},
            "second": {"flow": {"from": "b($X)", "to": "sink($X)"}, "label": "same"},
        },
        "policies": [{
            "name": "explicit",
            "checks": [
                {"type": "flow", "flows-from": "c($X)", "flows-to": "sink($X)", "label": "same"},
                {"type": "flow", "flows-from": "d($X)", "flows-to": "sink($X)", "label": "same"},
            ],
        }],
        "trace": {
            "sources": [
                {"pattern": "e($X)", "label": "same"},
                {"pattern": "f($X)", "label": "same"},
            ],
            "sinks": [{"pattern": "sink($X)", "label": "same"}],
        },
    })

    flows = compiled.all_flow_rules
    assert len(flows) == 6
    assert len({flow.rule_id for flow in flows}) == len(flows)
    assert {flow.label for flow in flows} == {"same"}
    assert {flow.rule_id for flow in compiled.trace.flow_rules} == {
        "trace:same:source:0:sink:0",
        "trace:same:source:1:sink:0",
    }


def test_compile_policy_payloads_are_typed_and_macro_expanded_for_unified_rules():
    compiled = compile_rules_document({
        "macros": {"log": "print($X)"},
        "rules": {
            "no-log": {"match": "{log}", "message": "No logging"},
        },
        "policies": [{
            "name": "types",
            "description": "Public API types",
            "severity": "error",
            "checks": [{
                "type": "type",
                "symbol-pattern": "$F($ARGS)",
                "expected-type": "str",
                "kind": "returns",
            }],
        }],
    })

    unified = compiled.unified_policies[0]
    assert isinstance(unified.checks[0].payload, CompiledStructuralCheck)
    assert unified.checks[0].payload.pattern == "print($X)"
    explicit = compiled.policies[0]
    assert explicit.name == "types"
    assert explicit.checks[0].kind == "type"
    assert explicit.checks[0].payload.expected_type == "str"


def test_compile_trace_preserves_declarations_and_builds_effect_flows():
    compiled = compile_rules_document({
        "presets": ["flask"],
        "trace": {
            "labels": ["dirty"],
            "sources": [{"pattern": "source($X)", "label": "dirty"}],
            "sinks": [{"effect": "writes($OBJ)", "label": "dirty", "message": "write"}],
            "sanitizers": [{
                "pattern": "clean($X)", "label": "dirty", "quantifier": "some_path",
            }],
            "scope_sanitizers": [{"pattern": "commit()", "label": "dirty"}],
            "exclude_paths": "migrations/**",
            "presets": ["django", "flask"],
            "language": "python",
            "files": "src/**",
            "enabled": False,
        },
    })

    trace = compiled.trace
    assert trace.labels == ("dirty",)
    assert trace.presets == ("flask", "django")
    assert trace.exclude_paths == ("migrations/**",)
    assert trace.sources[0].endpoint.pattern == "source($X)"
    assert trace.sinks[0].endpoint.effect == "writes($OBJ)"
    flow = trace.flow_rules[0]
    assert isinstance(flow, CompiledFlowRule)
    assert flow.sinks[0].pattern == ""
    assert flow.sinks[0].effect == "writes($OBJ)"
    assert flow.quantifier == "some_path"
    assert flow.scope_sanitizers[0].pattern == "commit()"
    assert flow.languages == ("python",)
    assert flow.files == ("src/**",)
    assert flow.enabled is False


def test_legacy_adapters_share_one_compiled_document(monkeypatch):
    from emend.checks.pattern_rules import load_rules
    from emend.lint import load_duplicate_code_config
    from emend.policy import load_policies

    document = compile_rules_document({
        "rules": {"no-print": {"find": "print($X)", "message": "no"}},
        "duplicate-code": True,
        "policies": [{
            "name": "explicit", "checks": [{"type": "structural", "pattern": "eval($X)"}],
        }],
    })

    def reopened(*_args, **_kwargs):
        raise AssertionError("adapter reopened rules.yaml")

    monkeypatch.setattr("emend.checks.pattern_rules.load_compiled_rules", reopened)
    monkeypatch.setattr("emend.lint.load_compiled_rules", reopened)
    monkeypatch.setattr("emend.policy.load_compiled_rules", reopened)
    assert load_rules(compiled=document)[0][0].name == "no-print"
    assert load_duplicate_code_config(compiled=document).enabled
    assert load_policies(compiled=document)[0].name == "explicit"
