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

## Mappings

emend includes cross-service identifier mappings and module-to-repo mappings.
Use map_read to query and map_write to add/update/delete entries.

## Quick reference

Selectors: file.py::func, file.py::Class.method, file.py::func[params][x],
  file.py::func[returns], file.py::Class[bases], file.py::func[decorators],
  file.py::func[body], file.py::*[params] (wildcards), 'src/**/*.py::func' (globs)
Patterns: print($X), func($...ARGS), $A + $B, return $X ($ prefix = metavar)
Where: 'def', 'class', 'def test_*', 'not class', '@decorator'
Output: code, location, selector, summary, metadata, json, count
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
    query: Annotated[str, Field(description=(
        "What to search for. Can be: "
        "(1) a code pattern with $-metavars: 'print($X)', 'assert $A == $B'; "
        "(2) a literal code pattern: 'assert False', 'import os'; "
        "(3) a symbol selector: 'MyClass.method', 'func[params]'; "
        "(4) a file/dir path for symbol summary. "
        "The file scope can be embedded with :: (e.g. 'src/::print($X)') "
        "but prefer using the separate 'files' parameter instead."
    ))],
    files: Annotated[str | None, Field(description=(
        "File scope: a file path, glob pattern, or directory. "
        "Examples: 'src/', '**/*.py', 'file.py'. "
        "Defaults to current directory (all Python files)."
    ))] = None,
    kind: Annotated[str | None, Field(description="Symbol kind filter: function, method, class, async_function, async_method.")] = None,
    name: Annotated[str | None, Field(description="Name pattern filter (glob like 'test_*' or /regex/).")] = None,
    returns: Annotated[str | None, Field(description="Return type filter.")] = None,
    depth: Annotated[str | None, Field(description="Nesting depth filter (lookup) or display depth (summary).")] = None,
    has_param: Annotated[str | None, Field(description="Parameter filter.")] = None,
    output: Annotated[str, Field(description="Output format: code, location, selector, summary, metadata, json, count, code::dedent, summary::flat.")] = "code",
    where: Annotated[str | None, Field(description="Scope constraint: 'def', 'class', 'def test_*', 'not class', 'MyClass.method', '@decorator'.")] = None,
    imported_from: Annotated[str | None, Field(description="Only match when root name is imported from this module.")] = None,
    scope_local: Annotated[bool, Field(description="Only match locally-defined names, exclude imports.")] = False,
    case_insensitive: Annotated[bool, Field(description="Case-insensitive matching.")] = False,
    smart_case: Annotated[bool, Field(description="Match naming convention variants (snake_case/camelCase/etc).")] = False,
) -> str:
    """Search for code patterns or symbols in Python files.

    Mode is auto-detected from the query:
    - Pattern mode: query has $-metavars ('print($X)') or isn't a valid
      symbol selector ('assert False', 'import os')
    - Lookup mode: query is a symbol selector ('MyClass.method', 'func[params]')
    - Summary mode: query is a bare file/dir path with no filters

    For pattern searches, set ``files`` to scope the search. For symbol
    lookups, embed the file in the query ('file.py::func') or set ``files``.
    """
    import re as _re
    from emend.cli import resolve_files, parse_where_clause, detect_query_shape

    where_params = parse_where_clause([where] if where else [])
    where_scope = where_params.get("scope")
    where_inside = where_params.get("inside")
    where_not_inside = where_params.get("not_inside")
    where_matching = where_params.get("matching")

    output_base = output
    output_modifier = None
    if output and "::" in output:
        parts = output.split("::", 1)
        output_base = parts[0]
        output_modifier = parts[1]

    _shape = detect_query_shape(query, files)
    query = _shape.query
    files = _shape.path
    is_pattern_mode = _shape.is_pattern_mode
    has_selector = _shape.has_selector
    is_line_selector = _shape.is_line_selector

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
    if effective_output == "summary" and not is_pattern_mode:
        tree_depth = int(depth) if depth else None
        file_for_summary = query
        selector_for_summary = None
        if has_selector:
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

    # --- Pattern mode ---
    if is_pattern_mode:
        target_path = files or "."

        resolved_files, is_multi_file = resolve_files(target_path)
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

    # --- Lookup mode ---
    file_or_pattern = query
    selector_str = None
    if has_selector or is_line_selector:
        selector_str = query
        if has_selector:
            parts = query.split("::", 1)
            file_or_pattern = parts[0]
        elif is_line_selector:
            m = _re.search(r"^(.+?):\d+", query)
            if m:
                file_or_pattern = m.group(1)

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
# replace
# ---------------------------------------------------------------------------


@mcp_app.tool()
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
# modify (unified edit + add + remove)
# ---------------------------------------------------------------------------


@mcp_app.tool()
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
# refs (find references)
# ---------------------------------------------------------------------------


@mcp_app.tool()
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
# rename
# ---------------------------------------------------------------------------


@mcp_app.tool()
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
# move
# ---------------------------------------------------------------------------


@mcp_app.tool()
def move(
    selector: Annotated[str, Field(description="Symbol selector (file.py::name) or module path (file.py). Uses :: for symbols, bare path for modules.")],
    destination: Annotated[str, Field(description="Destination file or package.")],
    copy_only: Annotated[bool, Field(description="Copy without removing from source (symbol mode only). The body is copied exactly from the AST.")] = False,
    dedent: Annotated[bool, Field(description="Dedent nested symbols (symbol mode only).")] = False,
    no_update_imports: Annotated[bool, Field(description="Don't update imports across the project (symbol mode only).")] = False,
    apply: Annotated[bool, Field(description="Write changes to disk. Default is dry-run.")] = False,
    project: Annotated[str | None, Field(description="Project root directory (module mode only).")] = None,
) -> str:
    """Move (or copy) a symbol or module to another file, updating all imports.

    Set copy_only=True to copy a symbol without removing it from the source file.
    """
    if copy_only:
        buf = io.StringIO()
        with redirect_stdout(buf):
            ast_commands.cmd_copy_to(selector, destination, append=True, dedent=dedent, apply=apply)
        return buf.getvalue()
    if "::" in selector:
        parsed = parse_extended_selector(selector)
        diffs = move_symbol(
            parsed,
            destination,
            dedent=dedent,
            update_imports=not no_update_imports,
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
# graph
# ---------------------------------------------------------------------------


@mcp_app.tool()
def graph(
    file_path: Annotated[str, Field(description="Python file to analyze.")],
    format: Annotated[str, Field(description="Output format: plain, json, or dot (Graphviz).")] = "json",
    project: Annotated[str | None, Field(description="Project root directory.")] = None,
) -> str:
    """Generate a call graph for functions in a Python file."""
    return generate_graph(file_path, project_path=project, format=format)


# ---------------------------------------------------------------------------
# deadcode
# ---------------------------------------------------------------------------


@mcp_app.tool()
def deadcode(
    path: Annotated[str, Field(description="Project directory to scan.")] = ".",
    kind: Annotated[str | None, Field(description="Symbol kind filter: function, class.")] = None,
    include_private: Annotated[bool, Field(description="Include _private symbols.")] = False,
    exclude_references_from: Annotated[list[str] | None, Field(description="Directories to ignore when scanning for references.")] = None,
    no_strings: Annotated[bool, Field(description="Don't count string literals as references.")] = False,
    no_last_reference: Annotated[bool, Field(description="Don't show git last-reference info.")] = False,
    all_files: Annotated[bool, Field(description="Scan all Python files, not just git-tracked ones.")] = False,
    entry_point_decorators: Annotated[list[str] | None, Field(description="Additional decorator names to treat as entry points.")] = None,
    entry_point_names: Annotated[list[str] | None, Field(description="Additional function/class names to treat as entry points.")] = None,
    exclude_paths: Annotated[list[str] | None, Field(description="Directories to exclude entirely from dead code analysis.")] = None,
) -> str:
    """Find potentially dead (unreferenced) code. Returns JSON.

    Skips dunder methods, test functions, decorated entry points,
    __all__ members, and conventional entry points.

    Use entry_point_decorators and entry_point_names to add custom
    exclusions beyond the built-in heuristics.
    """
    results = find_dead_code(
        project_path=path,
        kind=kind,
        include_private=include_private,
        exclude_references_from=exclude_references_from,
        strings_count_as_references=not no_strings,
        show_last_reference=not no_last_reference,
        all_files=all_files,
        entry_point_decorators=entry_point_decorators,
        entry_point_names=entry_point_names,
        exclude_paths=exclude_paths,
    )
    data = []
    for d in results:
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
# lint
# ---------------------------------------------------------------------------


@mcp_app.tool()
def lint(
    path: Annotated[str, Field(description="File or directory to lint.")],
    config: Annotated[str | None, Field(description="Path to patterns.yaml config file (default: .emend/patterns.yaml).")] = None,
    fix: Annotated[bool, Field(description="Auto-apply fix replacements.")] = False,
    rule: Annotated[str | None, Field(description="Run only a specific rule by name.")] = None,
) -> str:
    """Lint Python files using pattern rules from .emend/patterns.yaml."""
    from pathlib import Path as _Path
    from emend.lint import load_rules, run_lint
    from emend.cli import resolve_files

    config_path = _Path(config or ".emend/patterns.yaml")
    if not config_path.exists():
        return f"Error: Config file not found: {config_path}"

    rules, macros, deadcode_config = load_rules(str(config_path))
    resolved, _ = resolve_files(path)
    files = [str(f) for f in resolved]

    violations = run_lint(
        rules, files, fix=fix, rule_filter=rule,
        deadcode_config=deadcode_config, project_path=path,
    )

    if not violations:
        return "No violations found."

    lines = [
        f"{v.file_path}:{v.line}:{v.col}: [{v.rule_name}] {v.message}"
        for v in violations
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# impact
# ---------------------------------------------------------------------------


@mcp_app.tool()
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
# semantic_context — situational awareness for code agents
# ---------------------------------------------------------------------------


@mcp_app.tool()
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
# taint
# ---------------------------------------------------------------------------


@mcp_app.tool()
def taint(
    path: Annotated[str, Field(description="File or directory to analyze.")],
    config: Annotated[str | None, Field(description="Path to patterns.yaml (default: .emend/patterns.yaml).")] = None,
    label: Annotated[str | None, Field(description="Only check a specific taint label.")] = None,
    trace: Annotated[bool, Field(description="Include propagation traces.")] = False,
    interprocedural: Annotated[bool, Field(description="Enable interprocedural analysis (cross-function taint tracking).")] = False,
) -> str:
    """Run taint analysis to detect unsafe data flows.

    Tracks value flow from sources to sinks within functions.
    Set interprocedural=True for cross-function analysis.
    """
    from pathlib import Path as _Path
    from emend.taint import load_taint_config, run_taint_analysis, format_violations
    from emend.cli import resolve_files

    config_path = _Path(config or ".emend/patterns.yaml")
    if not config_path.exists():
        return json.dumps({"error": f"Config file not found: {config_path}"})

    taint_config = load_taint_config(str(config_path))
    if not taint_config.sources or not taint_config.sinks:
        return json.dumps({"error": "No taint sources or sinks configured."})

    resolved, _ = resolve_files(path)
    files = [str(f) for f in resolved]

    if interprocedural:
        from emend.taint import run_interprocedural_taint_analysis
        result = run_interprocedural_taint_analysis(
            files, taint_config, label_filter=label,
        )
        violations = result.violations
        # Build violation dicts directly to avoid serialize-deserialize-reserialize
        violation_data = []
        for v in violations:
            entry: dict = {
                "file": v.file_path, "line": v.line, "col": v.col,
                "label": v.label, "sink_pattern": v.sink_pattern, "message": v.message,
            }
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

    violations = run_taint_analysis(files, taint_config, label_filter=label)
    return format_violations(violations, show_trace=trace, json_output=True)


# ---------------------------------------------------------------------------
# fact_graph (query interface)
# ---------------------------------------------------------------------------


@mcp_app.tool()
def query_facts(
    project: Annotated[str, Field(description="Project root directory.")] = ".",
    fact_type: Annotated[str, Field(description="Fact type to query: symbols, calls, references, taint_flows, types, imports.")] = "symbols",
    name: Annotated[str | None, Field(description="Filter by name (symbols).")] = None,
    kind: Annotated[str | None, Field(description="Filter by kind (symbols).")] = None,
    file_path: Annotated[str | None, Field(description="Filter by file path.")] = None,
    symbol: Annotated[str | None, Field(description="Symbol qualified name (calls/references/types).")] = None,
    label: Annotated[str | None, Field(description="Taint label filter (taint_flows).")] = None,
    transitive: Annotated[bool, Field(description="Compute transitive closure (calls).")] = False,
    max_depth: Annotated[int, Field(description="Max depth for transitive queries.")] = 10,
    limit: Annotated[int, Field(description="Max results to return.")] = 100,
) -> str:
    """Query the relational fact graph for code invariants.

    Provides structured access to symbols, call relationships,
    references, taint flows, type information, and imports.
    """
    from emend.fact_graph import FactGraph
    import dataclasses

    graph = FactGraph.build_from_project(project)

    if fact_type == "symbols":
        results = graph.symbols(name=name, kind=kind, file_path=file_path)
        return json.dumps([dataclasses.asdict(f) for f in results[:limit]], indent=2)

    elif fact_type == "calls":
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

    elif fact_type == "references":
        if not symbol:
            return json.dumps({"error": "Provide 'symbol' parameter for reference queries."})
        refs = graph.references_to(symbol)
        return json.dumps([dataclasses.asdict(r) for r in refs[:limit]], indent=2)

    elif fact_type == "taint_flows":
        flows = graph.taint_flows(label=label, file_path=file_path)
        return json.dumps([dataclasses.asdict(f) for f in flows[:limit]], indent=2)

    elif fact_type == "types":
        if not symbol:
            return json.dumps({"error": "Provide 'symbol' parameter for type queries."})
        types = graph.types_for(symbol)
        return json.dumps([dataclasses.asdict(t) for t in types[:limit]], indent=2)

    elif fact_type == "imports":
        if not file_path:
            return json.dumps({"error": "Provide 'file_path' parameter for import queries."})
        imports = graph.imports_in(file_path)
        return json.dumps([dataclasses.asdict(i) for i in imports[:limit]], indent=2)

    return json.dumps({"error": f"Unknown fact_type: {fact_type}"})


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


@mcp_app.tool()
def check_policies(
    path: Annotated[str, Field(description="File or directory to check.")],
    config: Annotated[str | None, Field(description="Path to policies.yaml (default: .emend/policies.yaml).")] = None,
    policy_name: Annotated[str | None, Field(description="Run only a specific policy by name.")] = None,
) -> str:
    """Run policy checks against source code.

    Policies combine flow analysis, structural checks, type constraints,
    and dead code detection into named, reusable compliance rules.
    """
    from pathlib import Path as _Path
    from emend.policy import load_policies, run_policy_checks, format_policy_violations
    from emend.cli import resolve_files

    config_path = _Path(config or ".emend/policies.yaml")
    if not config_path.exists():
        return json.dumps({"error": f"Config file not found: {config_path}"})

    policies = load_policies(str(config_path))
    if policy_name:
        policies = [p for p in policies if p.name == policy_name]
        if not policies:
            return json.dumps({"error": f"Policy '{policy_name}' not found."})

    resolved, _ = resolve_files(path)
    files = [str(f) for f in resolved]

    violations = run_policy_checks(files, policies)
    return format_policy_violations(violations, json_output=True)


# ---------------------------------------------------------------------------
# Mappings (identifier + module)
# ---------------------------------------------------------------------------


@mcp_app.tool()
def map_read(
    kind: Annotated[str, Field(description="What to read: 'mapping' or 'module'.")] = "mapping",
    query: Annotated[str, Field(description="Search query (substring match). Omit to list.")] = "",
    identifier: Annotated[str | None, Field(description="Exact identifier lookup (mapping kind only).")] = None,
    module: Annotated[str | None, Field(description="Module name to resolve (module kind only).")] = None,
    project: Annotated[str | None, Field(description="Filter by project.")] = None,
    source_project: Annotated[str | None, Field(description="Filter mappings by source project.")] = None,
    target_project: Annotated[str | None, Field(description="Filter mappings by target project.")] = None,
    relationship: Annotated[str | None, Field(description="Filter mappings by relationship.")] = None,
    direction: Annotated[str, Field(description="Identifier lookup direction: source, target, both.")] = "both",
    limit: Annotated[int, Field(description="Max results.")] = 50,
) -> str:
    """Read from the mapping store.

    kind controls what is returned:
    - mapping: search/list/lookup identifier mappings
    - module: list module mappings, or resolve a module name to a local path
    """
    from emend.knowledge import MappingStore, mapping_to_dict, module_mapping_to_dict

    store = MappingStore(".")

    if kind == "mapping":
        if identifier is not None:
            results = store.find_mappings_for(identifier, project=project, direction=direction)
            return json.dumps([mapping_to_dict(m) for m in results], indent=2)
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
        if module is not None:
            mm = store.resolve_module(module)
            if mm is None:
                return json.dumps({"error": f"No module mapping found for '{module}'."})
            result = module_mapping_to_dict(mm)
            resolved = store.resolve_module_to_path(module)
            if resolved:
                result["resolved_path"] = resolved
            return json.dumps(result, indent=2)
        results = store.list_module_mappings()
        return json.dumps([module_mapping_to_dict(m) for m in results], indent=2)

    return json.dumps({"error": f"Unknown kind '{kind}'. Use: mapping, module."})


@mcp_app.tool()
def map_write(
    kind: Annotated[str, Field(description="Entry type: 'mapping' or 'module'.")],
    op: Annotated[str, Field(description="Operation: 'add' or 'delete'.")],
    source_project: Annotated[str | None, Field(description="Mapping source project.")] = None,
    source_identifier: Annotated[str | None, Field(description="Mapping source identifier.")] = None,
    source_kind: Annotated[str | None, Field(description="Mapping source kind.")] = None,
    target_project: Annotated[str | None, Field(description="Mapping target project.")] = None,
    target_identifier: Annotated[str | None, Field(description="Mapping target identifier.")] = None,
    target_kind: Annotated[str | None, Field(description="Mapping target kind.")] = None,
    relationship: Annotated[str | None, Field(description="Mapping relationship: equivalent, calls, implements, produces, consumes.")] = None,
    confidence: Annotated[float | None, Field(description="Mapping confidence 0–1.")] = None,
    provenance: Annotated[str | None, Field(description="Provenance: manual, llm, heuristic.")] = None,
    evidence: Annotated[str | None, Field(description="Mapping evidence text.")] = None,
    module_prefix: Annotated[str | None, Field(description="Module prefix (module kind).")] = None,
    repo: Annotated[str | None, Field(description="GitHub repo org/name (module kind).")] = None,
    local_path: Annotated[str | None, Field(description="Local path (module kind).")] = None,
    branch: Annotated[str | None, Field(description="Branch/tag (module kind).")] = None,
    subpath: Annotated[str | None, Field(description="Subpath within repo (module kind).")] = None,
    metadata: Annotated[dict | None, Field(description="Additional metadata dict.")] = None,
) -> str:
    """Write to the mapping store: add or delete entries.

    kind + op selects the operation:
    - mapping + add: requires source_project, source_identifier, target_project, target_identifier
    - mapping + delete: requires source_identifier
    - module + add: requires module_prefix, and one of repo or local_path
    - module + delete: requires module_prefix
    """
    from emend.knowledge import (
        MappingStore, IdentifierMapping, ModuleMapping,
        mapping_to_dict, module_mapping_to_dict,
    )

    store = MappingStore(".")

    if kind == "mapping":
        if op == "add":
            if not source_project or not source_identifier or not target_project or not target_identifier:
                return json.dumps({"error": "source_project, source_identifier, target_project, target_identifier required."})
            m = IdentifierMapping(
                source_project=source_project,
                source_identifier=source_identifier,
                source_kind=source_kind or "",
                target_project=target_project,
                target_identifier=target_identifier,
                target_kind=target_kind or "",
                relationship=relationship or "equivalent",
                confidence=confidence if confidence is not None else 1.0,
                provenance=provenance or "llm",
                evidence=evidence or "",
                metadata=metadata or {},
            )
            store.add_mapping(m)
            return json.dumps(mapping_to_dict(m), indent=2)
        if op == "delete":
            if not source_identifier:
                return json.dumps({"error": "source_identifier is required for delete."})
            ok = store.delete_mapping(
                source_identifier,
                source_project=source_project,
                target_identifier=target_identifier,
            )
            return json.dumps({"deleted": ok, "source_identifier": source_identifier})

    elif kind == "module":
        if op == "add":
            if not module_prefix:
                return json.dumps({"error": "module_prefix is required."})
            if not repo and not local_path:
                return json.dumps({"error": "Either repo or local_path is required."})
            m = ModuleMapping(
                module_prefix=module_prefix,
                repo=repo or "", local_path=local_path or "",
                branch=branch or "", subpath=subpath or "",
                provenance=provenance or "llm",
                metadata=metadata or {},
            )
            store.add_module_mapping(m)
            return json.dumps(module_mapping_to_dict(m), indent=2)
        if op == "delete":
            if not module_prefix:
                return json.dumps({"error": "module_prefix is required for delete."})
            ok = store.delete_module_mapping_by_prefix(module_prefix)
            return json.dumps({"deleted": ok, "module_prefix": module_prefix})

    else:
        return json.dumps({"error": f"Unknown kind '{kind}'. Use: mapping, module."})

    return json.dumps({"error": f"Unknown op '{op}'. Use: add, delete."})


# ---------------------------------------------------------------------------
# grammar_and_cookbook
# ---------------------------------------------------------------------------


@mcp_app.tool()
def grammar_and_cookbook() -> str:
    """Return the full emend grammar reference and cookbook.

    Call this tool when you need detailed syntax help for constructing
    selectors, patterns, or command invocations.  The response covers
    selector syntax, pattern metavariables, every command with examples,
    and common refactoring recipes.
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
        r"\.\. literalinclude:: [^\n]+\n(?:   :[^\n]+\n)*",
        _inline,
        text,
    )
    return text


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


def run_server(transport: str = "stdio", port: int = 8000) -> None:
    """Start the MCP server."""
    if transport not in ("stdio", "sse"):
        raise ValueError(f"Unknown transport: {transport!r}. Use 'stdio' or 'sse'.")

    # Kick off cache warming in a background process so the first tool
    # call doesn't pay the full indexing cost.
    _warm_caches_background()

    mcp_app.settings.port = port
    mcp_app.run(transport=transport)
