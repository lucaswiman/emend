import logging
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from emend import ast_commands
from emend.cli_base import (
    _is_source_file_query,
    _maybe_create_oracle,
    _state,
    app,
    detect_query_shape,
    parse_where_clause,
    resolve_file_scopes,
    resolve_files,
)
from emend.component_selector import parse_extended_selector
from emend.transform import cmd_lookup, find_pattern_in_project

_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_ANSI_MAGENTA = "\033[35m"
_ANSI_GREEN = "\033[32m"
_ANSI_CYAN = "\033[36m"
_ANSI_RED_BOLD = "\033[1;31m"


def _extract_dsl_symbols_from_region(region):
    """Extract DSL symbols from a single region based on its DSL type."""
    from emend.dsl import DslKind, extract_graphql_symbols, extract_jinja_symbols, extract_sql_symbols
    if region.dsl == DslKind.SQL:
        return extract_sql_symbols(region)
    elif region.dsl == DslKind.JINJA:
        return extract_jinja_symbols(region)
    elif region.dsl == DslKind.GRAPHQL:
        return extract_graphql_symbols(region)
    return []


def _emit_dsl_overlay(
    explicit_files: list | None,
    language: str,
    fallback_path: str,
    *,
    search_term: str | None = None,
) -> None:
    """Print DSL symbols found in the resolved file set.

    When *search_term* is given, only symbols whose name overlaps with
    the term are printed (used in lookup mode).
    """
    from emend.dsl import detect_dsl_regions

    if explicit_files:
        dsl_files, _ = resolve_file_scopes(explicit_files, language=language)
    else:
        dsl_files, _ = resolve_files(fallback_path, language=language)
    for f in dsl_files:
        for region in detect_dsl_regions(str(f)):
            for sym in _extract_dsl_symbols_from_region(region):
                if search_term and not (search_term in sym.name or sym.name in search_term):
                    continue
                print(
                    f"{sym.host_file}:{sym.host_line}:{sym.host_col}  "
                    f"[{sym.dsl.value}:{sym.kind.value}]  {sym.name}",
                    flush=True,
                )


def _print_pattern_match_code(
    file_path_str: str,
    match,
    file_lines_cache: dict[str, list[str]],
    *,
    is_tty: bool = False,
) -> None:
    """Print a pattern match with a file:line header followed by matched source lines."""
    if match.line is None:
        if is_tty:
            print(
                f"{_ANSI_MAGENTA}{file_path_str}{_ANSI_CYAN}:{_ANSI_GREEN}?{_ANSI_RESET}",
                flush=True,
            )
        else:
            print(f"{file_path_str}:?", flush=True)
        return

    start_line = match.line
    end_line = match.end_line or start_line

    if file_path_str not in file_lines_cache:
        try:
            file_lines_cache[file_path_str] = Path(file_path_str).read_text().splitlines()
        except Exception:
            file_lines_cache[file_path_str] = []
    lines = file_lines_cache[file_path_str]

    line_range = str(start_line) if start_line == end_line else f"{start_line}-{end_line}"
    if is_tty:
        print(
            f"{_ANSI_MAGENTA}{file_path_str}{_ANSI_CYAN}:{_ANSI_GREEN}{line_range}{_ANSI_RESET}",
            flush=True,
        )
    else:
        print(f"{file_path_str}:{line_range}", flush=True)

    col = match.col
    end_col_val = match.end_col
    for i in range(start_line, min(end_line + 1, len(lines) + 1)):
        line_text = lines[i - 1] if i <= len(lines) else ""
        if is_tty and col is not None and end_col_val is not None:
            if start_line == end_line:
                hl_start = col
                hl_end = end_col_val
            elif i == start_line:
                hl_start = col
                hl_end = len(line_text)
            elif i == end_line:
                hl_start = 0
                hl_end = end_col_val
            else:
                hl_start = 0
                hl_end = len(line_text)
            hl_start = max(0, min(hl_start, len(line_text)))
            hl_end = max(hl_start, min(hl_end, len(line_text)))
            before = line_text[:hl_start]
            highlighted = line_text[hl_start:hl_end]
            after = line_text[hl_end:]
            print(f"{before}{_ANSI_RED_BOLD}{highlighted}{_ANSI_RESET}{after}", flush=True)
        else:
            print(line_text, flush=True)

@app.command("find")
def search(
    query: Annotated[str, typer.Argument(help="Pattern with $X metavars, selector (file.py::sym), or file/dir path")],
    files: Annotated[
        Optional[list[str]],
        typer.Argument(help="File, glob, or directory scope(s) to search")
    ] = None,
    kind: Annotated[
        Optional[list[str]],
        typer.Option("--kind", help="Symbol kind filter (function, method, class, async_*)")
    ] = None,
    name: Annotated[
        Optional[list[str]],
        typer.Option("--name", help="Name pattern filter (glob or /regex/)")
    ] = None,
    returns: Annotated[
        Optional[list[str]],
        typer.Option("--returns", help="Return type filter")
    ] = None,
    depth: Annotated[
        Optional[list[str]],
        typer.Option("--depth", help="Nesting depth filter (lookup mode) or display depth limit (summary mode)")
    ] = None,
    has_param: Annotated[
        Optional[list[str]],
        typer.Option("--has-param", help="Parameter filter")
    ] = None,
    case_insensitive: Annotated[
        bool,
        typer.Option("-i", help="Case-insensitive matching")
    ] = False,
    smart_case: Annotated[
        bool,
        typer.Option("--smart-case", help="Match naming convention variants")
    ] = False,
    output: Annotated[
        Optional[str],
        typer.Option("--output", "-o", help=(
            "Output format: code, location, selector, summary, metadata, json, count, "
            "summary::flat, code::dedent"
        ))
    ] = None,
    within: Annotated[
        Optional[str],
        typer.Option("--within", help="Structural containment pattern, e.g. 'def test_*' or 'class MyClass'")
    ] = None,
    not_within: Annotated[
        Optional[str],
        typer.Option("--not-within", help="Exclude matches inside this structural pattern")
    ] = None,
    matching: Annotated[
        Optional[str],
        typer.Option("--matching", help="Lookup-mode body pattern or decorator filter, e.g. 'print($X)' or '@app.command'")
    ] = None,
    where: Annotated[
        Optional[list[str]],
        typer.Option("--where", help=(
            "Filter/scope constraint. Syntax auto-detected: "
            "'def test_*' (structural), 'not class' (negation), "
            "'MyClass.method' (scope), '@decorator' (decorator), "
            "'print($X)' (body pattern), 'class Foo' (in-class lookup)"
        ))
    ] = None,
    imported_from: Annotated[
        Optional[str],
        typer.Option("--imported-from", help="Only match when root name is imported from this module (pattern mode)")
    ] = None,
    scope_local: Annotated[
        bool,
        typer.Option("--scope-local", help="Only match locally-defined names, exclude imports (pattern mode)")
    ] = False,
    type_engine: Annotated[
        Optional[str],
        typer.Option("--type-engine", help="Type inference engine for :type[X] and :returns[X] constraints: auto, pyrefly, pyright, ty")
    ] = None,
    limit: Annotated[
        Optional[int],
        typer.Option("--limit", help="Maximum number of results to return (useful for completion)")
    ] = None,
    complete: Annotated[
        Optional[str],
        typer.Option("--complete", help="Symbol name prefix for typeahead completion (returns JSON)")
    ] = None,
    project: Annotated[
        Optional[str],
        typer.Option("--project", help="Project root for index-based queries")
    ] = None,
    include_map: Annotated[
        bool,
        typer.Option("--include-map", help="Take symbol / module mappings into account for resolution")
    ] = False,
    dsl: Annotated[
        Optional[str],
        typer.Option("--dsl", help="Search inside embedded DSL regions (sql, css, html)")
    ] = None,
):
    """Unified search: auto-detects pattern matching vs symbol lookup.

    Canonical syntax is::

        emend find [FLAGS] QUERY [FILES...]

    The :: separator splits file scope (left) from query (right). The right
    side is auto-detected as a pattern or selector:
    - Contains $ metavariables → pattern mode
    - Parses as valid selector (identifiers, dots, components) → selector mode
    - Doesn't parse as selector (parens, operators, etc.) → pattern mode

    Without ::, queries containing $ use pattern mode, bare names use symbol
    lookup, and file/dir paths use summary mode.

    Output formats (--output):
        code          Matched code with file:line header [default for pattern and selector]
        location      file.py:line only
        selector      file.py::Symbol.path
        summary       Symbol tree with signatures [default for bare file/dir]
        metadata      Per-symbol detail: lines, offset, kind, decorators, params
        json          Structured JSON output
        count         Number of matches only
        summary::flat Flat list with full dotted paths (summary mode)
        code::dedent  Dedented source code (lookup mode)

    --where syntax:
        'def test_*'      Structural containment (pattern mode)
        'not class'       Exclude structural container (pattern mode)
        'MyClass.method'  Limit to named scope (pattern mode)
        '@decorator'      Decorator filter (lookup mode)
        'print($X)'       Body pattern filter (lookup mode)
        'class MyClass'   In-class filter (lookup mode) or containment (pattern mode)

    Examples:
        # Pattern mode (has $):
        emend find 'print($X)' file.py
        emend find 'assertEqual($A, $B)' tests/ --output count

        # Pattern mode with file scope (:: separator):
        emend find '**::print($X)'
        emend find 'src/::assert False'
        emend find 'file.py::print()'

        # Lookup mode (valid selector after ::):
        emend find file.py::func[params]
        emend find src/ --kind function --matching '@app.command'

        # Bare name (auto-detects as symbol search):
        emend find process_encounter
        emend find ::MyClass.method
        emend find MyClass src/

        # Summary mode (list symbols):
        emend find file.py
        emend find file.py::MyClass --output summary
        emend find file.py --output summary::flat
    """
    import re as _re
    explicit_files = list(files or [])

    # --complete mode: fast typeahead from symbol_index
    if complete is not None:
        from emend.transform import query_symbol_index
        proj = project or "."
        pattern = f"{complete}*" if not any(c in complete for c in "*?") else complete
        results = query_symbol_index(
            proj,
            name_pattern=pattern,
            kind=kind[0] if kind else None,
            limit=limit or 20,
        )
        if results is None:
            # Index not available — fall through to normal search
            typer.echo("[]")
            return
        import json as _json
        typer.echo(_json.dumps(results, indent=2))
        return

    # --dsl mode: search inside embedded DSL regions
    if dsl is not None:
        from emend.dsl import find_in_dsl
        if len(explicit_files) > 1:
            raise ValueError("--dsl currently accepts at most one file scope.")
        target_path = explicit_files[0] if explicit_files else query
        _lang = _state["language"]
        _dsl_files, _ = resolve_files(target_path, language=_lang)

        # Determine the search pattern
        if "$" in query:
            search_pattern = query
        elif explicit_files:
            # query is a literal term to search for in DSL regions
            search_pattern = None  # will use string contains instead
        else:
            # query is the path, list all DSL regions
            search_pattern = None

        _out = output or "code"
        total_matches = 0
        _literal_term = query.lower() if search_pattern is None and explicit_files else None

        for dsl_f in _dsl_files:
            if search_pattern is not None:
                # Pattern mode: use find_in_dsl
                matches = find_in_dsl(search_pattern, str(dsl_f), dsl_type=dsl.lower())
                for m in matches:
                    total_matches += 1
                    if _out == "json":
                        import json as _dsl_json
                        print(_dsl_json.dumps({
                            "file": m.host_file,
                            "line": m.host_line,
                            "col": m.host_col,
                            "dsl": m.dsl.value,
                            "matched_text": m.matched_text,
                            "captures": m.captures,
                        }))
                    elif _out == "location":
                        print(f"{m.host_file}:{m.host_line}:{m.host_col}")
                    else:
                        print(f"{m.host_file}:{m.host_line}:{m.host_col}  [{m.dsl.value}]  {m.matched_text}")
                    if limit and total_matches >= limit:
                        break
            else:
                # Literal search: find DSL regions containing the term
                from emend.dsl import detect_dsl_regions as _dsl_detect, DslKind as _DslK
                regions = _dsl_detect(str(dsl_f))
                for region in regions:
                    if region.dsl.value != dsl.lower():
                        continue
                    if _literal_term and _literal_term not in region.content.lower():
                        continue
                    total_matches += 1
                    if _out == "location":
                        print(f"{region.host_file}:{region.host_start_line}:{region.host_start_col}")
                    else:
                        # Show the matching region content
                        display = region.content.strip().replace('\n', ' ')[:120]
                        print(f"{region.host_file}:{region.host_start_line}:{region.host_start_col}  [{region.dsl.value}]  {display}")
                    if limit and total_matches >= limit:
                        break
            if limit and total_matches >= limit:
                break
        return

    # Parse --where values
    where_params = parse_where_clause(where or [])
    where_scope = where_params.get("scope")
    where_inside = where_params.get("inside")
    where_not_inside = where_params.get("not_inside")
    where_matching = where_params.get("matching")
    effective_within = within or where_inside
    effective_not_within = not_within or where_not_inside
    effective_matching = matching or where_matching

    # Parse --output for :: modifier
    output_base = output
    output_modifier = None
    if output and "::" in output:
        parts = output.split("::", 1)
        output_base = parts[0]
        output_modifier = parts[1]

    # Detect query shape (shared logic with MCP server)
    _shape = detect_query_shape(query, explicit_files[0] if explicit_files else None)
    query = _shape.query
    path = _shape.path
    is_pattern_mode = _shape.is_pattern_mode
    has_selector = _shape.has_selector
    is_line_selector = _shape.is_line_selector

    if include_map and "::" not in query:
        from emend.knowledge import MappingStore
        store = MappingStore(project or ".")
        resolved_query = store.resolve_selector(query)
        if resolved_query and resolved_query != query:
            # Re-detect shape with resolved query
            query = resolved_query
            _shape = detect_query_shape(query, explicit_files[0] if explicit_files else None)
            query = _shape.query
            path = _shape.path
            is_pattern_mode = _shape.is_pattern_mode
            has_selector = _shape.has_selector
            is_line_selector = _shape.is_line_selector

    # If query looks like a file path/glob/directory (no $ or ::) but --where
    # provides a pattern (has $), the user likely intended:
    #   emend search 'Union[$X, $Y]' myproject/**/*.py
    # rather than the (broken) lookup-mode interpretation.
    # Swap them and activate pattern mode.
    if not is_pattern_mode and not has_selector and not is_line_selector:
        if effective_matching and "$" in effective_matching:
            _fop = Path(query)
            if _fop.is_dir() or '*' in query or '?' in query:
                explicit_files = [query]
                path = None
                query = effective_matching
                effective_matching = None
                is_pattern_mode = True

        # Bare name fallback: if query doesn't match a file/dir/glob,
        # treat it as a symbol name and search across Python files.
        if not is_pattern_mode:
            _query_path = Path(query)
            if (not _query_path.exists()
                    and '/' not in query
                    and not _is_source_file_query(query)
                    and not ('*' in query or '?' in query)):
                scope_hint = explicit_files[0] if explicit_files else path
                if scope_hint:
                    _p = Path(scope_hint)
                    file_scope = str(_p / '**') if _p.is_dir() else scope_hint
                    path = None
                    explicit_files = []
                else:
                    file_scope = '**'
                query = f'{file_scope}::{query}'
                has_selector = True

    # Build lookup-mode filters from --where
    lookup_has_decorator: Optional[list[str]] = None
    lookup_in_class: Optional[list[str]] = None
    lookup_matching: Optional[str] = None
    if effective_matching is not None:
        if effective_matching.startswith("@"):
            lookup_has_decorator = [effective_matching[1:]]
        else:
            lookup_matching = effective_matching
    if effective_within is not None and effective_within.startswith("class "):
        lookup_in_class = [effective_within[6:].strip()]

    if explicit_files and not is_pattern_mode and len(explicit_files) > 1:
        raise ValueError("Multiple file scopes are only supported for pattern searches.")

    if explicit_files:
        path = explicit_files[0] if len(explicit_files) == 1 else None

    has_filters = bool(
        kind or name or lookup_has_decorator or returns or lookup_in_class
        or depth or has_param or lookup_matching
    )

    # Check if selector has a component (file::sym[comp])
    has_component = False
    if has_selector:
        try:
            _parsed_sel = parse_extended_selector(query)
            has_component = _parsed_sel.component is not None
        except Exception:
            pass

    # Determine effective output format
    json_output = (output_base == "json")
    count_output = (output_base == "count")
    dedent_output = (output_modifier == "dedent")
    flat_output = (output_modifier == "flat")

    if output_base is not None and output_base not in ("json", "count"):
        effective_output = output_base
    elif json_output or count_output:
        effective_output = "code"
    elif is_pattern_mode:
        effective_output = "code"
    elif has_component:
        effective_output = "component"
    elif has_selector or is_line_selector:
        effective_output = "code"
    elif not has_filters:
        effective_output = "summary"
    else:
        effective_output = "selector"

    try:
        # ---- Create TypeOracle if needed ----
        oracle = None
        if type_engine is not None or (is_pattern_mode and (":type[" in query or ":returns[" in query)):
            oracle = _maybe_create_oracle(type_engine)

        # ---- SUMMARY MODE ----
        if effective_output == "summary" and not is_pattern_mode:
            unsupported = []
            if returns:
                unsupported.append("--returns")
            if has_param:
                unsupported.append("--has-param")
            if unsupported:
                raise ValueError(
                    f"Filter(s) {', '.join(unsupported)} not supported with --output=summary. "
                    "Use --output=selector instead."
                )

            # depth in summary mode = tree_depth
            tree_depth = int(depth[0]) if depth else None

            file_for_summary = query
            selector_for_summary = None
            if has_selector:
                parts = query.split('::', 1)
                file_for_summary = parts[0]
                selector_for_summary = parts[1] or None

            file_path_obj = Path(file_for_summary)
            _lang = _state["language"]
            if file_path_obj.is_dir() or '*' in file_for_summary or '?' in file_for_summary:
                files, _ = resolve_files(file_for_summary, language=_lang)
                from emend import emend_core
                from emend.transform import _find_source_root
                from emend.language_registry import get_extensions, get_module_separator

                # Single resolver for batch — extract the directory portion
                # (for globs like "src/*.py", use the literal parent directory)
                if '*' in file_for_summary or '?' in file_for_summary:
                    _base_dir = str(Path(file_for_summary.split('*')[0].split('?')[0]).parent)
                    if not Path(_base_dir).is_dir():
                        _base_dir = "."
                elif file_path_obj.is_file():
                    _base_dir = str(file_path_obj.parent)
                else:
                    _base_dir = str(file_path_obj)
                proj_root = _find_source_root(_base_dir, language=_lang)

                # Use the first file's extension if available, or first extension from language
                ext = None
                if files:
                    ext = files[0].suffix.lstrip('.')
                if not ext:
                    exts = get_extensions(_lang)
                    if exts:
                        ext = exts[0]

                resolver = emend_core.PyScopeResolver(str(proj_root), extension=ext)
                sep = get_module_separator(_lang)

                for fp in files:
                    file_str = str(fp)
                    try:
                        source = fp.read_text()
                        resolver.index_file(file_str, source)

                        symbol_dicts = resolver.get_symbols(file_str)

                        # Derive module path
                        try:
                            rel_path = fp.relative_to(proj_root)
                            parts = list(rel_path.parts)
                            if parts and parts[0] == "src":
                                parts.pop(0)
                            if parts:
                                parts[-1] = fp.stem
                            module_path = sep.join(parts)
                        except ValueError:
                            module_path = fp.stem

                        symbols = ast_commands.dicts_to_tree_symbols(symbol_dicts, module_path, separator=sep)
                        print(f"\nModule: {file_str}")
                        if symbols:
                            if flat_output:
                                ast_commands._print_symbol_flat(symbols, max_depth=tree_depth, separator=sep)
                            else:
                                ast_commands._print_symbol_tree(symbols, indent=1, max_depth=tree_depth)
                    except Exception as e:
                        logging.getLogger("emend.cli").warning("Failed to index %s: %s", file_str, e)
            else:
                if not file_path_obj.exists():
                    raise FileNotFoundError(f"No such file or directory: {file_for_summary!r}")
                symbols = ast_commands.collect_symbols(
                    file_for_summary, tree_depth=tree_depth, selector=selector_for_summary
                )
                print(f"\nModule: {file_for_summary}")
                if symbols:
                    if flat_output:
                        ast_commands._print_symbol_flat(symbols, max_depth=tree_depth)
                    else:
                        ast_commands._print_symbol_tree(symbols, indent=1, max_depth=tree_depth)
            return

        # ---- PATTERN MODE ----
        if is_pattern_mode:
            import time as _time
            _t_search_start = _time.monotonic()
            _logger = logging.getLogger("emend.search")
            target_path = path or "."
            _lang = _state["language"]

            _t0 = _time.monotonic()
            if explicit_files:
                resolved_files, is_multi_file = resolve_file_scopes(explicit_files, language=_lang)
                file_strs = [str(f) for f in resolved_files]
            else:
                target_obj = Path(target_path)
                if target_obj.is_dir() and _lang == "python":
                    # Fast path: get string list directly from Rust, skip Path creation
                    from emend import emend_core
                    file_strs = emend_core.collect_python_files(str(target_obj.resolve()))
                    is_multi_file = True
                else:
                    resolved_files, is_multi_file = resolve_files(target_path, language=_lang)
                    file_strs = [str(f) for f in resolved_files]
            _logger.info("resolve_files: %d files in %.3fs (%s)", len(file_strs), _time.monotonic() - _t0, ",".join(explicit_files) if explicit_files else target_path)

            # Build a lazy iterator over all matches, yielding as each file completes.
            # This allows streaming output rather than collecting everything first.
            def _iter_matches():
                project_matches = find_pattern_in_project(
                    query, file_strs,
                    scope=where_scope,
                    inside=effective_within,
                    not_inside=effective_not_within,
                    imported_from=imported_from,
                    scope_local=scope_local,
                    type_oracle=oracle,
                    language=_lang,
                )
                for pm in project_matches:
                    yield (pm.file_path, pm.match)

            if count_output:
                n_total = sum(1 for _ in _iter_matches())
                print(n_total)
                _logger.info("search total: %d matches in %.3fs", n_total, _time.monotonic() - _t_search_start)
            elif json_output:
                import json
                all_matches = list(_iter_matches())
                serialized_matches = []
                for file_path_str, match in all_matches:
                    code_str = (match.matched_text or "").strip()
                    serialized_matches.append({
                        "file": file_path_str,
                        "line": match.line,
                        "code": code_str,
                        "captures": match.captures
                    })
                print(json.dumps({"count": len(all_matches), "matches": serialized_matches}))
                _logger.info("search total: %d matches in %.3fs", len(all_matches), _time.monotonic() - _t_search_start)
            else:
                n_total = 0
                if effective_output == "selector":
                    from emend.ast_utils import find_nested_definitions, find_symbol_by_line
                    _defs_cache: dict[str, list] = {}
                    seen: set[str] = set()
                    for file_path_str, match in _iter_matches():
                        n_total += 1
                        if file_path_str not in _defs_cache:
                            _defs_cache[file_path_str] = find_nested_definitions(file_path_str)
                        if match.line is not None:
                            sym = find_symbol_by_line(_defs_cache[file_path_str], match.line)
                            if sym:
                                sel_path = f"{file_path_str}::{'.'.join(sym.path)}"
                                if sel_path not in seen:
                                    seen.add(sel_path)
                                    print(sel_path, flush=True)
                            else:
                                print(f"{file_path_str}:{match.line}", flush=True)
                        else:
                            print(f"{file_path_str}:?", flush=True)
                elif effective_output in ("location", "summary"):
                    for file_path_str, match in _iter_matches():
                        n_total += 1
                        if match.line is not None:
                            print(f"{file_path_str}:{match.line}", flush=True)
                        else:
                            print(f"{file_path_str}:?", flush=True)
                else:
                    # Default code display: header + matched source lines
                    _file_lines_cache: dict[str, list[str]] = {}
                    is_tty = sys.stdout.isatty()
                    for file_path_str, match in _iter_matches():
                        n_total += 1
                        _print_pattern_match_code(
                            file_path_str, match, _file_lines_cache,
                            is_tty=is_tty,
                        )
                _logger.info("search total: %d matches in %.3fs", n_total, _time.monotonic() - _t_search_start)

            # Only show DSL symbols when --dsl flag is explicitly provided.
            if dsl is not None:
                _emit_dsl_overlay(explicit_files, _state["language"], path or ".")

            return

        # ---- LOOKUP MODE ----
        file_or_pattern = query
        selector_str = None

        if has_selector or is_line_selector:
            selector_str = query
            if has_selector:
                parts = query.split('::', 1)
                file_or_pattern = parts[0]
            elif is_line_selector:
                m = _re.search(r'^(.+?):\d+', query)
                if m:
                    file_or_pattern = m.group(1)

        use_paths_only = (effective_output == "selector") and not json_output and not count_output
        use_metadata = (effective_output == "metadata")

        result = cmd_lookup(
            file_or_pattern=file_or_pattern,
            selector_str=selector_str,
            kind=kind,
            name=name,
            has_decorator=lookup_has_decorator,
            returns=returns,
            in_class=lookup_in_class,
            depth=depth,
            has_param=has_param,
            case_insensitive=case_insensitive,
            smart_case=smart_case,
            json_output=json_output,
            metadata=use_metadata,
            paths_only=use_paths_only,
            count=count_output,
            dedent=dedent_output,
            matching=lookup_matching,
            type_oracle=oracle,
            out=sys.stdout,
        )
        if result:
            print(result, end='')

        # Only show DSL symbols when --dsl flag is explicitly provided.
        if dsl is not None:
            _search_term = (selector_str or query).split("::")[-1].strip().lower() if (selector_str or query) else ""
            _emit_dsl_overlay(explicit_files, _state["language"], path or file_or_pattern or ".", search_term=_search_term)

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(2)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)
