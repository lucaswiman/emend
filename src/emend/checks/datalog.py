"""CozoScript Datalog query checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from emend.errors import BUG_EXCEPTIONS

from emend.checks.violations import PolicyViolation

if TYPE_CHECKING:
    from emend.policy import Policy

logger = logging.getLogger(__name__)


@dataclass
class DatalogCheck:
    """CozoScript Datalog query check.

    Each returned row becomes a policy violation. Optional ``file_path``
    (or ``file``), ``line``, ``col`` and ``message`` columns supply its location
    and message; arbitrary projections are preserved in the witness.
    """
    cozoscript: str


def run_datalog_check(
    check: DatalogCheck,
    policy: "Policy",
    project_path: str,
) -> "list[PolicyViolation]":
    """Run a CozoScript Datalog query against the project's fact graph."""
    from emend.fact_graph import FactGraph

    violations: list[PolicyViolation] = []
    try:
        graph = FactGraph.build_from_project(project_path)
        result = graph.run_query(check.cozoscript)
    except BUG_EXCEPTIONS:
        raise
    except Exception as exc:
        logger.debug("Datalog check failed", exc_info=True)
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

    def coordinate(value: object) -> int:
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0

    for row in rows:
        values = {}
        for header, value in zip(headers, row):
            values.setdefault(header, value)
        witness = [f"{h}={v}" for h, v in zip(headers, row)]
        violations.append(PolicyViolation(
            file_path=str(values.get("file_path", values.get("file", "<project>"))),
            line=coordinate(values.get("line", 0)),
            col=coordinate(values.get("col", 0)),
            policy_name=policy.name,
            check_name="datalog",
            severity=policy.severity,
            message=str(values.get("message", policy.description)),
            witness=witness,
        ))
    return violations
