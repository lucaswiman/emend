"""Custom expert query checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from emend.policy import Policy, PolicyViolation

logger = logging.getLogger(__name__)


@dataclass
class CustomCheck:
    """Expert query using raw Python eval query source."""
    query_source: str


def run_custom_check(
    check: CustomCheck,
    policy: "Policy",
    file_path: str,
    source: str,
    language: str,
) -> "list[PolicyViolation]":
    """Run a custom expert query.

    The ``query_source`` is executed as a Python expression with access to
    emend's ``find_pattern`` and the file content.  It must return a list
    of dicts with at least ``line``, ``col``, ``message`` keys.
    """
    from emend.transform import find_pattern
    from emend.policy import PolicyViolation

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
