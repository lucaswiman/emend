"""Structural pattern checks: pattern must/must-not appear in certain scopes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from emend.policy import Policy, PolicyViolation

logger = logging.getLogger(__name__)


@dataclass
class StructuralCheck:
    """Pattern must/must-not appear in certain scopes."""
    pattern: str
    inside: str | None = None
    not_inside: str | None = None
    where: str | None = None


def run_structural_check(
    check: StructuralCheck,
    policy: "Policy",
    file_path: str,
    source: str,
    language: str,
) -> "list[PolicyViolation]":
    """Run a structural pattern check using transform.find_pattern."""
    from emend.transform import find_pattern
    from emend.policy import PolicyViolation

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
