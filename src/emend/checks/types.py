"""Type constraint checks: oracle-driven type assertions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from emend.errors import BUG_EXCEPTIONS

from emend.checks.violations import PolicyViolation

if TYPE_CHECKING:
    from emend.policy import Policy

logger = logging.getLogger(__name__)


@dataclass
class TypeCheck:
    """Type constraint check on symbols."""
    symbol_pattern: str
    expected_type: str
    kind: str = "has_type"  # "has_type" or "returns"


def run_type_check(
    check: TypeCheck,
    policy: "Policy",
    file_path: str,
    source: str,
    language: str,
    project_root: str | None = None,
) -> "list[PolicyViolation]":
    """Run a type constraint check using the type oracle."""
    from emend.transform import find_pattern

    def unavailable(reason: str) -> list[PolicyViolation]:
        return [PolicyViolation(
            file_path=file_path,
            line=0,
            col=0,
            policy_name=policy.name,
            check_name=f"type:unavailable:{check.kind}",
            severity=policy.severity,
            message=f"{policy.description}: type oracle unavailable ({reason})",
            witness=[],
        )]

    if check.kind == "returns":
        augmented_pattern = f"{check.symbol_pattern}:returns[{check.expected_type}]"
    else:
        augmented_pattern = f"{check.symbol_pattern}:type[{check.expected_type}]"

    all_matches = find_pattern(
        check.symbol_pattern,
        file_path,
        source_override=source,
        language=language,
    )
    if not all_matches:
        return []
    if not project_root:
        return unavailable("no project root configured")

    type_oracle = None
    try:
        from emend.type_oracle import create_type_oracle
        type_oracle = create_type_oracle(project_root=Path(project_root))
        if not type_oracle.is_available():
            return unavailable("configured engine is not installed")
    except BUG_EXCEPTIONS:
        raise
    except Exception as exc:
        logger.debug("Could not create type oracle for type check", exc_info=True)
        return unavailable(str(exc) or "engine initialization failed")

    typed_matches = find_pattern(
        augmented_pattern,
        file_path,
        source_override=source,
        type_oracle=type_oracle,
        language=language,
    )

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
