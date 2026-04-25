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

import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr
from typing import Annotated, Any

from pydantic import Field

from mcp.server.fastmcp import FastMCP

from emend.component_selector import parse_extended_selector
from emend.rules_config import LEGACY_PATTERNS_PATH, LEGACY_POLICIES_PATH, resolve_rules_path
from emend.transform import (
    find_pattern,
    replace_pattern,
    find_references,
    rename_symbol,
    move_symbol,
    move_module,
    rename_module,
    cmd_lookup,
    cmd_edit,
    cmd_add,
    find_callers,
    find_callees,
    generate_graph,
    find_dead_code,
    find_impact,
    semantic_context as _semantic_context,
)
from emend import ast_commands

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


def _capture_output(func: Any, *args: Any, **kwargs: Any) -> str:
    """Call *func* and return whatever it printed to stdout."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@mcp_app.tool()
def search(
    mode: Annotated[str, Field(description="Search mode: code, symbol, summary, or auto (legacy inference).")] = "code",
    query: Annotated[str, Field(description=(
        "Search query payload. "
        "Code mode: pattern or literal code snippet. "
        "Symbol mode: selector (file.py::sym) or bare symbol name. "
        "Summary mode: optional selector filter when files is set."
    ))] = "",
    files: Annotated[list[str] | None, Field(description="File scope(s): file paths, globs, or directories.")] = None,
    within: Annotated[str | None, Field(description="Structural containment pattern for code mode.")] = None,
    not_within: Annotated[str | None, Field(description="Inverse containment pattern for code mode.")] = None,
    kind: Annotated[str | None, Field(description="Symbol kind filter: function, method, class, async_function, async_method.")] = None,
    name: Annotated[str | None, Field(description="Name pattern filter (glob like 'test_*' or /regex/).")] = None,
    returns: Annotated[str | None, Field(description="Return type filter.")] = None,
    depth: Annotated[str | None, Field(description="Nesting depth filter (lookup) or display depth (summary).")] = None,
    has_param: Annotated[str | None, Field(description="Parameter filter.")] = None,
    output: Annotated[str, Field(description="Output format: code, location, selector, summary, metadata, json, count, code::dedent, summary::flat.")] = "code",
    where: Annotated[str | None, Field(description="Legacy compatibility field. Prefer within/not_within and explicit mode.")] = None,
    imported_from: Annotated[str | None, Field(description="Only match when root name is imported from this module.")] = None,
    scope_local: Annotated[bool, Field(description="Only match locally-defined names, exclude imports.")] = False,
    case_insensitive: Annotated[bool, Field(description="Case-insensitive matching.")] = False,
    smart_case: Annotated[bool, Field(description="Match naming convention variants (snake_case/camelCase/etc).")] = False,
) -> str:
    """Search for code or symbols with explicit modes."""
    import re as _re
    from emend.cli import resolve_files, resolve_file_scopes, parse_where_clause, detect_query_shape

    mode = (mode or "code").lower()
    if mode not in {"code", "symbol", "summary", "auto"}:
        return json.dumps({"error": f"Unknown mode {mode!r}. Use: code, symbol, summary, auto."})

    # Keep --where behavior as a compatibility shim while canonical MCP usage
    # moves to explicit mode + dedicated fields.
    where_params = parse_where_clause([where] if where else [])
    where_scope = where_params.get("scope")
    where_inside = within or where_params.get("inside")
    where_not_inside = not_within or where_params.get("not_inside")
    where_matching = where_params.get("matching")

    output_base = output
    output_modifier = None
    if output and "::" in output:
        parts = output.split("::", 1)
        output_base = parts[0]
        output_modifier = parts[1]

    # auto mode preserves legacy inference for compatibility.
    is_pattern_mode = mode == "code"
    has_selector = False
    is_line_selector = bool(_re.search(r":\d+(-\d+)?$", query))
    if mode == "auto":
        _shape = detect_query_shape(query, files[0] if files else None)
        query = _shape.query
        files = [str(_shape.path)] if _shape.path else files
        is_pattern_mode = _shape.is_pattern_mode
        has_selector = _shape.has_selector
        is_line_selector = _shape.is_line_selector
    elif mode == "summary":
        is_pattern_mode = False
    elif mode == "symbol":
        if "::" in query and not is_line_selector:
            has_selector = True

    lookup_has_decorator: list[str] | None = None
    lookup_in_class: list[str] | None = None
    lookup_matching: str | None = None
    if where_matching is not None:
        if where_matching.startswith("@"):
            lookup_has_decorator = [where_matching[1:]]
        else:
            lookup_matching = where_matching
    if where_inside is not None and where_inside.startswith("class "):
        lookup_in_class = [where_inside[6:].strip()]

    has_component = False
    if has_selector:
        try:
            _parsed_sel = parse_extended_selector(query)
            has_component = _parsed_sel.component is not None
        except Exception:
            pass

    json_output = output_base == "json"
    count_output = output_base == "count"
    dedent_output = output_modifier == "dedent"
    flat_output = output_modifier == "flat"

    if output_base not in (None, "json", "count"):
        effective_output = output_base
    elif json_output or count_output:
        effective_output = "code"
    elif is_pattern_mode:
        effective_output = "location"
    elif has_component:
        effective_output = "component"
    elif has_selector or is_line_selector:
        effective_output = "code"
    elif not bool(kind or name or lookup_has_decorator or returns or lookup_in_class or depth or has_param or lookup_matching):
        effective_output = "summary"
    else:
        effective_output = "selector"

    # --- Summary mode ---
    if mode == "summary" or (effective_output == "summary" and not is_pattern_mode):
        tree_depth = int(depth) if depth else None
        file_for_summary = files[0] if files else query
        selector_for_summary = None
        if "::" in query and not files:
            parts = query.split("::", 1)
            file_for_summary = parts[0]
            selector_for_summary = parts[1] or None

        from pathlib import Path

        file_path_obj = Path(file_for_summary)
        lines: list[str] = []
        if file_path_obj.is_dir() or "*" in file_for_summary or "?" in file_for_summary:
            files, _ = resolve_files(file_for_summary)
            from emend import emend_core

            file_strs = [str(fp) for fp in files]
            batch_results = emend_core.collect_symbols_batch(
                file_strs, max_depth=tree_depth, selector=selector_for_summary
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                for file_path_str, symbol_dicts in batch_results:
                    symbols = ast_commands.dicts_to_tree_symbols(symbol_dicts)
                    print(f"\nModule: {file_path_str}")
                    if symbols:
                        if flat_output:
                            ast_commands._print_symbol_flat(symbols)
                        else:
                            ast_commands._print_symbol_tree(symbols, indent=1)
            return buf.getvalue()
        else:
            symbols = ast_commands.collect_symbols(
                file_for_summary, tree_depth=tree_depth, selector=selector_for_summary
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                print(f"\nModule: {file_for_summary}")
                if symbols:
                    if flat_output:
                        ast_commands._print_symbol_flat(symbols)
                    else:
                        ast_commands._print_symbol_tree(symbols, indent=1)
            return buf.getvalue()

    # --- Code pattern mode ---
    if is_pattern_mode or mode == "code":
        if mode == "code" and "::" in query and not files:
            _shape = detect_query_shape(query, None)
            if _shape.is_pattern_mode:
                query = _shape.query
                files = [str(_shape.path)] if _shape.path else None
        target_paths = files or ["."]

        resolved_files, is_multi_file = resolve_file_scopes(target_paths)
        all_matches: list[tuple[str, Any]] = []
        for file_path in resolved_files:
            file_path_str = str(file_path)
            try:
                file_matches = find_pattern(
                    query,
                    file_path_str,
                    scope=where_scope,
                    inside=where_inside,
                    not_inside=where_not_inside,
                    imported_from=imported_from,
                    scope_local=scope_local,
                )
                for match in file_matches:
                    all_matches.append((file_path_str, match))
            except (FileNotFoundError, Exception):
                if not is_multi_file:
                    raise
                continue

        if count_output:
            return str(len(all_matches))
        elif json_output:
            serialized = []
            for file_path_str, match in all_matches:
                code_str = (match.matched_text or match.node_text or "").strip()
                captures = {}
                for cap_name, captured in match.captures.items():
                    captures[cap_name] = captured.strip() if isinstance(captured, str) else str(captured)
                serialized.append(
                    {"file": file_path_str, "line": match.line, "code": code_str, "captures": captures}
                )
            return json.dumps({"count": len(all_matches), "matches": serialized}, indent=2)
        else:
            lines = []
            for file_path_str, match in all_matches:
                if match.line is not None:
                    lines.append(f"{file_path_str}:{match.line}")
                else:
                    lines.append(f"{file_path_str}:?")
            return "\n".join(lines)

    # --- Symbol lookup mode ---
    if files and len(files) > 1:
        return json.dumps({"error": "symbol mode accepts at most one file scope"})

    file_or_pattern = files[0] if files else (query or "**")
    selector_str = None
    if has_selector or is_line_selector or ("::" in query and not is_line_selector):
        selector_str = query
        if "::" in query and not is_line_selector:
            parts = query.split("::", 1)
            file_or_pattern = parts[0]
        elif is_line_selector:
            m = _re.search(r"^(.+?):\d+", query)
            if m:
                file_or_pattern = m.group(1)
    elif query:
        # In explicit symbol mode, a bare query acts as a name filter.
        if name is None:
            name = query

    return cmd_lookup(
        file_or_pattern=file_or_pattern,
        selector_str=selector_str,
        kind=[kind] if kind else None,
        name=[name] if name else None,
        has_decorator=lookup_has_decorator,
        returns=[returns] if returns else None,
        in_class=lookup_in_class,
        depth=[depth] if depth else None,
        has_param=[has_param] if has_param else None,
        case_insensitive=case_insensitive,
        smart_case=smart_case,
        json_output=json_output,
        metadata=(effective_output == "metadata"),
        paths_only=(effective_output == "selector" and not json_output and not count_output),
        count=count_output,
        dedent=dedent_output,
        matching=lookup_matching,
    )


# ---------------------------------------------------------------------------
# replace (internal helper used by transform dispatcher)
# ---------------------------------------------------------------------------


def replace(
    pattern: Annotated[str, Field(description="Pattern to find (e.g. 'print($X)').")],
    replacement: Annotated[str, Field(description="Replacement pattern using same $X captures (e.g. 'logger.info($X)').")],
    path: Annotated[str, Field(description="Python file, glob, or directory to modify.")],
    apply: Annotated[bool, Field(description="Write changes to disk. Default is dry-run.")] = False,
    where: Annotated[str | None, Field(description="Scope constraint: 'def test_*', 'not class', 'MyClass.method'.")] = None,
) -> str:
    """Replace pattern matches in Python file(s).

    Supports $X metavariables in both pattern and replacement.
    By default shows a diff (dry-run). Set apply=True to write changes.
    """
    from emend.cli import resolve_files, parse_where_clause

    where_params = parse_where_clause([where] if where else [])
    scope = where_params.get("scope")
    inside = where_params.get("inside")
    not_inside = where_params.get("not_inside")

    files, is_multi_file = resolve_files(path)
    all_diffs: list[str] = []
    total_count = 0
    for file_path in files:
        file_path_str = str(file_path)
        try:
            diff, cnt = replace_pattern(
                pattern,
                replacement,
                file_path_str,
                scope=scope,
                apply=apply,
                inside=inside,
                not_inside=not_inside,
            )
            if diff:
                all_diffs.append(diff)
            total_count += cnt
        except FileNotFoundError:
            if not is_multi_file:
                raise
            continue

    result = "".join(all_diffs)
    if total_count > 0:
        result += f"\n{total_count} replacement(s) {'applied' if apply else 'found (dry-run)'}."
    else:
        result = "No matches found."
    return result


# ---------------------------------------------------------------------------
# modify (internal helper used by transform dispatcher)
# ---------------------------------------------------------------------------


def modify(
    selector: Annotated[str, Field(description="Symbol selector with component (e.g. 'file.py::func[returns]', 'file.py::func[params]', 'file.py::Class[bases]').")],
    value: Annotated[str | None, Field(description="New value. Required for 'set' and 'add' modes. Omit for 'remove'.")] = None,
    mode: Annotated[str, Field(description="Operation: 'set' replaces a component value, 'add' inserts into a list component (params/bases/decorators), 'remove' deletes the component or symbol.")] = "set",
    before: Annotated[str | None, Field(description="Insert before this named item (add mode only).")] = None,
    after: Annotated[str | None, Field(description="Insert after this named item (add mode only).")] = None,
    at: Annotated[int | None, Field(description="Insert at position, 0-indexed (add mode only).")] = None,
    apply: Annotated[bool, Field(description="Write changes to disk. Default is dry-run.")] = False,
) -> str:
    """Modify a symbol component: set its value, add to a list, or remove it.

    Modes:
    - set: Replace a component's value (e.g. change return type, replace params)
    - add: Insert into a list component (params, bases, decorators). Use before/after/at for positioning.
    - remove: Delete a component or entire symbol
    """
    if mode == "set":
        return cmd_edit(selector_str=selector, value=value, rm=False, apply=apply)
    elif mode == "add":
        if value is None:
            return "Error: value is required for 'add' mode."
        return cmd_add(
            selector_str=selector,
            value=value,
            before=before,
            after=after,
            at=at,
            apply=apply,
        )
    elif mode == "remove":
        return cmd_edit(selector_str=selector, rm=True, apply=apply)
    else:
        return f"Error: unknown mode '{mode}'. Use 'set', 'add', or 'remove'."


# ---------------------------------------------------------------------------
# refs (internal helper used by references dispatcher)
# ---------------------------------------------------------------------------


def refs(
    selector: Annotated[str, Field(description="Symbol selector (e.g. 'file.py::func_name').")],
    exclude_definition: Annotated[bool, Field(description="Exclude the definition itself from results.")] = False,
    exclude_imports: Annotated[bool, Field(description="Exclude import statements from results.")] = False,
    writes_only: Annotated[bool, Field(description="Only show write (assignment) references.")] = False,
    reads_only: Annotated[bool, Field(description="Only show read (load) references.")] = False,
    calls_only: Annotated[bool, Field(description="Only show actual call sites.")] = False,
    project: Annotated[str | None, Field(description="Project root directory.")] = None,
) -> str:
    """Find all references to a symbol across the project. Returns JSON."""
    parsed = parse_extended_selector(selector)

    if calls_only:
        callers = find_callers(parsed, project_path=project)
        data = [{"file_path": r.file_path, "line": r.line, "column": r.column} for r in callers]
        return json.dumps(data, indent=2)

    references = find_references(
        parsed,
        project_path=project,
        include_definition=not exclude_definition,
        include_imports=not exclude_imports,
        writes_only=writes_only,
        reads_only=reads_only,
    )
    data = [
        {
            "file_path": r.file_path,
            "line": r.line,
            "column": r.column,
            "is_definition": r.is_definition,
            "is_import": r.is_import,
            "is_write": r.is_write,
        }
        for r in references
    ]
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# rename (internal helper used by transform dispatcher)
# ---------------------------------------------------------------------------


def rename(
    selector: Annotated[str, Field(description="Symbol selector (file.py::name) or module path (file.py). Uses :: for symbols, bare path for modules.")],
    to: Annotated[str, Field(description="New name for the symbol or module.")],
    apply: Annotated[bool, Field(description="Write changes to disk. Default is dry-run.")] = False,
    docs: Annotated[bool, Field(description="Also rename in docstrings (symbol mode only).")] = False,
    no_hierarchy: Annotated[bool, Field(description="Don't rename in class hierarchy (symbol mode only).")] = False,
    unsure: Annotated[bool, Field(description="Rename uncertain occurrences (symbol mode only).")] = False,
    project: Annotated[str | None, Field(description="Project root directory.")] = None,
) -> str:
    """Rename a symbol or module across the project, updating all references."""
    if "::" in selector:
        parsed = parse_extended_selector(selector)
        diffs = rename_symbol(
            parsed,
            to,
            project,
            in_hierarchy=not no_hierarchy,
            docs=docs,
            unsure=unsure,
            apply=apply,
        )
        if not diffs:
            return "No changes needed."
        parts = [d for d in diffs.values() if d]
        result = "".join(parts)
        if not apply:
            result += "\nDry-run. Set apply=True to write changes."
        return result
    else:
        diffs = rename_module(selector, to, project, apply)
        if apply:
            return "Module renamed successfully."
        if "__description__" in diffs:
            return diffs["__description__"] + "\nDry-run. Set apply=True to write changes."
        parts = [d for d in diffs.values() if d]
        return "".join(parts) + "\nDry-run. Set apply=True to write changes."


# ---------------------------------------------------------------------------
# move (internal helper used by transform dispatcher)
# ---------------------------------------------------------------------------


def move(
    selector: Annotated[str, Field(description="Symbol selector (file.py::name) or module path (file.py). Uses :: for symbols, bare path for modules.")],
    destination: Annotated[str, Field(description="Destination file or package.")],
    copy_only: Annotated[bool, Field(description="Copy without removing from source (symbol mode only). The body is copied exactly from the AST.")] = False,
    dedent: Annotated[bool, Field(description="Dedent nested symbols (symbol mode only).")] = False,
    no_update_imports: Annotated[bool, Field(description="Don't update imports across the project (symbol mode only).")] = False,
    apply: Annotated[bool, Field(description="Write changes to disk. Default is dry-run.")] = False,
    project: Annotated[str | None, Field(description="Project root directory.")] = None,
) -> str:
    """Move (or copy) a symbol or module to another file, updating all imports.

    Set copy_only=True to copy a symbol without removing it from the source file.
    """
    if copy_only:
        buf = io.StringIO()
        with redirect_stdout(buf):
            ast_commands.cmd_copy_to(selector, destination, append=True, dedent=dedent, apply=apply, project_path=project)
        return buf.getvalue()
    if "::" in selector:
        parsed = parse_extended_selector(selector)
        diffs = move_symbol(
            parsed,
            destination,
            dedent=dedent,
            update_imports=not no_update_imports,
            project_path=project,
            apply=apply,
        )
        if not diffs:
            return "No changes needed."
        parts = [d for d in diffs.values() if d]
        result = "".join(parts)
        if not apply:
            result += "\nDry-run. Set apply=True to write changes."
        return result
    else:
        diffs = move_module(selector, destination, project, apply)
        if apply:
            return "Module moved successfully."
        if "__description__" in diffs:
            return diffs["__description__"] + "\nDry-run. Set apply=True to write changes."
        parts = [d for d in diffs.values() if d]
        return "".join(parts) + "\nDry-run. Set apply=True to write changes."


# ---------------------------------------------------------------------------
# graph (internal helper used by analyze dispatcher)
# ---------------------------------------------------------------------------


def _graph_symbol(symbol: str, direction: str, transitive: bool, depth: int | None, format: str, project: str | None) -> str:
    """Symbol-level call graph query."""
    from emend.component_selector import parse_extended_selector
    from emend.transform import _find_project_root, _file_to_module, _normalize_module_qn, _get_or_build_fact_graph

    sel = parse_extended_selector(symbol)
    sym_name = sel.symbol_path[-1] if sel.symbol_path else None
    if not sym_name:
        return json.dumps({"error": "Could not parse symbol from selector."})

    # Build qualified name for Datalog queries
    module_root = _find_project_root(sel.file_path) if sel.file_path else (project or ".")
    scan_root = project or module_root
    fg = _get_or_build_fact_graph(scan_root)

    if sel.file_path:
        target_module = _normalize_module_qn(_file_to_module(sel.file_path, module_root))
        qn = ".".join([target_module] + sel.symbol_path) if target_module else ".".join(sel.symbol_path)
    else:
        qn = ".".join(sel.symbol_path)

    edges: list[tuple[str, str]] = []

    if direction in ("callers", "both"):
        caller_facts = fg.callers_datalog(qn)
        if not caller_facts:
            caller_facts = fg.callers_datalog(sym_name)
        for c in caller_facts:
            edges.append((c.caller_qn, qn))
        if transitive:
            visited = {qn}
            frontier = [c.caller_qn for c in caller_facts]
            current_depth = 1
            while frontier and (depth is None or current_depth < depth):
                next_level: list[str] = []
                for caller_qn in frontier:
                    if caller_qn in visited:
                        continue
                    visited.add(caller_qn)
                    upstream = fg.callers_datalog(caller_qn)
                    for u in upstream:
                        edges.append((u.caller_qn, caller_qn))
                        if u.caller_qn not in visited:
                            next_level.append(u.caller_qn)
                current_depth += 1
                frontier = next_level

    if direction in ("callees", "both"):
        callee_facts = fg.callees_datalog(qn)
        if not callee_facts:
            callee_facts = fg.callees_datalog(sym_name)
        for c in callee_facts:
            edges.append((qn, c.callee_qn))
        if transitive:
            visited_callees = {qn}
            frontier_callees = [c.callee_qn for c in callee_facts]
            current_depth = 1
            while frontier_callees and (depth is None or current_depth < depth):
                next_level_c: list[str] = []
                for callee_qn in frontier_callees:
                    if callee_qn in visited_callees:
                        continue
                    visited_callees.add(callee_qn)
                    downstream = fg.callees_datalog(callee_qn)
                    for d in downstream:
                        edges.append((callee_qn, d.callee_qn))
                        if d.callee_qn not in visited_callees:
                            next_level_c.append(d.callee_qn)
                current_depth += 1
                frontier_callees = next_level_c

    # Deduplicate edges
    edges = list(dict.fromkeys(edges))

    if format == "json":
        return json.dumps({"symbol": sym_name, "direction": direction, "transitive": transitive, "edges": [{"caller": c, "callee": e} for c, e in edges]}, indent=2)
    elif format == "dot":
        lines = ["digraph callgraph {"]
        for caller, callee in edges:
            lines.append(f'  "{caller}" -> "{callee}";')
        lines.append("}")
        return "\n".join(lines)
    else:
        return "\n".join(f"{c} -> {e}" for c, e in edges) or "(no edges found)"


def graph(
    file_path: Annotated[str | None, Field(description="Source file to analyze. Produces a full call graph for all functions in the file.")] = None,
    symbol: Annotated[str | None, Field(description="Symbol selector (e.g. 'file.py::func' or 'Class.method'). When given, direction/transitive/depth apply.")] = None,
    direction: Annotated[str, Field(description="'callers', 'callees', or 'both'. Only used with symbol.")] = "both",
    transitive: Annotated[bool, Field(description="Follow call chains recursively. Only used with symbol.")] = False,
    depth: Annotated[int | None, Field(description="Max traversal depth when transitive=true. Default: unlimited.")] = None,
    format: Annotated[str, Field(description="Output format: plain, json, or dot (Graphviz).")] = "json",
    project: Annotated[str | None, Field(description="Project root directory.")] = None,
) -> str:
    """Generate a call graph.

    Two modes:
    - file_path: full call graph for all functions in a file.
    - symbol: callers/callees for a specific symbol (with direction, transitive, depth).
    """
    if symbol:
        return _graph_symbol(symbol, direction, transitive, depth, format, project)
    if file_path:
        return generate_graph(file_path, project_path=project, format=format)
    return json.dumps({"error": "Provide file_path or symbol."})


# ---------------------------------------------------------------------------
# deadcode (internal helper used by analyze dispatcher)
# ---------------------------------------------------------------------------


def deadcode(
    path: Annotated[str, Field(description="File glob or directory to scan (e.g. 'src/**/*.py').")] = ".",
    kind: Annotated[str | None, Field(description="Symbol kind filter: function, class, method, variable.")] = None,
    include_private: Annotated[bool, Field(description="Include _private symbols.")] = False,
    unused_modules: Annotated[bool, Field(description="Also report Python modules with no incoming imports.")] = False,
    no_last_reference: Annotated[bool, Field(description="Don't show git last-reference info.")] = False,
    entry_point_decorators: Annotated[list[str] | None, Field(description="Decorators that mark entry points (not dead even if unreferenced). E.g. ['app.route', 'celery.task'].")] = None,
    entry_point_names: Annotated[list[str] | None, Field(description="Function names that are entry points. E.g. ['main', 'cli'].")] = None,
    exclude_paths: Annotated[list[str] | None, Field(description="Glob patterns for paths to exclude. E.g. ['tests/**', 'migrations/**'].")] = None,
    config: Annotated[str | None, Field(description="Path to rules.yaml or legacy patterns.yaml config. Direct params above override config values.")] = None,
) -> str:
    """Find potentially dead (unreferenced) code. Returns JSON.

    Skips dunder methods, test functions, decorated entry points,
    __all__ members, and conventional entry points.

    Entry points and exclusions can be set via parameters directly
    or via .emend/rules.yaml (falling back to legacy patterns.yaml). Direct parameters
    override config file values.
    """
    from pathlib import Path as _Path
    from emend.lint import load_rules

    # Load deadcode settings from config file if present
    cfg_exclude_refs_from = None
    cfg_strings_as_refs = True
    cfg_ep_decorators = None
    cfg_ep_names = None
    cfg_excl_paths = None

    config_path = resolve_rules_path(config, fallbacks=(LEGACY_PATTERNS_PATH,))
    if config_path.exists():
        _, _, deadcode_config = load_rules(str(config_path))
        if deadcode_config is not None:
            cfg_exclude_refs_from = deadcode_config.exclude_references_from
            cfg_strings_as_refs = deadcode_config.strings_count_as_references
            cfg_ep_decorators = deadcode_config.entry_point_decorators
            cfg_ep_names = deadcode_config.entry_point_names
            cfg_excl_paths = deadcode_config.exclude_paths

    # Direct params override config file values
    results = find_dead_code(
        project_path=path,
        kind=kind,
        include_private=include_private,
        exclude_references_from=cfg_exclude_refs_from,
        strings_count_as_references=cfg_strings_as_refs,
        show_last_reference=not no_last_reference,
        all_files=False,
        entry_point_decorators=entry_point_decorators or cfg_ep_decorators,
        entry_point_names=entry_point_names or cfg_ep_names,
        exclude_paths=exclude_paths or cfg_excl_paths,
        unused_modules=unused_modules,
    )
    data = []
    for d in results:
        if hasattr(d, "module_name"):
            entry = {
                "file_path": d.file_path,
                "name": d.name,
                "module_name": d.module_name,
                "kind": "module",
                "reason": d.reason,
            }
        elif hasattr(d, "func_qn") and hasattr(d, "block_id"):
            entry = {
                "file_path": d.file_path,
                "func_qn": d.func_qn,
                "kind": "unreachable_block",
                "start_line": d.start_line,
                "end_line": d.end_line,
                "reason": "unreachable code",
            }
        else:
            entry = {
                "file_path": d.file_path,
                "name": d.name,
                "kind": d.kind,
                "line": d.line,
                "selector": d.selector,
                "reason": d.reason,
            }
            if d.last_reference_commit:
                entry["last_reference_commit"] = d.last_reference_commit
        data.append(entry)
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# lint (deprecated; see check dispatcher)
# ---------------------------------------------------------------------------


def lint(
    path: Annotated[str, Field(description="File or directory to lint.")],
    config: Annotated[str | None, Field(description="Path to rules.yaml or legacy patterns.yaml config file.")] = None,
    fix: Annotated[bool, Field(description="Auto-apply fix replacements.")] = False,
    rule: Annotated[str | None, Field(description="Run only a specific rule by name.")] = None,
) -> str:
    """Lint Python files using rules from .emend/rules.yaml or legacy patterns.yaml."""
    from emend.checks.engine import run_checks
    from emend.cli import resolve_files

    config_path = resolve_rules_path(config, fallbacks=(LEGACY_PATTERNS_PATH,))
    if not config_path.exists():
        return f"Error: Config file not found: {config_path}"

    resolved, _ = resolve_files(path)
    files = [str(f) for f in resolved]

    violations = run_checks(
        files, config=str(config_path), rule_name=rule, fix=fix,
        project_path=path, allowed_kinds={"match", "flow", "deadcode"},
    )

    if not violations:
        return "No violations found."

    lines = [
        f"{v.file_path}:{v.line}:{v.col}: [{v.rule_name}] {v.message}"
        for v in violations
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# impact (internal helper used by analyze dispatcher)
# ---------------------------------------------------------------------------


def impact(
    selector: Annotated[str | None, Field(description="Symbol selector (file.py::Symbol). Provide this or diff.")] = None,
    diff: Annotated[str | None, Field(description="Git diff spec (e.g. HEAD, abc..def).")] = None,
    output: Annotated[str, Field(description="Output mode: symbols, tests, graph.")] = "symbols",
    max_depth: Annotated[int, Field(description="Maximum BFS depth for transitive closure.")] = 10,
    project: Annotated[str | None, Field(description="Project root directory.")] = None,
) -> str:
    """Compute transitive set of impacted symbols from a change.

    Given a changed symbol or git diff, computes impacted symbols,
    files, and tests via reverse-caller closure.
    """
    if not selector and not diff:
        return json.dumps({"error": "Provide a selector or diff parameter."})

    selectors_list = None
    if selector:
        parsed = parse_extended_selector(selector)
        selectors_list = [parsed]

    result = find_impact(
        selectors=selectors_list,
        diff_spec=diff,
        project_path=project,
        max_depth=max_depth,
    )

    data = {
        "changed_symbols": result.changed_symbols,
        "impacted_symbols": result.impacted_symbols,
        "impacted_tests": result.impacted_tests,
        "edges": [
            {"source": e.source, "target": e.target, "kind": e.kind}
            for e in result.edges
        ],
    }
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# semantic_context (internal helper used by analyze dispatcher)
# ---------------------------------------------------------------------------


def semantic_context(
    selector: Annotated[str, Field(description=(
        "Symbol selector (e.g. 'file.py::func_name', 'file.py::Class.method'). "
        "Call this when you're about to change a function/class and want to know "
        "what could go wrong — hidden API contracts, async side effects, dynamic "
        "string references, missing tests, caching issues."
    ))],
    project: Annotated[str | None, Field(description="Project root directory.")] = None,
    interface_decorators: Annotated[list[str] | None, Field(description=(
        "Additional decorator names that indicate external interfaces "
        "(e.g. 'rpc_endpoint', 'message_handler')."
    ))] = None,
) -> str:
    """Check a symbol for hidden dangers before editing it.

    Returns dangers (things you'd miss from just reading the code), plus
    a compact summary of callers, side effects, and test coverage.

    Danger categories:
    - external_interface: decorator exposes this as API/RPC/CLI (signature = contract)
    - async_side_effect: calls .delay()/.apply_async() (work finishes after return)
    - dynamic_reference: name appears as string literal (renaming won't catch it)
    - high_fan_out: 5+ non-test callers (wide blast radius)
    - caching: @lru_cache etc (mutations may serve stale data)
    - no_test_coverage: no test files call this directly
    """
    parsed = parse_extended_selector(selector)
    result = _semantic_context(
        parsed,
        project_path=project,
        extra_interface_decorators=interface_decorators,
    )

    # Return compact output focused on actionable information
    compact: dict = {
        "symbol": result.symbol,
        "kind": result.kind,
        "file": result.file,
        "line": result.line,
    }
    if result.decorators:
        compact["decorators"] = result.decorators
    if result.is_async:
        compact["is_async"] = True

    # Dangers are the whole point
    if result.dangers:
        compact["dangers"] = [
            {"level": d.level, "category": d.category,
             "message": d.message, "evidence": d.evidence}
            for d in result.dangers
        ]
    else:
        compact["dangers"] = "none detected"

    # Compact summary — counts, not full lists
    compact["callers_count"] = len(result.callers)
    compact["test_callers_count"] = sum(1 for c in result.callers if c.kind == "test")
    compact["references_count"] = result.references_count

    if result.side_effects:
        compact["side_effects"] = [
            {"kind": se.kind, "target": se.target}
            for se in result.side_effects
        ]

    return json.dumps(compact, indent=2)


# ---------------------------------------------------------------------------
# trace (internal helper used by analyze dispatcher; preserved as Python API
# because tests import it directly)
# ---------------------------------------------------------------------------


def trace_analysis(
    path: Annotated[str, Field(description="File or directory to analyze.")],
    from_pattern: Annotated[str | None, Field(description=(
        "Inline mode: source pattern where tainted data originates "
        "(e.g. 'request.args.get($X)'). When provided, config file is not needed."
    ))] = None,
    to_pattern: Annotated[str | None, Field(description=(
        "Inline mode: sink pattern where tainted data must not reach "
        "(e.g. 'cursor.execute($Q)')."
    ))] = None,
    not_through: Annotated[str | None, Field(description=(
        "Inline mode: sanitizer pattern. Data flowing through this is safe "
        "(e.g. 'escape($X)')."
    ))] = None,
    preset: Annotated[str | None, Field(description="Load framework-specific rules: flask, django, sqlalchemy, fastapi. Can combine with inline patterns.")] = None,
    config: Annotated[str | None, Field(description="Path to rules.yaml or legacy patterns.yaml. Not needed if using inline mode or preset.")] = None,
    label: Annotated[str | None, Field(description="Only check a specific trace label.")] = None,
    trace: Annotated[bool, Field(description="Include propagation traces in output.")] = False,
    interprocedural: Annotated[bool, Field(description="Enable cross-function analysis with fixed-point iteration.")] = False,
    engine: Annotated[str | None, Field(description="Force trace engine: 'datalog' (default) or 'python' (legacy escape hatch).")] = None,
) -> str:
    """Run trace analysis to detect unsafe data flows. Returns JSON.

    Two modes:
    - Inline: pass from_pattern + to_pattern directly (no config file needed).
    - Config: reads sources/sinks/sanitizers from .emend/rules.yaml or legacy patterns.yaml.

    Can also use preset= to load framework-specific rules (flask, django, etc.).
    Set interprocedural=True for cross-function analysis.
    """
    from pathlib import Path as _Path
    from emend.trace import (
        load_trace_config, run_trace_analysis, format_violations,
        TraceConfig, TraceSource, TraceSink, TraceSanitizer,
    )
    from emend.cli import resolve_files

    # Build config from inline params, preset, or config file
    if from_pattern and to_pattern:
        # Inline mode: build config from params
        inline_label = label or "inline"
        trace_config = TraceConfig(
            labels=[inline_label],
            sources=[TraceSource(pattern=from_pattern, label=inline_label)],
            sinks=[TraceSink(pattern=to_pattern, label=inline_label, message=f"Tainted data flows to {to_pattern}")],
            sanitizers=[TraceSanitizer(pattern=not_through, label=inline_label)] if not_through else [],
            scope_sanitizers=[],
        )
        if preset:
            from emend.trace_presets import get_preset, merge_configs
            preset_config = get_preset(preset)
            if preset_config:
                trace_config = merge_configs(trace_config, preset_config)
    elif preset:
        from emend.trace_presets import get_preset
        trace_config = get_preset(preset)
        if not trace_config:
            return json.dumps({"error": f"Unknown preset: {preset}"})
    else:
        config_path = resolve_rules_path(config, fallbacks=(LEGACY_PATTERNS_PATH,))
        if not config_path.exists():
            return json.dumps({"error": f"Config file not found: {config_path}. Provide from_pattern + to_pattern for inline mode, or use preset=."})
        trace_config = load_trace_config(str(config_path))

    if not trace_config.sources or not trace_config.sinks:
        return json.dumps({"error": "No trace sources or sinks configured. Provide from_pattern + to_pattern, a preset, or a config file."})

    resolved, _ = resolve_files(path)
    files = [str(f) for f in resolved]

    if interprocedural:
        from emend.trace import run_interprocedural_trace
        result = run_interprocedural_trace(
            files, trace_config, label_filter=label,
        )
        violations = result.violations
        # Build violation dicts directly to avoid serialize-deserialize-reserialize
        violation_data = []
        for v in violations:
            entry: dict = {
                "file": v.file_path, "line": v.line, "col": v.col,
                "label": v.label, "sink_pattern": v.sink_pattern, "message": v.message,
            }
            if v.engine:
                entry["engine"] = v.engine
            if trace:
                entry["trace"] = [
                    {"file": s.file_path, "line": s.line, "col": s.col,
                     "description": s.description, "variable": s.variable}
                    for s in v.trace
                ]
            violation_data.append(entry)
        data = {
            "violations": violation_data,
            "summaries_count": len(result.summaries),
            "iterations": result.iterations,
        }
        return json.dumps(data, indent=2)

    violations = run_trace_analysis(files, trace_config, label_filter=label, engine=engine)
    return format_violations(violations, show_trace=trace, json_output=True)


# ---------------------------------------------------------------------------
# facts_query (guided structured mode)
# ---------------------------------------------------------------------------


@mcp_app.tool()
def facts_query(
    project: Annotated[str, Field(description="Project root directory.")] = ".",
    limit: Annotated[int, Field(description="Maximum number of result rows to return.")] = 200,
    fact_type: Annotated[str | None, Field(description="Fact type (symbols, calls, references, trace_flows, types, imports).")] = None,
    name: Annotated[str | None, Field(description="Filter by name.")] = None,
    kind: Annotated[str | None, Field(description="Filter by kind.")] = None,
    file_path: Annotated[str | None, Field(description="Filter by file path.")] = None,
    symbol: Annotated[str | None, Field(description="Symbol qualified name.")] = None,
    label: Annotated[str | None, Field(description="Trace label filter.")] = None,
    transitive: Annotated[bool, Field(description="Compute transitive closure.")] = False,
    max_depth: Annotated[int, Field(description="Max depth for transitive queries.")] = 10,
) -> str:
    """Query the project fact graph via structured parameters."""
    from emend.fact_graph import FactGraph
    import dataclasses

    _fact_type = fact_type or "symbols"
    graph = FactGraph.build_from_project(project)

    if _fact_type == "symbols":
        results = graph.symbols(name=name, kind=kind, file_path=file_path)
        return json.dumps([dataclasses.asdict(f) for f in results[:limit]], indent=2)

    elif _fact_type == "calls":
        if not symbol:
            return json.dumps({"error": "Provide 'symbol' parameter for call queries."})
        if transitive:
            callers = graph.transitive_callers(symbol, max_depth=max_depth)
            return json.dumps({"symbol": symbol, "transitive_callers": sorted(callers)}, indent=2)
        from_calls = graph.calls_from(symbol)
        to_calls = graph.calls_to(symbol)
        return json.dumps({
            "calls_from": [dataclasses.asdict(c) for c in from_calls[:limit]],
            "calls_to": [dataclasses.asdict(c) for c in to_calls[:limit]],
        }, indent=2)

    elif _fact_type == "references":
        if not symbol:
            return json.dumps({"error": "Provide 'symbol' parameter for reference queries."})
        refs = graph.references_to(symbol)
        return json.dumps([dataclasses.asdict(r) for r in refs[:limit]], indent=2)

    elif _fact_type == "trace_flows":
        flows = graph.trace_flows(label=label, file_path=file_path)
        return json.dumps([dataclasses.asdict(f) for f in flows[:limit]], indent=2)

    elif _fact_type == "types":
        if not symbol:
            return json.dumps({"error": "Provide 'symbol' parameter for type queries."})
        types = graph.types_for(symbol)
        return json.dumps([dataclasses.asdict(t) for t in types[:limit]], indent=2)

    elif _fact_type == "imports":
        if not file_path:
            return json.dumps({"error": "Provide 'file_path' parameter for import queries."})
        imports = graph.imports_in(file_path)
        return json.dumps([dataclasses.asdict(i) for i in imports[:limit]], indent=2)

    return json.dumps({"error": f"Unknown fact_type: {_fact_type}"})


# ---------------------------------------------------------------------------
# policy (deprecated; see check dispatcher)
# ---------------------------------------------------------------------------


def check_policies(
    path: Annotated[str, Field(description="File or directory to check.")],
    config: Annotated[str | None, Field(description="Path to rules.yaml or legacy policies.yaml.")] = None,
    policy_name: Annotated[str | None, Field(description="Run only a specific policy by name.")] = None,
) -> str:
    """Run policy checks against source code.

    Policies combine flow analysis, structural checks, type constraints,
    and dead code detection into named, reusable compliance rules.
    """
    from emend.checks.engine import run_checks
    from emend.cli import resolve_files

    config_path = resolve_rules_path(
        config,
        fallbacks=(LEGACY_POLICIES_PATH, LEGACY_PATTERNS_PATH),
    )
    if not config_path.exists():
        return json.dumps({"error": f"Config file not found: {config_path}"})

    resolved, _ = resolve_files(path)
    files = [str(f) for f in resolved]

    violations = run_checks(
        files, config=str(config_path), rule_name=policy_name,
        project_path=path,
        allowed_kinds={"flow", "structural", "type", "deadcode", "datalog", "custom", "sequence"},
    )
    return json.dumps([
        {
            "rule": v.rule_name, "kind": v.kind, "severity": v.severity,
            "message": v.message, "file": v.file_path,
            "line": v.line, "col": v.col, "witness": v.witness or [],
        }
        for v in violations
    ], indent=2)


# ---------------------------------------------------------------------------
# Mappings (internal helpers used by mappings dispatcher; preserved as Python
# API because tests import them directly)
# ---------------------------------------------------------------------------


def map_read(
    kind: Annotated[str, Field(description="What to read: 'mapping' or 'module'.")] = "mapping",
    query: Annotated[str, Field(description="Search query (substring match) or exact identifier/module name. Omit to list all.")] = "",
    options: Annotated[dict | None, Field(description=(
        "Optional filters. "
        "For mappings: {source_project?, target_project?, relationship?, direction?, limit?}. "
        "For modules: no options needed."
    ))] = None,
) -> str:
    """Read from the mapping store.

    kind controls what is returned:
    - mapping: search/list/lookup identifier mappings. If query looks like an
      identifier (no spaces, non-empty), exact lookup is tried first; falls
      back to substring search. Use options.direction ('source'/'target'/'both')
      to control lookup direction.
    - module: list module mappings, or resolve a module name to a local path.
      If query is set, module resolution is attempted first; falls back to listing.
    """
    from emend.knowledge import MappingStore, mapping_to_dict, module_mapping_to_dict

    store = MappingStore(".")
    opts = options or {}

    if kind == "mapping":
        # Exact identifier lookup when query has no spaces (looks like an identifier)
        if query and " " not in query:
            project = opts.get("project")
            direction = opts.get("direction", "both")
            results = store.find_mappings_for(query, project=project, direction=direction)
            if results:
                return json.dumps([mapping_to_dict(m) for m in results], indent=2)
        # Substring search or list-all
        source_project = opts.get("source_project")
        target_project = opts.get("target_project")
        relationship = opts.get("relationship")
        limit = opts.get("limit", 50)
        if query:
            results = store.search_mappings(
                query, source_project=source_project,
                target_project=target_project, relationship=relationship, limit=limit,
            )
        else:
            results = store.list_mappings(
                source_project=source_project,
                target_project=target_project, relationship=relationship, limit=limit,
            )
        return json.dumps([mapping_to_dict(m) for m in results], indent=2)

    if kind == "module":
        if query:
            mm = store.resolve_module(query)
            if mm is not None:
                result = module_mapping_to_dict(mm)
                resolved = store.resolve_module_to_path(query)
                if resolved:
                    result["resolved_path"] = resolved
                return json.dumps(result, indent=2)
            return json.dumps({"error": f"No module mapping found for '{query}'."})
        results = store.list_module_mappings()
        return json.dumps([module_mapping_to_dict(m) for m in results], indent=2)

    return json.dumps({"error": f"Unknown kind '{kind}'. Use: mapping, module."})


def map_write(
    kind: Annotated[str, Field(description="Entry type: 'mapping' or 'module'.")],
    op: Annotated[str, Field(description="Operation: 'add' or 'delete'.")],
    entry: Annotated[dict, Field(description=(
        "Entry data. "
        "For mapping+add: {source_project, source_identifier, target_project, target_identifier, "
        "source_kind?, target_kind?, relationship?, confidence?, provenance?, evidence?, metadata?}. "
        "For mapping+delete: {source_identifier, source_project?, target_identifier?}. "
        "For module+add: {module_prefix, repo?, local_path?, branch?, subpath?, provenance?, metadata?}. "
        "For module+delete: {module_prefix}."
    ))],
) -> str:
    """Write to the mapping store: add or delete entries.

    kind + op selects the operation:
    - mapping + add: entry must contain source_project, source_identifier, target_project,
      target_identifier. Optional: source_kind, target_kind, relationship, confidence,
      provenance, evidence, metadata.
    - mapping + delete: entry must contain source_identifier. Optional: source_project,
      target_identifier to narrow the deletion.
    - module + add: entry must contain module_prefix and at least one of repo or local_path.
      Optional: branch, subpath, provenance, metadata.
    - module + delete: entry must contain module_prefix.
    """
    from emend.knowledge import (
        MappingStore, IdentifierMapping, ModuleMapping,
        mapping_to_dict, module_mapping_to_dict,
    )

    store = MappingStore(".")

    if kind == "mapping":
        if op == "add":
            source_project = entry.get("source_project")
            source_identifier = entry.get("source_identifier")
            target_project = entry.get("target_project")
            target_identifier = entry.get("target_identifier")
            if not source_project or not source_identifier or not target_project or not target_identifier:
                return json.dumps({"error": "source_project, source_identifier, target_project, target_identifier required."})
            m = IdentifierMapping(
                source_project=source_project,
                source_identifier=source_identifier,
                source_kind=entry.get("source_kind") or "",
                target_project=target_project,
                target_identifier=target_identifier,
                target_kind=entry.get("target_kind") or "",
                relationship=entry.get("relationship") or "equivalent",
                confidence=entry.get("confidence") if entry.get("confidence") is not None else 1.0,
                provenance=entry.get("provenance") or "llm",
                evidence=entry.get("evidence") or "",
                metadata=entry.get("metadata") or {},
            )
            store.add_mapping(m)
            return json.dumps(mapping_to_dict(m), indent=2)
        if op == "delete":
            source_identifier = entry.get("source_identifier")
            if not source_identifier:
                return json.dumps({"error": "source_identifier is required for delete."})
            ok = store.delete_mapping(
                source_identifier,
                source_project=entry.get("source_project"),
                target_identifier=entry.get("target_identifier"),
            )
            return json.dumps({"deleted": ok, "source_identifier": source_identifier})

    elif kind == "module":
        if op == "add":
            module_prefix = entry.get("module_prefix")
            if not module_prefix:
                return json.dumps({"error": "module_prefix is required."})
            repo = entry.get("repo")
            local_path = entry.get("local_path")
            if not repo and not local_path:
                return json.dumps({"error": "Either repo or local_path is required."})
            m = ModuleMapping(
                module_prefix=module_prefix,
                repo=repo or "", local_path=local_path or "",
                branch=entry.get("branch") or "", subpath=entry.get("subpath") or "",
                provenance=entry.get("provenance") or "llm",
                metadata=entry.get("metadata") or {},
            )
            store.add_module_mapping(m)
            return json.dumps(module_mapping_to_dict(m), indent=2)
        if op == "delete":
            module_prefix = entry.get("module_prefix")
            if not module_prefix:
                return json.dumps({"error": "module_prefix is required for delete."})
            ok = store.delete_module_mapping_by_prefix(module_prefix)
            return json.dumps({"deleted": ok, "module_prefix": module_prefix})

    else:
        return json.dumps({"error": f"Unknown kind '{kind}'. Use: mapping, module."})

    return json.dumps({"error": f"Unknown op '{op}'. Use: add, delete."})


# ---------------------------------------------------------------------------
# Unified MCP surface (discriminated tools)
# ---------------------------------------------------------------------------


@mcp_app.tool()
def transform(
    operation: Annotated[str, Field(description="Operation: replace, edit, add, remove, rename, move.")],
    apply: Annotated[bool, Field(description="Write changes to disk. Default is dry-run.")] = False,
    selector: Annotated[str | None, Field(description="Symbol selector for edit/add/remove/rename/move operations.")] = None,
    value: Annotated[str | None, Field(description="New component value for edit/add operations.")] = None,
    before: Annotated[str | None, Field(description="Insert before this name (add operation).")] = None,
    after: Annotated[str | None, Field(description="Insert after this name (add operation).")] = None,
    at: Annotated[int | None, Field(description="Insert at index (add operation).")] = None,
    pattern: Annotated[str | None, Field(description="Code pattern for replace operation.")] = None,
    replacement: Annotated[str | None, Field(description="Replacement code pattern for replace operation.")] = None,
    path: Annotated[str | None, Field(description="File path/glob/dir for replace operation.")] = None,
    destination: Annotated[str | None, Field(description="Destination for move operation.")] = None,
    to: Annotated[str | None, Field(description="Target name for rename operation.")] = None,
    where: Annotated[str | None, Field(description="Compatibility scope constraint for replace.")] = None,
    docs: Annotated[bool, Field(description="Rename docs in rename operation.")] = False,
    no_hierarchy: Annotated[bool, Field(description="Disable hierarchy-aware symbol rename.")] = False,
    unsure: Annotated[bool, Field(description="Rename uncertain occurrences.")] = False,
    copy_only: Annotated[bool, Field(description="Copy instead of move for move operation.")] = False,
    dedent: Annotated[bool, Field(description="Dedent moved/copied symbol body.")] = False,
    no_update_imports: Annotated[bool, Field(description="Skip import updates for move operation.")] = False,
    project: Annotated[str | None, Field(description="Project root for rename/move operations.")] = None,
) -> str:
    """Apply write-style refactoring operations through one discriminated endpoint."""
    op = (operation or "").lower()
    if op == "replace":
        if not pattern or not replacement or not path:
            return json.dumps({"error": "replace requires pattern, replacement, and path"})
        return replace(
            pattern=pattern,
            replacement=replacement,
            path=path,
            apply=apply,
            where=where,
        )
    if op in {"edit", "add", "remove"}:
        if not selector:
            return json.dumps({"error": f"{op} requires selector"})
        mode = "set" if op == "edit" else op
        return modify(
            selector=selector,
            value=value,
            mode=mode,
            before=before,
            after=after,
            at=at,
            apply=apply,
        )
    if op == "rename":
        if not selector or not to:
            return json.dumps({"error": "rename requires selector and to"})
        return rename(
            selector=selector,
            to=to,
            apply=apply,
            docs=docs,
            no_hierarchy=no_hierarchy,
            unsure=unsure,
            project=project,
        )
    if op == "move":
        if not selector or not destination:
            return json.dumps({"error": "move requires selector and destination"})
        return move(
            selector=selector,
            destination=destination,
            copy_only=copy_only,
            dedent=dedent,
            no_update_imports=no_update_imports,
            apply=apply,
            project=project,
        )
    return json.dumps({"error": f"Unknown operation {operation!r}. Use: replace, edit, add, remove, rename, move."})


@mcp_app.tool()
def references(
    selector: Annotated[str, Field(description="Symbol selector to inspect.")],
    kind: Annotated[str, Field(description="Reference kind: all, reads, writes, or calls.")] = "all",
    exclude_definition: Annotated[bool, Field(description="Exclude definition row (refs mode).")] = False,
    exclude_imports: Annotated[bool, Field(description="Exclude import rows (refs mode).")] = False,
    project: Annotated[str | None, Field(description="Project root directory.")] = None,
) -> str:
    """Find references through a focused endpoint."""
    query_kind = (kind or "all").lower()
    if query_kind not in {"all", "reads", "writes", "calls"}:
        return json.dumps({"error": f"Unknown kind {kind!r}. Use: all, reads, writes, calls."})
    return refs(
        selector=selector,
        exclude_definition=exclude_definition,
        exclude_imports=exclude_imports,
        writes_only=query_kind == "writes",
        reads_only=query_kind == "reads",
        calls_only=query_kind == "calls",
        project=project,
    )


def duplicates_analysis(
    path: str = ".",
    mode: str = "all",
    file_path: str | None = None,
    limit: int = 20,
    min_lines: int = 5,
    min_score: float = 0.0,
    cross_file: bool | None = None,
) -> str:
    """Run duplicate detection and return JSON results."""
    from emend.duplicate import query_duplicates, format_duplicates_json

    clusters = query_duplicates(
        project_path=path,
        mode=mode,
        file_scope=file_path,
        limit=limit,
        min_lines=min_lines,
        min_score=min_score,
        cross_file=cross_file,
    )
    return format_duplicates_json(clusters)


def check_duplicates(
    file_path: Annotated[str, Field(description="File to check for duplication (usually the just-written file in a post-write hook).")],
    project: Annotated[str | None, Field(description="Project root to scan against. Defaults to CWD.")] = None,
    mode: Annotated[str, Field(description="Detection mode: exact, sequence, or all.")] = "all",
    limit: Annotated[int, Field(description="Maximum number of clusters to return.")] = 10,
    min_lines: Annotated[int, Field(description="Minimum lines for a finding.")] = 5,
    min_score: Annotated[float, Field(description="Minimum score threshold (use ~50 to suppress tiny matches in hooks).")] = 0.0,
) -> str:
    """Check whether *file_path* introduces code duplication vs the project.

    Designed for post-write hooks: scans the full project and returns only
    clusters with at least one member in *file_path*. Returns an empty JSON
    array when no duplicates are found — safe to invoke from a ``PostToolUse``
    hook after ``Edit``/``Write``.
    """
    from emend.duplicate import format_duplicates_json, query_duplicates

    clusters = query_duplicates(
        project_path=project or ".",
        mode=mode,
        limit=limit,
        min_lines=min_lines,
        min_score=min_score,
        involves_file=file_path,
    )
    return format_duplicates_json(clusters)


@mcp_app.tool()
def analyze(
    mode: Annotated[str, Field(description="Analysis mode: graph, deadcode, impact, semantic_context, flow, trace, or duplicates.")] = "graph",
    path: Annotated[str | None, Field(description="Path scope for deadcode/trace/check-style modes.")] = None,
    selector: Annotated[str | None, Field(description="Selector input for semantic_context/impact modes.")] = None,
    symbol: Annotated[str | None, Field(description="Symbol selector for graph mode.")] = None,
    file_path: Annotated[str | None, Field(description="File path for graph mode.")] = None,
    format: Annotated[str, Field(description="Output format where supported (graph).")] = "json",
    direction: Annotated[str, Field(description="Graph direction for symbol graph mode.")] = "both",
    transitive: Annotated[bool, Field(description="Enable transitive graph/caller expansion.")] = False,
    depth: Annotated[int | None, Field(description="Max depth for transitive graph expansion.")] = None,
    kind: Annotated[str | None, Field(description="Deadcode kind filter.")] = None,
    include_private: Annotated[bool, Field(description="Include private names in deadcode mode.")] = False,
    no_last_reference: Annotated[bool, Field(description="Disable git last-reference info in deadcode mode.")] = False,
    entry_point_decorators: Annotated[list[str] | None, Field(description="Entry-point decorators for deadcode mode.")] = None,
    entry_point_names: Annotated[list[str] | None, Field(description="Entry-point names for deadcode mode.")] = None,
    exclude_paths: Annotated[list[str] | None, Field(description="Excluded globs for deadcode mode.")] = None,
    config: Annotated[str | None, Field(description="Config path for deadcode/trace mode.")] = None,
    diff: Annotated[str | None, Field(description="Git diff range for impact mode.")] = None,
    output: Annotated[str, Field(description="Impact output mode.")] = "symbols",
    max_depth: Annotated[int, Field(description="Impact BFS max depth.")] = 10,
    interface_decorators: Annotated[list[str] | None, Field(description="Extra interface decorators for semantic_context mode.")] = None,
    from_pattern: Annotated[str | None, Field(description="Flow source pattern for inline flow mode.")] = None,
    to_pattern: Annotated[str | None, Field(description="Flow sink pattern for inline flow mode.")] = None,
    not_through: Annotated[str | None, Field(description="Flow sanitizer pattern for inline flow mode.")] = None,
    preset: Annotated[str | None, Field(description="Flow preset (flask/django/sqlalchemy/fastapi).")] = None,
    label: Annotated[str | None, Field(description="Flow label filter.")] = None,
    trace: Annotated[bool, Field(description="Include propagation steps in flow mode.")] = False,
    interprocedural: Annotated[bool, Field(description="Enable interprocedural flow analysis.")] = False,
    project: Annotated[str | None, Field(description="Project root directory.")] = None,
) -> str:
    """Run analysis operations through one discriminated endpoint."""
    analysis_mode = (mode or "graph").lower()
    if analysis_mode == "graph":
        return graph(
            file_path=file_path or path,
            symbol=symbol or selector,
            direction=direction,
            transitive=transitive,
            depth=depth,
            format=format,
            project=project,
        )
    if analysis_mode == "deadcode":
        return deadcode(
            path=path or ".",
            kind=kind,
            include_private=include_private,
            no_last_reference=no_last_reference,
            entry_point_decorators=entry_point_decorators,
            entry_point_names=entry_point_names,
            exclude_paths=exclude_paths,
            config=config,
        )
    if analysis_mode == "impact":
        return impact(
            selector=selector,
            diff=diff,
            output=output,
            max_depth=max_depth,
            project=project,
        )
    if analysis_mode == "semantic_context":
        if not selector:
            return json.dumps({"error": "semantic_context mode requires selector"})
        return semantic_context(
            selector=selector,
            project=project,
            interface_decorators=interface_decorators,
        )
    if analysis_mode in {"flow", "trace"}:
        if not path:
            return json.dumps({"error": "flow mode requires path"})
        return trace_analysis(
            path=path,
            from_pattern=from_pattern,
            to_pattern=to_pattern,
            not_through=not_through,
            preset=preset,
            config=config,
            label=label,
            trace=trace,
            interprocedural=interprocedural,
        )
    if analysis_mode == "duplicates":
        return duplicates_analysis(
            path=path or ".",
            mode=mode if mode not in {"graph", "deadcode", "impact", "semantic_context", "flow", "trace", "duplicates"} else "all",
            file_path=file_path,
            limit=max_depth,
            min_lines=5,
            min_score=0.0,
            cross_file=True,
        )
    return json.dumps({"error": f"Unknown mode {mode!r}. Use: graph, deadcode, impact, semantic_context, flow, trace, duplicates."})


@mcp_app.tool()
def check(
    paths: Annotated[list[str] | None, Field(description="File or directory scope(s) to check.")] = None,
    config: Annotated[str | None, Field(description="Rules config path. Defaults to .emend/rules.yaml with legacy fallback.")] = None,
    rule: Annotated[str | None, Field(description="Run only one named rule.")] = None,
    kind: Annotated[str | None, Field(description="Restrict to one rule kind: match, flow, deadcode, type.")] = None,
    fix: Annotated[bool, Field(description="Apply auto-fixes for match rules when available.")] = False,
) -> str:
    """Run unified project rules from ``rules.yaml``."""
    from emend.checks import run_checks
    from emend.cli import resolve_file_scopes

    resolved, _ = resolve_file_scopes(paths or ["."], language="python")
    file_paths = [str(f) for f in resolved]
    project_path = paths[0] if paths else "."
    violations = run_checks(
        file_paths,
        config=config,
        rule_name=rule,
        kind=kind,
        fix=fix,
        language="python",
        project_path=project_path,
    )
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



@mcp_app.tool()
def mappings(
    operation: Annotated[str, Field(description="Mappings operation: read or write.")],
    kind: Annotated[str, Field(description="Mapping kind: mapping or module.")] = "mapping",
    query: Annotated[str, Field(description="Read query string for read operation.")] = "",
    options: Annotated[dict | None, Field(description="Read options for read operation.")] = None,
    op: Annotated[str | None, Field(description="Write operation: add or delete (write operation only).")] = None,
    entry: Annotated[dict | None, Field(description="Write payload (write operation only).")] = None,
) -> str:
    """Read/write mapping state through one discriminated endpoint."""
    normalized = (operation or "").lower()
    if normalized == "read":
        return map_read(kind=kind, query=query, options=options)
    if normalized == "write":
        if not op or entry is None:
            return json.dumps({"error": "write operation requires op and entry"})
        return map_write(kind=kind, op=op, entry=entry)
    return json.dumps({"error": f"Unknown operation {operation!r}. Use: read, write."})


# ---------------------------------------------------------------------------
# grammar_and_cookbook
# ---------------------------------------------------------------------------


def _parse_rst_sections(text: str) -> dict[str, str]:
    """Parse RST text into a dict mapping section keys to their content.

    Top-level sections are identified by headings underlined with ``---``.
    The document title (underlined with ``===``) is excluded.
    """
    import re as _re

    key_map = {
        "selector_syntax": "selectors",
        "pattern_syntax": "patterns",
        "commands": "commands",
        "cookbook_recipes": "recipes",
        "fact_graph_relations": "facts",
    }

    lines = text.split("\n")
    section_starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if (
            i > 0
            and stripped
            and all(c == "-" for c in stripped)
            and len(stripped) >= 3
        ):
            heading = lines[i - 1].strip()
            if heading:
                section_starts.append((i - 1, heading))

    sections: dict[str, str] = {}
    for idx, (start, heading) in enumerate(section_starts):
        end = section_starts[idx + 1][0] if idx + 1 < len(section_starts) else len(lines)
        content = "\n".join(lines[start:end]).strip()
        raw_key = _re.sub(r"\s+", "_", heading.lower())
        key = key_map.get(raw_key, raw_key)
        sections[key] = content

    return sections


_SECTION_SUMMARIES: dict[str, str] = {
    "selectors": "addressing symbols, components, wildcards, file globs",
    "patterns": "metavariables, expressions, statements, replacements",
    "commands": "grep, replace, edit, add, rm, refs, rename, mv, cp, graph, deadcode, lint, batch",
    "recipes": "common refactoring patterns and examples",
    "facts": "CozoDB stored relations and example Datalog queries",
}


@mcp_app.tool()
def grammar_and_cookbook(
    section: Annotated[
        str | None,
        Field(
            description=(
                'Section to retrieve. Pass None (default) to get a table of contents. '
                'Pass "all" for the full document. '
                'Other values: selectors, patterns, commands, recipes, facts.'
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Return the emend grammar reference and cookbook, or a specific section of it.

    Call this tool when you need detailed syntax help for constructing
    selectors, patterns, or command invocations.  The response covers
    selector syntax, pattern metavariables, every command with examples,
    and common refactoring recipes.

    When called without arguments, returns a table of contents listing the
    available sections.  Pass ``section="<name>"`` to retrieve a specific
    section, or ``section="all"`` for the complete document.

    Available section names: selectors, patterns, commands, recipes, facts, all.
    """
    import importlib.resources
    import re as _re

    text = importlib.resources.read_text("emend", "grammar_and_cookbook.rst")

    # Resolve ``.. literalinclude::`` directives by inlining the grammar files.
    # Sphinx handles these at build time, but plain-text consumers (LLMs) need
    # the content expanded inline.
    _grammars = {
        "selector.lark": importlib.resources.read_text("emend.grammars", "selector.lark"),
        "pattern.lark": importlib.resources.read_text("emend.grammars", "pattern.lark"),
    }

    def _inline(m: "_re.Match[str]") -> str:
        path = m.group(1).strip()
        for name, content in _grammars.items():
            if name in path:
                indented = "\n".join("    " + line for line in content.splitlines())
                return f"::\n\n{indented}\n"
        return m.group(0)  # leave unknown directives as-is

    text = _re.sub(
        r"\.\. literalinclude:: ([^\n]+)\n(?:   :[^\n]+\n)*",
        _inline,
        text,
    )

    # Return the full document when section="all".
    if section == "all":
        return text

    sections = _parse_rst_sections(text)

    # Return a specific named section.
    if section is not None:
        if section in sections:
            return sections[section]
        available = ", ".join(sorted(sections.keys()))
        return f"Unknown section {section!r}. Available sections: {available}, all"

    # Default: return a compact table of contents.
    toc_lines = [
        'Available sections (pass section="<name>" to retrieve):\n',
    ]
    # List known sections in a stable order, then any extras.
    ordered_keys = ["selectors", "patterns", "commands", "recipes", "facts"]
    seen: set[str] = set()
    for key in ordered_keys:
        if key in sections:
            summary = _SECTION_SUMMARIES.get(key, "")
            heading_line = sections[key].split("\n")[0]
            entry = f"- {key}: {heading_line} — {summary}" if summary else f"- {key}: {heading_line}"
            toc_lines.append(entry)
            seen.add(key)
    for key, content in sections.items():
        if key not in seen:
            heading_line = content.split("\n")[0]
            summary = _SECTION_SUMMARIES.get(key, "")
            entry = f"- {key}: {heading_line} — {summary}" if summary else f"- {key}: {heading_line}"
            toc_lines.append(entry)
    toc_lines.append("- all: Return the complete reference document")
    return "\n".join(toc_lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _warm_caches_background() -> None:
    """Warm parse and QN-index caches in a background process.

    Launched at MCP server start so that subsequent tool calls are fast.
    Uses multiprocessing so the cache-warming work doesn't block the
    event loop serving MCP requests.
    """
    import multiprocessing
    import logging as _logging

    def _worker() -> None:
        try:
            from emend.transform import warm_caches
            _logging.basicConfig(level=_logging.WARNING)
            warm_caches(".")
        except Exception:
            pass  # best-effort; don't crash the server

    proc = multiprocessing.Process(target=_worker, daemon=True)
    proc.start()


def _compress_schema(obj: object) -> object:
    """Recursively compress a JSON-Schema dict.

    Transformations applied:
    - Remove all ``title`` keys (Pydantic boilerplate).
    - Collapse ``anyOf: [{type: X}, {type: null}]`` (and the reversed order)
      into ``type: X``, retaining any existing ``default`` value.  Also
      handles the case where the non-null entry carries ``items`` (arrays).
    """
    if isinstance(obj, list):
        return [_compress_schema(item) for item in obj]
    if not isinstance(obj, dict):
        return obj

    # Recurse first so inner nodes are already compressed.
    compressed: dict = {k: _compress_schema(v) for k, v in obj.items() if k != "title"}

    # Collapse anyOf with a null alternative into a plain type.
    if "anyOf" in compressed:
        entries = compressed["anyOf"]
        if isinstance(entries, list) and len(entries) == 2:
            null_entries = [e for e in entries if isinstance(e, dict) and e.get("type") == "null"]
            real_entries = [e for e in entries if isinstance(e, dict) and e.get("type") != "null"]
            if len(null_entries) == 1 and len(real_entries) == 1:
                real = real_entries[0]
                # Only collapse simple types and typed arrays to keep schema valid.
                real_type = real.get("type")
                if real_type in ("string", "integer", "boolean", "number", "array"):
                    del compressed["anyOf"]
                    compressed["type"] = real_type
                    if real_type == "array" and "items" in real:
                        compressed["items"] = real["items"]
                    # Preserve default: null if present, otherwise set it.
                    compressed.setdefault("default", None)

    return compressed


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
    """Return the MCP tool schema as a JSON string.

    Each tool is serialised with its name, description, and full
    JSON-Schema ``inputSchema`` derived from the Pydantic/Field
    annotations on the tool functions.  The schema is post-processed to
    remove Pydantic boilerplate (``title`` keys) and collapse nullable
    ``anyOf`` unions into plain ``type`` entries.
    """
    selected = _resolve_profile_tools(profile=profile, tools=tools)
    tools = mcp_app._tool_manager.list_tools()
    result = [
        {
            "name": t.name,
            "description": t.description or "",
            "inputSchema": _compress_schema(t.parameters),
        }
        for t in tools
        if selected is None or t.name in selected
    ]
    return json.dumps({"tools": result}, indent=2)


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

# Snapshot all registered MCP tools so profiles can switch losslessly at
# runtime (e.g. expert profile re-adds the mappings tool after core pruning).
_ALL_TOOLS: dict[str, Any] = {
    t.name: t for t in mcp_app._tool_manager.list_tools()
}


def _restore_all_tools() -> None:
    mcp_app._tool_manager._tools.clear()
    mcp_app._tool_manager._tools.update(_ALL_TOOLS)


def configure_profile(
    profile: str | None = None,
    tools: list[str] | None = None,
) -> None:
    """Prune the tool registry to match *profile* or an explicit *tools* list.

    Must be called **before** ``run_server()`` or ``dump_schema()``.
    """
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

    # Kick off cache warming in a background process so the first tool
    # call doesn't pay the full indexing cost.
    _warm_caches_background()

    mcp_app.settings.port = port
    mcp_app.run(transport=transport)


# Keep module-import default aligned with the core profile so schema dumps and
# direct tool enumeration reflect the reduced MCP surface.
configure_profile(profile="core")
