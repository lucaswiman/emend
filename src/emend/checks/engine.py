"""Unified rule runner for ``.emend/rules.yaml``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from emend.lint import LintViolation, LintRule
    from emend.policy import PolicyViolation, Policy, FlowCheck, StructuralCheck, TypeCheck, DeadCodeCheck, DatalogCheck, CustomCheck, SequenceCheck
    from emend.rules_config import DeadCodeConfig
    from emend.checks.duplicates import DuplicateCodeConfig


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
        return "structural"
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
    duplicate_code_config: "DuplicateCodeConfig | None" = None,
) -> list[CheckViolation]:
    normalized: list[CheckViolation] = []
    for violation in violations:
        if deadcode_config is not None and violation.rule_name == deadcode_config.rule_name:
            kind = "deadcode"
            severity = "warning"
        elif duplicate_code_config is not None and violation.rule_name == duplicate_code_config.rule_name:
            kind = "duplicate-code"
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


# Kinds that name the same logical rule across the two engines. The lint
# engine emits pattern matches as ``match``; the policy engine emits the same
# rule (built from ``rules:`` via ``_build_unified_policy``) as ``structural``.
_KIND_ALIASES = {"structural": "match"}


def _dedup_key(violation: CheckViolation) -> tuple[str, int, str, str]:
    """Cross-engine identity for a violation.

    Column is deliberately excluded: the two engines report different columns
    for the same logical match (lint anchors at the statement/line start, the
    policy structural check anchors at the matched node), so a col-sensitive
    key would fail to dedup. Multiplicity within a single engine is preserved
    by only filtering the policy engine's output against lint's keys, never
    lint against itself.
    """
    norm_kind = _KIND_ALIASES.get(violation.kind, violation.kind)
    return (violation.file_path, violation.line, violation.rule_name, norm_kind)


# Rule kinds owned by the lint engine (pattern/flow/deadcode/duplicate rules).
LINT_KINDS = frozenset({"match", "flow", "deadcode", "duplicate-code"})
# Rule kinds owned by the policy engine (structural/type/datalog/custom/sequence).
POLICY_KINDS = frozenset({"match", "structural", "type", "flow", "deadcode", "datalog", "custom", "sequence"})
# All known kinds.
ALL_KINDS = LINT_KINDS | POLICY_KINDS


def run_checks(
    paths: list[str],
    *,
    config: str | None = None,
    rule_name: str | None = None,
    kind: str | None = None,
    mode: str | None = None,
    fix: bool = False,
    language: str = "python",
    project_path: str | None = None,
) -> list[CheckViolation]:
    """Run unified rules from ``rules.yaml``.

    Args:
        paths: Files to check.
        config: Path to rules.yaml (defaults to .emend/rules.yaml).
        rule_name: Restrict to one rule by name.
        kind: Restrict to one rule kind (match, flow, deadcode, type, …).
        mode: Restrict by engine: ``"lint"`` (pattern/flow/deadcode rules),
            ``"policy"`` (structural/type/datalog/custom/sequence policies),
            or ``None`` / ``"all"`` (both engines).
        fix: Apply auto-fix replacements for pattern rules.
        language: Source language for parsing.
        project_path: Project root for cross-file analysis.
    """
    from emend.lint import load_rules, load_duplicate_code_config, run_lint
    from emend.policy import load_policies, run_policy_checks

    if mode not in (None, "lint", "policy", "all"):
        raise ValueError(
            f"Unknown mode {mode!r}; expected one of None, 'lint', 'policy', 'all'"
        )

    normalized: list[CheckViolation] = []
    lint_checks: list[CheckViolation] = []

    run_lint_engine = mode in (None, "lint", "all")
    run_policy_engine = mode in (None, "policy", "all")

    lint_kinds = {"match", "flow", "deadcode", "duplicate-code"}
    if run_lint_engine and (kind is None or kind in lint_kinds):
        lint_rules, _macros, deadcode_config = load_rules(config)
        duplicate_code_config = load_duplicate_code_config(config)
        selected_lint_rules = lint_rules
        if rule_name is not None:
            selected_lint_rules = [rule for rule in lint_rules if rule.name == rule_name]
        if kind is not None and kind not in ("deadcode", "duplicate-code"):
            selected_lint_rules = [rule for rule in selected_lint_rules if _lint_kind(rule) == kind]

        run_duplicates = (
            duplicate_code_config is not None
            and (kind is None or kind == "duplicate-code")
            and (rule_name is None or rule_name == duplicate_code_config.rule_name)
        )
        if not run_duplicates:
            duplicate_code_config = None

        lint_rule_filter = None
        if rule_name is not None and kind in ("deadcode", "duplicate-code"):
            lint_rule_filter = rule_name
        elif rule_name is not None and (selected_lint_rules or run_duplicates):
            lint_rule_filter = rule_name
        elif kind == "deadcode":
            lint_rule_filter = "deadcode"
        elif kind == "duplicate-code" and duplicate_code_config is not None:
            lint_rule_filter = duplicate_code_config.rule_name
        lint_violations = run_lint(
            selected_lint_rules,
            paths,
            fix=fix,
            rule_filter=lint_rule_filter,
            deadcode_config=deadcode_config if kind in (None, "deadcode") else None,
            duplicate_code_config=duplicate_code_config,
            project_path=project_path,
            language=language,
        )
        lint_checks = _lint_violations_to_checks(
            lint_violations,
            {rule.name: rule for rule in lint_rules},
            deadcode_config,
            duplicate_code_config,
        )
        normalized.extend(lint_checks)

    policy_kinds = {"match", "structural", "type", "flow", "deadcode", "datalog", "custom", "sequence"}
    if run_policy_engine and (kind is None or kind in policy_kinds):
        # A rules document may legitimately contain no policy-bearing keys
        # (e.g. only a ``duplicate`` or ``trace`` section); skip the policy
        # engine then.  Malformed policies must still raise, so only the
        # missing-key case is treated as empty.
        from emend.checks.rules_config import load_rules_document

        data, _path = load_rules_document(config)
        # In combined mode (``None``/``"all"``) the lint engine already
        # processed every ``rules:`` entry, so the policy engine must only
        # handle the ``policies:`` key — otherwise ``load_policies`` would
        # synthesise a duplicate structural/flow/deadcode policy from each
        # ``rules:`` entry and every rule would be reported twice.  The
        # ``rules:`` fallback is only for a standalone ``mode == "policy"`` run
        # where the lint engine did not run.
        if "policies" in data:
            policies = load_policies(config)
        elif mode == "policy" and "rules" in data:
            policies = load_policies(config)
        else:
            policies = []
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
            policy_checks = _policy_violations_to_checks(policy_violations)
            # When both engines run, ``rules:`` entries are processed by lint
            # AND re-derived into unified policies, so the same match surfaces
            # from both. Drop the policy copies that lint already reported.
            if run_lint_engine:
                lint_keys = {_dedup_key(v) for v in lint_checks}
                policy_checks = [
                    v for v in policy_checks if _dedup_key(v) not in lint_keys
                ]
            normalized.extend(policy_checks)

    normalized.sort(key=lambda violation: (violation.file_path, violation.line, violation.col, violation.rule_name))
    return normalized
