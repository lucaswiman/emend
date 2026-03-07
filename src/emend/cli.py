"""emend - Python refactoring CLI with structured edits and pattern transforms."""

import glob as glob_mod
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import typer
from typing import Annotated

from emend.component_selector import parse_extended_selector
from emend.transform import (
    find_pattern, replace_pattern,
    find_references, rename_symbol, move_symbol,
    move_module, rename_module, cmd_lookup, cmd_edit, cmd_add,
    find_callers, generate_graph, find_dead_code,
    extract_pattern_literals, warm_caches,
    find_pattern_in_project,
)
from emend import ast_commands


def _maybe_create_oracle(type_engine: str | None):
    """Create a TypeOracle if *type_engine* is specified, returning ``None`` if unavailable."""
    from emend.type_oracle import create_type_oracle
    engine = type_engine or "auto"
    oracle = create_type_oracle(engine=engine)
    if not oracle.is_available():
        logging.getLogger("emend.type_oracle").warning(
            "Type engine '%s' not available; type constraints will have no effect", engine,
        )
        return None
    return oracle


def _reject_file_glob(selector_str: str, command_name: str) -> None:
    """Raise ValueError if selector contains file globs (for commands that don't support them)."""
    if '*' in selector_str.split('::')[0] or '?' in selector_str.split('::')[0]:
        raise ValueError(
            f"File glob selectors are not supported for {command_name}. "
            "Use a specific file path instead."
        )


def resolve_files(path: str, language: str = "python") -> tuple[list[Path], bool]:
    """Resolve a path argument to a list of source files.

    Args:
        path: A file path, directory, or glob pattern.
        language: Source language to filter by (default: "python").

    Returns:
        (files, is_multi_file) tuple.
    """
    from emend.language_registry import get_extensions, matches_language
    path_obj = Path(path)
    if path_obj.is_dir():
        from emend import emend_core
        abs_path = str(path_obj.resolve())
        exts = get_extensions(language)
        return [Path(f) for f in emend_core.collect_files(abs_path, exts)], True
    elif "*" in path or "?" in path:
        return [Path(f) for f in glob_mod.glob(path, recursive=True)
                if matches_language(f, language)], True
    else:
        return [path_obj], False



import re as _re_module

# Module-level state for options set in the app callback (e.g. --language).
_state: dict = {"language": "python"}


def _is_source_file_query(query: str) -> bool:
    """Return True if *query* ends with a known source file extension."""
    from emend.language_registry import is_source_file
    return is_source_file(query)


@dataclass
class QueryShape:
    """Result of detecting a query's mode (pattern, selector, or line)."""
    query: str
    path: str | None
    is_pattern_mode: bool
    has_selector: bool
    is_line_selector: bool


def detect_query_shape(query: str, path: str | None = None) -> QueryShape:
    """Detect whether a search query is a pattern, selector, or line selector.

    When ``::`` is present, splits the file scope (left) from the query (right):
    - Right side contains ``$`` → pattern mode (metavar search)
    - Right side parses as a valid selector → selector mode (symbol lookup)
    - Right side fails selector parse → pattern mode (literal code search)

    Without ``::``; queries containing ``$`` use pattern mode.

    Returns a ``QueryShape`` with possibly-modified ``query`` and ``path``.
    """
    is_line_selector = bool(_re_module.search(r':\d+(-\d+)?$', query))
    is_pattern_mode = False
    has_selector = False

    if '::' in query and not is_line_selector:
        _file_part, _right_part = query.split('::', 1)
        if '$' in _right_part:
            is_pattern_mode = True
        else:
            _sel_query = query if not query.startswith('::') else '**' + query
            try:
                parse_extended_selector(_sel_query)
                has_selector = True
            except Exception:
                is_pattern_mode = True

        if is_pattern_mode:
            query = _right_part
            _file_scope = _file_part.strip()
            if not path:
                if _file_scope and _file_scope != '**':
                    path = _file_scope
    elif '$' in query:
        is_pattern_mode = True
    elif _re_module.match(r'\s*(?:async\s+)?(?:def|class)\s+\w*[*?]', query):
        # Glob wildcards in def/class name → pattern mode
        is_pattern_mode = True

    if has_selector and query.startswith('::'):
        query = '**' + query

    return QueryShape(
        query=query,
        path=path,
        is_pattern_mode=is_pattern_mode,
        has_selector=has_selector,
        is_line_selector=is_line_selector,
    )


_STRUCTURAL_KEYWORDS = (
    "def", "async def", "class", "for", "while", "try", "with", "if", "except"
)


def parse_where_clause(values: list[str]) -> dict:
    """Parse --where values into internal API params.

    Detects syntax from each value:
    - "not ..." prefix → not_inside constraint
    - "@..." prefix → decorator filter (matching for lookup, or passed through)
    - contains "$" → body pattern match (matching)
    - structural keyword (def, class, etc.) → inside constraint
    - otherwise → dotted scope path

    Returns dict with keys: scope, inside, not_inside, matching
    """
    result: dict = {}
    for value in values:
        if value.startswith("not "):
            result["not_inside"] = value[4:].strip()
        elif value.startswith("@"):
            result["matching"] = value
        elif "$" in value:
            result["matching"] = value
        elif any(
            value == kw or value.startswith(kw + " ") or value.startswith(kw + ":")
            for kw in _STRUCTURAL_KEYWORDS
        ):
            result["inside"] = value
        else:
            result["scope"] = value.split(".")
    return result

# Create app with emend commands
app = typer.Typer(
    help="Python refactoring CLI",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        from emend import __version__
        typer.echo(f"emend {__version__}")
        raise typer.Exit()


@app.callback()
def _app_callback(
    version: Annotated[
        Optional[bool],
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version and exit."),
    ] = None,
    verbose: Annotated[
        int,
        typer.Option("-v", "--verbose", count=True, help="Verbose output (-v info, -vv debug with timestamps)."),
    ] = 0,
    language: Annotated[
        Optional[str],
        typer.Option("--language", "-L", help="Source language (python, typescript, etc.). Default: python."),
    ] = None,
) -> None:
    if verbose >= 2:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
            stream=sys.stderr,
        )
    elif verbose >= 1:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s.%(msecs)03d %(name)s: %(message)s",
            datefmt="%H:%M:%S",
            stream=sys.stderr,
        )
    if language is not None:
        _state["language"] = language


# ============================================================================
# Pattern match display helpers
# ============================================================================

# ANSI escape codes for match highlighting
_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_ANSI_MAGENTA = "\033[35m"
_ANSI_GREEN = "\033[32m"
_ANSI_CYAN = "\033[36m"
_ANSI_RED_BOLD = "\033[1;31m"


def _print_pattern_match_code(
    file_path_str: str,
    match,
    file_lines_cache: dict[str, list[str]],
    *,
    is_tty: bool = False,
) -> None:
    """Print a pattern match with a file:line header followed by matched source lines.

    In TTY mode, the matched characters are highlighted with ANSI colors.
    """
    if match.line is None:
        if is_tty:
            print(f"{_ANSI_MAGENTA}{file_path_str}{_ANSI_CYAN}:{_ANSI_GREEN}?{_ANSI_RESET}", flush=True)
        else:
            print(f"{file_path_str}:?", flush=True)
        return

    start_line = match.line
    end_line = match.end_line or start_line

    # Get source lines (cached per file)
    if file_path_str not in file_lines_cache:
        try:
            file_lines_cache[file_path_str] = Path(file_path_str).read_text().splitlines()
        except Exception:
            file_lines_cache[file_path_str] = []
    lines = file_lines_cache[file_path_str]

    # Print header
    if start_line == end_line:
        line_range = str(start_line)
    else:
        line_range = f"{start_line}-{end_line}"

    if is_tty:
        print(f"{_ANSI_MAGENTA}{file_path_str}{_ANSI_CYAN}:{_ANSI_GREEN}{line_range}{_ANSI_RESET}", flush=True)
    else:
        print(f"{file_path_str}:{line_range}", flush=True)

    # Print matched source lines with optional highlighting
    col = match.col
    end_col_val = match.end_col
    for i in range(start_line, min(end_line + 1, len(lines) + 1)):
        line_text = lines[i - 1] if i <= len(lines) else ""
        if is_tty and col is not None and end_col_val is not None:
            # Determine highlight range for this line
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
            # Clamp to line bounds
            hl_start = max(0, min(hl_start, len(line_text)))
            hl_end = max(hl_start, min(hl_end, len(line_text)))
            before = line_text[:hl_start]
            highlighted = line_text[hl_start:hl_end]
            after = line_text[hl_end:]
            print(f"{before}{_ANSI_RED_BOLD}{highlighted}{_ANSI_RESET}{after}", flush=True)
        else:
            print(line_text, flush=True)


# ============================================================================
# Unified Commands
# ============================================================================

@app.command("grep")
def search(
    query: Annotated[str, typer.Argument(help="Pattern with $X metavars, selector (file.py::sym), or file/dir path")],
    path: Annotated[Optional[str], typer.Argument(help="File, glob, or directory to search (pattern mode)")] = None,
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
):
    """Unified search: auto-detects pattern matching vs symbol lookup.

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
        emend search 'print($X)' file.py
        emend search 'assertEqual($A, $B)' tests/ --output count

        # Pattern mode with file scope (:: separator):
        emend search '**::print($X)'
        emend search 'src/::assert False'
        emend search 'file.py::print()'

        # Lookup mode (valid selector after ::):
        emend search file.py::func[params]
        emend search src/ --kind function --where '@app.command'

        # Bare name (auto-detects as symbol search):
        emend search process_encounter
        emend search ::MyClass.method
        emend search MyClass src/

        # Summary mode (list symbols):
        emend search file.py
        emend search file.py::MyClass --output summary
        emend search file.py --output summary::flat
    """
    import re as _re

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

    # Parse --where values
    where_params = parse_where_clause(where or [])
    where_scope = where_params.get("scope")
    where_inside = where_params.get("inside")
    where_not_inside = where_params.get("not_inside")
    where_matching = where_params.get("matching")

    # Parse --output for :: modifier
    output_base = output
    output_modifier = None
    if output and "::" in output:
        parts = output.split("::", 1)
        output_base = parts[0]
        output_modifier = parts[1]

    # Detect query shape (shared logic with MCP server)
    _shape = detect_query_shape(query, path)
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
        if where_matching and "$" in where_matching:
            _fop = Path(query)
            if _fop.is_dir() or '*' in query or '?' in query:
                path = query
                query = where_matching
                where_matching = None
                is_pattern_mode = True

        # Bare name fallback: if query doesn't match a file/dir/glob,
        # treat it as a symbol name and search across Python files.
        if not is_pattern_mode:
            _query_path = Path(query)
            if (not _query_path.exists()
                    and '/' not in query
                    and not _is_source_file_query(query)
                    and not ('*' in query or '?' in query)):
                if path:
                    _p = Path(path)
                    file_scope = str(_p / '**') if _p.is_dir() else path
                    path = None
                else:
                    file_scope = '**'
                query = f'{file_scope}::{query}'
                has_selector = True

    # Build lookup-mode filters from --where
    lookup_has_decorator: Optional[list[str]] = None
    lookup_in_class: Optional[list[str]] = None
    lookup_matching: Optional[str] = None
    if where_matching is not None:
        if where_matching.startswith("@"):
            lookup_has_decorator = [where_matching[1:]]
        else:
            lookup_matching = where_matching
    if where_inside is not None and where_inside.startswith("class "):
        lookup_in_class = [where_inside[6:].strip()]

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
            target_obj = Path(target_path)
            if target_obj.is_dir() and _lang == "python":
                # Fast path: get string list directly from Rust, skip Path creation
                from emend import emend_core
                file_strs = emend_core.collect_python_files(str(target_obj.resolve()))
                is_multi_file = True
            else:
                files, is_multi_file = resolve_files(target_path, language=_lang)
                file_strs = [str(f) for f in files]
            _logger.info("resolve_files: %d files in %.3fs (%s)", len(file_strs), _time.monotonic() - _t0, target_path)

            # Build a lazy iterator over all matches, yielding as each file completes.
            # This allows streaming output rather than collecting everything first.
            def _iter_matches():
                project_matches = find_pattern_in_project(
                    query, file_strs,
                    scope=where_scope,
                    inside=where_inside,
                    not_inside=where_not_inside,
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

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(2)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)


app.command("query", hidden=True)(search)
app.command("show", hidden=True)(search)
app.command("get", hidden=True)(search)
app.command("lookup", hidden=True)(search)
app.command("find", hidden=True)(search)
app.command("search", hidden=True)(search)
app.command("ls", hidden=True)(search)



@app.command("edit")
def edit(
    selector: Annotated[str, typer.Argument(help="Symbol selector (file.py::Symbol[component])")],
    value: Annotated[
        Optional[str],
        typer.Argument(help="New value (empty to remove)")
    ] = None,
    rm: Annotated[
        bool,
        typer.Option("--rm", help="Remove component or symbol")
    ] = False,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Apply changes to file")
    ] = False,
    returns: Annotated[
        Optional[list[str]],
        typer.Option("--returns", help="Only edit symbols whose return type matches (annotation or inferred)")
    ] = None,
    type_engine: Annotated[
        Optional[str],
        typer.Option("--type-engine",
                     help="Type inference engine for --returns fallback: auto, pyrefly, pyright, ty")
    ] = None,
):
    """Edit or replace existing symbol components.

    Examples:
        # Change return type
        emend edit api.py::get_user[returns] "User | None" --apply

        # Replace entire parameter list
        emend edit api.py::get_user[params] "x: int, y: str" --apply

        # Modify specific parameter
        emend edit api.py::get_user[params][x] "x: float" --apply

        # Remove a parameter
        emend edit api.py::get_user[params][force] --rm --apply

        # Remove entire function
        emend edit api.py::deprecated_function --rm --apply

        # Edit return type of all functions returning str (annotation or inferred)
        emend edit '*.py::*[returns]' 'str | None' --returns str --type-engine auto --apply
    """
    try:
        # Create TypeOracle when --type-engine or --returns is specified
        oracle = None
        if type_engine is not None or returns:
            oracle = _maybe_create_oracle(type_engine)

        result = cmd_edit(
            selector_str=selector,
            value=value,
            rm=rm,
            apply=apply,
            returns_filter=returns,
            type_oracle=oracle,
        )
        print(result, end='')
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(2)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)


@app.command("rm")
def remove_cmd(
    selector: Annotated[str, typer.Argument(help="Symbol selector (file.py::Symbol or file.py::Symbol[component])")],
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Apply changes to file")
    ] = False,
):
    """Remove a symbol or component.

    Shorthand for ``edit --rm``.

    Examples:
        # Remove a function
        emend rm api.py::deprecated_function --apply

        # Remove a parameter
        emend rm api.py::get_user[params][force] --apply

        # Remove a decorator
        emend rm api.py::handler[decorators][@deprecated] --apply

        # Remove a base class
        emend rm models.py::User[bases][OldMixin] --apply
    """
    try:
        result = cmd_edit(selector_str=selector, rm=True, apply=apply)
        print(result, end='')
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(2)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)


app.command("remove", hidden=True)(remove_cmd)
app.command("delete", hidden=True)(remove_cmd)
app.command("set", hidden=True)(edit)


@app.command("add")
def add(
    selector: Annotated[str, typer.Argument(help="Symbol selector (file.py::Symbol[component])")],
    value: Annotated[str, typer.Argument(help="Value to add")],
    before: Annotated[
        Optional[str],
        typer.Option("--before", help="Insert before named item")
    ] = None,
    after: Annotated[
        Optional[str],
        typer.Option("--after", help="Insert after named item")
    ] = None,
    at: Annotated[
        Optional[int],
        typer.Option("--at", help="Insert at position (0-indexed)")
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Apply changes to file")
    ] = False,
    returns: Annotated[
        Optional[list[str]],
        typer.Option("--returns", help="Only add to symbols whose return type matches (annotation or inferred)")
    ] = None,
    type_engine: Annotated[
        Optional[str],
        typer.Option("--type-engine",
                     help="Type inference engine for --returns fallback: auto, pyrefly, pyright, ty")
    ] = None,
):
    """Add new items to symbol components.

    Position modes:
    - --at N: Insert at position N (0-indexed)
    - --before NAME: Insert before named item
    - --after NAME: Insert after named item
    - No position: Append to end

    Pseudo-class selectors for parameters:
    - :KEYWORD_ONLY - Add keyword-only parameter
    - :POSITIONAL_ONLY - Add positional-only parameter
    - :POSITIONAL_OR_KEYWORD - Add regular parameter (default)

    Examples:
        # Append parameter at end
        emend add api.py::get_user[params] "ctx: Context" --apply

        # Add parameter at beginning
        emend add api.py::get_user[params] "db: Database" --at 0 --apply

        # Add parameter before specific param
        emend add api.py::get_user[params] "ctx: Context" --before user_id --apply

        # Add keyword-only parameter
        emend add api.py::get_user[params]:KEYWORD_ONLY "force: bool = False" --apply

        # Add parameter to all functions returning Connection (annotation or inferred)
        emend add '*.py::*[params]' 'timeout: int = 30' --returns Connection --type-engine auto --apply
    """
    try:
        # Create TypeOracle when --type-engine or --returns is specified
        oracle = None
        if type_engine is not None or returns:
            oracle = _maybe_create_oracle(type_engine)

        result = cmd_add(
            selector_str=selector,
            value=value,
            before=before,
            after=after,
            at=at,
            apply=apply,
            returns_filter=returns,
            type_oracle=oracle,
        )
        print(result, end='')
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(2)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)


app.command("insert", hidden=True)(add)




@app.command("replace")
def replace_cmd(
    pattern: Annotated[str, typer.Argument(help="Pattern to find (e.g., 'print($X)')")],
    replacement: Annotated[str, typer.Argument(help="Replacement pattern (e.g., 'logger.info($X)')")],
    path: Annotated[str, typer.Argument(help="Python file to modify")],
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Apply changes to file (default is dry-run)")
    ] = False,
    where: Annotated[
        Optional[list[str]],
        typer.Option("--where", help=(
            "Filter/scope constraint. Syntax auto-detected: "
            "'def test_*' (structural), 'not class' (negation), "
            "'MyClass.method' (scope)"
        ))
    ] = None,
    type_engine: Annotated[
        Optional[str],
        typer.Option("--type-engine",
                     help="Type inference engine for :type[X] and :returns[X] constraints: auto, pyrefly, pyright, ty")
    ] = None,
):
    """Replace pattern matches with replacement in Python file(s).

    Supports metavariables like $X, $A, $B in both patterns and replacements.
    Path can be a file, glob pattern (*.py), or directory (replaces in all .py files recursively).

    By default, shows a diff without modifying the file (dry-run).
    Use --apply to actually modify the file.

    Examples:
        emend replace 'print($X)' 'logger.info($X)' file.py
        emend replace 'assertEqual($A, $B)' 'assert $A == $B' tests/ --apply
        emend replace 'old_name' 'new_name' file.py --where my_func --apply
        emend replace 'print($X)' 'logger.info($X)' file.py --where def --apply
        emend replace 'print($X)' 'logger.info($X)' file.py --where 'def test_*' --apply
        emend replace '$X = $Y' '$X: int = $Y' src/*.py --where 'not class' --apply
        emend replace '$X:type[Connection].close()' '$X.shutdown()' src/ --type-engine auto
    """
    try:
        where_params = parse_where_clause(where or [])
        scope = where_params.get("scope")
        inside = where_params.get("inside")
        not_inside = where_params.get("not_inside")

        # Create TypeOracle when --type-engine is specified or pattern contains
        # oracle constraints (:type[X] / :returns[X]).
        oracle = None
        if type_engine is not None or ":type[" in pattern or ":returns[" in pattern:
            oracle = _maybe_create_oracle(type_engine)

        search_path = path
        _lang = _state["language"]
        files, is_multi_file = resolve_files(search_path, language=_lang)

        # Pre-filter: use Rust matcher to find which files actually have
        # matches, so we only need to process those files.
        file_strs = [str(f) for f in files]
        if is_multi_file and len(file_strs) > 1:
            import time as _time
            _logger = logging.getLogger("emend.replace")
            from emend import emend_core
            from emend.pattern import compile_pattern_to_rust_ir, compile_constraint_to_rust_ir

            # First: substring pre-filter via Rust parallel I/O
            literals = extract_pattern_literals(pattern)
            _t0 = _time.monotonic()
            file_contents = emend_core.read_and_filter_files(file_strs, literals)
            _logger.info("read_and_filter: %d -> %d files in %.3fs", len(file_strs), len(file_contents), _time.monotonic() - _t0)

            # Second: try structural pre-filter via Rust tree-sitter matcher
            pattern_ir = compile_pattern_to_rust_ir(pattern, language=_lang)
            if pattern_ir is not None:
                inside_ir = compile_constraint_to_rust_ir(inside, language=_lang) if inside else None
                not_inside_ir = compile_constraint_to_rust_ir(not_inside, language=_lang) if not_inside else None
                if (inside is None or inside_ir is not None) and \
                   (not_inside is None or not_inside_ir is not None):
                    _t0 = _time.monotonic()
                    raw_matches = emend_core.find_pattern_in_files(
                        list(file_contents), pattern_ir, inside_ir, not_inside_ir
                    )
                    candidate_files = {m[0] for m in raw_matches}
                    _logger.info("rust pre-filter: %d -> %d files with matches in %.3fs",
                                 len(file_contents), len(candidate_files), _time.monotonic() - _t0)
                    file_strs = sorted(candidate_files)
                else:
                    _logger.info("constraint could not compile to Rust IR, skipping structural pre-filter")
                    file_strs = [fp for fp, _ in file_contents]
            else:
                _logger.info("pattern could not compile to Rust IR, skipping structural pre-filter")
                file_strs = [fp for fp, _ in file_contents]
        else:
            file_strs = [str(f) for f in files]

        # Collect diffs and count across all files
        all_diffs = []
        total_count = 0
        for file_path_str in file_strs:
            try:
                diff, cnt = replace_pattern(
                    pattern, replacement, file_path_str,
                    scope=scope, apply=apply,
                    inside=inside, not_inside=not_inside,
                    type_oracle=oracle,
                    language=_lang,
                )
                if diff:  # Only include files with changes
                    all_diffs.append(diff)
                total_count += cnt
            except FileNotFoundError:
                # For multi-file operations, skip missing files silently
                # For single file, let the exception propagate
                if not is_multi_file:
                    raise
                continue

        # Print combined diff
        print("".join(all_diffs), end='')
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(2)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)


@app.command("lint")
def lint_cmd(
    path: Annotated[str, typer.Argument(help="File or directory to lint")],
    config: Annotated[
        Optional[str],
        typer.Option("--config", help="Path to patterns.yaml config file")
    ] = None,
    fix: Annotated[
        bool,
        typer.Option("--fix", help="Auto-apply replace rules")
    ] = False,
    rule: Annotated[
        Optional[str],
        typer.Option("--rule", help="Run only a specific rule by name")
    ] = None,
):
    """Lint files using pattern rules from a YAML config.

    Reads rules from .emend/patterns.yaml (or --config path).
    Rules define patterns to find and optional replacements.

    Examples:
        emend lint src/
        emend lint src/ --config .emend/patterns.yaml
        emend lint src/ --fix
        emend lint src/ --rule no-print
    """
    try:
        from emend.lint import load_rules, run_lint

        # Find config file
        if config is None:
            config = ".emend/patterns.yaml"
        config_path = Path(config)
        if not config_path.exists():
            print(f"Error: Config file not found: {config}", file=sys.stderr)
            raise typer.Exit(2)

        rules, macros, deadcode_config = load_rules(str(config_path))

        _lang = _state["language"]
        resolved, _ = resolve_files(path, language=_lang)
        files = [str(f) for f in resolved]

        violations = run_lint(
            rules, files, fix=fix, rule_filter=rule,
            deadcode_config=deadcode_config, project_path=path,
            language=_lang,
        )

        for v in violations:
            print(f"{v.file_path}:{v.line}:{v.col}: [{v.rule_name}] {v.message}")

        if violations:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)


@app.command("cp")
def copy_to_cmd(
    selector: Annotated[str, typer.Argument(help="Selector (file.py::Symbol.path)")],
    destination: Annotated[str, typer.Argument(help="Destination file path")],
    append: Annotated[bool, typer.Option("--append", help="Append to destination file")] = False,
    dedent: Annotated[bool, typer.Option("--dedent", help="Dedent the copied symbol (useful for nested functions)")] = False,
    apply: Annotated[bool, typer.Option("--apply", "-a", help="Apply the changes")] = False,
):
    """Copy a symbol to another file.

    Examples:
        emend cp file.py::my_function other.py --apply
        emend cp file.py::MyClass other.py --append --apply
        emend cp file.py::outer.inner other.py --dedent --apply
    """
    ast_commands.cmd_copy_to(selector, destination, append, dedent, apply)


app.command("copy", hidden=True)(copy_to_cmd)
app.command("copy-to", hidden=True)(copy_to_cmd)


@app.command("refs")
def refs_cmd(
    selector: Annotated[str, typer.Argument(help="Selector (file.py::Symbol)")],
    exclude_definition: Annotated[bool, typer.Option("--exclude-definition", help="Exclude the definition itself")] = False,
    exclude_imports: Annotated[bool, typer.Option("--exclude-imports", help="Exclude import statements")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    writes_only: Annotated[bool, typer.Option("--writes-only", help="Only show write (assignment) references")] = False,
    reads_only: Annotated[bool, typer.Option("--reads-only", help="Only show read (load) references")] = False,
    calls_only: Annotated[bool, typer.Option("--calls-only", help="Only show call sites (not mere references)")] = False,
    project: Annotated[Optional[str], typer.Option("--project", "-p", help="Project root directory (used with --calls-only)")] = None,
):
    """Find all references to a symbol across the project.

    Uses tree-sitter and Rust scope resolver for scope-aware reference finding.
    With --calls-only, only returns actual call sites (not mere references or imports).

    Examples:
        emend refs src/emend/transform.py::get_component
        emend refs src/emend/transform.py::get_component --json
        emend refs file.py::MyClass --exclude-imports
        emend refs file.py::config --writes-only
        emend refs file.py::config --reads-only
        emend refs src/module.py::process --calls-only
        emend refs src/module.py::process --calls-only --project src/
    """
    try:
        _reject_file_glob(selector, "refs")
        parsed_selector = parse_extended_selector(selector)

        if calls_only:
            if writes_only or reads_only or exclude_definition or exclude_imports:
                raise ValueError(
                    "--calls-only is incompatible with --writes-only, --reads-only, "
                    "--exclude-definition, and --exclude-imports"
                )
            callers = find_callers(parsed_selector, project_path=project)
            if json_output:
                import json
                data = [
                    {
                        "file_path": ref.file_path,
                        "line": ref.line,
                        "column": ref.column,
                    }
                    for ref in callers
                ]
                print(json.dumps(data, indent=2))
            else:
                for ref in callers:
                    print(f"{ref.file_path}:{ref.line}", flush=True)
            return

        references = find_references(
            parsed_selector,
            project_path=project,
            include_definition=not exclude_definition,
            include_imports=not exclude_imports,
            writes_only=writes_only,
            reads_only=reads_only,
        )

        if json_output:
            import json
            refs_data = [
                {
                    "file_path": ref.file_path,
                    "line": ref.line,
                    "column": ref.column,
                    "offset": ref.offset,
                    "is_definition": ref.is_definition,
                    "is_import": ref.is_import,
                    "is_write": ref.is_write
                }
                for ref in references
            ]
            print(json.dumps(refs_data, indent=2))
        else:
            for ref in references:
                marker = ""
                if ref.is_definition:
                    marker = " (definition)"
                elif ref.is_import:
                    marker = " (import)"
                print(f"{ref.file_path}:{ref.line}{marker}", flush=True)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(2)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)


app.command("references", hidden=True)(refs_cmd)
app.command("find-references", hidden=True)(refs_cmd)


@app.command("rename")
def rename_cmd(
    selector: Annotated[str, typer.Argument(help="Selector (file.py::Symbol for symbol rename, or file.py for module rename)")],
    new_name: Annotated[str, typer.Option("--to", help="New name")],
    apply: Annotated[bool, typer.Option("--apply", help="Apply changes")] = False,
    docs: Annotated[bool, typer.Option("--docs", help="Rename in docstrings (symbol mode only)")] = False,
    no_hierarchy: Annotated[bool, typer.Option("--no-hierarchy", help="Don't rename in class hierarchy (symbol mode only)")] = False,
    unsure: Annotated[bool, typer.Option("--unsure", help="Rename uncertain occurrences (symbol mode only)")] = False,
    project: Annotated[Optional[str], typer.Option("--project", "-p", help="Project root directory")] = None,
):
    """Rename a symbol or module across the project.

    If the selector contains '::', renames a symbol. Otherwise, renames a module file.

    Examples:
        emend rename file.py::old_name --to new_name
        emend rename file.py::MyClass --to BetterClass --apply
        emend rename file.py::func --to new_func --docs --apply
        emend rename old_utils.py --to new_utils --apply
    """
    try:
        if '::' in selector:
            # Symbol rename mode
            _reject_file_glob(selector, "rename")
            parsed_selector = parse_extended_selector(selector)
            diffs = rename_symbol(
                parsed_selector,
                new_name,
                project,
                in_hierarchy=not no_hierarchy,
                docs=docs,
                unsure=unsure,
                apply=apply,
            )

            if not diffs:
                print("No changes needed.")
            else:
                for file_path, diff in diffs.items():
                    print(diff, end='')

                if not apply:
                    print("\nRun with --apply to write changes.")
        else:
            # Module rename mode
            diffs = rename_module(selector, new_name, project, apply)
            if apply:
                print("Module renamed successfully.")
            else:
                if "__description__" in diffs:
                    print("\n" + "=" * 60)
                    print("CHANGES PREVIEW")
                    print("=" * 60)
                    print(diffs["__description__"])
                    print("=" * 60 + "\n")
                else:
                    for file_path, diff in diffs.items():
                        if diff:
                            print(diff)
                print("\nRun with --apply to apply these changes.")
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(2)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)


@app.command("mv")
def move_cmd(
    selector: Annotated[str, typer.Argument(help="Selector (file.py::Symbol for symbol move, or file.py for module move)")],
    destination: Annotated[str, typer.Argument(help="Destination file or package")],
    dedent: Annotated[bool, typer.Option("--dedent", help="Dedent nested symbols (symbol mode only)")] = False,
    no_update_imports: Annotated[bool, typer.Option("--no-update-imports", help="Don't update imports (symbol mode only)")] = False,
    apply: Annotated[bool, typer.Option("--apply", help="Apply changes")] = False,
    project: Annotated[Optional[str], typer.Option("--project", "-p", help="Project root directory")] = None,
):
    """Move a symbol or module with automatic import updates.

    If the selector contains '::', moves a symbol. Otherwise, moves a module file.

    Symbol mode:
        1. Copies the symbol to the destination file
        2. Removes the symbol from the source file
        3. Updates all import statements that reference the symbol

    Module mode:
        Moves the module file to the destination package and updates imports.

    Examples:
        emend mv file.py::helper_func dest.py
        emend mv file.py::MyClass dest.py --apply
        emend mv utils.py pkg --project . --apply
    """
    try:
        if '::' in selector:
            # Symbol move mode
            _reject_file_glob(selector, "move")
            parsed_selector = parse_extended_selector(selector)
            diffs = move_symbol(
                parsed_selector,
                destination,
                dedent=dedent,
                update_imports=not no_update_imports,
                apply=apply
            )

            if not diffs:
                print("No changes needed.")
            else:
                for file_path, diff in diffs.items():
                    if diff:  # Only print non-empty diffs
                        print(diff, end='')

                if not apply:
                    print("\nRun with --apply to write changes.")
        else:
            # Module move mode
            diffs = move_module(selector, destination, project, apply)
            if apply:
                print("Module moved successfully.")
            else:
                if "__description__" in diffs:
                    print("\n" + "=" * 60)
                    print("CHANGES PREVIEW")
                    print("=" * 60)
                    print(diffs["__description__"])
                    print("=" * 60 + "\n")
                else:
                    for file_path, diff in diffs.items():
                        if diff:
                            print(diff)
                print("\nRun with --apply to apply these changes.")
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(2)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)


app.command("move", hidden=True)(move_cmd)


@app.command("batch")
def batch_cmd(
    ops_file: Annotated[str, typer.Argument(help="YAML or JSON file with operations")],
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Apply changes (default is dry-run)")
    ] = False,
):
    """Apply batch refactoring operations from a YAML or JSON file.

    The file should contain an 'operations' list. Each operation is one of:
    - rename: Rename a symbol (selector + to)
    - replace: Pattern replace (pattern + replacement + path)
    - add: Add to a component (selector + value, optional at/before/after)
    - edit: Edit a component (selector + value)
    - remove: Remove a component (selector)

    By default shows diffs (dry-run). Use --apply to modify files.

    Examples:
        emend batch refactor.yaml
        emend batch refactor.json --apply
    """
    from pathlib import Path
    import json as json_mod

    try:
        ops_path = Path(ops_file)
        if not ops_path.exists():
            raise FileNotFoundError(f"Operations file not found: {ops_file}")

        content = ops_path.read_text()

        # Parse based on file extension
        if ops_path.suffix in ('.yaml', '.yml'):
            try:
                import yaml
            except ImportError:
                raise ValueError(
                    "PyYAML is required for YAML batch files. "
                    "Install it with: pip install pyyaml"
                )
            data = yaml.safe_load(content)
        elif ops_path.suffix == '.json':
            data = json_mod.loads(content)
        else:
            try:
                data = json_mod.loads(content)
            except json_mod.JSONDecodeError:
                try:
                    import yaml
                    data = yaml.safe_load(content)
                except ImportError:
                    raise ValueError(
                        "Could not parse as JSON. Install PyYAML for YAML support."
                    )

        if not isinstance(data, dict) or "operations" not in data:
            raise ValueError(
                "Operations file must contain an 'operations' key with a list of operations"
            )

        operations = data["operations"]
        if not isinstance(operations, list):
            raise ValueError("'operations' must be a list")

        all_output = []

        for i, op in enumerate(operations):
            if not isinstance(op, dict) or len(op) != 1:
                raise ValueError(
                    f"Operation #{i+1}: must be a dict with one key "
                    "(rename/replace/add/edit/remove)"
                )

            op_type = list(op.keys())[0]
            op_args = op[op_type]

            if op_type == "edit":
                selector_str = op_args.get("selector")
                value = op_args.get("value")
                if not selector_str or value is None:
                    raise ValueError(
                        f"Operation #{i+1} (edit): requires 'selector' and 'value'"
                    )
                result = cmd_edit(
                    selector_str=selector_str, value=value, apply=apply
                )
                if result.strip():
                    all_output.append(result)

            elif op_type == "add":
                selector_str = op_args.get("selector")
                value = op_args.get("value")
                if not selector_str or value is None:
                    raise ValueError(
                        f"Operation #{i+1} (add): requires 'selector' and 'value'"
                    )
                before = op_args.get("before")
                after = op_args.get("after")
                at = op_args.get("at")
                result = cmd_add(
                    selector_str=selector_str,
                    value=value,
                    before=before,
                    after=after,
                    at=at,
                    apply=apply,
                )
                if result.strip():
                    all_output.append(result)

            elif op_type == "remove":
                selector_str = op_args.get("selector")
                if not selector_str:
                    raise ValueError(
                        f"Operation #{i+1} (remove): requires 'selector'"
                    )
                result = cmd_edit(
                    selector_str=selector_str, rm=True, apply=apply
                )
                if result.strip():
                    all_output.append(result)

            elif op_type == "replace":
                pattern = op_args.get("pattern")
                replacement = op_args.get("replacement")
                target_path = op_args.get("path")
                if not pattern or not replacement or not target_path:
                    raise ValueError(
                        f"Operation #{i+1} (replace): requires 'pattern', "
                        "'replacement', and 'path'"
                    )

                _lang = _state["language"]
                files, _ = resolve_files(target_path, language=_lang)

                op_diffs = []
                for fp in files:
                    try:
                        diff, cnt = replace_pattern(
                            pattern, replacement, str(fp), apply=apply,
                            language=_lang,
                        )
                        if diff:
                            op_diffs.append(diff)
                    except FileNotFoundError:
                        continue
                if op_diffs:
                    all_output.append("".join(op_diffs))

            elif op_type == "rename":
                selector_str = op_args.get("selector")
                new_name = op_args.get("to")
                if not selector_str or not new_name:
                    raise ValueError(
                        f"Operation #{i+1} (rename): requires 'selector' and 'to'"
                    )
                parsed_selector = parse_extended_selector(selector_str)
                diffs = rename_symbol(
                    parsed_selector, new_name, apply=apply,
                )
                if diffs:
                    diff_text = "".join(d for d in diffs.values() if d)
                    if diff_text.strip():
                        all_output.append(diff_text)

            else:
                raise ValueError(
                    f"Operation #{i+1}: unknown operation type '{op_type}'. "
                    "Supported: rename, replace, add, edit, remove"
                )

        output = "\n".join(all_output)
        if output:
            print(output, end='')
            if not apply:
                print("\n\nRun with --apply to write changes.")
            print()

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(2)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)


@app.command("graph")
def graph_cmd(
    file: Annotated[str, typer.Argument(help="Python file to analyze")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format: plain, json, dot")] = "plain",
    project: Annotated[Optional[str], typer.Option("--project", "-p", help="Project root directory")] = None,
):
    """Generate a call graph for all functions in a file.

    Output formats:
    - plain: Human-readable text (default)
    - json: JSON adjacency list
    - dot: Graphviz DOT format

    Examples:
        emend graph src/module.py
        emend graph src/module.py --format dot
        emend graph src/module.py --format json
    """
    try:
        result = generate_graph(file, project_path=project, format=format)
        print(result)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(2)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)


@app.command("deadcode")
def dead_code_cmd(
    path: Annotated[str, typer.Argument(help="Project directory to scan")] = ".",
    kind: Annotated[Optional[str], typer.Option("--kind", "-k", help="Symbol kind: function, class")] = None,
    include_private: Annotated[bool, typer.Option("--include-private", help="Include _private symbols")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    exclude_references_from: Annotated[
        Optional[list[str]],
        typer.Option("--exclude-references-from", help="Directories to ignore when scanning for references (e.g. tests/)")
    ] = None,
    no_strings: Annotated[bool, typer.Option("--no-strings", help="Don't count string literals as references")] = False,
    no_last_reference: Annotated[bool, typer.Option("--no-last-reference", help="Don't show git last-reference info")] = False,
    all_files: Annotated[bool, typer.Option("--all-files", help="Scan all Python files, not just git-tracked ones")] = False,
    entry_point_decorator: Annotated[
        Optional[list[str]],
        typer.Option("--entry-point-decorator", help="Additional decorator names to treat as entry points (repeatable)")
    ] = None,
    entry_point_name: Annotated[
        Optional[list[str]],
        typer.Option("--entry-point-name", help="Additional function/class names to treat as entry points (repeatable)")
    ] = None,
    exclude_path: Annotated[
        Optional[list[str]],
        typer.Option("--exclude-path", help="Directories to exclude entirely from analysis (repeatable)")
    ] = None,
):
    """Find potentially dead (unreferenced) code in a project.

    Scans Python files and reports top-level symbols that have no
    references outside their own definition. Uses scope-aware analysis
    to avoid false positives from same-named symbols.

    By default, only git-tracked files are scanned. Use --all-files
    to include untracked files (e.g. in non-git projects).

    Automatically skips:
    - Dunder methods (__init__, __str__, etc.)
    - Test functions/classes (test_*, Test*)
    - Decorated entry points (@app.command, @pytest.fixture, etc.)
    - Symbols listed in __all__
    - Conventional entry points (main, setup, teardown)
    - Private symbols (_name) unless --include-private is set
    - Symbols with # noqa: emend:deadcode on the definition line

    Use --entry-point-decorator and --entry-point-name to add custom
    exclusions beyond the built-in heuristics.

    By default, string literals containing the symbol name are treated
    as references (e.g. getattr(obj, "method_name")).  Disable with
    --no-strings.

    Examples:
        emend deadcode src/
        emend deadcode . --kind function
        emend deadcode . --include-private --json
        emend deadcode src/ --exclude-references-from tests/
        emend deadcode . --no-strings --no-last-reference
        emend deadcode . --all-files
        emend deadcode . --entry-point-decorator my_framework.handler
        emend deadcode . --entry-point-name plugin_init
    """
    try:
        results = find_dead_code(
            project_path=path,
            kind=kind,
            include_private=include_private,
            exclude_references_from=exclude_references_from,
            strings_count_as_references=not no_strings,
            show_last_reference=not no_last_reference,
            all_files=all_files,
            entry_point_decorators=entry_point_decorator,
            entry_point_names=entry_point_name,
            exclude_paths=exclude_path,
        )

        if json_output:
            # JSON mode: must collect all results before printing
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
            if not data:
                print("[]")
            else:
                import json
                print(json.dumps(data, indent=2))
        else:
            count = 0
            for d in results:
                line = f"{d.file_path}:{d.line}  {d.name} ({d.kind}) - {d.reason}"
                if d.last_reference_commit:
                    line += f"\n    last commit: {d.last_reference_commit}"
                print(line, flush=True)
                count += 1
            if count == 0:
                print("No dead code found.")
            else:
                print(f"\nFound {count} potentially dead symbol(s).", file=sys.stderr)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(2)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)


app.command("dead-code", hidden=True)(dead_code_cmd)
app.command("dead_code", hidden=True)(dead_code_cmd)


# ============================================================================
# Type Inference Commands
# ============================================================================

@app.command("types")
def types_cmd(
    path: Annotated[str, typer.Argument(help="File or directory to analyze")],
    name: Annotated[Optional[str], typer.Option("--name", "-n", help="Filter by symbol name")] = None,
    kind: Annotated[Optional[str], typer.Option("--kind", "-k", help="Filter by binding kind: definition, reference, import, diagnostic")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    engine: Annotated[str, typer.Option("--engine", help="Type inference engine: auto, pyrefly, pyright, ty")] = "auto",
    definitions_only: Annotated[bool, typer.Option("--definitions-only", "-d", help="Show only definitions")] = False,
):
    """Show inferred types for symbols in a file.

    Uses a type inference engine (Pyrefly, Pyright, or ty) to analyze
    source files and display inferred types for all symbols and expressions.

    The engine is auto-detected from project configuration files
    (pyrightconfig.json, ty.toml, pyrefly.toml, or pyproject.toml sections)
    and installed tools.  Use --engine to override.

    Examples:
        emend types src/models/user.py
        emend types src/models/user.py --name User
        emend types src/models/ --definitions-only --json
        emend types app.py --engine pyright
        emend types app.py --engine ty
    """
    from emend.type_oracle import create_type_oracle

    import json as json_mod

    try:
        target = Path(path)
        is_glob = "*" in path or "?" in path
        # Use the target's parent as project root for autodetection;
        # for globs use CWD since Path("src/*.py") is not a real path.
        if is_glob:
            project_root = Path.cwd()
        elif target.is_file():
            project_root = target.parent
        else:
            project_root = target
        oracle = create_type_oracle(engine=engine, project_root=project_root)

        resolved_engine = engine
        if engine == "auto":
            resolved_engine = type(oracle).__name__.replace("Adapter", "").lower()

        if not oracle.is_available():
            print(f"Error: {resolved_engine} is not installed or not available on PATH.", file=sys.stderr)
            raise typer.Exit(2)

        if is_glob:
            files, _ = resolve_files(path)
        elif target.is_dir():
            files, _ = resolve_files(path)
        else:
            files = [target]

        all_bindings = []
        for f in files:
            ft = oracle.infer_file(f)
            for b in ft.bindings:
                if name and b.name != name:
                    continue
                if kind and b.binding_kind != kind:
                    continue
                if definitions_only and b.binding_kind != "definition":
                    continue
                all_bindings.append((str(f), b))

        if json_output:
            data = []
            for file_path, b in all_bindings:
                entry = {
                    "file": file_path,
                    "name": b.name,
                    "line": b.line,
                    "col_start": b.col_start,
                    "col_end": b.col_end,
                    "type": b.raw_type,
                    "kind": b.binding_kind,
                }
                data.append(entry)
            print(json_mod.dumps(data, indent=2))
        else:
            if not all_bindings:
                print("No type information found.")
            else:
                for file_path, b in all_bindings:
                    col_range = f"{b.col_start}-{b.col_end}" if b.col_end else str(b.col_start)
                    print(f"{file_path}:{b.line}:{col_range}  {b.name}: {b.raw_type}  ({b.binding_kind})")

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(2)


@app.command("index")
def index_cmd(
    path: Annotated[
        str,
        typer.Argument(help="Project root directory")
    ] = ".",
    jobs: Annotated[
        Optional[int],
        typer.Option("--jobs", "-j", help="Max parallel workers (default: CPU count)")
    ] = None,
    verbose: Annotated[
        int,
        typer.Option("--verbose", "-v", count=True, help="Increase verbosity (-v: show files, -vv: debug)")
    ] = 0,
    type_engine: Annotated[
        str,
        typer.Option(
            "--type-engine",
            help=(
                "Type inference engine for the type-cache phase. "
                "'auto' (default) detects from project config and PATH. "
                "'none' skips type indexing. "
                "Explicit choices: pyrefly, pyright, ty."
            ),
        ),
    ] = "auto",
    status: Annotated[
        bool,
        typer.Option("--status", help="Show index freshness status and exit")
    ] = False,
):
    """Pre-build caches for faster cross-project operations.

    Parses every Python file in the project and builds:
    - Parse cache (speeds up all pattern operations)
    - Qualified-name index (speeds up refs, rename, callers)
    - Symbol index (instant symbol lookup, typeahead, file outline)
    - Import graph (fast import-based file filtering)
    - Reference index (instant find-references, dead code detection)
    - Type-inference cache (speeds up :type[] / :returns[] queries)

    Run this once after cloning a repo or when starting work on a new
    codebase. Subsequent emend commands will be significantly faster.

    Examples:
        emend index
        emend index src/ --jobs 8
        emend index --status              # show index freshness
        emend index -v                    # show each file being indexed
        emend index -vv                   # debug-level logging
        emend index --type-engine none    # skip type indexing
        emend index --type-engine pyright # force pyright
    """
    if status:
        from emend.transform import get_index_status
        info = get_index_status(path)
        if info is None:
            print("No index found. Run `emend index` to build.", file=sys.stderr)
            raise typer.Exit(1)
        print(f"Index status for {path}:", file=sys.stderr)
        print(f"  Schema version:  {info.get('schema_version', 'unknown')}", file=sys.stderr)
        print(f"  Git HEAD:        {info.get('git_head', 'unknown')[:12]}{'...' if len(info.get('git_head', '')) > 12 else ''}", file=sys.stderr)
        print(f"  Indexed at:      {info.get('indexed_at', 'unknown')}", file=sys.stderr)
        print(f"  Files:           {info.get('file_manifest_count', 0)}", file=sys.stderr)
        print(f"  Symbols:         {info.get('symbol_index_count', 0)}", file=sys.stderr)
        print(f"  Import edges:    {info.get('import_graph_count', 0)}", file=sys.stderr)
        print(f"  References:      {info.get('reference_index_count', 0)}", file=sys.stderr)
        head_str = " (HEAD changed)" if info.get("git_head_changed") else ""
        stale = info.get("changed_files", 0) + info.get("new_files", 0)
        if stale:
            print(f"  Freshness:       {stale} files need re-indexing{head_str}", file=sys.stderr)
        else:
            print(f"  Freshness:       up to date{head_str}", file=sys.stderr)
        return

    import time as _time
    t0 = _time.monotonic()

    if verbose >= 2:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
    elif verbose >= 1:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    n_done = 0
    total = None

    def _progress(phase: str, file_path: str) -> None:
        nonlocal n_done
        n_done += 1
        if verbose >= 1:
            print(f"  {file_path}", file=sys.stderr)
        elif total and sys.stderr.isatty():
            pct = n_done * 100 // total
            print(f"\r  [{pct:3d}%] {n_done}/{total} files indexed", end="", file=sys.stderr)

    # Quick count for progress bar
    from emend.transform import _collect_source_files_scandir
    from pathlib import Path as _Path
    scan_root = str(_Path(path).resolve())
    total = len(_collect_source_files_scandir(scan_root))
    print(f"Indexing {total} source files in {scan_root}...", file=sys.stderr)

    from emend.type_oracle import TypeEngineUnavailableError
    try:
        stats = warm_caches(path, jobs=jobs, callback=_progress, type_engine=type_engine)
    except TypeEngineUnavailableError as exc:
        print("", file=sys.stderr)
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)

    if not verbose and sys.stderr.isatty():
        print("", file=sys.stderr)  # newline after progress

    elapsed = _time.monotonic() - t0
    skipped = stats.get("skipped", 0)
    new_indexed = stats["indexed"]
    new_qn = stats["qn_cached"]
    new_sym = stats.get("sym_cached", 0)
    new_ref = stats.get("ref_cached", 0)
    new_types = int(stats.get("type_cached", 0))
    engine_name = str(stats.get("type_engine", ""))

    indexed_qn = f"indexed: {new_indexed}, qn: {new_qn}"
    if skipped and not new_indexed and not new_qn:
        detail = f"all {skipped} already cached"
    elif skipped:
        detail = f"{indexed_qn}, already cached: {skipped}"
    else:
        detail = indexed_qn
    if new_sym:
        detail += f", symbols: {new_sym}"
    if new_ref:
        detail += f", refs: {new_ref}"
    if new_types:
        type_detail = f"types: {new_types}"
        if engine_name:
            type_detail += f" ({engine_name})"
        detail += f", {type_detail}"
    print(
        f"Indexed {stats['files']} files in {elapsed:.1f}s ({detail})",
        file=sys.stderr,
    )


@app.command("editor-search")
def editor_search_cmd(
    query: Annotated[str, typer.Argument(help="Search query (symbol name, pattern with $, or selector with ::)")],
    path: Annotated[
        str,
        typer.Argument(help="Project root or file scope")
    ] = ".",
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Max results")
    ] = 50,
    kind: Annotated[
        Optional[str],
        typer.Option("--kind", help="Symbol kind filter (function, class, method)")
    ] = None,
    mode: Annotated[
        Optional[str],
        typer.Option("--mode", help="Force search mode: symbol, pattern, selector, references")
    ] = None,
    file_scope: Annotated[
        Optional[str],
        typer.Option("--file", "-f", help="Restrict to file path (substring match)")
    ] = None,
):
    """Fast one-shot search (JSON output) for editor integration.

    Auto-detects search mode from the query:
    - Contains ``$`` → pattern search (``print($X)``)
    - Contains ``::`` → selector resolution (``file.py::Class.method``)
    - Otherwise → symbol name search

    Supports partial/incomplete patterns: ``foo(bar, $`` is auto-closed
    to ``foo(bar, $_)`` for matching.

    Examples:
        emend editor-search parse
        emend editor-search 'parse_pattern' --kind function
        emend editor-search 'src/emend/pattern.py::parse'
        emend editor-search 'print($X)' src/
        emend editor-search 'foo(bar, $' src/
    """
    from emend.editor_search import EditorSearchEngine

    engine = EditorSearchEngine(path)
    try:
        if mode == "references":
            result = engine.search_references(query, limit=limit)
        elif mode == "pattern":
            result = engine.search_pattern(query, limit=limit, file_scope=file_scope)
        elif mode == "symbols":
            result = engine.search_symbols(query, limit=limit, file_scope=file_scope, kind=kind)
        elif mode == "selector":
            result = engine.resolve_selector(query, limit=limit)
        else:
            result = engine.search(query, limit=limit, file_scope=file_scope, kind=kind)

        import json as _json
        from dataclasses import asdict as _asdict
        print(_json.dumps(_asdict(result), default=str))
    finally:
        engine.close()


@app.command("editor-server")
def editor_server_cmd(
    path: Annotated[
        str,
        typer.Argument(help="Project root directory")
    ] = ".",
):
    """Start a long-running search server for editor plugins (stdio JSON-RPC).

    Keeps the SQLite index and FTS5 trigram table warm in memory,
    giving sub-100ms response times for symbol search, pattern
    matching, and reference lookup.

    Each request is a JSON line on stdin, each response a JSON line on stdout.

    Methods:
        search         — auto-detect mode (symbol/pattern/selector)
        symbols        — symbol name search
        pattern        — code pattern search (supports partial input)
        references     — find references by qualified name
        selector       — resolve a selector (file.py::Class.method)
        file_symbols   — file outline
        status         — index status
        reindex        — refresh stale files + rebuild FTS
        shutdown       — clean exit

    Examples:
        emend editor-server
        emend editor-server src/

        # From the editor, send requests on stdin:
        {"id": 1, "method": "search", "params": {"query": "parse"}}
    """
    from emend.editor_search import run_editor_server

    run_editor_server(path)


@app.command("mcp")
def mcp_cmd(
    transport: Annotated[
        str,
        typer.Option("--transport", "-t", help="Transport protocol: stdio or sse")
    ] = "stdio",
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Port for SSE transport")
    ] = 8000,
):
    """Start an MCP (Model Context Protocol) server.

    Exposes emend commands as MCP tools for use by LLM-based clients.

    Requires the 'mcp' optional dependency:
        pip install emend[mcp]

    Examples:
        emend mcp
        emend mcp --transport sse --port 8080
    """
    try:
        from emend.mcp_server import run_server
    except ImportError:
        print(
            "Error: MCP dependencies not installed. "
            "Install with: pip install emend[mcp]",
            file=sys.stderr,
        )
        raise typer.Exit(2)

    run_server(transport=transport, port=port)


def main():
    try:
        app()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


__all__ = ["main", "app"]

if __name__ == "__main__":
    main()
