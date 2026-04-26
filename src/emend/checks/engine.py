"""Unified rule runner for ``.emend/rules.yaml``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from emend.lint import LintViolation, LintRule
    from emend.policy import PolicyViolation, Policy, FlowCheck, StructuralCheck, TypeCheck, DeadCodeCheck, DatalogCheck, CustomCheck, SequenceCheck
    from emend.rules_config import DeadCodeConfig


@dataclass
class CheckViolation:
    """Normalized violation shape shared by CLI and MCP."""
    rule_name: str
    kind: str
    severity: str
    message: str
    file_path: str
    line: int
    col: int = 0
    witness: list[str] | None = None


def _lint_kind(rule: "LintRule") -> str:
    if rule.flows_from and rule.flows_to:
        return "flow"
    return "match"


def _policy_check_kind(check: Any) -> str:
    from emend.policy import (
        FlowCheck, StructuralCheck, TypeCheck, DeadCodeCheck,
        DatalogCheck, SequenceCheck, CustomCheck,
    )
    if isinstance(check, FlowCheck):
        return "flow"
    if isinstance(check, StructuralCheck):
        return "match"
    if isinstance(check, TypeCheck):
        return "type"
    if isinstance(check, DeadCodeCheck):
        return "deadcode"
    if isinstance(check, DatalogCheck):
        return "datalog"
    if isinstance(check, SequenceCheck):
        return "sequence"
    if isinstance(check, CustomCheck):
        return "custom"
    return "unknown"


def _filter_policies(
    policies: "list[Policy]",
    *,
    rule_name: str | None,
    kind: str | None,
    allowed_kinds: set[str],
) -> "list[Policy]":
    from emend.policy import Policy

    filtered: list[Policy] = []
    for policy in policies:
        if rule_name is not None and policy.name != rule_name:
            continue
        checks = [check for check in policy.checks if _policy_check_kind(check) in allowed_kinds]
        if kind is not None:
            checks = [check for check in checks if _policy_check_kind(check) == kind]
        if checks:
            filtered.append(Policy(
                name=policy.name,
                description=policy.description,
                severity=policy.severity,
                checks=checks,
            ))
    return filtered


def _lint_violations_to_checks(
    violations: "list[LintViolation]",
    rules_by_name: "dict[str, LintRule]",
    deadcode_config: "DeadCodeConfig | None",
) -> list[CheckViolation]:
    normalized: list[CheckViolation] = []
    for violation in violations:
        if deadcode_config is not None and violation.rule_name == deadcode_config.rule_name:
            kind = "deadcode"
            severity = "warning"
        else:
            rule = rules_by_name.get(violation.rule_name)
            kind = _lint_kind(rule) if rule is not None else "match"
            severity = "warning"
        witness = None
        if violation.witness is not None:
            witness = [
                f"source L{violation.witness.source_line}: {violation.witness.source_text}",
                *[f"  -> L{line}: {name}" for line, name in violation.witness.taint_chain],
                f"sink L{violation.witness.sink_line}: {violation.witness.sink_text}",
            ]
        normalized.append(CheckViolation(
            rule_name=violation.rule_name,
            kind=kind,
            severity=severity,
            message=violation.message,
            file_path=violation.file_path,
            line=violation.line,
            col=violation.col,
            witness=witness,
        ))
    return normalized


def _policy_violations_to_checks(violations: "list[PolicyViolation]") -> list[CheckViolation]:
    return [
        CheckViolation(
            rule_name=v.policy_name,
            kind=v.check_name.split(":", 1)[0],
            severity=v.severity,
            message=v.message,
            file_path=v.file_path,
            line=v.line,
            col=v.col,
            witness=v.witness,
        )
        for v in violations
    ]


def run_checks(
    paths: list[str],
    *,
    config: str | None = None,
    rule_name: str | None = None,
    kind: str | None = None,
    fix: bool = False,
    language: str = "python",
    project_path: str | None = None,
) -> list[CheckViolation]:
    """Run unified rules from ``rules.yaml`` with compatibility fallback."""
    from emend.lint import load_rules, run_lint
    from emend.policy import load_policies, run_policy_checks

    normalized: list[CheckViolation] = []

    lint_kinds = {"match", "flow", "deadcode"}
    if kind is None or kind in lint_kinds:
        lint_rules, _macros, deadcode_config = load_rules(config)
        selected_lint_rules = lint_rules
        if rule_name is not None:
            selected_lint_rules = [rule for rule in lint_rules if rule.name == rule_name]
        if kind is not None and kind != "deadcode":
            selected_lint_rules = [rule for rule in selected_lint_rules if _lint_kind(rule) == kind]
        lint_rule_filter = None
        if rule_name is not None and kind == "deadcode":
            lint_rule_filter = rule_name
        elif rule_name is not None and selected_lint_rules:
            lint_rule_filter = rule_name
        elif kind == "deadcode":
            lint_rule_filter = "deadcode"
        lint_violations = run_lint(
            selected_lint_rules,
            paths,
            fix=fix,
            rule_filter=lint_rule_filter,
            deadcode_config=deadcode_config if kind in (None, "deadcode") else None,
            project_path=project_path,
            language=language,
        )
        normalized.extend(
            _lint_violations_to_checks(
                lint_violations,
                {rule.name: rule for rule in lint_rules},
                deadcode_config,
            )
        )

    policy_kinds = {"type", "datalog", "custom", "sequence"}
    if kind is None or kind in policy_kinds:
        policies = load_policies(config)
        selected_policies = _filter_policies(
            policies,
            rule_name=rule_name,
            kind=kind,
            allowed_kinds=policy_kinds,
        )
        if selected_policies:
            policy_violations = run_policy_checks(
                paths,
                selected_policies,
                language=language,
                project_path=project_path,
            )
            normalized.extend(_policy_violations_to_checks(policy_violations))

    normalized.sort(key=lambda violation: (violation.file_path, violation.line, violation.col, violation.rule_name))
    return normalized
