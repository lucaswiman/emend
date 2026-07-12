"""Temporal sequence checks: multi-step CFG-reachability rules."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from emend.errors import BUG_EXCEPTIONS

if TYPE_CHECKING:
    from emend.policy import Policy, PolicyViolation

logger = logging.getLogger(__name__)


@dataclass
class SequenceStep:
    """A single step in a temporal sequence rule."""
    bind: str
    pattern: str | None = None
    effect: str | None = None
    type_constraint: str | None = None


@dataclass
class SequencePathConstraint:
    """Path constraint between two consecutive sequence steps."""
    from_step: str
    to_step: str
    not_through: list[str] = field(default_factory=list)
    not_through_scope: list[str] = field(default_factory=list)


@dataclass
class SequenceCheck:
    """Multi-step temporal sequence check."""
    name: str
    message: str
    sequence: list[SequenceStep]
    path_constraints: list[SequencePathConstraint] = field(default_factory=list)
    severity: str = "error"


def run_sequence_check(
    check: SequenceCheck,
    policy: "Policy",
    project_path: str,
) -> "list[PolicyViolation]":
    """Run a temporal sequence check against the project's fact graph."""
    from emend.fact_graph import FactGraph, compile_sequence_rule
    from emend.checks.flow import WitnessStep, format_witness as _fmt_witness
    from emend.policy import PolicyViolation

    violations: list[PolicyViolation] = []
    try:
        graph = FactGraph.build_from_project(project_path)
    except BUG_EXCEPTIONS:
        raise
    except Exception as exc:
        logger.debug("Sequence check setup failed", exc_info=True)
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
    except BUG_EXCEPTIONS:
        raise
    except Exception as exc:
        logger.debug("Sequence check failed", exc_info=True)
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
