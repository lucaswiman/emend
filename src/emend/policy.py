"""Policy engine for emend: declarative checks on top of analysis capabilities.

Loads policy definitions from ``.emend/rules.yaml`` and runs them against
a project, producing structured violations with witness traces.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from emend.errors import BUG_EXCEPTIONS
from emend.rules_config import (
    DeadCodeConfig,
    compiled_deadcode_to_config,
    compile_rules_document,
    load_compiled_rules,
)
from emend.checks.rule_model import (
    CompiledCustomCheck,
    CompiledDatalogCheck,
    CompiledDeadCodeRule,
    CompiledFlowRule,
    CompiledPolicy as RulePolicy,
    CompiledPolicyCheck,
    CompiledRuleDocument,
    CompiledSequenceCheck,
    CompiledStructuralCheck,
    CompiledTypeCheck,
)

# Import check types from their canonical modules.
from emend.checks.structural import StructuralCheck, run_structural_check as _run_structural_check
from emend.checks.types import TypeCheck, run_type_check as _run_type_check
from emend.checks.deadcode import run_deadcode_check as _run_deadcode_check
from emend.checks.datalog import DatalogCheck, run_datalog_check as _run_datalog_check
from emend.checks.custom import CustomCheck, run_custom_check as _run_custom_check
from emend.checks.sequence import (
    SequenceCheck, SequenceStep, SequencePathConstraint,
    run_sequence_check as _run_sequence_check,
)
from emend.checks.violations import PolicyViolation

# Policy and lint share the same dead-code configuration dataclass.
# ``DeadCodeCheck`` is kept as an alias for readability in policy-side code.
DeadCodeCheck = DeadCodeConfig


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FlowCheck:
    """Taint-style flow check: value must not flow from source to sink."""
    flows_from: str
    flows_to: str
    not_through: list[str] | str | None = None
    label: str = ""


# Union of all check types
PolicyCheck = FlowCheck | StructuralCheck | TypeCheck | DeadCodeCheck | CustomCheck | DatalogCheck | SequenceCheck


@dataclass
class Policy:
    """A named policy containing one or more checks."""
    name: str
    description: str
    severity: str  # "error", "warning", "info"
    checks: list[PolicyCheck]


# ---------------------------------------------------------------------------
# YAML parsing
# ---------------------------------------------------------------------------

_VALID_SEVERITIES = {"error", "warning", "info"}
_VALID_TYPE_KINDS = {"has_type", "returns"}


def _adapt_compiled_check(check: CompiledPolicyCheck) -> PolicyCheck:
    payload = check.payload
    if isinstance(payload, CompiledFlowRule):
        sanitizers = [endpoint.pattern for endpoint in payload.sanitizers]
        return FlowCheck(
            payload.sources[0].pattern,
            payload.sinks[0].pattern,
            sanitizers[0] if len(sanitizers) == 1 else sanitizers or None,
            payload.label,
        )
    if isinstance(payload, CompiledStructuralCheck):
        return StructuralCheck(
            payload.pattern, payload.inside, payload.not_inside, payload.where,
        )
    if isinstance(payload, CompiledTypeCheck):
        return TypeCheck(payload.symbol_pattern, payload.expected_type, payload.kind)
    if isinstance(payload, CompiledDeadCodeRule):
        return compiled_deadcode_to_config(payload)
    if isinstance(payload, CompiledCustomCheck):
        return CustomCheck(payload.query_source)
    if isinstance(payload, CompiledDatalogCheck):
        return DatalogCheck(payload.cozoscript)
    if isinstance(payload, CompiledSequenceCheck):
        return SequenceCheck(
            payload.name,
            payload.message,
            [SequenceStep(step.bind, step.pattern, step.effect, step.type_constraint)
             for step in payload.sequence],
            [SequencePathConstraint(
                path.from_step, path.to_step,
                list(path.not_through), list(path.not_through_scope),
            ) for path in payload.path_constraints],
            payload.severity,
        )
    raise TypeError(f"Unsupported compiled policy payload: {type(payload).__name__}")


def _adapt_compiled_policy(policy: RulePolicy) -> Policy:
    return Policy(
        policy.name, policy.description, policy.severity,
        [_adapt_compiled_check(check) for check in policy.checks],
    )


def _parse_check(raw: dict) -> PolicyCheck:
    """Back-compatible single-check adapter through the canonical compiler."""
    document = compile_rules_document({
        "policies": [{"name": "_single", "checks": [raw]}],
    })
    return _adapt_compiled_check(document.policies[0].checks[0])


def load_policies(
    config_path: str | Path | None = None,
    *,
    compiled: CompiledRuleDocument | None = None,
) -> list[Policy]:
    """Adapt a canonical compiled document to the established policy API."""
    document = compiled or load_compiled_rules(config_path)
    if not document.has_policies_section and not document.has_rules_section:
        raise ValueError(
            "Policy config must be a YAML mapping with a top-level 'policies' or 'rules' key"
        )
    selected = (
        document.policies if document.has_policies_section
        else document.unified_policies
    )
    policies = [_adapt_compiled_policy(policy) for policy in selected]
    errors = validate_policies(policies)
    if errors:
        raise ValueError("\n".join(errors))
    return policies


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_policies(policies: list[Policy]) -> list[str]:
    """Validate a list of policies and return any error messages."""
    errors: list[str] = []
    seen_names: set[str] = set()

    for i, policy in enumerate(policies):
        prefix = f"Policy #{i + 1} ({policy.name!r})"

        if policy.name in seen_names:
            errors.append(f"{prefix}: duplicate policy name")
        seen_names.add(policy.name)

        if not policy.name or not policy.name.strip():
            errors.append(f"Policy #{i + 1}: name is required")

        if policy.severity not in _VALID_SEVERITIES:
            errors.append(
                f"{prefix}: invalid severity {policy.severity!r}, "
                f"must be one of {sorted(_VALID_SEVERITIES)}"
            )

        if not policy.checks:
            errors.append(f"{prefix}: must have at least one check")

        for j, check in enumerate(policy.checks):
            cprefix = f"{prefix}, check #{j + 1}"
            if isinstance(check, FlowCheck):
                if not check.flows_from:
                    errors.append(f"{cprefix}: flows_from is required")
                if not check.flows_to:
                    errors.append(f"{cprefix}: flows_to is required")
            elif isinstance(check, StructuralCheck):
                if not check.pattern:
                    errors.append(f"{cprefix}: pattern is required")
            elif isinstance(check, TypeCheck):
                if not check.symbol_pattern:
                    errors.append(f"{cprefix}: symbol_pattern is required")
                if not check.expected_type:
                    errors.append(f"{cprefix}: expected_type is required")
                if check.kind not in _VALID_TYPE_KINDS:
                    errors.append(
                        f"{cprefix}: invalid type check kind {check.kind!r}, "
                        f"must be one of {sorted(_VALID_TYPE_KINDS)}"
                    )
            elif isinstance(check, CustomCheck):
                if not check.query_source:
                    errors.append(f"{cprefix}: query_source is required")
            elif isinstance(check, DatalogCheck):
                if not check.cozoscript:
                    errors.append(f"{cprefix}: cozoscript is required")
            elif isinstance(check, SequenceCheck):
                if not check.name:
                    errors.append(f"{cprefix}: name is required")
                if len(check.sequence) < 2:
                    errors.append(f"{cprefix}: sequence must have at least 2 steps")
                step_names = set()
                for s, step in enumerate(check.sequence):
                    if not step.bind:
                        errors.append(f"{cprefix}: step #{s + 1} must have a 'bind' name")
                    if step.bind in step_names:
                        errors.append(f"{cprefix}: duplicate step bind name {step.bind!r}")
                    step_names.add(step.bind)
                    if not step.pattern and not step.effect:
                        errors.append(f"{cprefix}: step {step.bind!r} must have 'pattern' or 'effect'")
                for pc in check.path_constraints:
                    if pc.from_step not in step_names:
                        errors.append(f"{cprefix}: path references unknown step {pc.from_step!r}")
                    if pc.to_step not in step_names:
                        errors.append(f"{cprefix}: path references unknown step {pc.to_step!r}")

    return errors


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

def run_policy_checks(
    paths: list[str],
    policies: list[Policy],
    *,
    language: str | None = None,
    project_path: str | None = None,
    compiled_flows=None,
) -> list[PolicyViolation]:
    """Run all policy checks against the given file paths."""
    from emend import emend_core
    from emend.language_registry import detect_language

    violations: list[PolicyViolation] = []

    deadcode_policies: list[tuple[Policy, DeadCodeCheck]] = []
    datalog_policies: list[tuple[Policy, DatalogCheck]] = []
    sequence_policies: list[tuple[Policy, SequenceCheck]] = []
    flow_policies: list[tuple[Policy, FlowCheck]] = []
    file_policies: list[tuple[Policy, PolicyCheck]] = []

    for policy in policies:
        for check in policy.checks:
            if isinstance(check, DeadCodeCheck):
                deadcode_policies.append((policy, check))
            elif isinstance(check, DatalogCheck):
                datalog_policies.append((policy, check))
            elif isinstance(check, SequenceCheck):
                sequence_policies.append((policy, check))
            elif isinstance(check, FlowCheck):
                flow_policies.append((policy, check))
            else:
                file_policies.append((policy, check))

    if deadcode_policies and project_path:
        for policy, check in deadcode_policies:
            violations.extend(_run_deadcode_check(check, policy, project_path))

    if datalog_policies and project_path:
        for policy, check in datalog_policies:
            violations.extend(_run_datalog_check(check, policy, project_path))

    if sequence_policies and project_path:
        for policy, check in sequence_policies:
            violations.extend(_run_sequence_check(check, policy, project_path))

    if flow_policies or compiled_flows:
        from emend.checks.flow import (
            CompiledFlowConfig, FlowSanitizer, FlowSink, FlowSource,
            evaluate_compiled_flow,
        )
        if compiled_flows is None:
            adapter = CompiledFlowConfig(
                sources=[FlowSource(
                    check.flows_from, check.label or policy.name,
                    rule_id=f"policy:{policy.name}:{index}",
                ) for index, (policy, check) in enumerate(flow_policies)],
                sinks=[FlowSink(
                    check.flows_to, check.label or policy.name, policy.description,
                    rule_id=f"policy:{policy.name}:{index}", rule_name=policy.name,
                    severity=policy.severity,
                ) for index, (policy, check) in enumerate(flow_policies)],
                sanitizers=[FlowSanitizer(
                    pattern, check.label or policy.name,
                    rule_id=f"policy:{policy.name}:{index}",
                ) for index, (policy, check) in enumerate(flow_policies)
                    for pattern in ([check.not_through] if isinstance(check.not_through, str)
                                    else check.not_through or [])],
            )
            compiled_flows = adapter.compiled_rules()
        try:
            evaluated = evaluate_compiled_flow(
                compiled_flows, paths, interprocedural=True,
                language=language, project_path=project_path,
            )
        except BUG_EXCEPTIONS:
            raise
        except Exception:
            logger.warning("Compiled policy flow evaluation failed", exc_info=True)
            evaluated = []
        violations.extend(PolicyViolation(
            file_path=row.file_path,
            line=row.line,
            col=row.col,
            policy_name=row.rule_name,
            check_name=f"flow:{row.label}",
            severity=row.severity,
            message=row.message,
            witness=[
                f"{step.description.split(':', 1)[0]} L{step.line}: {step.variable}"
                for step in row.trace
            ],
        ) for row in evaluated)

    if file_policies:
        file_contents: dict[str, str] = dict(
            emend_core.read_and_filter_files(paths, [])
        )

        for file_path, source in file_contents.items():
            file_language = language or detect_language(file_path) or "python"
            for policy, check in file_policies:
                try:
                    if isinstance(check, StructuralCheck):
                        violations.extend(
                            _run_structural_check(check, policy, file_path, source, file_language)
                        )
                    elif isinstance(check, TypeCheck):
                        violations.extend(
                            _run_type_check(
                                check, policy, file_path, source, file_language,
                                project_root=project_path,
                            )
                        )
                    elif isinstance(check, CustomCheck):
                        violations.extend(
                            _run_custom_check(check, policy, file_path, source, file_language)
                        )
                except BUG_EXCEPTIONS:
                    raise
                except Exception:
                    logger.warning(
                        "Policy %r check failed on %s",
                        policy.name, file_path,
                        exc_info=True,
                    )

    violations.sort(key=lambda v: (v.file_path, v.line, v.col))
    return violations


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_policy_violations(
    violations: list[PolicyViolation],
    *,
    json_output: bool = False,
) -> str:
    """Format policy violations for display."""
    if json_output:
        data = []
        for v in violations:
            entry: dict[str, Any] = {
                "file": v.file_path,
                "line": v.line,
                "col": v.col,
                "policy": v.policy_name,
                "check": v.check_name,
                "severity": v.severity,
                "message": v.message,
            }
            if v.witness:
                entry["witness"] = v.witness
            data.append(entry)
        return json.dumps(data, indent=2)

    if not violations:
        return "No policy violations found."

    lines: list[str] = []
    current_policy = ""
    for v in violations:
        if v.policy_name != current_policy:
            if lines:
                lines.append("")
            lines.append(f"[{v.severity.upper()}] {v.policy_name}")
            current_policy = v.policy_name

        location = f"{v.file_path}:{v.line}"
        if v.col:
            location += f":{v.col}"
        lines.append(f"  {location}: {v.message}")

        for w in v.witness:
            lines.append(f"    | {w}")

    error_count = sum(1 for v in violations if v.severity == "error")
    warning_count = sum(1 for v in violations if v.severity == "warning")
    info_count = sum(1 for v in violations if v.severity == "info")
    parts = []
    if error_count:
        parts.append(f"{error_count} error(s)")
    if warning_count:
        parts.append(f"{warning_count} warning(s)")
    if info_count:
        parts.append(f"{info_count} info(s)")
    lines.append("")
    lines.append(f"Found {len(violations)} violation(s): {', '.join(parts)}")

    return "\n".join(lines)
