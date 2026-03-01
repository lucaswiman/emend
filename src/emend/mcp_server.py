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
from typing import Any

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
)
from emend import ast_commands

mcp_app = FastMCP(
    "emend",
    instructions="""\
emend is a Python refactoring tool with structured edits and pattern transforms.
Use these tools to search, edit, and refactor Python code.
All write operations (edit, add, replace, rename, move) default to dry-run mode
showing diffs. Set apply=True to write changes to disk.

## Selector syntax

Selectors identify symbols and their components in Python files.

### Symbol selectors
  file.py::func                  # module-level function
  file.py::Class                 # class
  file.py::Class.method          # method
  file.py::Class.method.nested   # nested function inside a method

### Extended selectors (with components)
  file.py::func[params]          # all parameters
  file.py::func[params][ctx]     # parameter by name
  file.py::func[params][0]       # parameter by index
  file.py::func[returns]         # return annotation
  file.py::func[decorators]      # decorator list
  file.py::Class[bases]          # base classes
  file.py::func[body]            # function body

### Pseudo-class selectors
  file.py::func[params]:KEYWORD_ONLY       # keyword-only parameter slot
  file.py::func[params]:POSITIONAL_ONLY    # positional-only parameter slot

### Wildcard selectors
  file.py::*[params]             # all function parameters in file
  file.py::Test*[decorators]     # symbols starting with "Test"
  file.py::*.*[returns]          # all method return types
  file.py::Class.*[body]         # all method bodies in Class

### Line selectors
  file.py:42                     # single line
  file.py:42-100                 # line range

### File globs
  'src/**/*.py::func'            # match across files

## Pattern syntax

Patterns match code structures using metavariables (prefixed with $).

### Metavariables
  $X                # capture any single expression
  $NAME             # named capture (uppercase)
  $_                # anonymous (match but don't capture)
  $...ARGS          # capture variable number of arguments (ellipsis)
  $X:int            # type-constrained (int, str, float, expr, stmt, identifier, call, attr)

### Examples
  print($X)                     # function call with one arg
  func($A, $B)                  # call with two args
  func($...ARGS)                # call with any number of args
  $A + $B                       # binary operation
  $X[$Y]                        # subscript
  return $X                     # return statement
  assert $A == $B               # assert with comparison
  [$X, $Y]                      # list with two elements

### String content interpolation
  ${X.content}                  # in replacement: strips quotes from a captured string literal

## Where constraints (search and replace)

The where parameter filters by scope/context:
  'def'                         # inside any function
  'async def'                   # inside any async function
  'class'                       # inside any class
  'def test_*'                  # inside functions matching glob
  'MyClass.method'              # inside a specific method
  'not class'                   # NOT inside a class
  'not def test_*'              # NOT inside test functions
  '@decorator'                  # inside decorated symbols

## Output formats (search)

  code       # matched source code (default for lookup)
  location   # file:line (default for pattern mode)
  selector   # emend selectors
  summary    # symbol tree (default for bare file/dir)
  metadata   # detailed symbol metadata
  json       # JSON output
  count      # match count only
  code::dedent    # dedented source
  summary::flat   # flat symbol list
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
    query: str,
    path: str | None = None,
    kind: str | None = None,
    name: str | None = None,
    returns: str | None = None,
    depth: str | None = None,
    has_param: str | None = None,
    output: str = "code",
    where: str | None = None,
    imported_from: str | None = None,
    scope_local: bool = False,
    case_insensitive: bool = False,
    smart_case: bool = False,
) -> str:
    """Search for code patterns or symbols in Python files.

    Three modes (auto-detected from query):
    - Pattern mode: query contains $X metavariables (e.g. 'print($X)')
    - Lookup mode:  query contains :: or is file:line (e.g. 'file.py::func')
    - Summary mode:  bare file/directory path lists symbols

    Args:
        query: Pattern with $X metavars, selector (file.py::sym), or file/dir.
        path:  File, glob, or directory for pattern mode search scope.
        kind:  Symbol kind filter: function, method, class, async_function, async_method.
        name:  Name pattern filter (glob like 'test_*' or /regex/).
        returns: Return type filter.
        depth: Nesting depth filter (lookup) or display depth (summary).
        has_param: Parameter filter.
        output: Output format: code, location, selector, summary, metadata, json, count.
        where: Scope/filter constraint: 'def test_*', 'not class', 'MyClass.method', '@decorator', 'print($X)'.
        imported_from: Only match when root name is imported from this module.
        scope_local: Only match locally-defined names, exclude imports.
        case_insensitive: Case-insensitive matching.
        smart_case: Match naming convention variants.

    Returns:
        Search results in the requested output format.
    """
    import re as _re
    from emend.cli import resolve_files, parse_where_clause

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

    is_pattern_mode = "$" in query
    is_line_selector = _re.search(r":\d+(-\d+)?$", query) is not None
    has_selector = "::" in query and not is_pattern_mode

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
        target_path = path or "."
        import libcst as cst

        files, is_multi_file = resolve_files(target_path)
        all_matches: list[tuple[str, Any]] = []
        for file_path in files:
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
                if match.matched_text is not None:
                    code_str = match.matched_text.strip()
                else:
                    code_str = cst.Module([]).code_for_node(match.node).strip()
                captures = {}
                for cap_name, captured in match.captures.items():
                    if isinstance(captured, tuple):
                        items = [cst.Module([]).code_for_node(item).strip() for item in captured]
                        captures[cap_name] = ", ".join(items)
                    else:
                        captures[cap_name] = cst.Module([]).code_for_node(captured).strip()
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
    pattern: str,
    replacement: str,
    path: str,
    apply: bool = False,
    where: str | None = None,
) -> str:
    """Replace pattern matches in Python file(s).

    Supports $X metavariables in both pattern and replacement.
    By default shows a diff (dry-run). Set apply=True to write changes.

    Args:
        pattern: Pattern to find (e.g. 'print($X)').
        replacement: Replacement pattern (e.g. 'logger.info($X)').
        path: Python file, glob, or directory to modify.
        apply: If True, write changes to disk. Default is dry-run.
        where: Scope constraint: 'def test_*', 'not class', 'MyClass.method'.

    Returns:
        Unified diff showing the changes (or empty string if no matches).
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
# edit
# ---------------------------------------------------------------------------


@mcp_app.tool()
def edit(
    selector: str,
    value: str | None = None,
    rm: bool = False,
    apply: bool = False,
) -> str:
    """Edit or remove existing symbol components.

    Args:
        selector: Symbol selector (e.g. 'file.py::func[returns]', 'file.py::Class.method[params][x]').
        value: New value for the component (omit when using rm=True).
        rm: If True, remove the component or entire symbol.
        apply: If True, write changes to disk. Default is dry-run.

    Returns:
        Unified diff showing the changes.
    """
    return cmd_edit(selector_str=selector, value=value, rm=rm, apply=apply)


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


@mcp_app.tool()
def add(
    selector: str,
    value: str,
    before: str | None = None,
    after: str | None = None,
    at: int | None = None,
    apply: bool = False,
) -> str:
    """Add new items to symbol components.

    Args:
        selector: Symbol selector (e.g. 'file.py::func[params]', 'file.py::Class[bases]').
        value: Value to add (e.g. 'ctx: Context', 'BaseClass').
        before: Insert before this named item.
        after: Insert after this named item.
        at: Insert at this position (0-indexed).
        apply: If True, write changes to disk. Default is dry-run.

    Returns:
        Unified diff showing the changes.
    """
    return cmd_add(
        selector_str=selector,
        value=value,
        before=before,
        after=after,
        at=at,
        apply=apply,
    )


# ---------------------------------------------------------------------------
# refs (find references)
# ---------------------------------------------------------------------------


@mcp_app.tool()
def refs(
    selector: str,
    exclude_definition: bool = False,
    exclude_imports: bool = False,
    writes_only: bool = False,
    reads_only: bool = False,
    calls_only: bool = False,
    project: str | None = None,
) -> str:
    """Find all references to a symbol across the project.

    Args:
        selector: Symbol selector (e.g. 'file.py::func_name').
        exclude_definition: Exclude the definition itself from results.
        exclude_imports: Exclude import statements from results.
        writes_only: Only show write (assignment) references.
        reads_only: Only show read (load) references.
        calls_only: Only show actual call sites.
        project: Project root directory.

    Returns:
        References as file:line entries (one per line).
    """
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
    selector: str,
    to: str,
    apply: bool = False,
    docs: bool = False,
    no_hierarchy: bool = False,
    unsure: bool = False,
    project: str | None = None,
) -> str:
    """Rename a symbol or module across the project.

    If selector contains '::', renames a symbol. Otherwise renames a module.

    Args:
        selector: Symbol selector (file.py::name) or module path (file.py).
        to: New name.
        apply: If True, write changes to disk. Default is dry-run.
        docs: Also rename in docstrings (symbol mode only).
        no_hierarchy: Don't rename in class hierarchy (symbol mode only).
        unsure: Rename uncertain occurrences (symbol mode only).
        project: Project root directory.

    Returns:
        Unified diff showing the changes.
    """
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
    selector: str,
    destination: str,
    dedent: bool = False,
    no_update_imports: bool = False,
    apply: bool = False,
    project: str | None = None,
) -> str:
    """Move a symbol or module with automatic import updates.

    If selector contains '::', moves a symbol. Otherwise moves a module.

    Args:
        selector: Symbol selector (file.py::name) or module path (file.py).
        destination: Destination file or package.
        dedent: Dedent nested symbols (symbol mode only).
        no_update_imports: Don't update imports (symbol mode only).
        apply: If True, write changes to disk. Default is dry-run.
        project: Project root directory (module mode only).

    Returns:
        Unified diff showing the changes.
    """
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
    file_path: str,
    format: str = "json",
    project: str | None = None,
) -> str:
    """Generate a call graph for functions in a Python file.

    Args:
        file_path: Python file to analyze.
        format: Output format: plain, json, or dot (Graphviz).
        project: Project root directory.

    Returns:
        Call graph in the requested format.
    """
    return generate_graph(file_path, project_path=project, format=format)


# ---------------------------------------------------------------------------
# deadcode
# ---------------------------------------------------------------------------


@mcp_app.tool()
def deadcode(
    path: str = ".",
    kind: str | None = None,
    include_private: bool = False,
    exclude_references_from: list[str] | None = None,
    no_strings: bool = False,
    no_last_reference: bool = False,
    all_files: bool = False,
) -> str:
    """Find potentially dead (unreferenced) code in a project.

    Scans Python files and reports top-level symbols with no external
    references. Uses scope-aware analysis with QualifiedNameProvider.

    Automatically skips dunder methods, test functions, decorated entry
    points, __all__ members, and conventional entry points.

    Args:
        path: Project directory to scan.
        kind: Symbol kind filter: function, class.
        include_private: Include _private symbols.
        exclude_references_from: Directories to ignore when scanning for references.
        no_strings: Don't count string literals as references.
        no_last_reference: Don't show git last-reference info.
        all_files: Scan all Python files, not just git-tracked ones.

    Returns:
        JSON array of dead code findings.
    """
    results = find_dead_code(
        project_path=path,
        kind=kind,
        include_private=include_private,
        exclude_references_from=exclude_references_from,
        strings_count_as_references=not no_strings,
        show_last_reference=not no_last_reference,
        all_files=all_files,
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
    path: str,
    config: str | None = None,
    fix: bool = False,
    rule: str | None = None,
) -> str:
    """Lint Python files using pattern rules from a YAML config.

    Args:
        path: File or directory to lint.
        config: Path to patterns.yaml config file (default: .emend/patterns.yaml).
        fix: Auto-apply fix replacements.
        rule: Run only a specific rule by name.

    Returns:
        Lint violations (one per line) or empty string if clean.
    """
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
# copy_to
# ---------------------------------------------------------------------------


@mcp_app.tool()
def copy_to(
    selector: str,
    destination: str,
    append: bool = False,
    dedent: bool = False,
    apply: bool = False,
) -> str:
    """Copy a symbol to another file.

    Args:
        selector: Symbol selector (e.g. 'file.py::my_function').
        destination: Destination file path.
        append: Append to destination file instead of creating new.
        dedent: Dedent the copied symbol (useful for nested functions).
        apply: If True, write changes to disk. Default is dry-run.

    Returns:
        Diff showing what would be written/changed.
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        ast_commands.cmd_copy_to(selector, destination, append, dedent, apply)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_server(transport: str = "stdio", port: int = 8000) -> None:
    """Start the MCP server."""
    if transport not in ("stdio", "sse"):
        raise ValueError(f"Unknown transport: {transport!r}. Use 'stdio' or 'sse'.")
    mcp_app.settings.port = port
    mcp_app.run(transport=transport)
