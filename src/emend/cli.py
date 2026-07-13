"""emend CLI assembly and backward-compatible exports."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from emend.cli_analysis import (
    cfg_cmd,
    dead_code_cmd,
    dsl_debug_cmd,
    dupes_cmd,
    facts_cmd,
    graph_cmd,
    impact_cmd,
    refs_cmd,
    trace_cmd,
    types_cmd,
)
from emend.cli_base import analyze_app, app, edit_app, tool_app
from emend.cli_checks import check_cmd, lint_cmd, policy_cmd
from emend.cli_edit import (
    add,
    batch_cmd,
    copy_to_cmd,
    delete_cmd,
    edit_set_cmd,
    move_cmd,
    remove_cmd,
    rename_cmd,
    replace_cmd,
    saturate_cmd,
)
from emend.cli_find import search
from emend.cli_map import (
    map_app,
    map_add_cmd,
    map_add_module_cmd,
    map_list_modules_cmd,
    map_lookup_cmd,
    map_resolve_cmd,
    map_rm_cmd,
    map_rm_module_cmd,
    map_search_cmd,
    map_update_module_cmd,
)
from emend.cli_tooling import editor_search_cmd, editor_server_cmd, index_cmd, mcp_cmd
from emend.errors import BUG_EXCEPTIONS


@dataclass
class _CmdEntry:
    """Single registration entry for one CLI command."""

    subapp: object
    name: str
    fn: object
    hidden: bool = False
    aliases: list[str] = field(default_factory=list)


_COMMANDS: list[_CmdEntry] = [
    # ---- top-level public commands ----
    _CmdEntry(app, "lint",   lint_cmd,     hidden=False),
    _CmdEntry(app, "policy", policy_cmd,   hidden=False),
    _CmdEntry(app, "check",  check_cmd,    hidden=False),
    _CmdEntry(app, "find",   search,       hidden=False, aliases=["search", "grep", "show", "get", "lookup", "ls"]),
    _CmdEntry(app, "mcp",    mcp_cmd,      hidden=False),

    # ---- top-level hidden aliases for edit commands ----
    _CmdEntry(app, "set",        edit_set_cmd, hidden=True),
    _CmdEntry(app, "rm",         remove_cmd,   hidden=True, aliases=["remove"]),
    _CmdEntry(app, "delete",     delete_cmd,   hidden=True),
    _CmdEntry(app, "add",        add,           hidden=True, aliases=["insert"]),
    _CmdEntry(app, "replace",    replace_cmd,  hidden=True),
    _CmdEntry(app, "cp",         copy_to_cmd,  hidden=True, aliases=["copy", "copy-to"]),
    _CmdEntry(app, "rename",     rename_cmd,   hidden=True),
    _CmdEntry(app, "mv",         move_cmd,     hidden=True, aliases=["move"]),
    _CmdEntry(app, "batch",      batch_cmd,    hidden=True),
    _CmdEntry(app, "saturate",   saturate_cmd, hidden=True),

    # ---- top-level hidden aliases for analysis commands ----
    _CmdEntry(app, "refs",       refs_cmd,     hidden=True, aliases=["references"]),
    _CmdEntry(app, "graph",      graph_cmd,    hidden=True),
    _CmdEntry(app, "deadcode",   dead_code_cmd, hidden=True, aliases=["dead-code", "dead_code"]),
    _CmdEntry(app, "impact",     impact_cmd,   hidden=True),
    _CmdEntry(app, "types",      types_cmd,    hidden=True),
    _CmdEntry(app, "trace",      trace_cmd,    hidden=True),
    _CmdEntry(app, "facts",      facts_cmd,    hidden=True),
    _CmdEntry(app, "cfg",        cfg_cmd,      hidden=True),
    _CmdEntry(app, "dsl",        dsl_debug_cmd, hidden=True, aliases=["dsl-debug"]),

    # ---- top-level hidden aliases for tooling commands ----
    _CmdEntry(app, "index",         index_cmd,         hidden=True),
    _CmdEntry(app, "editor-search", editor_search_cmd, hidden=True),
    _CmdEntry(app, "editor-server", editor_server_cmd, hidden=True),

    # ---- edit subapp ----
    _CmdEntry(edit_app, "set",      edit_set_cmd, hidden=False),
    _CmdEntry(edit_app, "rm",       remove_cmd,   hidden=False),
    _CmdEntry(edit_app, "delete",   delete_cmd,   hidden=False),
    _CmdEntry(edit_app, "add",      add,           hidden=False),
    _CmdEntry(edit_app, "replace",  replace_cmd,  hidden=False),
    _CmdEntry(edit_app, "cp",       copy_to_cmd,  hidden=False),
    _CmdEntry(edit_app, "rename",   rename_cmd,   hidden=False),
    _CmdEntry(edit_app, "mv",       move_cmd,     hidden=False),
    _CmdEntry(edit_app, "batch",    batch_cmd,    hidden=False),
    _CmdEntry(edit_app, "saturate", saturate_cmd, hidden=False),
    _CmdEntry(edit_app, "remove",   remove_cmd,   hidden=True),
    _CmdEntry(edit_app, "copy",     copy_to_cmd,  hidden=True),
    _CmdEntry(edit_app, "copy-to",  copy_to_cmd,  hidden=True),
    _CmdEntry(edit_app, "move",     move_cmd,     hidden=True),

    # ---- analyze subapp (dupes first) ----
    _CmdEntry(analyze_app, "dupes",      dupes_cmd,     hidden=False),
    _CmdEntry(analyze_app, "refs",       refs_cmd,      hidden=False),
    _CmdEntry(analyze_app, "graph",      graph_cmd,     hidden=False),
    _CmdEntry(analyze_app, "deadcode",   dead_code_cmd, hidden=False),
    _CmdEntry(analyze_app, "impact",     impact_cmd,    hidden=False),
    _CmdEntry(analyze_app, "types",      types_cmd,     hidden=False),
    _CmdEntry(analyze_app, "trace",      trace_cmd,     hidden=False),
    _CmdEntry(analyze_app, "facts",      facts_cmd,     hidden=False),
    _CmdEntry(analyze_app, "cfg",        cfg_cmd,       hidden=False),
    _CmdEntry(analyze_app, "dsl",        dsl_debug_cmd, hidden=False, aliases=["dsl-debug"]),
    _CmdEntry(analyze_app, "references", refs_cmd,      hidden=True),
    _CmdEntry(analyze_app, "dead-code",  dead_code_cmd, hidden=True),
    _CmdEntry(analyze_app, "dead_code",  dead_code_cmd, hidden=True),

    # ---- tool subapp ----
    _CmdEntry(tool_app, "index",         index_cmd,         hidden=False),
    _CmdEntry(tool_app, "editor-search", editor_search_cmd, hidden=False),
    _CmdEntry(tool_app, "editor-server", editor_server_cmd, hidden=False),
    _CmdEntry(tool_app, "mcp",           mcp_cmd,           hidden=True),

    # ---- map subapp ----
    _CmdEntry(map_app, "add",          map_add_cmd,         hidden=False),
    _CmdEntry(map_app, "search",       map_search_cmd,      hidden=False),
    _CmdEntry(map_app, "lookup",       map_lookup_cmd,      hidden=False),
    _CmdEntry(map_app, "rm",           map_rm_cmd,          hidden=False),
    _CmdEntry(map_app, "add-module",   map_add_module_cmd,  hidden=False),
    _CmdEntry(map_app, "list-modules", map_list_modules_cmd, hidden=False),
    _CmdEntry(map_app, "update-module", map_update_module_cmd, hidden=False),
    _CmdEntry(map_app, "rm-module",    map_rm_module_cmd,   hidden=False),
    _CmdEntry(map_app, "resolve",      map_resolve_cmd,     hidden=False),
]

app.add_typer(map_app, name="map")

for _entry in _COMMANDS:
    _entry.subapp.command(_entry.name, hidden=_entry.hidden)(_entry.fn)
    for _alias in _entry.aliases:
        _entry.subapp.command(_alias, hidden=True)(_entry.fn)


def main():
    try:
        app()
    except BUG_EXCEPTIONS:
        raise
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


__all__ = [
    "app",
    "main",
]


if __name__ == "__main__":
    main()
