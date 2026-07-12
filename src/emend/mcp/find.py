"""MCP find/search tools."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from typing import Annotated, Any

from pydantic import Field

from emend.component_selector import parse_extended_selector, parse_selector
from emend.transform import find_pattern, cmd_lookup
from emend import ast_commands

from emend.mcp.dispatch import mcp_app


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
    from emend.cli_base import resolve_files, resolve_file_scopes, parse_where_clause, detect_query_shape

    mode = (mode or "code").lower()
    if mode not in {"code", "symbol", "summary", "auto"}:
        return json.dumps({"error": f"Unknown mode {mode!r}. Use: code, symbol, summary, auto."})

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
            file_for_summary, sym_part = parse_selector(query)
            selector_for_summary = sym_part or None

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
            except (FileNotFoundError, UnicodeDecodeError):
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
            file_or_pattern, _ = parse_selector(query)
        elif is_line_selector:
            m = _re.search(r"^(.+?):\d+", query)
            if m:
                file_or_pattern = m.group(1)
    elif query:
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
