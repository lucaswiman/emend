"""MCP checks tool."""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field

from emend.mcp.dispatch import mcp_app


@mcp_app.tool()
def check(
    paths: Annotated[list[str] | None, Field(description="File or directory scope(s) to check.")] = None,
    config: Annotated[str | None, Field(description="Rules config path. Defaults to .emend/rules.yaml with legacy fallback.")] = None,
    rule: Annotated[str | None, Field(description="Run only one named rule.")] = None,
    kind: Annotated[str | None, Field(description="Restrict to one rule kind: match, flow, deadcode, type.")] = None,
    mode: Annotated[str | None, Field(description="Engine mode: 'lint' (pattern/flow/deadcode), 'policy' (structural/type/datalog/custom/sequence), or None for all.")] = None,
    fix: Annotated[bool, Field(description="Apply auto-fixes for match rules when available.")] = False,
) -> str:
    """Run unified project rules from ``rules.yaml``.

    Routes through the single checks engine. Use mode='lint' to restrict to
    pattern/flow/deadcode rules, mode='policy' for structural/type/datalog/
    custom/sequence policies, or omit mode for all rules.
    """
    from emend.checks import run_checks
    from emend.cli_base import resolve_file_scopes

    resolved, _ = resolve_file_scopes(paths or ["."], language="python")
    file_paths = [str(f) for f in resolved]
    project_path = paths[0] if paths else "."
    try:
        violations = run_checks(
            file_paths,
            config=config,
            rule_name=rule,
            kind=kind,
            mode=mode,
            fix=fix,
            language="python",
            project_path=project_path,
        )
    except FileNotFoundError as e:
        return json.dumps({"error": str(e)})
    return json.dumps([
        {
            "rule": violation.rule_name,
            "kind": violation.kind,
            "severity": violation.severity,
            "message": violation.message,
            "file": violation.file_path,
            "line": violation.line,
            "col": violation.col,
            "witness": violation.witness or [],
        }
        for violation in violations
    ], indent=2)
