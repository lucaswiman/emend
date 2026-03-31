"""emend CLI assembly and backward-compatible exports."""

import sys

from emend.cli_analysis import (
    cfg_cmd,
    dead_code_cmd,
    dsl_debug_cmd,
    facts_cmd,
    graph_cmd,
    impact_cmd,
    refs_cmd,
    trace_cmd,
    types_cmd,
)
from emend.cli_base import (
    QueryShape,
    _reject_file_glob,
    app,
    analyze_app,
    detect_query_shape,
    edit_app,
    parse_where_clause,
    resolve_file_scopes,
    resolve_files,
    resolve_many_files,
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
from emend.cli_map import map_app as _map_app  # noqa: F401
from emend.cli_tooling import editor_search_cmd, editor_server_cmd, index_cmd, mcp_cmd, query_cmd


app.command("search", hidden=True)(search)
app.command("grep", hidden=True)(search)
app.command("show", hidden=True)(search)
app.command("get", hidden=True)(search)
app.command("lookup", hidden=True)(search)
app.command("ls", hidden=True)(search)
app.command("remove", hidden=True)(remove_cmd)
app.command("insert", hidden=True)(add)
app.command("copy", hidden=True)(copy_to_cmd)
app.command("copy-to", hidden=True)(copy_to_cmd)
app.command("move", hidden=True)(move_cmd)
app.command("references", hidden=True)(refs_cmd)
app.command("dsl", hidden=True)(dsl_debug_cmd)
app.command("dead-code", hidden=True)(dead_code_cmd)
app.command("dead_code", hidden=True)(dead_code_cmd)

edit_app.command("set")(edit_set_cmd)
edit_app.command("rm")(remove_cmd)
edit_app.command("delete")(delete_cmd)
edit_app.command("add")(add)
edit_app.command("replace")(replace_cmd)
edit_app.command("cp")(copy_to_cmd)
edit_app.command("rename")(rename_cmd)
edit_app.command("mv")(move_cmd)
edit_app.command("batch")(batch_cmd)
edit_app.command("saturate")(saturate_cmd)
edit_app.command("remove", hidden=True)(remove_cmd)
edit_app.command("copy", hidden=True)(copy_to_cmd)
edit_app.command("copy-to", hidden=True)(copy_to_cmd)
edit_app.command("move", hidden=True)(move_cmd)

analyze_app.command("refs")(refs_cmd)
analyze_app.command("graph")(graph_cmd)
analyze_app.command("deadcode")(dead_code_cmd)
analyze_app.command("impact")(impact_cmd)
analyze_app.command("types")(types_cmd)
analyze_app.command("trace")(trace_cmd)
analyze_app.command("facts")(facts_cmd)
analyze_app.command("cfg")(cfg_cmd)
analyze_app.command("dsl")(dsl_debug_cmd)
analyze_app.command("references", hidden=True)(refs_cmd)
analyze_app.command("dead-code", hidden=True)(dead_code_cmd)
analyze_app.command("dead_code", hidden=True)(dead_code_cmd)

tool_app.command("index")(index_cmd)
tool_app.command("editor-search")(editor_search_cmd)
tool_app.command("editor-server")(editor_server_cmd)
tool_app.command("query")(query_cmd)
tool_app.command("datalog", hidden=True)(query_cmd)
tool_app.command("mcp", hidden=True)(mcp_cmd)


def main():
    try:
        app()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


__all__ = [
    "QueryShape",
    "_reject_file_glob",
    "app",
    "detect_query_shape",
    "main",
    "parse_where_clause",
    "resolve_file_scopes",
    "resolve_files",
    "resolve_many_files",
    "search",
]


if __name__ == "__main__":
    main()
