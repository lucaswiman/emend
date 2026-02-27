"""emend - Python refactoring CLI with structured edits and pattern transforms."""

import glob as glob_mod
import sys
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
    extract_pattern_literals,
)
from emend import ast_commands


def _reject_file_glob(selector_str: str, command_name: str) -> None:
    """Raise ValueError if selector contains file globs (for commands that don't support them)."""
    if '*' in selector_str.split('::')[0] or '?' in selector_str.split('::')[0]:
        raise ValueError(
            f"File glob selectors are not supported for {command_name}. "
            "Use a specific file path instead."
        )


def resolve_files(path: str) -> tuple[list[Path], bool]:
    """Resolve a path argument to a list of Python files.

    Args:
        path: A file path, directory, or glob pattern.

    Returns:
        (files, is_multi_file) tuple.
    """
    path_obj = Path(path)
    if path_obj.is_dir():
        from emend import emend_core
        abs_path = str(path_obj.resolve())
        return [Path(f) for f in emend_core.collect_python_files(abs_path)], True
    elif "*" in path or "?" in path:
        return [Path(f) for f in glob_mod.glob(path, recursive=True) if f.endswith('.py')], True
    else:
        return [path_obj], False


def parse_scope_in(scope_in: str | None, default_path: str | None = None) -> tuple[list[str] | None, str | None]:
    """Parse --in argument, supporting both dotted paths and selectors.

    Args:
        scope_in: The --in value (e.g., 'MyClass.method' or '**/*.py::MyClass.method')
        default_path: The default file path if --in doesn't provide one.

    Returns:
        (scope, file_path_override) tuple. file_path_override is set only
        if the --in value contained a :: selector with a file path.
    """
    if not scope_in:
        return None, None

    if '::' in scope_in:
        sel = parse_extended_selector(scope_in)
        scope = sel.symbol_path if sel.symbol_path else None
        # Only override path if the selector has a file glob or specific file
        file_override = sel.file_path if sel.file_path else None
        return scope, file_override

    return scope_in.split("."), None


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
) -> None:
    pass


# ============================================================================
# Unified Commands
# ============================================================================

@app.command("search")
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
):
    """Unified search: auto-detects pattern matching vs symbol lookup.

    If the query contains metavariables ($X, $...Y), uses pattern matching mode.
    If the query contains :: or is a plain file path, uses symbol lookup mode.
    A bare file/dir path with no filters shows a symbol summary.

    Output formats (--output):
        code          Full source of matched symbol(s) [default for selector]
        location      file.py:line [default for pattern mode]
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

        # Lookup mode (has :: or file path):
        emend search file.py::func[params]
        emend search src/ --kind function --where '@app.command'

        # Summary mode (list symbols):
        emend search file.py
        emend search file.py::MyClass --output summary
        emend search file.py --output summary::flat
    """
    import re as _re

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

    # Detect query shape
    is_pattern_mode = "$" in query
    is_line_selector = _re.search(r':\d+(-\d+)?$', query) is not None
    has_selector = '::' in query and not is_pattern_mode

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
        effective_output = "location"
    elif has_component:
        effective_output = "component"
    elif has_selector or is_line_selector:
        effective_output = "code"
    elif not has_filters:
        effective_output = "summary"
    else:
        effective_output = "selector"

    try:
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
            if file_path_obj.is_dir() or '*' in file_for_summary or '?' in file_for_summary:
                files, _ = resolve_files(file_for_summary)
                from emend import emend_core
                file_strs = [str(fp) for fp in files]
                batch_results = emend_core.collect_symbols_batch(
                    file_strs, max_depth=tree_depth, selector=selector_for_summary,
                )
                for file_path_str, symbol_dicts in batch_results:
                    symbols = ast_commands.dicts_to_tree_symbols(symbol_dicts)
                    print(f"\nModule: {file_path_str}")
                    if symbols:
                        if flat_output:
                            ast_commands._print_symbol_flat(symbols)
                        else:
                            ast_commands._print_symbol_tree(symbols, indent=1)
            else:
                symbols = ast_commands.collect_symbols(
                    file_for_summary, tree_depth=tree_depth, selector=selector_for_summary
                )
                print(f"\nModule: {file_for_summary}")
                if symbols:
                    if flat_output:
                        ast_commands._print_symbol_flat(symbols)
                    else:
                        ast_commands._print_symbol_tree(symbols, indent=1)
            return

        # ---- PATTERN MODE ----
        if is_pattern_mode:
            target_path = path or "."
            import libcst as cst
            from emend import emend_core

            files, is_multi_file = resolve_files(target_path)

            all_matches = []
            if is_multi_file and len(files) > 1:
                # Single Rust pass: parallel read + filter (no double read)
                file_strs = [str(f) for f in files]
                literals = extract_pattern_literals(query)
                file_contents = emend_core.read_and_filter_files(file_strs, literals)

                # Try Rust batch fast-path (no scope/imported_from/scope_local constraints)
                rust_path_used = False
                if where_scope is None and imported_from is None and not scope_local:
                    from emend.pattern import compile_pattern_to_rust_ir, compile_constraint_to_rust_ir
                    from emend.transform import PatternMatch
                    pattern_ir = compile_pattern_to_rust_ir(query)
                    if pattern_ir is not None:
                        inside_ir = compile_constraint_to_rust_ir(where_inside) if where_inside else None
                        not_inside_ir = compile_constraint_to_rust_ir(where_not_inside) if where_not_inside else None
                        if (where_inside is None or inside_ir is not None) and \
                           (where_not_inside is None or not_inside_ir is not None):
                            raw_matches = emend_core.find_pattern_in_files(
                                list(file_contents), pattern_ir, inside_ir, not_inside_ir
                            )
                            for file_path_str, line, _col, _end_line, _end_col, text in raw_matches:
                                all_matches.append((file_path_str, PatternMatch(
                                    node=None, captures={}, line=line, matched_text=text
                                )))
                            rust_path_used = True

                if not rust_path_used:
                    for file_path_str, content in file_contents:
                        try:
                            file_matches = find_pattern(
                                query, file_path_str,
                                scope=where_scope,
                                inside=where_inside,
                                not_inside=where_not_inside,
                                imported_from=imported_from,
                                scope_local=scope_local,
                                source_override=content,
                            )
                            for match in file_matches:
                                all_matches.append((file_path_str, match))
                        except Exception:
                            continue
            else:
                for file_path in files:
                    file_path_str = str(file_path)
                    try:
                        file_matches = find_pattern(
                            query, file_path_str,
                            scope=where_scope,
                            inside=where_inside,
                            not_inside=where_not_inside,
                            imported_from=imported_from,
                            scope_local=scope_local,
                        )
                        for match in file_matches:
                            all_matches.append((file_path_str, match))
                    except FileNotFoundError:
                        if not is_multi_file:
                            raise
                        continue

            if count_output:
                print(len(all_matches))
            elif json_output:
                import json
                serialized_matches = []
                for file_path_str, match in all_matches:
                    if match.matched_text is not None:
                        code_str = match.matched_text.strip()
                    else:
                        code_str = cst.Module([]).code_for_node(match.node).strip()
                    captures = {}
                    for cap_name, captured in match.captures.items():
                        if isinstance(captured, tuple):
                            items = []
                            for item in captured:
                                items.append(cst.Module([]).code_for_node(item).strip())
                            captures[cap_name] = ", ".join(items)
                        else:
                            captures[cap_name] = cst.Module([]).code_for_node(captured).strip()
                    serialized_matches.append({
                        "file": file_path_str,
                        "line": match.line,
                        "code": code_str,
                        "captures": captures
                    })
                print(json.dumps({"count": len(all_matches), "matches": serialized_matches}))
            else:
                if all_matches:
                    if effective_output == "selector":
                        from emend.ast_utils import find_nested_definitions, find_symbol_by_line
                        _defs_cache: dict[str, list] = {}
                        seen: set[str] = set()
                        for file_path_str, match in all_matches:
                            if file_path_str not in _defs_cache:
                                _defs_cache[file_path_str] = find_nested_definitions(file_path_str)
                            if match.line is not None:
                                sym = find_symbol_by_line(_defs_cache[file_path_str], match.line)
                                if sym:
                                    sel_path = f"{file_path_str}::{'.'.join(sym.path)}"
                                    if sel_path not in seen:
                                        seen.add(sel_path)
                                        print(sel_path)
                                else:
                                    print(f"{file_path_str}:{match.line}")
                            else:
                                print(f"{file_path_str}:?")
                    else:
                        for file_path_str, match in all_matches:
                            if match.line is not None:
                                print(f"{file_path_str}:{match.line}")
                            else:
                                print(f"{file_path_str}:?")
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


app.command("query", hidden=True)(search)
app.command("show", hidden=True)(search)
app.command("get", hidden=True)(search)
app.command("lookup", hidden=True)(search)
app.command("find", hidden=True)(search)



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
    """
    try:
        result = cmd_edit(
            selector_str=selector,
            value=value,
            rm=rm,
            apply=apply,
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
    """
    try:
        result = cmd_add(
            selector_str=selector,
            value=value,
            before=before,
            after=after,
            at=at,
            apply=apply,
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
    """
    try:
        where_params = parse_where_clause(where or [])
        scope = where_params.get("scope")
        inside = where_params.get("inside")
        not_inside = where_params.get("not_inside")

        search_path = path
        files, is_multi_file = resolve_files(search_path)

        # Collect diffs and count across all files
        all_diffs = []
        total_count = 0
        for file_path in files:
            file_path_str = str(file_path)
            try:
                diff, cnt = replace_pattern(
                    pattern, replacement, file_path_str,
                    scope=scope, apply=apply,
                    inside=inside, not_inside=not_inside,
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

        resolved, _ = resolve_files(path)
        files = [str(f) for f in resolved]

        violations = run_lint(
            rules, files, fix=fix, rule_filter=rule,
            deadcode_config=deadcode_config, project_path=path,
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


@app.command("copy-to")
def copy_to_cmd(
    selector: Annotated[str, typer.Argument(help="Selector (file.py::Symbol.path)")],
    destination: Annotated[str, typer.Argument(help="Destination file path")],
    append: Annotated[bool, typer.Option("--append", help="Append to destination file")] = False,
    dedent: Annotated[bool, typer.Option("--dedent", help="Dedent the copied symbol (useful for nested functions)")] = False,
    apply: Annotated[bool, typer.Option("--apply", "-a", help="Apply the changes")] = False,
):
    """Copy a symbol to another file.

    Examples:
        emend copy-to file.py::my_function other.py --apply
        emend copy-to file.py::MyClass other.py --append --apply
        emend copy-to file.py::outer.inner other.py --dedent --apply
    """
    ast_commands.cmd_copy_to(selector, destination, append, dedent, apply)



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

    Uses LibCST to find usages, not just text matches.
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
                    print(f"{ref.file_path}:{ref.line}")
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
                print(f"{ref.file_path}:{ref.line}{marker}")
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(2)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)


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


@app.command("move")
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
        emend move file.py::helper_func dest.py
        emend move file.py::MyClass dest.py --apply
        emend move utils.py pkg --project . --apply
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

                files, _ = resolve_files(target_path)

                op_diffs = []
                for fp in files:
                    try:
                        diff, cnt = replace_pattern(
                            pattern, replacement, str(fp), apply=apply
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
    kind: Annotated[Optional[str], typer.Option("--kind", "-k", help="Filter by binding kind: definition, reference, import")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    engine: Annotated[str, typer.Option("--engine", help="Type inference engine")] = "pyrefly",
    definitions_only: Annotated[bool, typer.Option("--definitions-only", "-d", help="Show only definitions")] = False,
):
    """Show inferred types for symbols in a file.

    Uses Pyrefly (or another type inference engine) to analyze source
    files and display inferred types for all symbols and expressions.

    Examples:
        emend types src/models/user.py
        emend types src/models/user.py --name User
        emend types src/models/ --definitions-only --json
        emend types app.py --engine pyrefly
    """
    from emend.type_oracle import create_type_oracle
    import json as json_mod

    try:
        oracle = create_type_oracle(engine=engine)

        if not oracle.is_available():
            print(f"Error: {engine} is not installed or not available on PATH.", file=sys.stderr)
            raise typer.Exit(2)

        target = Path(path)
        if target.is_dir():
            files, _ = resolve_files(path)
        elif "*" in path or "?" in path:
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


def main():
    try:
        app()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


__all__ = ["main", "app"]

if __name__ == "__main__":
    main()
