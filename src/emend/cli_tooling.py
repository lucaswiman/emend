import logging
import sys
from typing import Annotated, Optional

import typer

from emend.cli_base import app, tool_app
from emend.transform import warm_caches

@app.command("index", hidden=True)
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
            # Display progress in terms of file_count (total includes multiple phases per file)
            display_done = min(n_done * file_count // total, file_count)
            print(f"\r  [{pct:3d}%] {display_done}/{file_count} files indexed", end="", file=sys.stderr)

    # Quick count for progress bar
    from emend.transform import _collect_source_files_scandir
    from pathlib import Path as _Path
    scan_root = str(_Path(path).resolve())
    file_count = len(_collect_source_files_scandir(scan_root))
    # Callback is called twice per file (index + types phases)
    total = file_count * 2
    print(f"Indexing {file_count} source files in {scan_root}...", file=sys.stderr)

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



@app.command("editor-search", hidden=True)
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



@app.command("editor-server", hidden=True)
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
    schema: Annotated[
        bool,
        typer.Option("--schema", help="Print the MCP tool schema as JSON and exit")
    ] = False,
    profile: Annotated[
        Optional[str],
        typer.Option("--profile", help="Tool profile: core (default), refactor, expert, or full")
    ] = None,
    tools: Annotated[
        Optional[str],
        typer.Option("--tools", help="Comma-separated list of tool names to expose")
    ] = None,
):
    """Start an MCP (Model Context Protocol) server.

    Exposes emend commands as MCP tools for use by LLM-based clients.

    Requires the 'mcp' optional dependency:
        pip install emend[mcp]

    Profiles control which tools are exposed:
        core (default): search, transform/references/analyze/check, grammar_and_cookbook
        refactor: core + datalog
        expert: refactor + mappings
        full: all canonical + legacy compatibility tools

    Examples:
        emend mcp
        emend mcp --transport sse --port 8080
        emend mcp --schema
        emend mcp --profile core
        emend mcp --tools search,replace,modify
    """
    tools_list = [t.strip() for t in tools.split(",")] if tools else None
    try:
        if schema:
            from emend.mcp_server import dump_schema
            print(dump_schema(profile=profile, tools=tools_list))
            return
        from emend.mcp_server import run_server
    except ImportError:
        print(
            "Error: MCP dependencies not installed. "
            "Install with: pip install emend[mcp]",
            file=sys.stderr,
        )
        raise typer.Exit(2)

    run_server(transport=transport, port=port, profile=profile, tools=tools_list)

