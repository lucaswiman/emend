"""FastMCP app instance and server lifecycle helpers."""

from __future__ import annotations

import json
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from emend.errors import BUG_EXCEPTIONS

mcp_app = FastMCP(
    "emend",
    instructions="""\
emend is a Python refactoring tool. All write operations default to dry-run
(showing diffs). Set apply=True to write changes.

Call the grammar_and_cookbook tool for full syntax reference.

## Quick reference

Prefer the discriminated tools:
- search(mode=code|symbol|summary)
- transform(operation=replace|edit|add|remove|rename|move)
- references(mode=refs|callers|callees)
- analyze(mode=graph|deadcode|impact|semantic_context|trace|duplicates)
- check(mode=lint|policy)
- facts_query(fact_type=symbols|calls|references|trace_flows|types|imports)
- mappings(operation=read|write)
""",
)


def _warm_caches_background() -> None:
    """Warm parse and QN-index caches in a background process."""
    import multiprocessing
    import logging as _logging

    def _worker() -> None:
        _logging.basicConfig(level=_logging.WARNING)
        try:
            from emend.transform import warm_caches
            warm_caches(".")
        except BUG_EXCEPTIONS:
            raise
        except Exception:
            _logging.getLogger("emend.mcp").debug("Background cache warming failed", exc_info=True)

    proc = multiprocessing.Process(target=_worker, daemon=True)
    proc.start()


def _compress_schema(obj: object) -> object:
    """Recursively compress a JSON-Schema dict."""
    if isinstance(obj, list):
        return [_compress_schema(item) for item in obj]
    if not isinstance(obj, dict):
        return obj

    compressed: dict = {k: _compress_schema(v) for k, v in obj.items() if k != "title"}

    if "anyOf" in compressed:
        entries = compressed["anyOf"]
        if isinstance(entries, list) and len(entries) == 2:
            null_entries = [e for e in entries if isinstance(e, dict) and e.get("type") == "null"]
            real_entries = [e for e in entries if isinstance(e, dict) and e.get("type") != "null"]
            if len(null_entries) == 1 and len(real_entries) == 1:
                real = real_entries[0]
                real_type = real.get("type")
                if real_type in ("string", "integer", "boolean", "number", "array"):
                    del compressed["anyOf"]
                    compressed["type"] = real_type
                    if real_type == "array" and "items" in real:
                        compressed["items"] = real["items"]
                    compressed.setdefault("default", None)

    return compressed


_CORE_TOOLS: set[str] = {
    "search",
    "transform",
    "references",
    "analyze",
    "check",
    "facts_query",
    "grammar_and_cookbook",
}

PROFILES: dict[str, set[str]] = {
    "core": set(_CORE_TOOLS),
    "refactor": set(_CORE_TOOLS),
    "expert": set(_CORE_TOOLS) | {"mappings"},
}

_ALL_TOOLS: dict[str, Any] = {}


def _snapshot_all_tools() -> None:
    """Capture all registered tools after all buckets have been imported."""
    _ALL_TOOLS.clear()
    _ALL_TOOLS.update({t.name: t for t in mcp_app._tool_manager.list_tools()})


def _restore_all_tools() -> None:
    mcp_app._tool_manager._tools.clear()
    mcp_app._tool_manager._tools.update(_ALL_TOOLS)


def _resolve_profile_tools(
    profile: str | None = None,
    tools: list[str] | None = None,
) -> set[str] | None:
    if tools is not None:
        return set(tools)
    if profile == "full":
        return None
    if profile is None:
        return set(_CORE_TOOLS)
    keep = PROFILES.get(profile)
    if keep is None:
        valid = ", ".join(sorted(PROFILES.keys()) + ["full"])
        raise ValueError(f"Unknown profile {profile!r}. Available: {valid}")
    return set(keep)


def dump_schema(
    profile: str | None = None,
    tools: list[str] | None = None,
) -> str:
    """Return the MCP tool schema as a JSON string."""
    selected = _resolve_profile_tools(profile=profile, tools=tools)
    all_tools = mcp_app._tool_manager.list_tools()
    result = [
        {
            "name": t.name,
            "description": t.description or "",
            "inputSchema": _compress_schema(t.parameters),
        }
        for t in all_tools
        if selected is None or t.name in selected
    ]
    return json.dumps({"tools": result}, indent=2)


def configure_profile(
    profile: str | None = None,
    tools: list[str] | None = None,
) -> None:
    """Prune the tool registry to match *profile* or an explicit *tools* list."""
    _restore_all_tools()

    keep = _resolve_profile_tools(profile=profile, tools=tools)
    if keep is None:
        return

    all_tools = mcp_app._tool_manager.list_tools()
    for t in all_tools:
        if t.name not in keep:
            mcp_app._tool_manager._tools.pop(t.name, None)


def run_server(
    transport: str = "stdio",
    port: int = 8000,
    profile: str | None = None,
    tools: list[str] | None = None,
) -> None:
    """Start the MCP server."""
    if transport not in ("stdio", "sse"):
        raise ValueError(f"Unknown transport: {transport!r}. Use 'stdio' or 'sse'.")

    configure_profile(profile=profile, tools=tools)

    _warm_caches_background()

    mcp_app.settings.port = port
    mcp_app.run(transport=transport)
