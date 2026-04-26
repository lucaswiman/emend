"""CozoScript Datalog query checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from emend.policy import Policy, PolicyViolation

logger = logging.getLogger(__name__)


@dataclass
class DatalogCheck:
    """CozoScript Datalog query check.

    The query must return rows with at least ``line``, ``col``, ``message``
    columns. Each returned row becomes a policy violation.
    """
    cozoscript: str


def run_datalog_check(
    check: DatalogCheck,
    policy: "Policy",
    project_path: str,
) -> "list[PolicyViolation]":
    """Run a CozoScript Datalog query against the project's fact graph."""
    from emend.fact_graph import FactGraph
    from emend.policy import PolicyViolation

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
