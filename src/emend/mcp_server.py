"""MCP (Model Context Protocol) server for emend.

Exposes emend's refactoring commands as MCP tools, allowing LLM-based
clients to perform structured code search, editing, and refactoring.

Usage:
    emend mcp              # Start MCP server on stdio
    emend mcp --transport sse --port 8080  # Start on SSE transport

Requires the 'mcp' optional dependency:
    pip install emend[mcp]
"""

from __future__ import annotations

# Re-export everything from the mcp package for backwards compatibility.
from emend.mcp import (  # noqa: F401
    mcp_app,
    dump_schema,
    configure_profile,
    run_server,
    PROFILES,
    _CORE_TOOLS,
    _ALL_TOOLS,
    _restore_all_tools,
    search,
    transform,
    replace,
    modify,
    rename,
    move,
    analyze,
    references,
    refs,
    graph,
    deadcode,
    impact,
    semantic_context,
    trace_analysis,
    duplicates_analysis,
    check,
    facts_query,
    mappings,
    grammar_and_cookbook,
    map_read,
    map_write,
)
