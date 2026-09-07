"""Dead code detection as a policy check wrapper."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from emend.checks.violations import PolicyViolation

if TYPE_CHECKING:
    from emend.policy import Policy

logger = logging.getLogger(__name__)

# DeadCodeCheck is an alias for DeadCodeConfig — re-export for check-side code.
from emend.checks.rules_config import DeadCodeConfig as DeadCodeCheck  # noqa: F401, E402


def run_deadcode_check(
    check: "DeadCodeCheck",
    policy: "Policy",
    project_path: str,
) -> "list[PolicyViolation]":
    """Run dead code detection as a policy check."""
    from emend.checks.rules_config import deadcode_engine_kwargs
    from emend.transform import dead_code_result_details, find_dead_code

    violations: list[PolicyViolation] = []
    for ds in find_dead_code(
        project_path,
        **deadcode_engine_kwargs(check),
    ):
        name, line, witness, reason = dead_code_result_details(ds)
        violations.append(PolicyViolation(
            file_path=ds.file_path,
            line=line,
            col=0,
            policy_name=policy.name,
            check_name="deadcode",
            severity=policy.severity,
            message=f"{policy.description}: {name} ({reason})",
            witness=[witness],
        ))
    return violations
