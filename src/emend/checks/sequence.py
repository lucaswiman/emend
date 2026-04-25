"""SequenceCheck: multi-step temporal sequence check.

Extracted from policy.py's SequenceCheck, SequenceStep, SequencePathConstraint,
and _run_sequence_check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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


def run_sequence_check(
    check: SequenceCheck,
    policy_name: str,
    policy_desc: str,
    policy_severity: str,
    project_path: str,
) -> list[dict[str, Any]]:
    """Run a temporal sequence check against the project's fact graph.

    Returns violation dicts.
    """
    from emend.fact_graph import FactGraph, compile_sequence_rule
    from emend.checks.flow import WitnessStep, format_witness as _fmt_witness

    try:
        graph = FactGraph.build_from_project(project_path)
    except Exception as exc:
        return [{
            "file_path": "<project>",
            "line": 0,
            "col": 0,
            "policy_name": policy_name,
            "check_name": f"sequence:{check.name}:error",
            "severity": policy_severity,
            "message": f"Sequence check setup failed: {exc}",
            "witness": [],
        }]

    try:
        result = compile_sequence_rule(graph, check)
        if result is None:
            return []
        query_str, step_data = result
        query_result = graph.run_query(query_str)
    except Exception as exc:
        return [{
            "file_path": "<project>",
            "line": 0,
            "col": 0,
            "policy_name": policy_name,
            "check_name": f"sequence:{check.name}:error",
            "severity": policy_severity,
            "message": f"Sequence check failed: {exc}",
            "witness": [],
        }]

    headers = query_result.get("headers", [])
    rows = query_result.get("rows", [])

    violations = []
    for row in rows:
        row_dict = dict(zip(headers, row))
        fp = row_dict.get("fp", "<unknown>")
        fq = row_dict.get("fq", "")
        first_line = row_dict.get("first_line", 0)

        # Build WitnessStep entries from per-step line columns
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

        violations.append({
            "file_path": str(fp),
            "line": int(first_line),
            "col": 0,
            "policy_name": policy_name,
            "check_name": f"sequence:{check.name}",
            "severity": check.severity,
            "message": check.message,
            "witness": witness,
        })

    return violations
