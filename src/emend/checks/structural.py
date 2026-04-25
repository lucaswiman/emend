"""Structural check: pattern must/must-not appear in certain scopes.

Extracted from policy.py's _run_structural_check and StructuralCheck.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


@dataclass
class StructuralCheck:
    """Pattern must/must-not appear in certain scopes."""
    pattern: str
    inside: str | None = None
    not_inside: str | None = None
    where: str | None = None


def run_structural_check(
    check: StructuralCheck,
    policy_name: str,
    policy_desc: str,
    policy_severity: str,
    file_path: str,
    source: str,
    language: str,
) -> list[dict[str, Any]]:
    """Run a structural pattern check using transform.find_pattern.

    Returns a list of violation dicts with keys:
      file_path, line, col, policy_name, check_name, severity, message, witness
    """
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

    violations = []
    for m in matches:
        violations.append({
            "file_path": file_path,
            "line": m.line or 0,
            "col": m.col or 0,
            "policy_name": policy_name,
            "check_name": f"structural:{check.pattern}",
            "severity": policy_severity,
            "message": policy_desc,
            "witness": [m.matched_text or m.node_text or ""],
        })
    return violations
