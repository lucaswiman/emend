"""Public trace configuration and reporting adapters.

Flow semantics live exclusively in :mod:`emend.checks.flow`.  This module
keeps the established trace API without maintaining another evaluator.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from emend.checks.flow import (
    CompiledFlowConfig as TraceConfig,
    EvaluatedFlow as TraceViolation,
    FlowSanitizer as TraceSanitizer,
    FlowScopeSanitizer as TraceScopeSanitizer,
    FlowSink as TraceSink,
    FlowSource as TraceSource,
    evaluate_compiled_flow,
)
from emend.checks.rule_model import CompiledFlowRule, CompiledTraceConfig

@functools.lru_cache(maxsize=8)
def _get_keywords(language: str = "python") -> frozenset[str]:
    """Return configured language keywords for compatibility callers."""
    from emend.language_registry import load_config
    configured = load_config(language).get("trace", {}).get("keywords", ())
    if configured:
        return frozenset(configured)
    return frozenset({
        "False", "None", "True", "and", "as", "async", "await", "class",
        "def", "else", "for", "from", "if", "import", "in", "is", "lambda",
        "not", "or", "return", "while", "with", "yield",
    })


def _extract_identifiers(expr: str, language: str = "python") -> set[str]:
    """Extract identifier/access occurrences through the Rust syntax walker."""
    from emend import emend_core
    from emend.language_registry import get_extensions
    ext = (get_extensions(language) or ["py"])[0]
    facts = emend_core.build_flow_facts(expr, ext=ext)
    keywords = _get_keywords(language)
    return {
        value
        for event in facts.get("events", ())
        for value in (event.get("access_path") or event.get("var"),)
        if value and value not in keywords
    }


@dataclass
class TraceStep:
    file_path: str
    line: int
    col: int
    description: str
    variable: str


def _config_from_rules(
    rules: tuple[CompiledFlowRule, ...],
    *,
    labels: tuple[str, ...] = (),
    exclude_paths: tuple[str, ...] = (),
) -> TraceConfig:
    """Expose compiled rules through the mutable legacy collection surface."""
    sources: list[TraceSource] = []
    sinks: list[TraceSink] = []
    sanitizers: list[TraceSanitizer] = []
    scope_sanitizers: list[TraceScopeSanitizer] = []
    for rule in rules:
        sources.extend(TraceSource(
            endpoint.pattern, rule.label, endpoint.type_constraint, rule.rule_id,
        ) for endpoint in rule.sources)
        sinks.extend(TraceSink(
            endpoint.pattern, rule.label, rule.message,
            effect=endpoint.effect, type_constraint=endpoint.type_constraint,
            rule_id=rule.rule_id, rule_name=rule.name, severity=rule.severity,
            files=rule.files, languages=rule.languages,
            audiences=("trace",),
        ) for endpoint in rule.sinks)
        sanitizers.extend(TraceSanitizer(
            endpoint.pattern, rule.label, rule.quantifier,
            endpoint.type_constraint, rule.rule_id, effect=endpoint.effect,
        ) for endpoint in rule.sanitizers)
        scope_sanitizers.extend(TraceScopeSanitizer(
            endpoint.pattern, rule.label, rule.rule_id,
        ) for endpoint in rule.scope_sanitizers)
    return TraceConfig(
        labels=list(labels or tuple(dict.fromkeys(rule.label for rule in rules))),
        sources=sources,
        sinks=sinks,
        sanitizers=sanitizers,
        scope_sanitizers=scope_sanitizers,
        exclude_paths=list(exclude_paths),
        flow_rules=rules,
    )


def _config_from_trace_model(
    trace: CompiledTraceConfig,
    extra_rules: tuple[CompiledFlowRule, ...] = (),
) -> TraceConfig:
    """Adapt compiled trace endpoints without dropping unpaired presets."""
    config = _config_from_rules(
        extra_rules, labels=trace.labels, exclude_paths=trace.exclude_paths,
    )
    config.sources.extend(
        TraceSource(item.endpoint.pattern, item.label, item.endpoint.type_constraint)
        for item in trace.sources
    )
    config.sinks.extend(
        TraceSink(
            item.endpoint.pattern, item.label, item.message,
            effect=item.endpoint.effect,
            type_constraint=item.endpoint.type_constraint,
            audiences=("trace",),
        )
        for item in trace.sinks
    )
    config.sanitizers.extend(
        TraceSanitizer(
            item.endpoint.pattern, item.label, item.quantifier,
            item.endpoint.type_constraint, effect=item.endpoint.effect,
        )
        for item in trace.sanitizers
    )
    config.scope_sanitizers.extend(
        TraceScopeSanitizer(item.endpoint.pattern, item.label)
        for item in trace.scope_sanitizers
    )
    # TraceConfig is a mutable compatibility surface (callers may append a
    # sanitizer after loading a preset). Compile its current endpoint lists at
    # execution time so those edits cannot be hidden by a stale stored tuple.
    config.flow_rules = ()
    return config


def _trace_config_from_trace_section(raw: dict[str, Any] | None) -> TraceConfig:
    """Compile the legacy trace-section spelling through the canonical loader."""
    from emend.checks.rules_config import compile_rules_document
    document = compile_rules_document({"trace": raw or {}})
    return _config_from_trace_model(document.trace)


def load_trace_config(config_path: str) -> TraceConfig:
    """Load trace-visible rules from the canonical compiled document."""
    from emend.checks.rules_config import load_compiled_rules
    document = load_compiled_rules(config_path)
    explicit = _config_from_trace_model(document.trace, document.flow_rules)
    if not document.trace.presets:
        return explicit
    from emend.trace_presets import get_preset, merge_configs
    return merge_configs(
        *(get_preset(name) for name in document.trace.presets), explicit,
    )


def run_trace_analysis(
    paths: list[str],
    config: TraceConfig,
    label_filter: str | None = None,
    language: str = "python",
    project_path: str | None = None,
) -> list[TraceViolation]:
    """Run the occurrence evaluator without interprocedural call edges."""
    return evaluate_compiled_flow(
        config.compiled_rules(), paths, interprocedural=False,
        label_filter=label_filter, language=language, project_path=project_path,
    )


def format_violations(
    violations: list[TraceViolation],
    show_trace: bool = False,
    json_output: bool = False,
) -> str:
    if json_output:
        return json.dumps([row.to_dict(show_trace=show_trace) for row in violations], indent=2)
    lines: list[str] = []
    for violation in violations:
        lines.append(
            f"{violation.file_path}:{violation.line}:{violation.col}: "
            f"[trace:{violation.label}] {violation.message}"
        )
        if show_trace:
            lines.extend(
                f"  {step.file_path}:{step.line}:{step.col}: {step.description} "
                f"(variable: {step.variable})"
                for step in violation.trace
            )
    return "\n".join(lines)


@dataclass
class FunctionSummary:
    """Compatibility report shape; summaries are no longer an evaluator."""
    qualified_name: str
    file_path: str
    param_to_return: dict[str, set[str]] = field(default_factory=dict)
    param_to_sink: dict[str, list[tuple[str, str, int]]] = field(default_factory=dict)
    param_to_param: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class InterproceduralResult:
    violations: list[TraceViolation]
    summaries: dict[str, FunctionSummary]
    iterations: int


def run_interprocedural_trace(
    paths: list[str],
    config: TraceConfig,
    label_filter: str | None = None,
    language: str = "python",
    max_chain_depth: int | None = None,
    project_path: str | None = None,
) -> InterproceduralResult:
    """Run the same evaluator with balanced call edges admitted."""
    rules = config.compiled_rules()
    violations = evaluate_compiled_flow(
        rules, paths, interprocedural=True,
        label_filter=label_filter, language=language, project_path=project_path,
        max_call_depth=max_chain_depth,
    )
    summaries = _project_summaries(paths, rules, language, project_path)
    return InterproceduralResult(violations, summaries, 1 if summaries else 0)


def _project_summaries(
    paths: list[str], rules: tuple[CompiledFlowRule, ...],
    language: str, project_path: str | None,
) -> dict[str, FunctionSummary]:
    """Project parameter behavior from the evaluator's occurrence graph."""
    if not paths or not rules:
        return {}
    from collections import defaultdict
    from emend.analysis_store import AnalysisStore
    from emend.checks.flow import (
        _CALL_EDGES, _INTRA_VALUE_EDGES, _OPAQUE_CALL_EDGE, _add_cross_file_calls,
        _resolve_endpoints, _walk,
    )

    actual_paths = [str(Path(path).resolve()) for path in paths if Path(path).is_file()]
    if not actual_paths:
        return {}
    root = str(Path(project_path or Path(actual_paths[0]).parent).resolve())
    graph = AnalysisStore.open(root).query_facts()
    actual_to_stored = {path: graph.stored_path(path) for path in actual_paths}
    stored_to_actual = {stored: path for path, stored in actual_to_stored.items()}
    contents = {path: graph.source_text(path) for path in actual_paths}
    events = {(event.file_path, event.event_id): event for event in graph.flow_events()}
    by_actual: dict[str, list[Any]] = defaultdict(list)
    for event in events.values():
        actual = stored_to_actual.get(
            event.file_path, str((Path(root) / event.file_path).resolve()),
        )
        stored_to_actual[event.file_path] = actual
        if actual in actual_to_stored:
            by_actual[actual].append(event)
    adjacency: dict[Any, list[Any]] = defaultdict(list)
    control: dict[Any, list[Any]] = defaultdict(list)
    for edge in graph.flow_edges():
        source, target = ((edge.file_path, edge.from_event),
                          (edge.file_path, edge.to_event))
        if source not in events or target not in events:
            continue
        (control if edge.edge_kind == "control" else adjacency)[source].append(
            (target, edge.edge_kind)
        )
    resolved_calls = _add_cross_file_calls(events, adjacency, control, graph)
    for source_node, outgoing in adjacency.items():
        source_event = events[source_node]
        if (source_event.file_path, source_event.call_id) in resolved_calls:
            outgoing[:] = [edge for edge in outgoing if edge[1] != _OPAQUE_CALL_EDGE]

    sink_rows: list[tuple[CompiledFlowRule, Any, Any]] = []
    pairs = list(contents.items())
    match_cache: dict[tuple[Any, ...], list[Any]] = {}
    for rule in rules:
        for endpoint in rule.sinks:
            if endpoint.pattern:
                sink_rows.extend(
                    (rule, endpoint, match)
                    for match in _resolve_endpoints(
                        endpoint, "sink", pairs, by_actual, actual_to_stored, language,
                        match_cache,
                    )
                )
    allowed = _INTRA_VALUE_EDGES | _CALL_EDGES | frozenset({_OPAQUE_CALL_EDGE})
    summaries: dict[str, FunctionSummary] = {}
    symbols = graph.symbols()
    symbols_by_qn = {symbol.qualified_name: symbol for symbol in symbols}

    def summary_name(event: Any, actual: str) -> str:
        candidates = [
            symbol for symbol in symbols
            if symbol.file_path == event.file_path and symbol.name == event.func_name
            and symbol.line <= event.start_line + 1 <= symbol.end_line
        ]
        if not candidates:
            return event.func_id.rsplit("@", 1)[0]
        symbol = min(candidates, key=lambda item: item.end_line - item.line)
        names = [symbol.name]
        parent = symbol.parent
        while parent in symbols_by_qn:
            owner = symbols_by_qn[parent]
            names.append(owner.name)
            parent = owner.parent
        return "::".join(reversed(names))

    for node, event in events.items():
        if event.role != "param_in":
            continue
        actual = stored_to_actual.get(event.file_path, event.file_path)
        qn = f"{actual}::{summary_name(event, actual)}"
        summary = summaries.setdefault(qn, FunctionSummary(qn, actual))
        reached, _predecessor = _walk(node, adjacency, events, allowed)
        reached_nodes = {state[0] for state in reached}
        labels = {rule.label for rule in rules}
        if any(events[target].role == "return_out" for target in reached_nodes):
            summary.param_to_return.setdefault(event.var, set()).update(labels)
        for rule, endpoint, match in sink_rows:
            if match.node in reached_nodes:
                summary.param_to_sink.setdefault(event.var, []).append((
                    rule.label, endpoint.pattern or endpoint.effect,
                    match.span.start_line + 1,
                ))
    return summaries


def evaluate_type_constraint(constraint: str, type_name: str) -> bool:
    parsed = _parse_type_constraint(constraint)
    return parsed is None or any(
        all(_eval_atom(atom, type_name) for atom in group) for group in parsed
    )


@functools.lru_cache(maxsize=256)
def _parse_type_constraint(
    constraint: str,
) -> tuple[tuple[tuple[str, bool], ...], ...] | None:
    if not constraint.strip():
        return None
    return tuple(tuple(
        (clean.removeprefix("!").strip(), clean.startswith("!"))
        for atom in group.split("&") for clean in (atom.strip(),)
    ) for group in constraint.split("|"))


def _eval_atom(atom: tuple[str, bool], type_name: str) -> bool:
    from emend.type_oracle import type_name_matches
    name, negated = atom
    matched = type_name_matches(name, type_name)
    return not matched if negated else matched
