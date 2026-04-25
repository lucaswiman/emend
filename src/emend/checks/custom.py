"""CustomCheck: Python callable check.

Extracted from policy.py's CustomCheck and _run_custom_check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CustomCheck:
    """Expert query using raw query source."""
    query_source: str


def run_custom_check(
    check: CustomCheck,
    policy_name: str,
    policy_desc: str,
    policy_severity: str,
    file_path: str,
    source: str,
    language: str,
) -> list[dict[str, Any]]:
    """Run a custom expert query.

    Returns violation dicts.
    """
    from emend.transform import find_pattern

    local_ns: dict[str, Any] = {
        "find_pattern": find_pattern,
        "file_path": file_path,
        "source": source,
        "language": language,
    }

    try:
        result = eval(  # noqa: S307 - intentional expert-mode eval
            compile(check.query_source, f"<policy:{policy_name}>", "eval"),
            {"__builtins__": {}},
            local_ns,
        )
    except Exception as exc:
        return [{
            "file_path": file_path,
            "line": 0,
            "col": 0,
            "policy_name": policy_name,
            "check_name": "custom:error",
            "severity": policy_severity,
            "message": f"Custom check failed: {exc}",
            "witness": [],
        }]

    if not isinstance(result, list):
        return []

    violations = []
    for entry in result:
        if not isinstance(entry, dict):
            continue
        violations.append({
            "file_path": file_path,
            "line": entry.get("line", 0),
            "col": entry.get("col", 0),
            "policy_name": policy_name,
            "check_name": "custom",
            "severity": policy_severity,
            "message": entry.get("message", policy_desc),
            "witness": entry.get("witness", []),
        })
    return violations
