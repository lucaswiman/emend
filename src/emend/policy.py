"""Policy engine for emend: declarative checks on top of analysis capabilities.

Loads policy definitions from ``.emend/rules.yaml`` and runs them against
a project, producing structured violations with witness traces.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from emend.rules_config import (
    DeadCodeConfig,
    load_rules_document,
    expand_macros,
    expand_not_through,
    yaml_key,
    as_list,
)

# Import check types from their canonical modules.
from emend.checks.structural import StructuralCheck
from emend.checks.types import TypeCheck
from emend.checks.datalog import DatalogCheck
from emend.checks.custom import CustomCheck
from emend.checks.sequence import SequenceCheck, SequenceStep, SequencePathConstraint

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
    not_through: str | None = None
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


@dataclass
class PolicyViolation:
    """A single policy violation."""
    file_path: str
    line: int
    col: int
    policy_name: str
    check_name: str
    severity: str
    message: str
    witness: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# YAML parsing
# ---------------------------------------------------------------------------

_VALID_SEVERITIES = {"error", "warning", "info"}
_VALID_CHECK_TYPES = {"flow", "structural", "type", "deadcode", "custom", "datalog", "sequence"}
_VALID_TYPE_KINDS = {"has_type", "returns"}


def _build_unified_policy(
    name: str,
    rule_def: dict[str, Any],
    macros: dict[str, str],
) -> Policy | None:
    severity = rule_def.get("severity", "warning")
    message = rule_def.get("message", "") or rule_def.get("description", "") or name
    checks: list[PolicyCheck] = []

    if "match" in rule_def or "find" in rule_def:
        pattern = rule_def.get("match", rule_def.get("find", ""))
        checks.append(StructuralCheck(
            pattern=expand_macros(pattern, macros),
            inside=rule_def.get("within", rule_def.get("inside")),
            not_inside=rule_def.get("not-within", rule_def.get("not-inside")),
            where=rule_def.get("where"),
        ))

    flow_def = rule_def.get("flow")
    if isinstance(flow_def, dict):
        flow_from = yaml_key(flow_def, "from", "flows_from")
        flow_to = yaml_key(flow_def, "to", "flows_to")
        # Unwrap dict-form ``{pattern: ...}`` to the pattern string, mirroring
        # the lint engine (checks/pattern_rules.py).
        if isinstance(flow_from, dict):
            flow_from = flow_from.get("pattern")
        if isinstance(flow_to, dict):
            flow_to = flow_to.get("pattern")
        if flow_from and flow_to:
            not_through = expand_not_through(yaml_key(flow_def, "not_through"), macros)
            checks.append(FlowCheck(
                flows_from=expand_macros(str(flow_from), macros),
                flows_to=expand_macros(str(flow_to), macros),
                not_through=not_through,
                label=flow_def.get("label", name),
            ))

    deadcode_def = rule_def.get("deadcode")
    if isinstance(deadcode_def, bool):
        if deadcode_def:
            checks.append(DeadCodeCheck())
    elif isinstance(deadcode_def, dict):
        entry_points = deadcode_def.get("entry-points", {})
        checks.append(DeadCodeCheck(
            entry_point_decorators=[str(v) for v in as_list(
                yaml_key(deadcode_def, "entry_point_decorators") or (
                    entry_points.get("decorators") if isinstance(entry_points, dict) else None
                )
            )],
            entry_point_names=[str(v) for v in as_list(
                yaml_key(deadcode_def, "entry_point_names") or (
                    entry_points.get("names") if isinstance(entry_points, dict) else None
                )
            )],
            exclude_paths=[str(v) for v in as_list(yaml_key(deadcode_def, "exclude_paths"))],
        ))

    type_def = rule_def.get("type-check")
    if type_def is None:
        type_def = rule_def.get("type_check")
    if isinstance(type_def, dict):
        symbol_pattern = type_def.get("selector") or yaml_key(type_def, "symbol_pattern")
        expected_type = type_def.get("expected") or yaml_key(type_def, "expected_type")
        if symbol_pattern and expected_type:
            checks.append(TypeCheck(
                symbol_pattern=str(symbol_pattern),
                expected_type=str(expected_type),
                kind=type_def.get("kind", "has_type"),
            ))

    if "datalog" in rule_def:
        datalog_def = rule_def["datalog"]
        if isinstance(datalog_def, str):
            checks.append(DatalogCheck(cozoscript=datalog_def))
        elif isinstance(datalog_def, dict):
            query = datalog_def.get("query") or datalog_def.get("cozoscript")
            if query:
                checks.append(DatalogCheck(cozoscript=str(query)))

    if not checks:
        return None
    return Policy(
        name=name,
        description=message,
        severity=severity,
        checks=checks,
    )


def _as_list(val: Any) -> list[str]:
    """Coerce a value to a list of strings."""
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    return list(val)


def _parse_flow_check(raw: dict[str, Any]) -> FlowCheck:
    flows_from = yaml_key(raw, "flows_from")
    flows_to = yaml_key(raw, "flows_to")
    if not flows_from or not flows_to:
        raise ValueError("FlowCheck requires 'flows_from' and 'flows_to'")
    return FlowCheck(
        flows_from=flows_from,
        flows_to=flows_to,
        not_through=yaml_key(raw, "not_through"),
        label=yaml_key(raw, "label") or "",
    )


def _parse_structural_check(raw: dict[str, Any]) -> StructuralCheck:
    pattern = raw.get("pattern")
    if not pattern:
        raise ValueError("StructuralCheck requires 'pattern'")
    return StructuralCheck(
        pattern=pattern,
        inside=raw.get("inside"),
        not_inside=yaml_key(raw, "not_inside"),
        where=raw.get("where"),
    )


def _parse_type_check(raw: dict[str, Any]) -> TypeCheck:
    kind = raw.get("kind", "has_type")
    symbol_pattern = yaml_key(raw, "symbol_pattern")
    expected_type = yaml_key(raw, "expected_type")
    if not symbol_pattern or not expected_type:
        raise ValueError(
            "TypeCheck requires 'symbol_pattern' and 'expected_type'"
        )
    return TypeCheck(
        symbol_pattern=symbol_pattern,
        expected_type=expected_type,
        kind=kind,
    )


def _parse_deadcode_check(raw: dict[str, Any]) -> DeadCodeCheck:
    return DeadCodeCheck(
        entry_point_decorators=_as_list(yaml_key(raw, "entry_point_decorators")),
        entry_point_names=_as_list(yaml_key(raw, "entry_point_names")),
        exclude_paths=_as_list(yaml_key(raw, "exclude_paths")),
    )


def _parse_custom_check(raw: dict[str, Any]) -> CustomCheck:
    query_source = yaml_key(raw, "query_source")
    if not query_source:
        raise ValueError("CustomCheck requires 'query_source'")
    return CustomCheck(query_source=query_source)


def _parse_datalog_check(raw: dict[str, Any]) -> DatalogCheck:
    cozoscript = raw.get("cozoscript") or yaml_key(raw, "query")
    if not cozoscript:
        raise ValueError("DatalogCheck requires 'cozoscript' or 'query'")
    return DatalogCheck(cozoscript=cozoscript)


def _parse_sequence_check(raw: dict[str, Any]) -> SequenceCheck:
    name = raw.get("name", "")
    message = raw.get("message", "")
    if not name:
        raise ValueError("SequenceCheck requires 'name'")
    raw_sequence = raw.get("sequence", [])
    if not raw_sequence or len(raw_sequence) < 2:
        raise ValueError("SequenceCheck requires at least 2 steps in 'sequence'")
    steps = []
    for step_raw in raw_sequence:
        steps.append(SequenceStep(
            bind=step_raw.get("bind", ""),
            pattern=step_raw.get("pattern"),
            effect=step_raw.get("effect"),
            type_constraint=yaml_key(step_raw, "type_constraint"),
        ))
    path_constraints = []
    raw_path = raw.get("path") or {}
    for path_key, path_val in raw_path.items():
        parts = [p.strip() for p in path_key.split("->")]
        if len(parts) != 2:
            raise ValueError(f"Invalid path key {path_key!r}: expected 'step1 -> step2'")
        nt_patterns = []
        for item in _as_list(yaml_key(path_val, "not_through") or []):
            if isinstance(item, dict):
                nt_patterns.append(item.get("pattern", ""))
            else:
                nt_patterns.append(item)
        nts_patterns = []
        for item in _as_list(yaml_key(path_val, "not_through_scope") or []):
            if isinstance(item, dict):
                nts_patterns.append(item.get("pattern", ""))
            else:
                nts_patterns.append(item)
        path_constraints.append(SequencePathConstraint(
            from_step=parts[0],
            to_step=parts[1],
            not_through=nt_patterns,
            not_through_scope=nts_patterns,
        ))
    return SequenceCheck(
        name=name,
        message=message,
        sequence=steps,
        path_constraints=path_constraints,
        severity=raw.get("severity", "error"),
    )


_CHECK_PARSERS: dict[str, Callable[[dict[str, Any]], PolicyCheck]] = {
    "flow": _parse_flow_check,
    "structural": _parse_structural_check,
    "type": _parse_type_check,
    "deadcode": _parse_deadcode_check,
    "custom": _parse_custom_check,
    "datalog": _parse_datalog_check,
    "sequence": _parse_sequence_check,
}


def _parse_check(raw: dict[str, Any]) -> PolicyCheck:
    """Parse a single check definition from a YAML dict."""
    check_type = raw.get("type", "")
    parser = _CHECK_PARSERS.get(check_type)
    if parser is None:
        raise ValueError(f"Unknown check type: {check_type!r}")
    return parser(raw)


def load_policies(config_path: str | Path | None = None) -> list[Policy]:
    """Load policies from a YAML file."""
    data, path = load_rules_document(config_path)

    if "policies" not in data and "rules" not in data:
        raise ValueError(
            "Policy config must be a YAML mapping with a top-level 'policies' or 'rules' key"
        )

    policies: list[Policy] = []
    if "policies" in data:
        for raw_policy in (data["policies"] or []):
            checks = [_parse_check(c) for c in raw_policy.get("checks", [])]
            policies.append(Policy(
                name=raw_policy["name"],
                description=raw_policy.get("description", ""),
                severity=raw_policy.get("severity", "warning"),
                checks=checks,
            ))
        return policies

    macros = data.get("macros", {}) or {}
    for name, rule_def in (data.get("rules", {}) or {}).items():
        if not isinstance(rule_def, dict):
            continue
        if rule_def.get("enabled") is False:
            continue
        policy = _build_unified_policy(name, rule_def, macros)
        if policy is not None:
            policies.append(policy)

    top_level_deadcode = data.get("deadcode")
    if top_level_deadcode is not None and all(p.name != "deadcode" for p in policies):
        deadcode_policy = _build_unified_policy(
            "deadcode",
            {
                "deadcode": top_level_deadcode,
                "message": "Dead code check",
                "severity": "warning",
            },
            macros,
        )
        if deadcode_policy is not None:
            policies.append(deadcode_policy)
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
# Check runners — delegate to checks/<kind>.py
# ---------------------------------------------------------------------------

def _run_flow_check(
    check: FlowCheck,
    policy: Policy,
    file_path: str,
    source: str,
    language: str,
    fact_graph: Any = None,
) -> list[PolicyViolation]:
    """Run a flow check using the unified flow IR engine."""
    from emend.flow_ir import from_flow_check, execute_flow_spec, format_witness

    spec = from_flow_check(check, policy.name, policy.description, policy.severity)
    flow_violations = execute_flow_spec(spec, file_path, source, language, fact_graph=fact_graph)

    violations: list[PolicyViolation] = []
    for fv in flow_violations:
        witness = format_witness(fv.witness) if fv.witness else []
        violations.append(PolicyViolation(
            file_path=fv.file_path,
            line=fv.line,
            col=fv.col,
            policy_name=policy.name,
            check_name=f"flow:{check.label or 'default'}",
            severity=policy.severity,
            message=fv.message or policy.description,
            witness=witness,
        ))
    return violations


def _run_structural_check(
    check: StructuralCheck,
    policy: Policy,
    file_path: str,
    source: str,
    language: str,
) -> list[PolicyViolation]:
    from emend.checks.structural import run_structural_check
    return run_structural_check(check, policy, file_path, source, language)


def _run_type_check(
    check: TypeCheck,
    policy: Policy,
    file_path: str,
    source: str,
    language: str,
    project_root: str | None = None,
) -> list[PolicyViolation]:
    from emend.checks.types import run_type_check
    return run_type_check(check, policy, file_path, source, language, project_root)


def _run_deadcode_check(
    check: DeadCodeCheck,
    policy: Policy,
    project_path: str,
) -> list[PolicyViolation]:
    from emend.checks.deadcode import run_deadcode_check
    return run_deadcode_check(check, policy, project_path)


def _run_custom_check(
    check: CustomCheck,
    policy: Policy,
    file_path: str,
    source: str,
    language: str,
) -> list[PolicyViolation]:
    from emend.checks.custom import run_custom_check
    return run_custom_check(check, policy, file_path, source, language)


def _run_datalog_check(
    check: DatalogCheck,
    policy: Policy,
    project_path: str,
) -> list[PolicyViolation]:
    from emend.checks.datalog import run_datalog_check
    return run_datalog_check(check, policy, project_path)


def _run_sequence_check(
    check: SequenceCheck,
    policy: Policy,
    project_path: str,
) -> list[PolicyViolation]:
    from emend.checks.sequence import run_sequence_check
    return run_sequence_check(check, policy, project_path)


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

def run_policy_checks(
    paths: list[str],
    policies: list[Policy],
    *,
    language: str = "python",
    project_path: str | None = None,
) -> list[PolicyViolation]:
    """Run all policy checks against the given file paths."""
    from emend import emend_core

    violations: list[PolicyViolation] = []

    deadcode_policies: list[tuple[Policy, DeadCodeCheck]] = []
    datalog_policies: list[tuple[Policy, DatalogCheck]] = []
    sequence_policies: list[tuple[Policy, SequenceCheck]] = []
    file_policies: list[tuple[Policy, PolicyCheck]] = []

    for policy in policies:
        for check in policy.checks:
            if isinstance(check, DeadCodeCheck):
                deadcode_policies.append((policy, check))
            elif isinstance(check, DatalogCheck):
                datalog_policies.append((policy, check))
            elif isinstance(check, SequenceCheck):
                sequence_policies.append((policy, check))
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

    if file_policies:
        file_contents: dict[str, str] = dict(
            emend_core.read_and_filter_files(paths, [])
        )

        for file_path, source in file_contents.items():
            for policy, check in file_policies:
                try:
                    if isinstance(check, FlowCheck):
                        violations.extend(
                            _run_flow_check(check, policy, file_path, source, language)
                        )
                    elif isinstance(check, StructuralCheck):
                        violations.extend(
                            _run_structural_check(check, policy, file_path, source, language)
                        )
                    elif isinstance(check, TypeCheck):
                        violations.extend(
                            _run_type_check(
                                check, policy, file_path, source, language,
                                project_root=project_path,
                            )
                        )
                    elif isinstance(check, CustomCheck):
                        violations.extend(
                            _run_custom_check(check, policy, file_path, source, language)
                        )
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
