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
class DeadCodeCheck:
    """Dead code detection policy."""
    entry_point_decorators: list[str] = field(default_factory=list)
    entry_point_names: list[str] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=list)


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


# Union of all check types
PolicyCheck = FlowCheck | StructuralCheck | TypeCheck | DeadCodeCheck | CustomCheck | DatalogCheck


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
_VALID_CHECK_TYPES = {"flow", "structural", "type", "deadcode", "custom", "datalog"}
_VALID_TYPE_KINDS = {"has_type", "returns"}


def _yaml_key(raw: dict[str, Any], *keys: str) -> Any:
    """Look up a value by trying multiple key variants (underscore and hyphen)."""
    for key in keys:
        if key in raw:
            return raw[key]
        alt = key.replace("_", "-")
        if alt in raw:
            return raw[alt]
    return None


def _parse_check(raw: dict[str, Any]) -> PolicyCheck:
    """Parse a single check definition from a YAML dict.

    Accepts both underscore (``flows_from``) and hyphenated (``flows-from``)
    key variants so that YAML files can use the more natural hyphenated form.
    """
    check_type = raw.get("type", "")
    if check_type == "flow":
        flows_from = _yaml_key(raw, "flows_from")
        flows_to = _yaml_key(raw, "flows_to")
        if not flows_from or not flows_to:
            raise ValueError("FlowCheck requires 'flows_from' and 'flows_to'")
        return FlowCheck(
            flows_from=flows_from,
            flows_to=flows_to,
            not_through=_yaml_key(raw, "not_through"),
            label=_yaml_key(raw, "label") or "",
        )
    elif check_type == "structural":
        pattern = raw.get("pattern")
        if not pattern:
            raise ValueError("StructuralCheck requires 'pattern'")
        return StructuralCheck(
            pattern=pattern,
            inside=raw.get("inside"),
            not_inside=_yaml_key(raw, "not_inside"),
            where=raw.get("where"),
        )
    elif check_type == "type":
        kind = raw.get("kind", "has_type")
        symbol_pattern = _yaml_key(raw, "symbol_pattern")
        expected_type = _yaml_key(raw, "expected_type")
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
            entry_point_decorators=_as_list(_yaml_key(raw, "entry_point_decorators")),
            entry_point_names=_as_list(_yaml_key(raw, "entry_point_names")),
            exclude_paths=_as_list(_yaml_key(raw, "exclude_paths")),
        )
    elif check_type == "custom":
        query_source = _yaml_key(raw, "query_source")
        if not query_source:
            raise ValueError("CustomCheck requires 'query_source'")
        return CustomCheck(query_source=query_source)
    elif check_type == "datalog":
        cozoscript = raw.get("cozoscript") or _yaml_key(raw, "query")
        if not cozoscript:
            raise ValueError("DatalogCheck requires 'cozoscript' or 'query'")
        return DatalogCheck(cozoscript=cozoscript)
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
    import yaml

    if config_path is None:
        config_path = Path(".emend/policies.yaml")
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Policy config not found: {path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict) or "policies" not in data:
        raise ValueError(
            "Policy config must be a YAML mapping with a top-level 'policies' key"
        )

    policies: list[Policy] = []
    for raw_policy in data["policies"]:
        checks = [_parse_check(c) for c in raw_policy.get("checks", [])]
        policies.append(Policy(
            name=raw_policy["name"],
            description=raw_policy.get("description", ""),
            severity=raw_policy.get("severity", "warning"),
            checks=checks,
        ))
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
) -> list[PolicyViolation]:
    """Run a flow check using the lint engine's _check_flow_rule."""
    from emend.lint import LintRule, _check_flow_rule

    rule = LintRule(
        name=policy.name,
        find="",
        message=policy.description,
        flows_from=check.flows_from,
        flows_to=check.flows_to,
        not_through=check.not_through,
    )
    lint_violations = _check_flow_rule(rule, file_path, source, language)

    violations: list[PolicyViolation] = []
    for lv in lint_violations:
        witness: list[str] = []
        if lv.witness is not None:
            witness.append(f"source L{lv.witness.source_line}: {lv.witness.source_text}")
            for step_line, step_var in lv.witness.taint_chain:
                witness.append(f"  -> L{step_line}: {step_var}")
            witness.append(f"sink L{lv.witness.sink_line}: {lv.witness.sink_text}")
        violations.append(PolicyViolation(
            file_path=lv.file_path,
            line=lv.line,
            col=lv.col,
            policy_name=policy.name,
            check_name=f"flow:{check.label or 'default'}",
            severity=policy.severity,
            message=lv.message or policy.description,
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

    fp_idx = _col_idx("file_path") or _col_idx("file") or 0
    line_idx = _col_idx("line") or (1 if len(headers) > 1 else 0)
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
    file_policies: list[tuple[Policy, PolicyCheck]] = []

    for policy in policies:
        for check in policy.checks:
            if isinstance(check, DeadCodeCheck):
                deadcode_policies.append((policy, check))
            elif isinstance(check, DatalogCheck):
                datalog_policies.append((policy, check))
            else:
                file_policies.append((policy, check))

    # Run project-level checks
    if deadcode_policies and project_path:
        for policy, check in deadcode_policies:
            violations.extend(_run_deadcode_check(check, policy, project_path))

    if datalog_policies and project_path:
        for policy, check in datalog_policies:
            violations.extend(_run_datalog_check(check, policy, project_path))

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
