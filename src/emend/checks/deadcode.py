"""DeadCodeCheck: wrapper around transform.find_dead_code.

Extracted from policy.py's DeadCodeCheck and _run_deadcode_check.
The DeadCodeConfig dataclass lives in checks/rules_config.py (shared with lint).
"""

from __future__ import annotations

from typing import Any

# DeadCodeCheck is an alias for DeadCodeConfig (shared config dataclass)
from emend.checks.rules_config import DeadCodeConfig as DeadCodeCheck


def run_deadcode_check(
    check: DeadCodeCheck,
    policy_name: str,
    policy_desc: str,
    policy_severity: str,
    project_path: str,
) -> list[dict[str, Any]]:
    """Run dead code detection as a policy check.

    Returns violation dicts.
    """
    from emend.transform import find_dead_code

    violations = []
    for ds in find_dead_code(
        project_path,
        entry_point_decorators=check.entry_point_decorators or None,
        entry_point_names=check.entry_point_names or None,
        exclude_paths=check.exclude_paths or None,
    ):
        violations.append({
            "file_path": ds.file_path,
            "line": ds.line,
            "col": 0,
            "policy_name": policy_name,
            "check_name": "deadcode",
            "severity": policy_severity,
            "message": f"{policy_desc}: {ds.name} ({ds.reason})",
            "witness": [ds.selector],
        })
    return violations
