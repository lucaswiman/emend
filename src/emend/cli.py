"""emend CLI assembly — single registration site for all commands.

External-import audit (grep -rn "from emend.cli import" src/ tests/):
  app              — many tests + entry point                 KEEP in __all__
  main             — pyproject.toml entry_points              KEEP in __all__
  search           — not directly imported from emend.cli     DROPPED from __all__
  resolve_files    — mcp_server, editor_search, tests import
                     from emend.cli, but the symbol lives in
                     cli_base; consumers updated to import
                     from emend.cli_base                      DROPPED from __all__
  resolve_file_scopes — same as resolve_files                 DROPPED from __all__
  resolve_many_files  — zero external importers               DROPPED from __all__
  parse_where_clause  — mcp_server; moved to cli_base import  DROPPED from __all__
  detect_query_shape  — mcp_server; moved to cli_base import  DROPPED from __all__
  QueryShape          — zero external importers               DROPPED from __all__
  _reject_file_glob   — tests import from emend.cli; updated
                        to use emend.cli_base directly        DROPPED from __all__
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable

import typer

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
from emend.cli_base import (
    app,
    analyze_app,
    edit_app,
    tool_app,
)
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

# Wire map_app into the root app (was previously done in cli_map.py)
app.add_typer(map_app, name="map")


@dataclass
class CommandSpec:
    """Descriptor for a single CLI command registration."""

    subapp: typer.Typer
    name: str
    fn: Callable
    hidden: bool = False
    extra_kwargs: dict = field(default_factory=dict)


# Single source of truth for all command registrations.
# Format: CommandSpec(subapp, name, fn, hidden=False, extra_kwargs={})
# Aliases are listed as separate CommandSpec entries with hidden=True.
COMMANDS: list[CommandSpec] = [
    # ---- top-level: find (and aliases) ----
    CommandSpec(app, "find",       search,       hidden=False),
    CommandSpec(app, "search",     search,       hidden=True),
    CommandSpec(app, "grep",       search,       hidden=True),
    CommandSpec(app, "show",       search,       hidden=True),
    CommandSpec(app, "get",        search,       hidden=True),
    CommandSpec(app, "lookup",     search,       hidden=True),
    CommandSpec(app, "ls",         search,       hidden=True),

    # ---- top-level: checks (visible) ----
    CommandSpec(app, "lint",       lint_cmd,     hidden=False),
    CommandSpec(app, "policy",     policy_cmd,   hidden=False),
    CommandSpec(app, "check",      check_cmd,    hidden=False),
    CommandSpec(app, "mcp",        mcp_cmd,      hidden=False),

    # ---- top-level: hidden aliases for edit subcommands ----
    CommandSpec(app, "set",        edit_set_cmd, hidden=True),
    CommandSpec(app, "rm",         remove_cmd,   hidden=True),
    CommandSpec(app, "remove",     remove_cmd,   hidden=True),
    CommandSpec(app, "delete",     delete_cmd,   hidden=True),
    CommandSpec(app, "add",        add,          hidden=True),
    CommandSpec(app, "insert",     add,          hidden=True),
    CommandSpec(app, "replace",    replace_cmd,  hidden=True),
    CommandSpec(app, "cp",         copy_to_cmd,  hidden=True),
    CommandSpec(app, "copy",       copy_to_cmd,  hidden=True),
    CommandSpec(app, "copy-to",    copy_to_cmd,  hidden=True),
    CommandSpec(app, "rename",     rename_cmd,   hidden=True),
    CommandSpec(app, "mv",         move_cmd,     hidden=True),
    CommandSpec(app, "move",       move_cmd,     hidden=True),
    CommandSpec(app, "batch",      batch_cmd,    hidden=True),

    # ---- top-level: hidden aliases for analysis subcommands ----
    CommandSpec(app, "refs",       refs_cmd,     hidden=True),
    CommandSpec(app, "references", refs_cmd,     hidden=True),
    CommandSpec(app, "graph",      graph_cmd,    hidden=True),
    CommandSpec(app, "deadcode",   dead_code_cmd, hidden=True),
    CommandSpec(app, "dead-code",  dead_code_cmd, hidden=True),
    CommandSpec(app, "dead_code",  dead_code_cmd, hidden=True),
    CommandSpec(app, "impact",     impact_cmd,   hidden=True),
    CommandSpec(app, "trace",      trace_cmd,    hidden=True),
    CommandSpec(app, "dsl",        dsl_debug_cmd, hidden=True),
    CommandSpec(app, "index",      index_cmd,    hidden=True),

    # ---- edit subapp ----
    CommandSpec(edit_app, "set",     edit_set_cmd, hidden=False),
    CommandSpec(edit_app, "rm",      remove_cmd,   hidden=False),
    CommandSpec(edit_app, "delete",  delete_cmd,   hidden=False),
    CommandSpec(edit_app, "add",     add,          hidden=False),
    CommandSpec(edit_app, "replace", replace_cmd,  hidden=False),
    CommandSpec(edit_app, "cp",      copy_to_cmd,  hidden=False),
    CommandSpec(edit_app, "rename",  rename_cmd,   hidden=False),
    CommandSpec(edit_app, "mv",      move_cmd,     hidden=False),
    CommandSpec(edit_app, "batch",   batch_cmd,    hidden=False),
    CommandSpec(edit_app, "saturate", saturate_cmd, hidden=False),
    # hidden aliases within edit subapp
    CommandSpec(edit_app, "remove",  remove_cmd,   hidden=True),
    CommandSpec(edit_app, "copy",    copy_to_cmd,  hidden=True),
    CommandSpec(edit_app, "copy-to", copy_to_cmd,  hidden=True),
    CommandSpec(edit_app, "move",    move_cmd,     hidden=True),

    # ---- analyze subapp ----
    CommandSpec(analyze_app, "refs",       refs_cmd,      hidden=False),
    CommandSpec(analyze_app, "graph",      graph_cmd,     hidden=False),
    CommandSpec(analyze_app, "deadcode",   dead_code_cmd, hidden=False),
    CommandSpec(analyze_app, "impact",     impact_cmd,    hidden=False),
    CommandSpec(analyze_app, "types",      types_cmd,     hidden=False),
    CommandSpec(analyze_app, "trace",      trace_cmd,     hidden=False),
    CommandSpec(analyze_app, "facts",      facts_cmd,     hidden=False),
    CommandSpec(analyze_app, "cfg",        cfg_cmd,       hidden=False),
    CommandSpec(analyze_app, "dsl",        dsl_debug_cmd, hidden=False),
    CommandSpec(analyze_app, "dupes",      dupes_cmd,     hidden=False),
    # hidden aliases within analyze subapp
    CommandSpec(analyze_app, "references", refs_cmd,      hidden=True),
    CommandSpec(analyze_app, "dead-code",  dead_code_cmd, hidden=True),
    CommandSpec(analyze_app, "dead_code",  dead_code_cmd, hidden=True),

    # ---- tool subapp ----
    CommandSpec(tool_app, "index",         index_cmd,        hidden=False),
    CommandSpec(tool_app, "editor-search", editor_search_cmd, hidden=False),
    CommandSpec(tool_app, "editor-server", editor_server_cmd, hidden=False),
    CommandSpec(tool_app, "mcp",           mcp_cmd,           hidden=True),

    # ---- map subapp (commands registered directly on map_app) ----
    CommandSpec(map_app, "add",          map_add_cmd,         hidden=False),
    CommandSpec(map_app, "add-module",   map_add_module_cmd,  hidden=False),
    CommandSpec(map_app, "lookup",       map_lookup_cmd,      hidden=False),
    CommandSpec(map_app, "search",       map_search_cmd,      hidden=False),
    CommandSpec(map_app, "resolve",      map_resolve_cmd,     hidden=False),
    CommandSpec(map_app, "rm",           map_rm_cmd,          hidden=False),
    CommandSpec(map_app, "rm-module",    map_rm_module_cmd,   hidden=False),
    CommandSpec(map_app, "list-modules", map_list_modules_cmd, hidden=False),
    CommandSpec(map_app, "update-module", map_update_module_cmd, hidden=False),
]

# Register every command from the table
for _spec in COMMANDS:
    _spec.subapp.command(
        _spec.name,
        hidden=_spec.hidden,
        **_spec.extra_kwargs,
    )(_spec.fn)


def main():
    try:
        app()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


__all__ = [
    "app",
    "main",
]


if __name__ == "__main__":
    main()
