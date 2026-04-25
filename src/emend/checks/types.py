"""TypeCheck: type constraint check on symbols.

Extracted from policy.py's TypeCheck and _run_type_check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_VALID_TYPE_KINDS = {"has_type", "returns"}


@dataclass
class TypeCheck:
    """Type constraint check on symbols."""
    symbol_pattern: str
    expected_type: str
    kind: str = "has_type"  # "has_type" or "returns"


def run_type_check(
    check: TypeCheck,
    policy_name: str,
    policy_desc: str,
    policy_severity: str,
    file_path: str,
    source: str,
    language: str,
    project_root: str | None = None,
) -> list[dict[str, Any]]:
    """Run a type constraint check using the type oracle.

    Returns violation dicts with file_path, line, col, message, etc.
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
    violations = []
    for m in all_matches:
        if (m.line, m.col) not in typed_positions:
            violations.append({
                "file_path": file_path,
                "line": m.line or 0,
                "col": m.col or 0,
                "policy_name": policy_name,
                "check_name": f"type:{check.kind}:{check.expected_type}",
                "severity": policy_severity,
                "message": (
                    f"{policy_desc}: expected {check.kind} "
                    f"{check.expected_type!r} on {m.matched_text or m.node_text or '?'}"
                ),
                "witness": [m.matched_text or m.node_text or ""],
            })
    return violations
