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
    find_callers, find_callees, generate_graph,
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
        return list(path_obj.rglob("*.py")), True
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

# Create app with emend commands
app = typer.Typer(
    help="Python refactoring CLI",
    no_args_is_help=True,
    add_completion=False,
)


# ============================================================================
# Unified Commands
# ============================================================================

@app.command("search")
def search(
    query: Annotated[str, typer.Argument(help="Pattern with $X metavars, or selector path (file.py::sym[component])")],
    path: Annotated[Optional[str], typer.Argument(help="File, glob, or directory to search")] = None,
    kind: Annotated[
        Optional[list[str]],
        typer.Option("--kind", help="Symbol kind filter (function, method, class, async_*)")
    ] = None,
    name: Annotated[
        Optional[list[str]],
        typer.Option("--name", help="Name pattern filter (glob or /regex/)")
    ] = None,
    has_decorator: Annotated[
        Optional[list[str]],
        typer.Option("--has-decorator", help="Decorator filter")
    ] = None,
    returns: Annotated[
        Optional[list[str]],
        typer.Option("--returns", help="Return type filter")
    ] = None,
    in_class: Annotated[
        Optional[list[str]],
        typer.Option("--in-class", help="Restrict to methods of named class")
    ] = None,
    depth: Annotated[
        Optional[list[str]],
        typer.Option("--depth", help="Nesting depth filter")
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
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON")
    ] = False,
    metadata: Annotated[
        bool,
        typer.Option("--metadata", help="Include location metadata")
    ] = False,
    paths_only: Annotated[
        bool,
        typer.Option("--paths-only", help="Output only selector paths")
    ] = False,
    count: Annotated[
        bool,
        typer.Option("--count", help="Output only count of matches")
    ] = False,
    dedent: Annotated[
        bool,
        typer.Option("--dedent", help="Dedent the source code")
    ] = False,
    scope_in: Annotated[
        Optional[str],
        typer.Option("--in", help="Limit search to within a symbol (e.g., 'MyClass' or 'my_func')")
    ] = None,
    inside: Annotated[
        Optional[str],
        typer.Option("--inside", help="Only match inside this structure (def, class, for, while, try, with, if)")
    ] = None,
    not_inside: Annotated[
        Optional[str],
        typer.Option("--not-inside", help="Only match outside this structure")
    ] = None,
    where: Annotated[
        Optional[str],
        typer.Option("--where", help="Only match inside structures matching this pattern (e.g., 'class MyClass', 'def test_*')")
    ] = None,
    output_selectors: Annotated[
        bool,
        typer.Option("--output-selectors", help="Output file.py::ContainingSymbol instead of file.py:line")
    ] = False,
    imported_from: Annotated[
        Optional[str],
        typer.Option("--imported-from", help="Only match when root name is imported from this module (pattern mode)")
    ] = None,
    scope_local: Annotated[
        bool,
        typer.Option("--scope-local", help="Only match locally-defined names, exclude imports (pattern mode)")
    ] = False,
    matching: Annotated[
        Optional[str],
        typer.Option("--matching", help="Filter to symbols whose body matches this pattern (lookup mode)")
    ] = None,
    callees_mode: Annotated[
        bool,
        typer.Option("--callees", help="List all functions/methods called by the target function (query must be a selector with ::)")
    ] = False,
    project: Annotated[
        Optional[str],
        typer.Option("--project", "-p", help="Project root directory (callees mode)")
    ] = None,
):
    """Unified search: auto-detects pattern matching vs symbol lookup.

    If the query contains metavariables ($X, $...Y), uses pattern matching mode.
    If the query contains :: or is a plain file path, uses symbol lookup mode.
    With --callees, lists all functions/methods called by the target function.

    Examples:
        # Pattern mode (has $):
        emend search 'print($X)' file.py
        emend search 'assertEqual($A, $B)' tests/ --count

        # Lookup mode (has :: or file path):
        emend search file.py::func[params]
        emend search src/ --kind function --has-decorator pytest

        # Callees mode:
        emend search src/module.py::main --callees
        emend search src/module.py::main --callees --json
    """
    if callees_mode:
        try:
            _reject_file_glob(query, "search --callees")
            parsed_selector = parse_extended_selector(query)
            callees = find_callees(parsed_selector, project_path=project)

            if json_output:
                import json
                data = [
                    {
                        "name": c.name,
                        "qualified_name": c.qualified_name,
                        "line": c.line,
                    }
                    for c in callees
                ]
                print(json.dumps(data, indent=2))
            else:
                for c in callees:
                    if c.qualified_name:
                        print(f"{c.name} ({c.qualified_name})")
                    else:
                        print(c.name)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            raise typer.Exit(3)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            raise typer.Exit(2)
        except Exception as e:
            print(f"Error: {e!r}", file=sys.stderr)
            raise typer.Exit(1)
        return

    # Detect mode: $ in query → pattern matching, otherwise → lookup
    is_pattern_mode = "$" in query

    if is_pattern_mode:
        # Pattern matching mode (delegates to find logic)
        target_path = path or "."
        try:
            import libcst as cst

            scope, scope_file_override = parse_scope_in(scope_in, target_path)
            if scope_file_override:
                target_path = scope_file_override

            files, is_multi_file = resolve_files(target_path)

            all_matches = []
            for file_path in files:
                file_path_str = str(file_path)
                try:
                    file_matches = find_pattern(query, file_path_str, scope=scope, inside=inside, not_inside=not_inside, imported_from=imported_from, where=where, scope_local=scope_local)
                    for match in file_matches:
                        all_matches.append((file_path_str, match))
                except FileNotFoundError:
                    if not is_multi_file:
                        raise
                    continue

            if count:
                print(len(all_matches))
            elif json_output:
                import json
                serialized_matches = []
                for file_path_str, match in all_matches:
                    code = cst.Module([]).code_for_node(match.node).strip()
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
                        "code": code,
                        "captures": captures
                    })
                print(json.dumps({"count": len(all_matches), "matches": serialized_matches}))
            else:
                if all_matches:
                    if output_selectors:
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
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            raise typer.Exit(3)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            raise typer.Exit(2)
        except Exception as e:
            print(f"Error: {e!r}", file=sys.stderr)
            raise typer.Exit(1)
    else:
        # Lookup mode
        try:
            import re
            file_or_pattern = query
            selector_str = None

            line_selector_pattern = r':\d+(-\d+)?$'
            is_line_selector = re.search(line_selector_pattern, query) is not None

            if '::' in query or is_line_selector:
                selector_str = query
                if '::' in query:
                    parts = query.split('::', 1)
                    file_or_pattern = parts[0]
                elif is_line_selector:
                    match = re.search(r'^(.+?):\d+', query)
                    if match:
                        file_or_pattern = match.group(1)

            result = cmd_lookup(
                file_or_pattern=file_or_pattern,
                selector_str=selector_str,
                kind=kind,
                name=name,
                has_decorator=has_decorator,
                returns=returns,
                in_class=in_class,
                depth=depth,
                has_param=has_param,
                case_insensitive=case_insensitive,
                smart_case=smart_case,
                json_output=json_output,
                metadata=metadata,
                paths_only=paths_only,
                count=count,
                dedent=dedent,
                matching=matching,
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
    scope_in: Annotated[
        Optional[str],
        typer.Option("--in", help="Limit replacements to within a symbol (e.g., 'MyClass' or 'my_func')")
    ] = None,
    inside: Annotated[
        Optional[str],
        typer.Option("--inside", help="Only replace inside this structure (def, class, for, while, try, with, if)")
    ] = None,
    not_inside: Annotated[
        Optional[str],
        typer.Option("--not-inside", help="Only replace outside this structure")
    ] = None,
    where: Annotated[
        Optional[str],
        typer.Option("--where", help="Only replace inside structures matching this pattern (e.g., 'class MyClass', 'def test_*')")
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
        emend replace 'old_name' 'new_name' file.py --in my_func --apply
        emend replace 'print($X)' 'logger.info($X)' file.py --inside def --apply
        emend replace 'print($X)' 'logger.info($X)' file.py --inside 'def test_*' --apply
        emend replace '$X = $Y' '$X: int = $Y' src/*.py --not-inside class --apply
    """
    try:
        scope, scope_file_override = parse_scope_in(scope_in, path)
        search_path = scope_file_override or path
        files, is_multi_file = resolve_files(search_path)

        # Collect diffs and count across all files
        all_diffs = []
        total_count = 0
        for file_path in files:
            file_path_str = str(file_path)
            try:
                diff, count = replace_pattern(pattern, replacement, file_path_str, scope=scope, apply=apply, inside=inside, not_inside=not_inside, where=where)
                if diff:  # Only include files with changes
                    all_diffs.append(diff)
                total_count += count
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

        rules, macros = load_rules(str(config_path))

        resolved, _ = resolve_files(path)
        files = [str(f) for f in resolved]

        violations = run_lint(rules, files, fix=fix, rule_filter=rule)

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
    calls_only: Annotated[bool, typer.Option("--calls-only", help="Only show call sites (equivalent to former callers command)")] = False,
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


@app.command("list-symbols")
def list_symbols(
    file: Annotated[str, typer.Argument(help="Module file")],
    tree_depth: Annotated[Optional[int], typer.Option("--tree-depth", help="Nesting depth (default: unlimited)")] = None,
    flat: Annotated[bool, typer.Option("--flat", help="Flat output with full paths")] = False,
    selector: Annotated[Optional[str], typer.Option("--selector", "-s", help="Filter to symbol path (e.g., 'Calculator.add')")] = None,
    project: Annotated[Optional[str], typer.Option("--project", "-p", help="Project root directory")] = None,
):
    """List symbols (classes, functions, variables) in a module."""
    from emend.ast_commands import cmd_list_symbols
    cmd_list_symbols(file, project, tree_depth, flat, selector)





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


def main():
    try:
        app()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


__all__ = ["main", "app"]

if __name__ == "__main__":
    main()
