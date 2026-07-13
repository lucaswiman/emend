"""CozoScript Datalog query checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from emend.errors import BUG_EXCEPTIONS

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

    def _col_idx(name: str) -> int | None:
        try:
            return headers.index(name)
        except ValueError:
            return None

    # Identify columns by name only.  Positional guessing is unsafe: a query
    # like ``?[count, name]`` has no file/line columns, and treating column 0
    # as the file path (or coercing column 1 into an int line) silently
    # corrupts data or raises.  When a column is absent we use a sentinel and
    # rely on the witness (below) to preserve the full row.
    _fp = _col_idx("file_path")
    fp_idx = _fp if _fp is not None else _col_idx("file")
    line_idx = _col_idx("line")
    msg_idx = _col_idx("message")

    for row in rows:
        file_path = (
            str(row[fp_idx])
            if fp_idx is not None and fp_idx < len(row)
            else "<project>"
        )
        line = 0
        if line_idx is not None and line_idx < len(row):
            try:
                line = int(row[line_idx])
            except (ValueError, TypeError):
                # Malformed/non-numeric line value: keep going with line 0 so
                # one bad row doesn't crash the whole check.  The raw value
                # remains available in the witness.
                line = 0
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
