"""Unified check engine for emend.

Schema validation choice:
    emend's rules.yaml mixes lint rules (under ``rules:``) and policy definitions
    (under ``policies:``).  Rather than a unified schema validated at parse time,
    we use **per-kind schema validation** at dispatch time:
      - lint / pattern rules are validated when parsed by load_rules()
      - policy / structural / type / etc. rules are validated by validate_policies()
    This matches the pre-existing behaviour and avoids false positives on YAML
    documents that intentionally omit sections not used by the active kind filter.
    The single document is loaded once in run_checks() and passed to both engines.

This module is the single entry point for all check kinds.  CLI commands
(``emend lint``, ``emend policy``, ``emend check``) and MCP tools all call
``run_checks()`` with an appropriate ``allowed_kinds`` filter.

Note: imports from lint/policy are deferred inside functions to avoid circular
imports via the rules_config shim chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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


def _lint_kind(rule: Any) -> str:
    if rule.flows_from and rule.flows_to:
        return "flow"
    return "match"


def _policy_check_kind(check: Any) -> str:
    # Import lazily to avoid circular imports
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
    policies: list[Any],
    *,
    rule_name: str | None,
    kind: str | None,
    allowed_kinds: set[str],
) -> list[Any]:
    from emend.policy import Policy
    filtered: list[Any] = []
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
    violations: list[Any],
    rules_by_name: dict[str, Any],
    deadcode_config: Any | None,
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


def _policy_violations_to_checks(violations: list[Any]) -> list[CheckViolation]:
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


def load_rules(config_path: str | None = None) -> Any:
    """Load rules from YAML config. Convenience re-export for callers of checks."""
    from emend.lint import load_rules as _load_rules
    return _load_rules(config_path)


def run_checks(
    paths: list[str],
    *,
    config: str | None = None,
    rule_name: str | None = None,
    kind: str | None = None,
    fix: bool = False,
    language: str = "python",
    project_path: str | None = None,
    allowed_kinds: set[str] | None = None,
) -> list[CheckViolation]:
    """Run unified rules from ``rules.yaml`` with compatibility fallback.

    Args:
        paths: File paths to check.
        config: Path to rules.yaml (default: .emend/rules.yaml).
        rule_name: Only run this rule by name.
        kind: Only run rules of this kind (match, flow, deadcode, type, ...).
        fix: Apply auto-fixes for match rules.
        language: Source language hint (default: python).
        project_path: Project root for cross-file analysis.
        allowed_kinds: Explicit allow-list of kinds for CLI command filtering.
            When None, all kinds are run. Takes precedence over ``kind``.

    Returns:
        Sorted list of CheckViolation objects.
    """
    from emend.lint import load_rules as _load_rules, run_lint
    from emend.policy import load_policies, run_policy_checks

    normalized: list[CheckViolation] = []

    # Determine which kinds to run.  ``allowed_kinds`` is set by CLI commands
    # to implement lint/policy/check command semantics; ``kind`` is a user
    # filter applied on top of that.
    if allowed_kinds is None:
        effective_lint_kinds = {"match", "flow", "deadcode"}
        effective_policy_kinds = {"type", "datalog", "custom", "sequence"}
    else:
        effective_lint_kinds = allowed_kinds & {"match", "flow", "deadcode"}
        effective_policy_kinds = allowed_kinds & {"type", "datalog", "custom", "sequence"}

    # Apply per-invocation kind filter
    if kind is not None:
        effective_lint_kinds = effective_lint_kinds & {kind}
        effective_policy_kinds = effective_policy_kinds & {kind}

    # Flow checks live in both lint (via _check_flow_rule) and policy
    # (via FlowCheck). When kind == "flow" or all kinds, run both.
    if kind is None or kind == "flow":
        if allowed_kinds is None or "flow" in allowed_kinds:
            effective_policy_kinds |= {"flow"}

    if effective_lint_kinds:
        lint_rules, _macros, deadcode_config = _load_rules(config)
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
            deadcode_config=deadcode_config if (kind is None or kind == "deadcode") and "deadcode" in effective_lint_kinds else None,
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

    if effective_policy_kinds:
        policies = load_policies(config)
        selected_policies = _filter_policies(
            policies,
            rule_name=rule_name,
            kind=kind,
            allowed_kinds=effective_policy_kinds,
        )
        if selected_policies:
            policy_violations = run_policy_checks(
                paths,
                selected_policies,
                language=language,
                project_path=project_path,
            )
            normalized.extend(_policy_violations_to_checks(policy_violations))

    normalized.sort(key=lambda v: (v.file_path, v.line, v.col, v.rule_name))
    return normalized
