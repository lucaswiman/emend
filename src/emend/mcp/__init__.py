"""emend MCP package.

Importing this package registers all MCP tools onto `mcp_app` and
re-exports the public surface that external callers depend on.
"""

from __future__ import annotations

# Import dispatch first (creates mcp_app), then each bucket module in order.
# Each bucket's @mcp_app.tool() decorators fire on import, registering tools.
from emend.mcp import dispatch as _dispatch
from emend.mcp import find as _find
from emend.mcp import edit as _edit
from emend.mcp import analyze as _analyze
from emend.mcp import checks as _checks
from emend.mcp import tooling as _tooling

# Snapshot all tools after all buckets are registered so configure_profile()
# can restore the full set.
_dispatch._snapshot_all_tools()

# Apply the default (core) profile.
_dispatch.configure_profile(profile="core")

# Public re-exports
from emend.mcp.dispatch import (
    mcp_app,
    dump_schema,
    configure_profile,
    run_server,
    PROFILES,
    _CORE_TOOLS,
    _ALL_TOOLS,
    _restore_all_tools,
)

from emend.mcp.find import search
from emend.mcp.edit import transform, replace, modify, rename, move
from emend.mcp.analyze import (
    analyze,
    references,
    refs,
    graph,
    deadcode,
    impact,
    semantic_context,
    trace_analysis,
    duplicates_analysis,
)
from emend.mcp.checks import check
from emend.mcp.tooling import (
    facts_query,
    mappings,
    grammar_and_cookbook,
    map_read,
    map_write,
)

__all__ = [
    "mcp_app",
    "dump_schema",
    "configure_profile",
    "run_server",
    "PROFILES",
    "_CORE_TOOLS",
    "_ALL_TOOLS",
    "_restore_all_tools",
    "search",
    "transform",
    "replace",
    "modify",
    "rename",
    "move",
    "analyze",
    "references",
    "refs",
    "graph",
    "deadcode",
    "impact",
    "semantic_context",
    "trace_analysis",
    "duplicates_analysis",
    "check",
    "facts_query",
    "mappings",
    "grammar_and_cookbook",
    "map_read",
    "map_write",
]
