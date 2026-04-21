"""Policy engine for emend: declarative checks on top of analysis capabilities.

Loads policy definitions from ``.emend/policies.yaml`` and runs them against
a project, producing structured violations with witness traces.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from emend.rules_config import (
    DeadCodeConfig,
    LEGACY_POLICIES_PATH,
    LEGACY_PATTERNS_PATH,
    load_rules_document,
    expand_macros,
    expand_not_through,
    yaml_key,
    as_list,
)

# Policy and lint share the same dead-code configuration dataclass.
# ``DeadCodeCheck`` is kept as an alias for readability in policy-side code
# (where it appears alongside ``FlowCheck``, ``StructuralCheck``, etc.) and
# for backwards compatibility with external importers.
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


@dataclass
class StructuralCheck:
    """Pattern must/must-not appear in certain scopes."""
    pattern: str
    inside: str | None = None
    not_inside: str | None = None
    where: str | None = None


@dataclass
class TypeCheck:
    """Type constraint check on symbols."""
    symbol_pattern: str
    expected_type: str
    kind: str = "has_type"  # "has_type" or "returns"


@dataclass
class CustomCheck:
    """Expert query using raw query source."""
    query_source: str


@dataclass
class DatalogCheck:
    """CozoScript Datalog query check.

    The query must return rows with at least ``line``, ``col``, ``message``
    columns. Each returned row becomes a policy violation.
    """
    cozoscript: str


@dataclass
class SequenceStep:
    """A single step in a temporal sequence rule."""
    bind: str  # step label (e.g. "load", "mutate")
    pattern: str | None = None  # pattern to match (e.g. "$OBJ = session.query($MODEL)")
    effect: str | None = None  # effect predicate (e.g. "writes($OBJ)")
    type_constraint: str | None = None  # optional type constraint


@dataclass
class SequencePathConstraint:
    """Path constraint between two consecutive sequence steps."""
    from_step: str  # step label (e.g. "load")
    to_step: str  # step label (e.g. "mutate")
    not_through: list[str] = field(default_factory=list)  # patterns that must not appear on any path
    not_through_scope: list[str] = field(default_factory=list)  # scope boundary patterns


@dataclass
class SequenceCheck:
    """Multi-step temporal sequence check.

    Each rule defines an ordered list of steps (matched by pattern or effect)
    with binding constraints across steps and CFG-path constraints between them.
    Examples: TOCTOU, double-free, use-after-close.
    """
    name: str
    message: str
    sequence: list[SequenceStep]
    path_constraints: list[SequencePathConstraint] = field(default_factory=list)
    severity: str = "error"


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

    if isinstance(rule_def.get("flow"), dict) and not checks:
        flow_def = rule_def["flow"]
        # Keep compatibility with older partial flow rules by skipping empty checks.
        if flow_def.get("from") and flow_def.get("to"):
            checks.append(FlowCheck(
                flows_from=expand_macros(str(flow_def["from"]), macros),
                flows_to=expand_macros(str(flow_def["to"]), macros),
                not_through=expand_macros(str(flow_def.get("not-through")), macros) if flow_def.get("not-through") else None,
                label=flow_def.get("label", name),
            ))

    if not checks:
        return None
    return Policy(
        name=name,
        description=message,
        severity=severity,
        checks=checks,
    )


def _parse_check(raw: dict[str, Any]) -> PolicyCheck:
    """Parse a single check definition from a YAML dict.

    Accepts both underscore (``flows_from``) and hyphenated (``flows-from``)
    key variants so that YAML files can use the more natural hyphenated form.
    """
    check_type = raw.get("type", "")
    if check_type == "flow":
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
    elif check_type == "structural":
        pattern = raw.get("pattern")
        if not pattern:
            raise ValueError("StructuralCheck requires 'pattern'")
        return StructuralCheck(
            pattern=pattern,
            inside=raw.get("inside"),
            not_inside=yaml_key(raw, "not_inside"),
            where=raw.get("where"),
        )
    elif check_type == "type":
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
    elif check_type == "deadcode":
        return DeadCodeCheck(
            entry_point_decorators=_as_list(yaml_key(raw, "entry_point_decorators")),
            entry_point_names=_as_list(yaml_key(raw, "entry_point_names")),
            exclude_paths=_as_list(yaml_key(raw, "exclude_paths")),
        )
    elif check_type == "custom":
        query_source = yaml_key(raw, "query_source")
        if not query_source:
            raise ValueError("CustomCheck requires 'query_source'")
        return CustomCheck(query_source=query_source)
    elif check_type == "datalog":
        cozoscript = raw.get("cozoscript") or yaml_key(raw, "query")
        if not cozoscript:
            raise ValueError("DatalogCheck requires 'cozoscript' or 'query'")
        return DatalogCheck(cozoscript=cozoscript)
    elif check_type == "sequence":
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
        # Parse path constraints
        path_constraints = []
        raw_path = raw.get("path", {})
        for path_key, path_val in raw_path.items():
            # path_key is like "load -> mutate"
            parts = [p.strip() for p in path_key.split("->")]
            if len(parts) != 2:
                raise ValueError(f"Invalid path key {path_key!r}: expected 'step1 -> step2'")
            nt_patterns = []
            for item in _as_list(path_val.get("not_through", [])):
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
    else:
        raise ValueError(f"Unknown check type: {check_type!r}")


def _as_list(val: Any) -> list[str]:
    """Coerce a value to a list of strings."""
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    return list(val)


def load_policies(config_path: str | Path | None = None) -> list[Policy]:
    """Load policies from a YAML file.

    Args:
        config_path: Path to the policies YAML file.  Defaults to
            ``.emend/policies.yaml`` relative to the current directory.

    Returns:
        List of ``Policy`` objects.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If the config file is malformed.
    """
    data, path = load_rules_document(
        config_path,
        fallbacks=(LEGACY_POLICIES_PATH, LEGACY_PATTERNS_PATH),
    )

    if "policies" not in data and "rules" not in data:
        raise ValueError(
            "Policy config must be a YAML mapping with a top-level 'policies' or 'rules' key"
        )

    policies: list[Policy] = []
    if "policies" in data:
        for raw_policy in data["policies"]:
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

    # Top-level deadcode block in rules.yaml acts as a default deadcode policy.
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
    """Validate a list of policies and return any error messages.

    Returns an empty list if all policies are valid.
    """
    errors: list[str] = []
    seen_names: set[str] = set()

    for i, policy in enumerate(policies):
        prefix = f"Policy #{i + 1} ({policy.name!r})"

        # Duplicate name check
        if policy.name in seen_names:
            errors.append(f"{prefix}: duplicate policy name")
        seen_names.add(policy.name)

        # Name validation
        if not policy.name or not policy.name.strip():
            errors.append(f"Policy #{i + 1}: name is required")

        # Severity validation
        if policy.severity not in _VALID_SEVERITIES:
            errors.append(
                f"{prefix}: invalid severity {policy.severity!r}, "
                f"must be one of {sorted(_VALID_SEVERITIES)}"
            )

        # Must have at least one check
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
# Check runners
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
    """Run a structural pattern check using transform.find_pattern."""
    from emend.transform import find_pattern

    matches = find_pattern(
        check.pattern,
        file_path,
        inside=check.inside,
        not_inside=check.not_inside,
        where=check.where,
        source_override=source,
        language=language,
    )

    violations: list[PolicyViolation] = []
    for m in matches:
        violations.append(PolicyViolation(
            file_path=file_path,
            line=m.line or 0,
            col=m.col or 0,
            policy_name=policy.name,
            check_name=f"structural:{check.pattern}",
            severity=policy.severity,
            message=policy.description,
            witness=[m.matched_text or m.node_text or ""],
        ))
    return violations


def _run_type_check(
    check: TypeCheck,
    policy: Policy,
    file_path: str,
    source: str,
    language: str,
    project_root: str | None = None,
) -> list[PolicyViolation]:
    """Run a type constraint check using the type oracle.

    Finds symbols matching ``symbol_pattern``, then checks their inferred type
    against ``expected_type``.
    """
    from emend.transform import find_pattern

    # Build the oracle-aware pattern with type constraint
    if check.kind == "returns":
        augmented_pattern = f"{check.symbol_pattern}:returns[{check.expected_type}]"
    else:
        augmented_pattern = f"{check.symbol_pattern}:type[{check.expected_type}]"

    # First find matches of the plain symbol pattern to identify candidates
    all_matches = find_pattern(
        check.symbol_pattern,
        file_path,
        source_override=source,
        language=language,
    )

    # Then find matches that satisfy the type constraint
    type_oracle = None
    if project_root:
        try:
            from emend.type_oracle import create_type_oracle
            type_oracle = create_type_oracle(project_root=Path(project_root))
        except Exception:
            logger.debug("Could not create type oracle for type check", exc_info=True)

    typed_matches = find_pattern(
        augmented_pattern,
        file_path,
        source_override=source,
        type_oracle=type_oracle,
        language=language,
    )

    # Violations are symbols that matched the pattern but NOT the type constraint
    typed_positions = {(m.line, m.col) for m in typed_matches}
    violations: list[PolicyViolation] = []
    for m in all_matches:
        if (m.line, m.col) not in typed_positions:
            violations.append(PolicyViolation(
                file_path=file_path,
                line=m.line or 0,
                col=m.col or 0,
                policy_name=policy.name,
                check_name=f"type:{check.kind}:{check.expected_type}",
                severity=policy.severity,
                message=(
                    f"{policy.description}: expected {check.kind} "
                    f"{check.expected_type!r} on {m.matched_text or m.node_text or '?'}"
                ),
                witness=[m.matched_text or m.node_text or ""],
            ))
    return violations


def _run_deadcode_check(
    check: DeadCodeCheck,
    policy: Policy,
    project_path: str,
) -> list[PolicyViolation]:
    """Run dead code detection as a policy check."""
    from emend.transform import find_dead_code

    violations: list[PolicyViolation] = []
    for ds in find_dead_code(
        project_path,
        entry_point_decorators=check.entry_point_decorators or None,
        entry_point_names=check.entry_point_names or None,
        exclude_paths=check.exclude_paths or None,
    ):
        violations.append(PolicyViolation(
            file_path=ds.file_path,
            line=ds.line,
            col=0,
            policy_name=policy.name,
            check_name="deadcode",
            severity=policy.severity,
            message=f"{policy.description}: {ds.name} ({ds.reason})",
            witness=[ds.selector],
        ))
    return violations


def _run_custom_check(
    check: CustomCheck,
    policy: Policy,
    file_path: str,
    source: str,
    language: str,
) -> list[PolicyViolation]:
    """Run a custom expert query.

    The ``query_source`` is executed as a Python expression with access to
    emend's ``find_pattern`` and the file content.  It must return a list
    of dicts with at least ``line``, ``col``, ``message`` keys.
    """
    from emend.transform import find_pattern

    local_ns: dict[str, Any] = {
        "find_pattern": find_pattern,
        "file_path": file_path,
        "source": source,
        "language": language,
    }

    try:
        result = eval(  # noqa: S307 - intentional expert-mode eval
            compile(check.query_source, f"<policy:{policy.name}>", "eval"),
            {"__builtins__": {}},
            local_ns,
        )
    except Exception as exc:
        return [PolicyViolation(
            file_path=file_path,
            line=0,
            col=0,
            policy_name=policy.name,
            check_name="custom:error",
            severity=policy.severity,
            message=f"Custom check failed: {exc}",
        )]

    if not isinstance(result, list):
        return []

    violations: list[PolicyViolation] = []
    for entry in result:
        if not isinstance(entry, dict):
            continue
        violations.append(PolicyViolation(
            file_path=file_path,
            line=entry.get("line", 0),
            col=entry.get("col", 0),
            policy_name=policy.name,
            check_name="custom",
            severity=policy.severity,
            message=entry.get("message", policy.description),
            witness=entry.get("witness", []),
        ))
    return violations


def _run_datalog_check(
    check: DatalogCheck,
    policy: Policy,
    project_path: str,
) -> list[PolicyViolation]:
    """Run a CozoScript Datalog query against the project's fact graph.

    The query must return columns that include at least ``file_path``
    and ``line``.  An optional ``message`` column overrides the policy
    description.  All other columns are added to the witness list.
    """
    from emend.fact_graph import FactGraph

    violations: list[PolicyViolation] = []
    try:
        graph = FactGraph.build_from_project(project_path)
        result = graph.run_query(check.cozoscript)
    except Exception as exc:
        return [PolicyViolation(
            file_path="<project>",
            line=0,
            col=0,
            policy_name=policy.name,
            check_name="datalog:error",
            severity=policy.severity,
            message=f"Datalog check failed: {exc}",
        )]

    headers = result.get("headers", [])
    rows = result.get("rows", [])

    # Find column indices
    def _col_idx(name: str) -> int | None:
        try:
            return headers.index(name)
        except ValueError:
            return None

    _fp = _col_idx("file_path")
    _f = _col_idx("file")
    fp_idx = _fp if _fp is not None else _f if _f is not None else 0
    _ln = _col_idx("line")
    line_idx = _ln if _ln is not None else (1 if len(headers) > 1 else 0)
    msg_idx = _col_idx("message")

    for row in rows:
        file_path = str(row[fp_idx]) if fp_idx is not None and fp_idx < len(row) else "<unknown>"
        line = int(row[line_idx]) if line_idx is not None and line_idx < len(row) else 0
        message = str(row[msg_idx]) if msg_idx is not None and msg_idx < len(row) else policy.description
        witness = [f"{h}={v}" for h, v in zip(headers, row)]
        violations.append(PolicyViolation(
            file_path=file_path,
            line=line,
            col=0,
            policy_name=policy.name,
            check_name="datalog",
            severity=policy.severity,
            message=message,
            witness=witness,
        ))
    return violations


def _run_sequence_check(
    check: SequenceCheck,
    policy: Policy,
    project_path: str,
) -> list[PolicyViolation]:
    """Run a temporal sequence check against the project's fact graph.

    Resolves each step via pattern matching (Python), then compiles to
    a CozoScript Datalog query for CFG-reachability and def-use liveness
    checks via ``compile_sequence_rule()`` in ``fact_graph.py``.
    """
    from emend.fact_graph import FactGraph, compile_sequence_rule

    violations: list[PolicyViolation] = []
    try:
        graph = FactGraph.build_from_project(project_path)
    except Exception as exc:
        return [PolicyViolation(
            file_path="<project>",
            line=0, col=0,
            policy_name=policy.name,
            check_name=f"sequence:{check.name}:error",
            severity=policy.severity,
            message=f"Sequence check setup failed: {exc}",
        )]

    try:
        result = compile_sequence_rule(graph, check)
        if result is None:
            return []
        query_str, step_data = result
        query_result = graph.run_query(query_str)
    except Exception as exc:
        return [PolicyViolation(
            file_path="<project>",
            line=0, col=0,
            policy_name=policy.name,
            check_name=f"sequence:{check.name}:error",
            severity=policy.severity,
            message=f"Sequence check failed: {exc}",
        )]

    headers = query_result.get("headers", [])
    rows = query_result.get("rows", [])

    for row in rows:
        row_dict = dict(zip(headers, row))
        fp = row_dict.get("fp", "<unknown>")
        fq = row_dict.get("fq", "")
        first_line = row_dict.get("first_line", 0)
        last_line = row_dict.get("last_line", 0)

        # Build WitnessStep entries from per-step line columns
        from emend.flow_ir import WitnessStep, format_witness as _fmt_witness

        witness_steps: list[WitnessStep] = []
        for h, v in zip(headers, row):
            if h not in ("fp", "fq") and h.startswith("line_"):
                step_idx = h.replace("line_", "")
                step_name = ""
                try:
                    idx = int(step_idx)
                    if idx < len(check.sequence):
                        step_name = check.sequence[idx].bind
                except (ValueError, IndexError):
                    pass
                witness_steps.append(WitnessStep(
                    file_path=str(fp),
                    func_qn=str(fq),
                    block_id=0,
                    line=int(v),
                    var_name=step_name,
                    kind="step",
                ))
            elif h in ("first_line", "last_line"):
                kind = "source" if h == "first_line" else "sink"
                witness_steps.append(WitnessStep(
                    file_path=str(fp),
                    func_qn=str(fq),
                    block_id=0,
                    line=int(v),
                    kind=kind,
                ))

        witness = _fmt_witness(witness_steps) if witness_steps else [
            f"{h}={v}" for h, v in zip(headers, row) if h not in ("fp", "fq")
        ]

        violations.append(PolicyViolation(
            file_path=str(fp),
            line=int(first_line),
            col=0,
            policy_name=policy.name,
            check_name=f"sequence:{check.name}",
            severity=check.severity,
            message=check.message,
            witness=witness,
        ))

    return violations


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
    """Run all policy checks against the given file paths.

    Args:
        paths: List of file paths to check.
        policies: List of ``Policy`` objects to enforce.
        language: Source language (default ``"python"``).
        project_path: Project root directory (needed for dead code and type
            checks that require cross-file analysis).

    Returns:
        List of ``PolicyViolation`` objects, sorted by file path and line.
    """
    from emend import emend_core

    violations: list[PolicyViolation] = []

    # Separate project-level policies from per-file policies
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

    # Run project-level checks
    if deadcode_policies and project_path:
        for policy, check in deadcode_policies:
            violations.extend(_run_deadcode_check(check, policy, project_path))

    if datalog_policies and project_path:
        for policy, check in datalog_policies:
            violations.extend(_run_datalog_check(check, policy, project_path))

    if sequence_policies and project_path:
        for policy, check in sequence_policies:
            violations.extend(_run_sequence_check(check, policy, project_path))

    # Run per-file checks
    if file_policies:
        # Batch-read files via Rust for performance
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

    # Sort by file path, then line
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
    """Format policy violations for display.

    Args:
        violations: List of violations to format.
        json_output: If True, output as JSON.

    Returns:
        Formatted string.
    """
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

    # Summary
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
