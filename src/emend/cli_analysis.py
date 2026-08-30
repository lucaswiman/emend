import logging
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from emend.cli_base import (
    JsonFlag,
    _reject_file_glob,
    _state,
    cli_error_handler,
    resolve_files,
)
from emend.component_selector import parse_extended_selector
from emend.checks.rules_config import resolve_rules_path
from emend.errors import BUG_EXCEPTIONS
from emend.transform import (
    DeadBlock,
    DeadModule,
    dead_code_result_to_dict,
    find_callers,
    find_dead_code,
    find_impact,
    find_references,
    generate_graph,
)
from emend.cli_output import emit_json

logger = logging.getLogger("emend.cli.analysis")


def _extract_dsl_symbols(files, dsl_filter=None):
    """Detect DSL regions across *files* and extract their symbols per kind."""
    from emend.dsl import (
        DslKind, detect_dsl_regions, extract_sql_symbols,
        extract_jinja_symbols, extract_graphql_symbols,
    )

    all_symbols: list = []
    for file_path in files:
        regions = detect_dsl_regions(str(file_path), dsls=dsl_filter)
        for region in regions:
            if region.dsl == DslKind.SQL:
                all_symbols.extend(extract_sql_symbols(region))
            elif region.dsl == DslKind.JINJA:
                all_symbols.extend(extract_jinja_symbols(region))
            elif region.dsl == DslKind.GRAPHQL:
                all_symbols.extend(extract_graphql_symbols(region))
    return all_symbols


def _resolve_dsl_links(symbols, project_root, orm="sqlalchemy"):
    """Resolve cross-language links for DSL *symbols* per kind."""
    from emend.dsl import (
        DslKind, resolve_orm_links, resolve_jinja_links, resolve_graphql_links,
    )

    links: list = []
    sql_syms = [s for s in symbols if s.dsl == DslKind.SQL]
    jinja_syms = [s for s in symbols if s.dsl == DslKind.JINJA]
    gql_syms = [s for s in symbols if s.dsl == DslKind.GRAPHQL]
    if sql_syms:
        links.extend(resolve_orm_links(sql_syms, project_root, orm=orm))
    if jinja_syms:
        links.extend(resolve_jinja_links(jinja_syms, project_root))
    if gql_syms:
        links.extend(resolve_graphql_links(gql_syms, project_root))
    return links


def _trace_cmd_impl(
    path: str,
    config: str | None,
    label: str | None,
    trace: bool,
    json_output: bool,
    project: str | None,
    interprocedural: bool,
    max_iterations: int,
    preset: str | None,
    exclude_path: list[str] | None = None,
    max_chain_depth: int | None = None,
) -> None:
    """Shared implementation for the ``trace`` command."""
    try:
        from emend.trace import load_trace_config, run_trace_analysis, format_violations

        config_path = resolve_rules_path(config)
        if not config_path.exists() and preset is None:
            print(f"Error: Config file not found: {config_path}", file=sys.stderr)
            raise typer.Exit(2)

        if config_path.exists():
            trace_config = load_trace_config(str(config_path))
        else:
            from emend.trace import TraceConfig as _TraceConfig
            trace_config = _TraceConfig()

        if preset:
            from emend.trace_presets import get_preset, merge_configs
            preset_config = get_preset(preset)
            trace_config = merge_configs(trace_config, preset_config)
        if exclude_path:
            trace_config.exclude_paths = list(trace_config.exclude_paths) + list(exclude_path)

        if not trace_config.sources:
            print("No trace sources configured.", file=sys.stderr)
            raise typer.Exit(0)
        if not trace_config.sinks:
            print("No trace sinks configured.", file=sys.stderr)
            raise typer.Exit(0)

        _lang = _state["language"]
        resolved, _ = resolve_files(path, language=_lang)
        files = [str(f) for f in resolved]

        _proj_root = project or str(Path(path).resolve() if Path(path).is_dir() else Path(path).resolve().parent)

        if interprocedural:
            from emend.trace import run_interprocedural_trace
            result = run_interprocedural_trace(
                files, trace_config,
                label_filter=label,
                language=_lang,
                max_iterations=max_iterations,
                max_chain_depth=max_chain_depth,
                project_path=_proj_root,
            )
            violations = result.violations
            if not json_output:
                print(
                    f"Interprocedural analysis: {result.iterations} iteration(s), "
                    f"{len(result.summaries)} function summary(ies)",
                    file=sys.stderr,
                )
        else:
            violations = run_trace_analysis(
                files, trace_config,
                label_filter=label,
                language=_lang,
                project_path=_proj_root,
            )

        output = format_violations(violations, show_trace=trace, json_output=json_output)
        if output:
            print(output, end='\n' if not output.endswith('\n') else '')

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



def trace_cmd(
    path: Annotated[str, typer.Argument(help="File or directory to analyze")],
    config: Annotated[Optional[str], typer.Option("--config", help="Path to rules.yaml")] = None,
    label: Annotated[Optional[str], typer.Option("--label", help="Only check a specific trace label")] = None,
    trace: Annotated[bool, typer.Option("--trace", help="Show full propagation traces")] = False,
    json_output: JsonFlag = False,
    project: Annotated[Optional[str], typer.Option("--project", "-p", help="Project root")] = None,
    interprocedural: Annotated[bool, typer.Option("--interprocedural", help="Enable cross-function trace tracking")] = False,
    max_iterations: Annotated[int, typer.Option("--max-iterations", help="Max fixed-point iterations (interprocedural only)")] = 10,
    max_chain_depth: Annotated[Optional[int], typer.Option("--max-chain-depth", help="Max call-chain depth for transitive sink propagation (default: unlimited)")] = None,
    preset: Annotated[Optional[str], typer.Option("--preset", help="Load framework-specific trace rules. Python: django, flask, sqlalchemy, fastapi. TypeScript/Node.js: express, react, nextjs, node-sql. Rust: actix-web, axum, sqlx, diesel. Special: all")] = None,
    exclude_path: Annotated[Optional[list[str]], typer.Option("--exclude-path", help="Glob patterns for paths to exclude from analysis (repeatable)")] = None,
):
    """Run trace analysis to detect unsafe data flows.

    Tracks labeled value flow from sources (e.g. user input) to sinks
    (e.g. SQL queries, eval) within individual functions, reporting
    violations when traced data reaches a sink without sanitization.

    With --interprocedural, tracks values across function boundaries using
    function summaries and Datalog-based analysis.

    Use --max-chain-depth to limit transitive sink propagation depth.
    For example, --max-chain-depth 2 detects sinks up to 2 call levels
    deep (A calls B which has a sink).  Default is unlimited.

    Configuration is read from .emend/rules.yaml by default. Use --preset to load
    built-in rules for a specific framework (django, flask, sqlalchemy,
    fastapi, all).

    Examples:
        emend trace src/
        emend trace app.py --label user_input
        emend trace src/ --trace
        emend trace src/ --json
        emend trace src/ --interprocedural
        emend trace src/ --interprocedural --max-chain-depth 3
        emend trace app.py --preset flask
        emend trace src/ --exclude-path "*/migrations/*.py"
    """
    _trace_cmd_impl(path, config, label, trace, json_output, project,
                    interprocedural, max_iterations, preset, exclude_path,
                    max_chain_depth=max_chain_depth)



def dsl_debug_cmd(
    path: Annotated[str, typer.Argument(help="File or directory to analyze")],
    dsl_type: Annotated[Optional[str], typer.Option("--type", help="DSL type to detect (sql, css, html)")] = None,
    orm: Annotated[str, typer.Option("--orm", help="ORM framework (sqlalchemy, django)")] = "sqlalchemy",
    resolve: Annotated[bool, typer.Option("--resolve", help="Resolve cross-language links")] = False,
    json_output: JsonFlag = False,
    project: Annotated[Optional[str], typer.Option("--project", "-p", help="Project root")] = None,
):
    """[Debug] Detect and analyze embedded DSL regions (SQL, CSS, HTML).

    Diagnostic command for inspecting DSL detection and symbol extraction.
    For production use, prefer `emend search --include-dsl` and
    `emend refs --include-dsl`.

    Examples:
        emend dsl-debug src/
        emend dsl-debug app.py --type sql --resolve
        emend dsl-debug src/ --type sql --orm django --json
    """
    try:
        from emend.dsl import DslKind, format_symbols, DslLink

        # Parse DSL type filter
        dsl_filter: list[DslKind] | None = None
        if dsl_type:
            try:
                dsl_filter = [DslKind(dsl_type.lower())]
            except ValueError:
                print(f"Error: Unknown DSL type '{dsl_type}'. Valid types: sql, css, html, graphql, jinja", file=sys.stderr)
                raise typer.Exit(2)

        _lang = _state["language"]
        resolved_files, _ = resolve_files(path, language=_lang)
        files = [str(f) for f in resolved_files]

        all_symbols = _extract_dsl_symbols(files, dsl_filter=dsl_filter)

        # Resolve links if requested
        links: list[DslLink] = []
        if resolve and all_symbols:
            project_root = project or str(Path(path).resolve() if Path(path).is_dir() else Path(path).parent.resolve())
            links = _resolve_dsl_links(all_symbols, project_root, orm=orm)

        output = format_symbols(all_symbols, links=links if resolve else None, json_output=json_output)
        if output:
            print(output, end='')

    except typer.Exit:
        raise
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)



def refs_cmd(
    selector: Annotated[str, typer.Argument(help="Selector (file.py::Symbol)")],
    exclude_definition: Annotated[bool, typer.Option("--exclude-definition", help="Exclude the definition itself")] = False,
    exclude_imports: Annotated[bool, typer.Option("--exclude-imports", help="Exclude import statements")] = False,
    json_output: JsonFlag = False,
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
    with cli_error_handler():
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
                data = [
                    {
                        "file_path": ref.file_path,
                        "line": ref.line,
                        "column": ref.column,
                    }
                    for ref in callers
                ]
                emit_json(data)
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

        references = list(references)
        refs_data: list[dict] = [
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

        if not json_output:
            for ref in references:
                marker = ""
                if ref.is_definition:
                    marker = " (definition)"
                elif ref.is_import:
                    marker = " (import)"
                print(f"{ref.file_path}:{ref.line}{marker}", flush=True)

        # ---- DSL cross-language references ----
        _sel_name = parsed_selector.symbol_path[-1] if parsed_selector.symbol_path else ""
        if _sel_name:
            _proj = project or (str(Path(parsed_selector.file_path).parent) if parsed_selector.file_path else ".")
            _lang = _state["language"]
            _dsl_files, _ = resolve_files(_proj, language=_lang)
            _dsl_all_symbols = _extract_dsl_symbols(_dsl_files)
            if _dsl_all_symbols:
                _dsl_links = _resolve_dsl_links(_dsl_all_symbols, _proj)
                _matched = [
                    lnk for lnk in _dsl_links
                    if _sel_name.lower() in lnk.target_qualified_name.lower()
                ]
                if _matched:
                    for lnk in _matched:
                        refs_data.append({
                            "file_path": lnk.dsl_symbol.host_file,
                            "line": lnk.dsl_symbol.host_line,
                            "column": lnk.dsl_symbol.host_col,
                            "is_definition": False,
                            "is_import": False,
                            "is_write": False,
                        })
                    if not json_output:
                        for lnk in _matched:
                            print(f"{lnk.dsl_symbol.host_file}:{lnk.dsl_symbol.host_line}", flush=True)

        if json_output:
            emit_json(refs_data)





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
    with cli_error_handler():
        result = generate_graph(file, project_path=project, format=format)
        print(result)



def dead_code_cmd(
    path: Annotated[str, typer.Argument(help="Project directory to scan")] = ".",
    kind: Annotated[Optional[str], typer.Option("--kind", "-k", help="Symbol kind: function, class")] = None,
    include_private: Annotated[
        bool,
        typer.Option(
            "--include-private/--exclude-private",
            help="Report _private symbols and private methods",
        ),
    ] = True,
    json_output: JsonFlag = False,
    exclude_references_from: Annotated[
        Optional[list[str]],
        typer.Option("--exclude-references-from", help="Directories to ignore when scanning for references (e.g. tests/)")
    ] = None,
    include_test_references: Annotated[
        bool,
        typer.Option(
            "--include-test-references",
            help="Let references from tests keep production code alive",
        ),
    ] = False,
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
    unused_modules: Annotated[
        bool,
        typer.Option(
            "--unused-modules/--no-unused-modules",
            help="Report Python modules that are never imported",
        ),
    ] = True,
):
    """Find potentially dead (unreferenced) code in a project.

    Scans source files and reports unreferenced top-level symbols, private
    methods on live classes, and unused Python modules. Uses scope-aware
    analysis to avoid false positives from same-named symbols.

    By default, only git-tracked files are scanned. Use --all-files
    to include untracked files (e.g. in non-git projects).

    Automatically skips:
    - Dunder methods (__init__, __str__, etc.)
    - Test functions/classes (test_*, Test*)
    - Decorated entry points (@app.command, @pytest.fixture, etc.)
    - Symbols listed in __all__
    - Conventional entry points (main, setup, teardown)
    - Symbols with # noqa: emend:deadcode on the definition line

    References from test files are ignored by default. Use
    --include-test-references to count them. Private symbols/methods and
    unused modules are reported by default; use --exclude-private or
    --no-unused-modules to opt out.

    Use --entry-point-decorator and --entry-point-name to add custom
    exclusions beyond the built-in heuristics.

    By default, string literals containing the symbol name are treated
    as references (e.g. getattr(obj, "method_name")).  Disable with
    --no-strings.

    Examples:
        emend deadcode src/
        emend deadcode . --kind function
        emend deadcode . --exclude-private --json
        emend deadcode src/ --include-test-references
        emend deadcode . --no-strings --no-last-reference
        emend deadcode . --all-files
        emend deadcode . --entry-point-decorator my_framework.handler
        emend deadcode . --entry-point-name plugin_init
        emend deadcode . --no-unused-modules
    """
    with cli_error_handler():
        results = find_dead_code(
            project_path=path,
            kind=kind,
            include_private=include_private,
            exclude_references_from=exclude_references_from,
            exclude_test_references=not include_test_references,
            strings_count_as_references=not no_strings,
            show_last_reference=not no_last_reference,
            all_files=all_files,
            entry_point_decorators=entry_point_decorator,
            entry_point_names=entry_point_name,
            exclude_paths=exclude_path,
            unused_modules=unused_modules,
        )

        if json_output:
            # JSON mode: must collect all results before printing
            data = [dead_code_result_to_dict(result) for result in results]
            if not data:
                print("[]")
            else:
                emit_json(data)
        else:
            count = 0
            for d in results:
                if isinstance(d, DeadBlock):
                    func_name = d.func_qn.rsplit(".", 1)[-1] if "." in d.func_qn else d.func_qn
                    line = f"{d.file_path}:{d.start_line}-{d.end_line}  unreachable block in {func_name}()"
                elif isinstance(d, DeadModule):
                    line = f"{d.file_path}  {d.module_name} (module) - {d.reason}"
                else:
                    line = f"{d.file_path}:{d.line}  {d.name} ({d.kind}) - {d.reason}"
                    if d.last_reference_commit:
                        line += f"\n    last commit: {d.last_reference_commit}"
                print(line, flush=True)
                count += 1
            if count == 0:
                print("No dead code found.")
            else:
                print(f"\nFound {count} potentially dead result(s).", file=sys.stderr)



def impact_cmd(
    selector: Annotated[Optional[str], typer.Argument(help="Selector (file.py::Symbol)")] = None,
    diff: Annotated[Optional[str], typer.Option("--diff", help="Git diff spec (e.g. HEAD, abc..def)")] = None,
    output: Annotated[str, typer.Option("--output", "-o", help="Output mode: symbols, tests, graph")] = "symbols",
    json_output: JsonFlag = False,
    project: Annotated[Optional[str], typer.Option("--project", "-p", help="Project root directory")] = None,
    max_depth: Annotated[int, typer.Option("--max-depth", help="Maximum BFS depth for transitive closure")] = 10,
):
    """Compute the transitive set of impacted symbols from a change.

    Given a changed symbol (selector) or git diff, computes the transitive
    set of impacted symbols, files, and tests via reverse-caller closure.

    Output modes:
    - symbols: List impacted symbol selectors (default)
    - tests: List impacted test files/symbols
    - graph: Show witness edges explaining why each symbol is impacted

    Examples:
        emend impact mymodule.py::MyClass.method
        emend impact --diff HEAD
        emend impact --diff abc123..def456
        emend impact mymodule.py::func --output tests
        emend impact mymodule.py::func --output graph --json
    """
    if not selector and not diff:
        print("Error: provide a selector argument or --diff option", file=sys.stderr)
        raise typer.Exit(2)

    with cli_error_handler():
        selectors_list = None
        if selector:
            sel = parse_extended_selector(selector)
            selectors_list = [sel]

        result = find_impact(
            selectors=selectors_list,
            diff_spec=diff,
            project_path=project,
            max_depth=max_depth,
        )

        # DSL impact: find SQL queries affected by changed ORM models
        dsl_impacts: list[tuple[str, str, str]] = []
        if result.changed_symbols:
            try:
                from emend.dsl import find_dsl_impact
                _proj = project or "."
                dsl_impacts = find_dsl_impact(result.changed_symbols, _proj)
            except BUG_EXCEPTIONS:
                raise
            except Exception:
                logger.debug("DSL impact analysis failed", exc_info=True)

        if json_output:
            data = {
                "changed_symbols": result.changed_symbols,
                "impacted_symbols": result.impacted_symbols,
                "impacted_tests": result.impacted_tests,
                "edges": [
                    {"source": e.source, "target": e.target, "kind": e.kind}
                    for e in result.edges
                ],
            }
            if dsl_impacts:
                data["dsl_impacts"] = [
                    {"file": f, "line": l, "reason": r}
                    for f, l, r in dsl_impacts
                ]
            emit_json(data)
        elif output == "tests":
            if not result.impacted_tests:
                print("No impacted tests found.")
            else:
                for t in result.impacted_tests:
                    print(t)
        elif output == "graph":
            if not result.edges:
                print("No impact edges found.")
            else:
                for edge in result.edges:
                    print(f"{edge.source} --[{edge.kind}]--> {edge.target}")
            if dsl_impacts:
                for f, l, r in dsl_impacts:
                    print(f"{f}:{l} --[dsl]--> {r}")
        else:
            # Default: symbols mode
            if not result.changed_symbols and not result.impacted_symbols and not dsl_impacts:
                print("No impacted symbols found.")
            else:
                if result.changed_symbols:
                    print("Changed:")
                    for s in result.changed_symbols:
                        print(f"  {s}")
                if result.impacted_symbols:
                    print("Impacted:")
                    for s in result.impacted_symbols:
                        print(f"  {s}")
                if result.impacted_tests:
                    print("Tests:")
                    for t in result.impacted_tests:
                        print(f"  {t}")
                if dsl_impacts:
                    print("DSL impacts:")
                    for f, l, r in dsl_impacts:
                        print(f"  {f}:{l}  {r}")



def types_cmd(
    path: Annotated[str, typer.Argument(help="File or directory to analyze")],
    name: Annotated[Optional[str], typer.Option("--name", "-n", help="Filter by symbol name")] = None,
    kind: Annotated[Optional[str], typer.Option("--kind", "-k", help="Filter by binding kind: definition, reference, import, diagnostic")] = None,
    json_output: JsonFlag = False,
    engine: Annotated[str, typer.Option("--engine", help="Type inference engine: pyrefly, pyright, ty, typescript, rust-analyzer, auto")] = "auto",
    definitions_only: Annotated[bool, typer.Option("--definitions-only", "-d", help="Show only definitions")] = False,
):
    """Show inferred types for symbols in a file.

    Uses a type inference engine to analyze source files and display
    inferred types for all symbols and expressions.

    Defaults to auto-detection based on file extension and project
    configuration.  Use --engine to override.

    Supported engines:
      Python:     pyrefly (default), pyright, ty
      TypeScript: typescript (uses TypeScript Compiler API via Node.js)
      Rust:       rust-analyzer (LSP-based)

    Examples:
        emend types src/models/user.py
        emend types src/app.ts
        emend types src/lib.rs
        emend types src/models/user.py --name User
        emend types src/models/ --definitions-only --json
        emend types app.py --engine pyright
    """
    from emend.type_oracle import create_type_oracle

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
        file_hint = target if target.is_file() else None
        oracle = create_type_oracle(
            engine=engine, project_root=project_root, file_path=file_hint,
        )

        resolved_engine = engine
        if engine == "auto":
            resolved_engine = type(oracle).__name__.replace("Adapter", "").lower()

        if not oracle.is_available():
            print(f"Error: {resolved_engine} is not installed or not available on PATH.", file=sys.stderr)
            raise typer.Exit(2)

        _lang = _state["language"]
        if is_glob:
            files, _ = resolve_files(path, language=_lang)
        elif target.is_dir():
            files, _ = resolve_files(path, language=_lang)
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
            emit_json(data)
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



def facts_cmd(
    project: Annotated[str, typer.Argument(help="Project root directory")] = ".",
    fact_type: Annotated[str, typer.Option("--type", "-t", help="Fact type: symbols, calls, references, trace_flows (or taint_flows), types, imports")] = "symbols",
    name: Annotated[Optional[str], typer.Option("--name", "-n", help="Filter by name (symbols)")] = None,
    kind: Annotated[Optional[str], typer.Option("--kind", "-k", help="Filter by kind (symbols)")] = None,
    file: Annotated[Optional[str], typer.Option("--file", "-f", help="Filter by file path")] = None,
    symbol: Annotated[Optional[str], typer.Option("--symbol", "-s", help="Symbol qualified name (calls/references/types)")] = None,
    label: Annotated[Optional[str], typer.Option("--label", help="Trace label filter (trace_flows)")] = None,
    transitive: Annotated[bool, typer.Option("--transitive", help="Compute transitive closure (calls)")] = False,
    max_depth: Annotated[int, typer.Option("--max-depth", help="Max depth for transitive queries")] = 10,
    json_output: JsonFlag = False,
    limit: Annotated[int, typer.Option("--limit", help="Max results")] = 100,
):
    """Query the relational fact graph for code invariants.

    Builds and queries a unified graph of code facts extracted from
    the project. Supports symbols, call relationships, references,
    trace flows, type information, and imports.

    Examples:
        emend facts .                                   # list all symbols
        emend facts . --type calls --symbol mod.func    # call graph for func
        emend facts . --type calls --symbol mod.func --transitive
        emend facts . --type references --symbol mod.Class
        emend facts . --type trace_flows --label user_input
        emend facts . --type imports --file src/app.py
    """
    import dataclasses

    try:
        from emend.fact_graph import FactGraph

        graph = FactGraph.build_from_project(project)

        results: list = []
        extra: dict | None = None

        if fact_type == "symbols":
            results = graph.symbols(name=name, kind=kind, file_path=file)
        elif fact_type == "calls":
            if not symbol:
                print("Error: --symbol required for call queries", file=sys.stderr)
                raise typer.Exit(2)
            if transitive:
                callers = graph.transitive_callers(symbol, max_depth=max_depth)
                extra = {"symbol": symbol, "transitive_callers": sorted(callers)}
            else:
                from_calls = graph.calls_from(symbol)
                to_calls = graph.calls_to(symbol)
                extra = {
                    "calls_from": [dataclasses.asdict(c) for c in from_calls[:limit]],
                    "calls_to": [dataclasses.asdict(c) for c in to_calls[:limit]],
                }
        elif fact_type == "references":
            if not symbol:
                print("Error: --symbol required for reference queries", file=sys.stderr)
                raise typer.Exit(2)
            results = graph.references_to(symbol)
        elif fact_type in ("trace_flows", "taint_flows"):
            results = graph.trace_flows(label=label, file_path=file)
        elif fact_type == "types":
            if not symbol:
                print("Error: --symbol required for type queries", file=sys.stderr)
                raise typer.Exit(2)
            results = graph.types_for(symbol)
        elif fact_type == "imports":
            if not file:
                print("Error: --file required for import queries", file=sys.stderr)
                raise typer.Exit(2)
            results = graph.imports_in(file)
        else:
            print(f"Error: unknown fact type '{fact_type}'", file=sys.stderr)
            raise typer.Exit(2)

        if extra:
            if json_output:
                emit_json(extra)
            else:
                for key, val in extra.items():
                    if isinstance(val, list):
                        print(f"{key}:")
                        for item in val[:limit]:
                            print(f"  {item}")
                    else:
                        print(f"{key}: {val}")
        elif results:
            data = [dataclasses.asdict(f) for f in results[:limit]]
            if json_output:
                emit_json(data)
            else:
                for item in data:
                    parts = [f"{k}={v}" for k, v in item.items() if v is not None]
                    print("  ".join(parts))
            print(f"\n{min(len(results), limit)} of {len(results)} results.", file=sys.stderr)
        else:
            print("No results found.")

    except typer.Exit:
        raise
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)



def cfg_cmd(
    path: Annotated[str, typer.Argument(help="File or directory to analyze")],
    function: Annotated[
        Optional[str],
        typer.Option("--function", "-f", help="Restrict to a specific function name"),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text, json, or dot"),
    ] = "text",
    unreachable: Annotated[
        bool,
        typer.Option("--unreachable", help="Only show unreachable blocks"),
    ] = False,
):
    """Build and display per-function control flow graphs.

    Constructs basic-block CFGs for every function in the target file(s).
    Supports text, JSON, and Graphviz DOT output.

    Examples:
        emend cfg src/app.py
        emend cfg src/app.py --function process --format dot
        emend cfg src/ --unreachable --format json
    """
    with cli_error_handler():
        from emend.cfg import (
            build_cfgs_for_file,
            find_unreachable_blocks,
            format_cfg_text,
            format_cfgs_dot,
            format_cfgs_json,
        )

        _lang = _state["language"]
        resolved, _ = resolve_files(path, language=_lang)
        files = [str(f) for f in resolved]

        all_cfgs: list = []
        cfg_files: list[str] = []

        for fpath in files:
            try:
                cfgs = build_cfgs_for_file(fpath)
            except BUG_EXCEPTIONS:
                raise
            except Exception as exc:
                logger.debug("CFG build failed for %s: %s", fpath, exc)
                continue

            for cfg in cfgs:
                if function and cfg.func_name != function:
                    continue
                all_cfgs.append(cfg)
                cfg_files.append(fpath)

        if not all_cfgs:
            if function:
                print(f"No function named '{function}' found.", file=sys.stderr)
            else:
                print("No functions found.", file=sys.stderr)
            raise typer.Exit(2)

        if unreachable:
            results = []

            # Try Datalog path first via FactGraph
            datalog_used = False
            try:
                from emend.transform import _get_or_build_fact_graph
                graph = _get_or_build_fact_graph(path)
                func_filter = function if function else None
                unr_blocks = graph.unreachable_blocks_datalog(func_qn=func_filter)
                datalog_used = True
            except BUG_EXCEPTIONS:
                raise
            except Exception:
                logger.debug("Datalog unreachable query failed, falling back", exc_info=True)

            if datalog_used:
                # The Datalog facts identify unreachable blocks by id but do
                # not carry line spans.  Resolve real spans from the freshly
                # built CFGs (block ids are consistent between fact population
                # and a plain build_cfgs pass, both come from cfg.get_blocks()).
                import os
                span_lookup: dict[tuple[str, str, int], tuple[int, int]] = {}
                for i, cfg in enumerate(all_cfgs):
                    base = os.path.basename(cfg_files[i])
                    for block in cfg.get_blocks():
                        span_lookup[(base, cfg.func_name, block["id"])] = (
                            block["start_line"],
                            block["end_line"],
                        )
                # Group by (file_path, func_qn)
                from collections import defaultdict
                grouped: dict[tuple[str, str], list] = defaultdict(list)
                for blk in unr_blocks:
                    grouped[(blk.file_path, blk.func_qn)].append(blk)
                for (fp, fq), blks in grouped.items():
                    short_name = fq.rsplit(".", 1)[-1] if "." in fq else fq
                    base = os.path.basename(fp)
                    entries = []
                    for b in blks:
                        start_line, end_line = span_lookup.get(
                            (base, short_name, b.block_id), (0, 0)
                        )
                        entries.append({
                            "id": b.block_id,
                            "start_line": start_line,
                            "end_line": end_line,
                        })
                    results.append({
                        "file": fp,
                        "function": short_name,
                        "unreachable_blocks": entries,
                    })

            # Fallback to per-CFG BFS
            if not datalog_used:
                for i, cfg in enumerate(all_cfgs):
                    blocks = find_unreachable_blocks(cfg)
                    if blocks:
                        results.append({
                            "file": cfg_files[i],
                            "function": cfg.func_name,
                            "unreachable_blocks": blocks,
                        })

            if output_format == "json":
                emit_json(results)
            else:
                if not results:
                    print("No unreachable blocks found.")
                for r in results:
                    for blk in r["unreachable_blocks"]:
                        print(
                            f"{r['file']}:{blk.get('start_line', 0)+1}: "
                            f"unreachable code in {r['function']} "
                            f"(block B{blk.get('id', blk.get('block_id', '?'))}, "
                            f"lines {blk.get('start_line', '?')}-{blk.get('end_line', '?')})"
                        )
            raise typer.Exit(0)

        if output_format == "json":
            print(format_cfgs_json(all_cfgs, file_path=files[0] if len(files) == 1 else None))
        elif output_format == "dot":
            print(format_cfgs_dot(all_cfgs))
        else:
            parts = []
            for i, cfg in enumerate(all_cfgs):
                parts.append(f"# {cfg_files[i]}")
                parts.append(format_cfg_text(cfg))
            print("\n\n".join(parts))


def dupes_cmd(
    path: Annotated[str, typer.Argument(help="Project root directory")] = ".",
    mode: Annotated[str, typer.Option("--mode", help="Detection mode: exact, sequence, or all")] = "all",
    file: Annotated[Optional[str], typer.Option("--file", help="Restrict to a specific file")] = None,
    check_file: Annotated[Optional[str], typer.Option("--check-file", help="Scan the full project and report only duplicates involving this file (for post-write hooks)")] = None,
    symbol: Annotated[Optional[str], typer.Option("--symbol", help="Restrict to a specific symbol")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum number of clusters to show")] = 50,
    min_lines: Annotated[int, typer.Option("--min-lines", help="Minimum lines for a finding")] = 3,
    min_score: Annotated[float, typer.Option("--min-score", help="Minimum score threshold")] = 0.0,
    json_output: JsonFlag = False,
    cross_file: Annotated[Optional[bool], typer.Option("--cross-file/--intra-file", help="Filter by cross-file or intra-file")] = None,
):
    """Find duplicate code via AST canonicalization.

    Detects exact structural duplicates (same canonical subtree) and
    sibling-sequence duplicates (shared statement runs across functions).

    Examples:
        emend analyze dupes
        emend analyze dupes src/ --mode exact --min-lines 5
        emend analyze dupes --json --limit 20
        emend analyze dupes --file src/emend/transform.py
        emend analyze dupes --check-file src/emend/foo.py  # post-write hook
    """
    from emend.duplicate import (
        format_duplicates_json,
        format_duplicates_text,
        query_duplicates,
    )

    clusters = query_duplicates(
        project_path=path,
        mode=mode,
        file_scope=file,
        symbol_scope=symbol,
        limit=limit,
        min_lines=min_lines,
        min_score=min_score,
        cross_file=cross_file,
        involves_file=check_file,
    )

    if json_output:
        print(format_duplicates_json(clusters), end='')
    else:
        if not clusters:
            print("No duplicates found.", file=sys.stderr)
            return
        print(format_duplicates_text(clusters), end='')
