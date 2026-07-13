"""Dead code detection as a policy check wrapper."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from emend.policy import Policy, PolicyViolation
    from emend.checks.rules_config import DeadCodeConfig

logger = logging.getLogger(__name__)

# DeadCodeCheck is an alias for DeadCodeConfig — re-export for check-side code.
from emend.checks.rules_config import DeadCodeConfig as DeadCodeCheck  # noqa: F401, E402


def run_deadcode_check(
    check: "DeadCodeCheck",
    policy: "Policy",
    project_path: str,
) -> "list[PolicyViolation]":
    """Run dead code detection as a policy check."""
    from emend.transform import find_dead_code
    from emend.policy import PolicyViolation

    violations: list[PolicyViolation] = []
    for ds in find_dead_code(
        project_path,
        entry_point_decorators=check.entry_point_decorators or None,
        entry_point_names=check.entry_point_names or None,
        exclude_paths=check.exclude_paths or None,
    ):
        violations.append(PolicyViolation(
            file_path=ds.file_path,
            line=ds.line,
            col=0,
            policy_name=policy.name,
            check_name="deadcode",
            severity=policy.severity,
            message=f"{policy.description}: {ds.name} ({ds.reason})",
            witness=[ds.selector],
        ))
    return violations
