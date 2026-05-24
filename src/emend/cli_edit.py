import logging
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from emend import ast_commands
from emend.cli_base import (
    ApplyFlag,
    JsonFlag,
    _maybe_create_oracle,
    _reject_file_glob,
    _state,
    cli_error_handler,
    parse_where_clause,
    resolve_file_scopes,
    resolve_files,
)
from emend.component_selector import parse_extended_selector
from emend.transform import (
    cmd_add,
    cmd_edit,
    extract_pattern_literals,
    move_module,
    move_symbol,
    rename_module,
    rename_symbol,
    replace_pattern,
    safe_delete,
)
from emend.cli_output import emit_json

def edit_set_cmd(
    selector: Annotated[str, typer.Argument(help="Symbol selector (file.py::Symbol[component])")],
    value: Annotated[
        Optional[str],
        typer.Argument(help="New value (empty to remove)")
    ] = None,
    rm: Annotated[
        bool,
        typer.Option("--rm", help="Remove component or symbol")
    ] = False,
    apply: ApplyFlag = False,
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
    with cli_error_handler():
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


def remove_cmd(
    selector: Annotated[str, typer.Argument(help="Symbol selector (file.py::Symbol or file.py::Symbol[component])")],
    apply: ApplyFlag = False,
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
    with cli_error_handler():
        result = cmd_edit(selector_str=selector, rm=True, apply=apply)
        print(result, end='')



def delete_cmd(
    selector: Annotated[str, typer.Argument(help="Symbol selector (file.py::Symbol)")],
    cascade: Annotated[
        bool,
        typer.Option("--cascade", help="Transitively delete symbols that become dead after removal")
    ] = False,
    apply: ApplyFlag = False,
    json_output: JsonFlag = False,
    project: Annotated[
        Optional[str],
        typer.Option("--project", help="Project root directory")
    ] = None,
):
    """Delete a symbol with optional cascading removal of newly-dead code.

    Without --cascade, removes the target symbol only (like ``rm``).
    With --cascade, identifies symbols that become unreferenced after
    the deletion and removes them transitively.

    Dry-run by default: prints the plan and diffs without modifying files.

    Examples:
        # Preview what would be deleted
        emend delete models.py::LegacyUser --cascade

        # Apply the deletion
        emend delete models.py::LegacyUser --cascade --apply

        # Simple single-symbol delete (same as rm)
        emend delete api.py::deprecated_function --apply

        # JSON output for tooling
        emend delete models.py::LegacyUser --cascade --json
    """
    with cli_error_handler():
        sel = parse_extended_selector(selector)
        plan = safe_delete(
            sel, cascade=cascade, project_path=project, apply=apply,
        )

        if json_output:
            data = {
                "target": plan.target,
                "deletions": plan.deletions,
                "files_affected": list(plan.diffs.keys()),
                "applied": apply,
            }
            emit_json(data)
        else:
            if not plan.deletions:
                print("Nothing to delete.")
                raise typer.Exit(0)

            action = "Deleted" if apply else "Would remove"
            print(f"{action}:")
            for d in plan.deletions:
                print(f"  {d['file_path']}:{d['line']}  {d['name']} ({d['kind']}) - {d['reason']}")

            count = len(plan.deletions)
            files = len(plan.diffs)
            print(f"\n{count} symbol(s) across {files} file(s).", file=sys.stderr)

            if plan.diffs:
                print()
                for diff in plan.diffs.values():
                    print(diff, end='')



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
    apply: ApplyFlag = False,
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
    with cli_error_handler():
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



def replace_cmd(
    pattern: Annotated[str, typer.Argument(help="Pattern to find (e.g., 'print($X)')")],
    replacement: Annotated[str, typer.Argument(help="Replacement pattern (e.g., 'logger.info($X)')")],
    paths: Annotated[list[str], typer.Argument(help="File, glob, or directory scope(s) to modify")],
    apply: ApplyFlag = False,
    within: Annotated[
        Optional[str],
        typer.Option("--within", help="Only replace inside this structural container")
    ] = None,
    not_within: Annotated[
        Optional[str],
        typer.Option("--not-within", help="Exclude replacements inside this structural container")
    ] = None,
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
    Paths can be files, glob patterns, or directories.

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
    with cli_error_handler():
        where_params = parse_where_clause(where or [])
        scope = where_params.get("scope")
        inside = within or where_params.get("inside")
        not_inside = not_within or where_params.get("not_inside")

        # Create TypeOracle when --type-engine is specified or pattern contains
        # oracle constraints (:type[X] / :returns[X]).
        oracle = None
        if type_engine is not None or ":type[" in pattern or ":returns[" in pattern:
            oracle = _maybe_create_oracle(type_engine)

        _lang = _state["language"]
        files, is_multi_file = resolve_file_scopes(paths, language=_lang)

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



def copy_to_cmd(
    selector: Annotated[str, typer.Argument(help="Selector (file.py::Symbol.path)")],
    destination: Annotated[str, typer.Argument(help="Destination file path")],
    append: Annotated[bool, typer.Option("--append", help="Append to destination file")] = False,
    dedent: Annotated[bool, typer.Option("--dedent", help="Dedent the copied symbol (useful for nested functions)")] = False,
    apply: Annotated[bool, typer.Option("--apply", "-a", help="Apply the changes")] = False,
    project: Annotated[Optional[str], typer.Option("--project", "-p", help="Project root directory")] = None,
):
    """Copy a symbol to another file.

    Examples:
        emend cp file.py::my_function other.py --apply
        emend cp file.py::MyClass other.py --append --apply
        emend cp file.py::outer.inner other.py --dedent --apply
    """
    with cli_error_handler():
        ast_commands.cmd_copy_to(selector, destination, append, dedent, apply, project_path=project)



def rename_cmd(
    selector: Annotated[str, typer.Argument(help="Selector (file.py::Symbol for symbol rename, or file.py for module rename)")],
    new_name: Annotated[str, typer.Option("--to", help="New name")],
    apply: ApplyFlag = False,
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
    with cli_error_handler():
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



def move_cmd(
    selector: Annotated[str, typer.Argument(help="Selector (file.py::Symbol for symbol move, or file.py for module move)")],
    destination: Annotated[str, typer.Argument(help="Destination file or package")],
    dedent: Annotated[bool, typer.Option("--dedent", help="Dedent nested symbols (symbol mode only)")] = False,
    no_update_imports: Annotated[bool, typer.Option("--no-update-imports", help="Don't update imports (symbol mode only)")] = False,
    apply: ApplyFlag = False,
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
    with cli_error_handler():
        if '::' in selector:
            # Symbol move mode
            _reject_file_glob(selector, "move")
            parsed_selector = parse_extended_selector(selector)
            diffs = move_symbol(
                parsed_selector,
                destination,
                dedent=dedent,
                update_imports=not no_update_imports,
                project_path=project,
                apply=apply,
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



def batch_cmd(
    ops_file: Annotated[str, typer.Argument(help="YAML or JSON file with operations")],
    apply: ApplyFlag = False,
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
    import json as json_mod

    with cli_error_handler():
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



def saturate_cmd(
    path: Annotated[str, typer.Argument(help="File or directory to rewrite")],
    config: Annotated[Optional[str], typer.Option("--config", help="Path to rewrites.yaml")] = None,
    apply: Annotated[bool, typer.Option("--apply", "-a", help="Apply rewrites")] = False,
    max_iterations: Annotated[int, typer.Option("--max-iterations", help="Max saturation iterations")] = 30,
    json_output: JsonFlag = False,
):
    """Experimental: apply equality saturation rewrites.

    Uses e-graph equality saturation to find optimal rewrites for
    expressions matching rules in .emend/rewrites.yaml.

    This is an experimental feature. Use --apply to write changes.

    Examples:
        emend saturate src/                      # dry-run with default config
        emend saturate src/ --config rules.yaml  # custom rules
        emend saturate file.py --apply           # apply rewrites
        emend saturate src/ --json               # JSON output
    """
    try:
        from emend.rewrite_engine import load_rewrite_rules, run_saturation

        if config is None:
            config = ".emend/rewrites.yaml"
        config_path = Path(config)
        if not config_path.exists():
            print(f"Error: Config file not found: {config}", file=sys.stderr)
            raise typer.Exit(2)

        rules = load_rewrite_rules(str(config_path))
        if not rules:
            print("No rewrite rules configured.", file=sys.stderr)
            raise typer.Exit(0)

        _lang = _state["language"]
        resolved, _ = resolve_files(path, language=_lang)
        files = [str(f) for f in resolved]

        all_results = []
        for file_path in files:
            results = run_saturation(
                str(file_path), rules,
                max_iterations=max_iterations,
            )
            all_results.extend(results)

        if json_output:
            data = [
                {
                    "file": r.file_path,
                    "line": r.line,
                    "col": r.col,
                    "original": r.original_text,
                    "rewritten": r.rewritten_text,
                    "rules": r.rules_applied,
                }
                for r in all_results
            ]
            emit_json(data)
        elif not all_results:
            print("No rewrites found.")
        else:
            for r in all_results:
                print(f"{r.file_path}:{r.line}:{r.col}")
                print(f"  - {r.original_text}")
                print(f"  + {r.rewritten_text}")
                print(f"  rules: {', '.join(r.rules_applied)}")
            print(f"\nFound {len(all_results)} rewrite(s).", file=sys.stderr)
            if not apply:
                print("Dry-run. Use --apply to write changes.", file=sys.stderr)

        if apply and all_results:
            # Apply rewrites by file, bottom-up to preserve offsets
            from collections import defaultdict
            by_file: dict[str, list] = defaultdict(list)
            for r in all_results:
                by_file[r.file_path].append(r)

            for fpath, results in by_file.items():
                source = Path(fpath).read_text()
                lines = source.split("\n")
                # Sort by line desc to apply bottom-up
                for r in sorted(results, key=lambda x: x.line, reverse=True):
                    if 1 <= r.line <= len(lines):
                        lines[r.line - 1] = lines[r.line - 1].replace(
                            r.original_text, r.rewritten_text, 1
                        )
                Path(fpath).write_text("\n".join(lines))
            print(f"Applied {len(all_results)} rewrite(s).", file=sys.stderr)

    except typer.Exit:
        raise
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)

