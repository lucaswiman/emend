"""MCP analysis tools: refs, graph, deadcode, impact, trace, duplicates."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from typing import Annotated

from pydantic import Field

from emend.component_selector import parse_extended_selector
from emend.transform import (
    find_references,
    find_callers,
    find_callees,
    generate_graph,
    find_dead_code,
    find_impact,
    semantic_context as _semantic_context,
)
from emend.rules_config import LEGACY_PATTERNS_PATH, resolve_rules_path

from emend.mcp.dispatch import mcp_app


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


def _graph_symbol(symbol: str, direction: str, transitive: bool, depth: int | None, format: str, project: str | None) -> str:
    """Symbol-level call graph query."""
    from emend.transform import _find_project_root, _file_to_module, _normalize_module_qn, _get_or_build_fact_graph

    sel = parse_extended_selector(symbol)
    sym_name = sel.symbol_path[-1] if sel.symbol_path else None
    if not sym_name:
        return json.dumps({"error": "Could not parse symbol from selector."})

    module_root = _find_project_root(sel.file_path) if sel.file_path else (project or ".")
    scan_root = project or module_root
    fg = _get_or_build_fact_graph(scan_root)

    if sel.file_path:
        target_module = _normalize_module_qn(_file_to_module(sel.file_path, module_root))
        qn = ".".join([target_module] + sel.symbol_path) if target_module else ".".join(sel.symbol_path)
    else:
        qn = ".".join(sel.symbol_path)

    edges: list[tuple[str, str]] = []

    if direction in ("callers", "both"):
        caller_facts = fg.callers_datalog(qn)
        if not caller_facts:
            caller_facts = fg.callers_datalog(sym_name)
        for c in caller_facts:
            edges.append((c.caller_qn, qn))
        if transitive:
            visited = {qn}
            frontier = [c.caller_qn for c in caller_facts]
            current_depth = 1
            while frontier and (depth is None or current_depth < depth):
                next_level: list[str] = []
                for caller_qn in frontier:
                    if caller_qn in visited:
                        continue
                    visited.add(caller_qn)
                    upstream = fg.callers_datalog(caller_qn)
                    for u in upstream:
                        edges.append((u.caller_qn, caller_qn))
                        if u.caller_qn not in visited:
                            next_level.append(u.caller_qn)
                current_depth += 1
                frontier = next_level

    if direction in ("callees", "both"):
        callee_facts = fg.callees_datalog(qn)
        if not callee_facts:
            callee_facts = fg.callees_datalog(sym_name)
        for c in callee_facts:
            edges.append((qn, c.callee_qn))
        if transitive:
            visited_callees = {qn}
            frontier_callees = [c.callee_qn for c in callee_facts]
            current_depth = 1
            while frontier_callees and (depth is None or current_depth < depth):
                next_level_c: list[str] = []
                for callee_qn in frontier_callees:
                    if callee_qn in visited_callees:
                        continue
                    visited_callees.add(callee_qn)
                    downstream = fg.callees_datalog(callee_qn)
                    for d in downstream:
                        edges.append((callee_qn, d.callee_qn))
                        if d.callee_qn not in visited_callees:
                            next_level_c.append(d.callee_qn)
                current_depth += 1
                frontier_callees = next_level_c

    edges = list(dict.fromkeys(edges))

    if format == "json":
        return json.dumps({"symbol": sym_name, "direction": direction, "transitive": transitive, "edges": [{"caller": c, "callee": e} for c, e in edges]}, indent=2)
    elif format == "dot":
        lines = ["digraph callgraph {"]
        for caller, callee in edges:
            lines.append(f'  "{caller}" -> "{callee}";')
        lines.append("}")
        return "\n".join(lines)
    else:
        return "\n".join(f"{c} -> {e}" for c, e in edges) or "(no edges found)"


def graph(
    file_path: Annotated[str | None, Field(description="Source file to analyze. Produces a full call graph for all functions in the file.")] = None,
    symbol: Annotated[str | None, Field(description="Symbol selector (e.g. 'file.py::func' or 'Class.method'). When given, direction/transitive/depth apply.")] = None,
    direction: Annotated[str, Field(description="'callers', 'callees', or 'both'. Only used with symbol.")] = "both",
    transitive: Annotated[bool, Field(description="Follow call chains recursively. Only used with symbol.")] = False,
    depth: Annotated[int | None, Field(description="Max traversal depth when transitive=true. Default: unlimited.")] = None,
    format: Annotated[str, Field(description="Output format: plain, json, or dot (Graphviz).")] = "json",
    project: Annotated[str | None, Field(description="Project root directory.")] = None,
) -> str:
    """Generate a call graph."""
    if symbol:
        return _graph_symbol(symbol, direction, transitive, depth, format, project)
    if file_path:
        return generate_graph(file_path, project_path=project, format=format)
    return json.dumps({"error": "Provide file_path or symbol."})


def deadcode(
    path: Annotated[str, Field(description="File glob or directory to scan (e.g. 'src/**/*.py').")] = ".",
    kind: Annotated[str | None, Field(description="Symbol kind filter: function, class, method, variable.")] = None,
    include_private: Annotated[bool, Field(description="Include _private symbols.")] = False,
    unused_modules: Annotated[bool, Field(description="Also report Python modules with no incoming imports.")] = False,
    no_last_reference: Annotated[bool, Field(description="Don't show git last-reference info.")] = False,
    entry_point_decorators: Annotated[list[str] | None, Field(description="Decorators that mark entry points (not dead even if unreferenced). E.g. ['app.route', 'celery.task'].")] = None,
    entry_point_names: Annotated[list[str] | None, Field(description="Function names that are entry points. E.g. ['main', 'cli'].")] = None,
    exclude_paths: Annotated[list[str] | None, Field(description="Glob patterns for paths to exclude. E.g. ['tests/**', 'migrations/**'].")] = None,
    config: Annotated[str | None, Field(description="Path to rules.yaml or legacy patterns.yaml config. Direct params above override config values.")] = None,
) -> str:
    """Find potentially dead (unreferenced) code. Returns JSON."""
    from emend.lint import load_rules

    cfg_exclude_refs_from = None
    cfg_strings_as_refs = True
    cfg_ep_decorators = None
    cfg_ep_names = None
    cfg_excl_paths = None

    config_path = resolve_rules_path(config, fallbacks=(LEGACY_PATTERNS_PATH,))
    if config_path.exists():
        _, _, deadcode_config = load_rules(str(config_path))
        if deadcode_config is not None:
            cfg_exclude_refs_from = deadcode_config.exclude_references_from
            cfg_strings_as_refs = deadcode_config.strings_count_as_references
            cfg_ep_decorators = deadcode_config.entry_point_decorators
            cfg_ep_names = deadcode_config.entry_point_names
            cfg_excl_paths = deadcode_config.exclude_paths

    results = find_dead_code(
        project_path=path,
        kind=kind,
        include_private=include_private,
        exclude_references_from=cfg_exclude_refs_from,
        strings_count_as_references=cfg_strings_as_refs,
        show_last_reference=not no_last_reference,
        all_files=False,
        entry_point_decorators=entry_point_decorators or cfg_ep_decorators,
        entry_point_names=entry_point_names or cfg_ep_names,
        exclude_paths=exclude_paths or cfg_excl_paths,
        unused_modules=unused_modules,
    )
    data = []
    for d in results:
        if hasattr(d, "module_name"):
            entry = {
                "file_path": d.file_path,
                "name": d.name,
                "module_name": d.module_name,
                "kind": "module",
                "reason": d.reason,
            }
        elif hasattr(d, "func_qn") and hasattr(d, "block_id"):
            entry = {
                "file_path": d.file_path,
                "func_qn": d.func_qn,
                "kind": "unreachable_block",
                "start_line": d.start_line,
                "end_line": d.end_line,
                "reason": "unreachable code",
            }
        else:
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


def impact(
    selector: Annotated[str | None, Field(description="Symbol selector (file.py::Symbol). Provide this or diff.")] = None,
    diff: Annotated[str | None, Field(description="Git diff spec (e.g. HEAD, abc..def).")] = None,
    output: Annotated[str, Field(description="Output mode: symbols, tests, graph.")] = "symbols",
    max_depth: Annotated[int, Field(description="Maximum BFS depth for transitive closure.")] = 10,
    project: Annotated[str | None, Field(description="Project root directory.")] = None,
) -> str:
    """Compute transitive set of impacted symbols from a change."""
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
    """Check a symbol for hidden dangers before editing it."""
    parsed = parse_extended_selector(selector)
    result = _semantic_context(
        parsed,
        project_path=project,
        extra_interface_decorators=interface_decorators,
    )

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

    if result.dangers:
        compact["dangers"] = [
            {"level": d.level, "category": d.category,
             "message": d.message, "evidence": d.evidence}
            for d in result.dangers
        ]
    else:
        compact["dangers"] = "none detected"

    compact["callers_count"] = len(result.callers)
    compact["test_callers_count"] = sum(1 for c in result.callers if c.kind == "test")
    compact["references_count"] = result.references_count

    if result.side_effects:
        compact["side_effects"] = [
            {"kind": se.kind, "target": se.target}
            for se in result.side_effects
        ]

    return json.dumps(compact, indent=2)


def trace_analysis(
    path: Annotated[str, Field(description="File or directory to analyze.")],
    from_pattern: Annotated[str | None, Field(description=(
        "Inline mode: source pattern where tainted data originates "
        "(e.g. 'request.args.get($X)'). When provided, config file is not needed."
    ))] = None,
    to_pattern: Annotated[str | None, Field(description=(
        "Inline mode: sink pattern where tainted data must not reach "
        "(e.g. 'cursor.execute($Q)')."
    ))] = None,
    not_through: Annotated[str | None, Field(description=(
        "Inline mode: sanitizer pattern. Data flowing through this is safe "
        "(e.g. 'escape($X)')."
    ))] = None,
    preset: Annotated[str | None, Field(description="Load framework-specific rules: flask, django, sqlalchemy, fastapi. Can combine with inline patterns.")] = None,
    config: Annotated[str | None, Field(description="Path to rules.yaml or legacy patterns.yaml. Not needed if using inline mode or preset.")] = None,
    label: Annotated[str | None, Field(description="Only check a specific trace label.")] = None,
    trace: Annotated[bool, Field(description="Include propagation traces in output.")] = False,
    interprocedural: Annotated[bool, Field(description="Enable cross-function analysis with fixed-point iteration.")] = False,
    engine: Annotated[str | None, Field(description="Force trace engine: 'datalog' (default) or 'python' (legacy escape hatch).")] = None,
) -> str:
    """Run trace analysis to detect unsafe data flows. Returns JSON."""
    from emend.trace import (
        load_trace_config, run_trace_analysis, format_violations,
        TraceConfig, TraceSource, TraceSink, TraceSanitizer,
    )
    from emend.cli_base import resolve_files

    if from_pattern and to_pattern:
        inline_label = label or "inline"
        trace_config = TraceConfig(
            labels=[inline_label],
            sources=[TraceSource(pattern=from_pattern, label=inline_label)],
            sinks=[TraceSink(pattern=to_pattern, label=inline_label, message=f"Tainted data flows to {to_pattern}")],
            sanitizers=[TraceSanitizer(pattern=not_through, label=inline_label)] if not_through else [],
            scope_sanitizers=[],
        )
        if preset:
            from emend.trace_presets import get_preset, merge_configs
            preset_config = get_preset(preset)
            if preset_config:
                trace_config = merge_configs(trace_config, preset_config)
    elif preset:
        from emend.trace_presets import get_preset
        trace_config = get_preset(preset)
        if not trace_config:
            return json.dumps({"error": f"Unknown preset: {preset}"})
    else:
        config_path = resolve_rules_path(config, fallbacks=(LEGACY_PATTERNS_PATH,))
        if not config_path.exists():
            return json.dumps({"error": f"Config file not found: {config_path}. Provide from_pattern + to_pattern for inline mode, or use preset=."})
        trace_config = load_trace_config(str(config_path))

    if not trace_config.sources or not trace_config.sinks:
        return json.dumps({"error": "No trace sources or sinks configured. Provide from_pattern + to_pattern, a preset, or a config file."})

    resolved, _ = resolve_files(path)
    files = [str(f) for f in resolved]

    if interprocedural:
        from emend.trace import run_interprocedural_trace
        result = run_interprocedural_trace(
            files, trace_config, label_filter=label,
        )
        violations = result.violations
        violation_data = []
        for v in violations:
            entry: dict = {
                "file": v.file_path, "line": v.line, "col": v.col,
                "label": v.label, "sink_pattern": v.sink_pattern, "message": v.message,
            }
            if v.engine:
                entry["engine"] = v.engine
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

    violations = run_trace_analysis(files, trace_config, label_filter=label, engine=engine)
    return format_violations(violations, show_trace=trace, json_output=True)


def duplicates_analysis(
    path: str = ".",
    mode: str = "all",
    file_path: str | None = None,
    limit: int = 20,
    min_lines: int = 5,
    min_score: float = 0.0,
    cross_file: bool | None = None,
) -> str:
    """Run duplicate detection and return JSON results."""
    from emend.duplicate import query_duplicates, format_duplicates_json

    clusters = query_duplicates(
        project_path=path,
        mode=mode,
        file_scope=file_path,
        limit=limit,
        min_lines=min_lines,
        min_score=min_score,
        cross_file=cross_file,
    )
    return format_duplicates_json(clusters)


def check_duplicates(
    file_path: Annotated[str, Field(description="File to check for duplication (usually the just-written file in a post-write hook).")],
    project: Annotated[str | None, Field(description="Project root to scan against. Defaults to CWD.")] = None,
    mode: Annotated[str, Field(description="Detection mode: exact, sequence, or all.")] = "all",
    limit: Annotated[int, Field(description="Maximum number of clusters to return.")] = 10,
    min_lines: Annotated[int, Field(description="Minimum lines for a finding.")] = 5,
    min_score: Annotated[float, Field(description="Minimum score threshold (use ~50 to suppress tiny matches in hooks).")] = 0.0,
) -> str:
    """Check whether *file_path* introduces code duplication vs the project."""
    from emend.duplicate import format_duplicates_json, query_duplicates

    clusters = query_duplicates(
        project_path=project or ".",
        mode=mode,
        limit=limit,
        min_lines=min_lines,
        min_score=min_score,
        involves_file=file_path,
    )
    return format_duplicates_json(clusters)


@mcp_app.tool()
def references(
    selector: Annotated[str, Field(description="Symbol selector to inspect.")],
    kind: Annotated[str, Field(description="Reference kind: all, reads, writes, or calls.")] = "all",
    exclude_definition: Annotated[bool, Field(description="Exclude definition row (refs mode).")] = False,
    exclude_imports: Annotated[bool, Field(description="Exclude import rows (refs mode).")] = False,
    project: Annotated[str | None, Field(description="Project root directory.")] = None,
) -> str:
    """Find references through a focused endpoint."""
    query_kind = (kind or "all").lower()
    if query_kind not in {"all", "reads", "writes", "calls"}:
        return json.dumps({"error": f"Unknown kind {kind!r}. Use: all, reads, writes, calls."})
    return refs(
        selector=selector,
        exclude_definition=exclude_definition,
        exclude_imports=exclude_imports,
        writes_only=query_kind == "writes",
        reads_only=query_kind == "reads",
        calls_only=query_kind == "calls",
        project=project,
    )


@mcp_app.tool()
def analyze(
    mode: Annotated[str, Field(description="Analysis mode: graph, deadcode, impact, semantic_context, flow, trace, or duplicates.")] = "graph",
    path: Annotated[str | None, Field(description="Path scope for deadcode/trace/check-style modes.")] = None,
    selector: Annotated[str | None, Field(description="Selector input for semantic_context/impact modes.")] = None,
    symbol: Annotated[str | None, Field(description="Symbol selector for graph mode.")] = None,
    file_path: Annotated[str | None, Field(description="File path for graph mode.")] = None,
    format: Annotated[str, Field(description="Output format where supported (graph).")] = "json",
    direction: Annotated[str, Field(description="Graph direction for symbol graph mode.")] = "both",
    transitive: Annotated[bool, Field(description="Enable transitive graph/caller expansion.")] = False,
    depth: Annotated[int | None, Field(description="Max depth for transitive graph expansion.")] = None,
    kind: Annotated[str | None, Field(description="Deadcode kind filter.")] = None,
    include_private: Annotated[bool, Field(description="Include private names in deadcode mode.")] = False,
    no_last_reference: Annotated[bool, Field(description="Disable git last-reference info in deadcode mode.")] = False,
    entry_point_decorators: Annotated[list[str] | None, Field(description="Entry-point decorators for deadcode mode.")] = None,
    entry_point_names: Annotated[list[str] | None, Field(description="Entry-point names for deadcode mode.")] = None,
    exclude_paths: Annotated[list[str] | None, Field(description="Excluded globs for deadcode mode.")] = None,
    config: Annotated[str | None, Field(description="Config path for deadcode/trace mode.")] = None,
    diff: Annotated[str | None, Field(description="Git diff range for impact mode.")] = None,
    output: Annotated[str, Field(description="Impact output mode.")] = "symbols",
    max_depth: Annotated[int, Field(description="Impact BFS max depth.")] = 10,
    interface_decorators: Annotated[list[str] | None, Field(description="Extra interface decorators for semantic_context mode.")] = None,
    from_pattern: Annotated[str | None, Field(description="Flow source pattern for inline flow mode.")] = None,
    to_pattern: Annotated[str | None, Field(description="Flow sink pattern for inline flow mode.")] = None,
    not_through: Annotated[str | None, Field(description="Flow sanitizer pattern for inline flow mode.")] = None,
    preset: Annotated[str | None, Field(description="Flow preset (flask/django/sqlalchemy/fastapi).")] = None,
    label: Annotated[str | None, Field(description="Flow label filter.")] = None,
    trace: Annotated[bool, Field(description="Include propagation steps in flow mode.")] = False,
    interprocedural: Annotated[bool, Field(description="Enable interprocedural flow analysis.")] = False,
    project: Annotated[str | None, Field(description="Project root directory.")] = None,
) -> str:
    """Run analysis operations through one discriminated endpoint."""
    analysis_mode = (mode or "graph").lower()
    if analysis_mode == "graph":
        return graph(
            file_path=file_path or path,
            symbol=symbol or selector,
            direction=direction,
            transitive=transitive,
            depth=depth,
            format=format,
            project=project,
        )
    if analysis_mode == "deadcode":
        return deadcode(
            path=path or ".",
            kind=kind,
            include_private=include_private,
            no_last_reference=no_last_reference,
            entry_point_decorators=entry_point_decorators,
            entry_point_names=entry_point_names,
            exclude_paths=exclude_paths,
            config=config,
        )
    if analysis_mode == "impact":
        return impact(
            selector=selector,
            diff=diff,
            output=output,
            max_depth=max_depth,
            project=project,
        )
    if analysis_mode == "semantic_context":
        if not selector:
            return json.dumps({"error": "semantic_context mode requires selector"})
        return semantic_context(
            selector=selector,
            project=project,
            interface_decorators=interface_decorators,
        )
    if analysis_mode in {"flow", "trace"}:
        if not path:
            return json.dumps({"error": "flow mode requires path"})
        return trace_analysis(
            path=path,
            from_pattern=from_pattern,
            to_pattern=to_pattern,
            not_through=not_through,
            preset=preset,
            config=config,
            label=label,
            trace=trace,
            interprocedural=interprocedural,
        )
    if analysis_mode == "duplicates":
        return duplicates_analysis(
            path=path or ".",
            mode=mode if mode not in {"graph", "deadcode", "impact", "semantic_context", "flow", "trace", "duplicates"} else "all",
            file_path=file_path,
            limit=max_depth,
            min_lines=5,
            min_score=0.0,
            cross_file=True,
        )
    return json.dumps({"error": f"Unknown mode {mode!r}. Use: graph, deadcode, impact, semantic_context, flow, trace, duplicates."})
