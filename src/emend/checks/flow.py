"""The single occurrence-based evaluator for compiled flow rules.

All public flow surfaces end here. Value propagation uses exact events and
edges emitted by ``emend_core.build_flow_facts``; control edges are used only
for executable-path and sanitizer quantification.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from emend.checks.rule_model import CompiledEndpoint, CompiledFlowRule

if TYPE_CHECKING:
    from emend.analysis_snapshot import FlowEventFact
    from emend.fact_graph import FactGraph
    from emend.checks.pattern_rules import LintRule
    from emend.policy import FlowCheck


@dataclass(frozen=True)
class FlowSource:
    pattern: str
    label: str
    type_constraint: str = ""
    rule_id: str = ""


@dataclass(frozen=True)
class FlowSink:
    pattern: str
    label: str
    message: str
    effect: str = ""
    type_constraint: str = ""
    rule_id: str = ""
    rule_name: str = ""
    severity: str = "warning"
    files: list[str] | tuple[str, ...] | None = None
    languages: list[str] | tuple[str, ...] | None = None
    audiences: list[str] | tuple[str, ...] = field(default_factory=lambda: ["trace"])


@dataclass(frozen=True)
class FlowSanitizer:
    pattern: str
    label: str
    quantifier: str = "all_paths"
    type_constraint: str = ""
    rule_id: str = ""
    effect: str = ""


@dataclass(frozen=True)
class FlowScopeSanitizer:
    pattern: str
    label: str
    rule_id: str = ""


@dataclass
class CompiledFlowConfig:
    """Legacy collection shape, retained as a construction adapter only."""

    labels: list[str] = field(default_factory=list)
    sources: list[FlowSource] = field(default_factory=list)
    sinks: list[FlowSink] = field(default_factory=list)
    sanitizers: list[FlowSanitizer] = field(default_factory=list)
    scope_sanitizers: list[FlowScopeSanitizer] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=list)
    flow_rules: tuple[CompiledFlowRule, ...] = ()

    def select(
        self, *, audience: str | None = None, rule_name: str | None = None,
    ) -> "CompiledFlowConfig":
        sinks = [
            sink for sink in self.sinks
            if (audience is None or audience in sink.audiences)
            and (rule_name is None or sink.rule_name == rule_name)
        ]
        ids = {sink.rule_id for sink in sinks if sink.rule_id}
        labels = {sink.label for sink in sinks}

        def related(endpoint: Any) -> bool:
            return endpoint.rule_id in ids if endpoint.rule_id else endpoint.label in labels

        rules = tuple(
            rule for rule in self.flow_rules
            if (rule_name is None or rule.name == rule_name)
            and (
                audience is None
                or audience in tuple(rule.options.get("audiences", ()))
                or not rule.options.get("audiences")
            )
        )
        return CompiledFlowConfig(
            labels=[label for label in self.labels if label in labels],
            sources=[endpoint for endpoint in self.sources if related(endpoint)],
            sinks=sinks,
            sanitizers=[endpoint for endpoint in self.sanitizers if related(endpoint)],
            scope_sanitizers=[endpoint for endpoint in self.scope_sanitizers if related(endpoint)],
            exclude_paths=list(self.exclude_paths),
            flow_rules=rules,
        )

    def compiled_rules(self) -> tuple[CompiledFlowRule, ...]:
        if self.flow_rules:
            return self.flow_rules
        rules: list[CompiledFlowRule] = []
        for index, sink in enumerate(self.sinks):
            sources = [s for s in self.sources if sink.rule_id and s.rule_id == sink.rule_id]
            if not sources:
                sources = [s for s in self.sources if s.label == sink.label]
            if not sources:
                continue
            sanitizers = [
                s for s in self.sanitizers if sink.rule_id and s.rule_id == sink.rule_id
            ] or [s for s in self.sanitizers if s.label == sink.label and not s.rule_id]
            scope_sanitizers = [
                s for s in self.scope_sanitizers if sink.rule_id and s.rule_id == sink.rule_id
            ] or [s for s in self.scope_sanitizers if s.label == sink.label and not s.rule_id]
            rules.append(CompiledFlowRule(
                rule_id=sink.rule_id or f"compat:{sink.rule_name or sink.label}:{index}",
                name=sink.rule_name or sink.label,
                label=sink.label,
                message=sink.message,
                sources=tuple(CompiledEndpoint(s.pattern, type_constraint=s.type_constraint)
                              for s in sources),
                sinks=(CompiledEndpoint(
                    sink.pattern, effect=sink.effect,
                    type_constraint=sink.type_constraint,
                ),),
                sanitizers=tuple(CompiledEndpoint(
                    s.pattern, effect=s.effect, type_constraint=s.type_constraint,
                ) for s in sanitizers),
                scope_sanitizers=tuple(CompiledEndpoint(s.pattern)
                                       for s in scope_sanitizers),
                quantifier=sanitizers[0].quantifier if sanitizers else "all_paths",
                severity=sink.severity,
                languages=tuple(sink.languages or ()),
                files=tuple(sink.files or ()),
                exclude_paths=tuple(self.exclude_paths),
            ))
        return tuple(rules)


@dataclass(frozen=True)
class FlowSpec:
    name: str
    message: str
    sources: str
    sinks: str
    sanitizers: list[str] | str | None = None
    through: str | None = None
    severity: str = "warning"
    label: str = ""


@dataclass(frozen=True)
class WitnessStep:
    file_path: str
    func_qn: str
    block_id: int
    line: int
    col: int = 0
    var_name: str = ""
    kind: str = ""


@dataclass
class FlowViolation:
    spec_name: str
    message: str
    severity: str
    file_path: str
    line: int
    col: int = 0
    source_line: int = 0
    source_text: str = ""
    sink_text: str = ""
    witness: list[WitnessStep] = field(default_factory=list)


@dataclass
class EvaluatedFlow:
    """Canonical evaluator result; trace/lint/policy only reformat it."""

    file_path: str
    line: int
    col: int
    label: str
    sink_pattern: str
    message: str
    trace: list[Any] = field(default_factory=list)
    engine: str = "occurrence"
    rule_id: str = ""
    rule_name: str = ""
    severity: str = "warning"

    def to_dict(self, *, show_trace: bool = False) -> dict[str, Any]:
        row: dict[str, Any] = {
            "file": self.file_path, "line": self.line, "col": self.col,
            "label": self.label, "sink_pattern": self.sink_pattern,
            "message": self.message,
        }
        if self.engine:
            row["engine"] = self.engine
        if show_trace:
            row["trace"] = [
                {"file": step.file_path, "line": step.line, "col": step.col,
                 "description": step.description, "variable": step.variable}
                for step in self.trace
            ]
        return row


def from_lint_rule(rule: "LintRule") -> FlowSpec:
    return FlowSpec(
        rule.name, rule.message, rule.flows_from or "", rule.flows_to or "",
        rule.not_through, severity=rule.severity, label=rule.name,
    )


def from_flow_check(
    check: "FlowCheck", policy_name: str, policy_desc: str,
    severity: str = "warning",
) -> FlowSpec:
    return FlowSpec(
        policy_name, policy_desc, check.flows_from, check.flows_to,
        check.not_through, severity=severity, label=check.label or policy_name,
    )


def format_witness(steps: list[WitnessStep]) -> list[str]:
    return [f"{s.kind or 'step'} L{s.line}" + (f": {s.var_name}" if s.var_name else "")
            for s in steps]


@dataclass(frozen=True)
class _PatternSpan:
    file_path: str
    start_byte: int
    end_byte: int
    start_line: int
    start_col: int
    end_line: int
    end_col: int
    text: str
    captures: tuple[tuple[str, int, int, int, int, int, int, str], ...] = ()


@dataclass(frozen=True)
class _EndpointMatch:
    node: tuple[str, int]
    span: _PatternSpan
    variable: str


def _pattern_spans(
    pattern: str, file_pairs: list[tuple[str, str]], language: str | None,
) -> list[_PatternSpan]:
    """Run the Rust matcher and retain exact match/capture byte spans."""
    from emend import emend_core
    from emend.language_registry import detect_language, get_extensions
    from emend.pattern import compile_pattern_to_rust_ir

    groups: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for path, source in file_pairs:
        lang = language or detect_language(path) or "python"
        suffix = Path(path).suffix.lstrip(".")
        extensions = get_extensions(lang) or [suffix]
        extension = suffix if suffix in extensions else extensions[0]
        groups[(lang, extension)].append((path, source))
    rows = []
    for (lang, extension), pairs in groups.items():
        ir = compile_pattern_to_rust_ir(pattern, language=lang)
        if ir is None:
            raise ValueError(f"Pattern {pattern!r} could not be compiled")
        rows.extend(emend_core.find_pattern_spans_in_files(
            pairs, ir, None, None, extension=extension,
        ))

    result: list[_PatternSpan] = []
    for row in rows:
        captures = tuple(
            (
                str(name), int(position["start_byte"]), int(position["end_byte"]),
                int(position["start_line"]) - 1, int(position["start_column"]),
                int(position["end_line"]) - 1, int(position["end_column"]),
                str(value.get("text", "")),
            )
            for name, value in row.get("captures", {}).items()
            for position in value.get("ranges", ())
        )
        result.append(_PatternSpan(
            str(row["file"]), int(row["start_byte"]), int(row["end_byte"]),
            int(row["line"]) - 1, int(row["column"]), int(row["end_line"]) - 1,
            int(row["end_column"]), str(row.get("matched_text", "")), captures,
        ))
    return result


def _choose_event(
    events: list["FlowEventFact"], span: _PatternSpan, purpose: str,
    capture_range: tuple[int, int] | None = None,
) -> "FlowEventFact | None":
    ranges = ([capture_range] if capture_range is not None else
              [(cap[1], cap[2]) for cap in span.captures if cap[0] != "_"])
    if purpose in {"sink", "sanitizer", "source_capture"} and ranges:
        candidates = [event for event in events if any(
            event.start_byte >= start and event.end_byte <= end for start, end in ranges
        )]
        preferred = (
            ("use", "call_arg", "param_in", "def")
            if purpose == "source_capture" else
            ("call_arg", "use", "call_result", "mutation", "def", "param_in")
        )
    else:
        candidates = [event for event in events
                      if event.start_byte >= span.start_byte and event.end_byte <= span.end_byte]
        preferred = (
            ("call_result", "return_out", "param_in", "use", "def", "call_arg", "call")
            if purpose == "source" else
            ("call", "mutation", "call_arg", "use", "call_result", "def")
        )
    for role in preferred:
        role_events = [event for event in candidates if event.role == role]
        if role_events:
            return max(role_events, key=lambda event: event.ordinal)
    return max(candidates, key=lambda event: event.ordinal, default=None)


def _resolve_endpoints(
    endpoint: CompiledEndpoint,
    purpose: str,
    file_pairs: list[tuple[str, str]],
    events_by_actual: dict[str, list["FlowEventFact"]],
    actual_to_stored: dict[str, str],
    language: str | None,
    match_cache: dict[tuple[Any, ...], list[_PatternSpan]] | None = None,
) -> list[_EndpointMatch]:
    if not endpoint.pattern:
        return []
    matches: list[_EndpointMatch] = []
    key = (language, endpoint.pattern, tuple(file_pairs))
    spans = match_cache.get(key) if match_cache is not None else None
    if spans is None:
        spans = _pattern_spans(endpoint.pattern, file_pairs, language)
        if match_cache is not None:
            match_cache[key] = spans
    for span in spans:
        actual = str(Path(span.file_path).resolve())
        captures = [capture for capture in span.captures if capture[0] != "_"]
        targets = captures if purpose in {"sink", "sanitizer"} and captures else [None]
        seen_nodes: set[tuple[str, int]] = set()
        for capture in targets:
            event = _choose_event(
                events_by_actual.get(actual, []), span, purpose,
                (capture[1], capture[2]) if capture is not None else None,
            )
            if event is None:
                continue
            node = (actual_to_stored.get(actual, event.file_path), event.event_id)
            if node in seen_nodes:
                continue
            seen_nodes.add(node)
            captured_value = capture[7] if capture is not None else next(
                (item[7] for item in captures), "",
            )
            matches.append(_EndpointMatch(
                node, span,
                event.access_path or event.var or captured_value or span.text,
            ))
    return matches


_INTRA_VALUE_EDGES = frozenset({"reaching", "transfer"})
_CALL_EDGES = frozenset({"call_arg", "call_return"})
_OPAQUE_CALL_EDGE = "call_input"


def _call_transition(
    kind: str,
    source: "FlowEventFact",
    target: "FlowEventFact",
    stack: tuple[tuple[str, int], ...],
    max_call_depth: int | None,
) -> tuple[tuple[str, int], ...] | None:
    """Apply one balanced call transition, rejecting cross-call returns."""
    if kind == "call_arg":
        if source.call_id is None:
            return None
        marker = (source.file_path, source.call_id)
        # Re-entering the same syntactic recursive call adds no new balanced
        # reachability. Keeping one marker makes the finite graph terminate.
        if marker in stack:
            return stack
        if max_call_depth is not None and len(stack) >= max_call_depth:
            return None
        return (*stack, marker)
    if kind == "call_return":
        if target.call_id is None:
            return None
        marker = (target.file_path, target.call_id)
        if stack:
            return stack[:-1] if stack[-1] == marker else None
        # A source originating in a callee may return along this concrete edge.
        return stack
    return stack


def _walk(
    start: tuple[str, int],
    adjacency: dict[tuple[str, int], list[tuple[tuple[str, int], str]]],
    events: dict[tuple[str, int], "FlowEventFact"],
    allowed: frozenset[str],
    *,
    blocked: frozenset[tuple[str, int]] = frozenset(),
    max_call_depth: int | None = None,
) -> tuple[set[Any], dict[Any, Any]]:
    initial = (start, ())
    queue = deque([initial])
    seen = {initial}
    predecessor: dict[Any, Any] = {}
    while queue:
        node, stack = queue.popleft()
        for target, kind in adjacency.get(node, ()):
            if kind not in allowed or target in blocked:
                continue
            next_stack = _call_transition(
                kind, events[node], events[target], stack, max_call_depth,
            )
            if next_stack is None:
                continue
            state = (target, next_stack)
            if state in seen:
                continue
            seen.add(state)
            predecessor[state] = ((node, stack), kind)
            queue.append(state)
    return seen, predecessor


def _path_to(state: Any, predecessor: dict[Any, Any]) -> list[tuple[str, int]]:
    path = [state[0]]
    while state in predecessor:
        state, _kind = predecessor[state]
        path.append(state[0])
    return list(reversed(path))


def _can_reach(
    start: tuple[str, int],
    target: tuple[str, int],
    adjacency: dict[tuple[str, int], list[tuple[tuple[str, int], str]]],
    events: dict[tuple[str, int], "FlowEventFact"],
    allowed: frozenset[str],
    *,
    blocked: frozenset[tuple[str, int]] = frozenset(),
    max_call_depth: int | None = None,
) -> bool:
    return any(state[0] == target for state in _walk(
        start, adjacency, events, allowed, blocked=blocked,
        max_call_depth=max_call_depth,
    )[0])


def _is_sanitized(
    source: _EndpointMatch,
    sink: _EndpointMatch,
    sanitizers: list[_EndpointMatch],
    scope_sanitizers: list[_EndpointMatch],
    quantifier: str,
    value_adjacency: dict[tuple[str, int], list[tuple[tuple[str, int], str]]],
    control_adjacency: dict[tuple[str, int], list[tuple[tuple[str, int], str]]],
    events: dict[tuple[str, int], "FlowEventFact"],
    value_edges: frozenset[str],
    control_edges: frozenset[str],
    max_call_depth: int | None,
    return_sanitizers: list[_EndpointMatch],
) -> bool:
    # Pure-return sanitizers clean the returned generation, never their input.
    # In particular, evaluating escape(x) must not clean a later use of x.
    # Return and validation coverage remain separate: mixed branch coverage
    # can conservatively warn without control-conditioned value edges.
    outputs = frozenset(match.node for match in return_sanitizers)
    if source.node in outputs:
        return True
    if outputs and (
        any(_can_reach(source.node, node, value_adjacency, events, value_edges,
                       max_call_depth=max_call_depth)
            and _can_reach(node, sink.node, value_adjacency, events, value_edges,
                           max_call_depth=max_call_depth)
            for node in outputs)
        if quantifier == "some_path" else
        not _can_reach(source.node, sink.node, value_adjacency, events, value_edges,
                       blocked=outputs, max_call_depth=max_call_depth)
    ):
        return True
    # Value sanitizers only affect the generation which reaches their exact
    # argument occurrence. Scope sanitizers apply to every value in the scope.
    # A source pattern can wrap a sanitizer (for example
    # ``password = redact(password)``).  In that case the matched source is the
    # sanitizer's output generation, so it starts clean rather than becoming a
    # new tainted value after the nested call.
    if any(
        sanitizer.span.file_path == source.span.file_path
        and source.span.start_byte <= sanitizer.span.start_byte
        and sanitizer.span.end_byte <= source.span.end_byte
        and _can_reach(
            sanitizer.node, source.node, value_adjacency, events, value_edges,
            max_call_depth=max_call_depth,
        )
        for sanitizer in sanitizers
    ):
        return True
    relevant = [sanitizer for sanitizer in sanitizers if _can_reach(
        source.node, sanitizer.node, value_adjacency, events, value_edges,
        max_call_depth=max_call_depth,
    )]
    relevant.extend(scope_sanitizers)
    on_a_path = [sanitizer for sanitizer in relevant if (
        _can_reach(
            source.node, sanitizer.node, control_adjacency, events, control_edges,
            max_call_depth=max_call_depth,
        )
        and _can_reach(
            sanitizer.node, sink.node, control_adjacency, events, control_edges,
            max_call_depth=max_call_depth,
        )
    )]
    if not on_a_path:
        return False
    if quantifier == "some_path":
        return True
    # all_paths suppresses only if deleting every applicable sanitizer makes
    # the source-to-sink execution path unreachable.
    return not _can_reach(
        source.node, sink.node, control_adjacency, events, control_edges,
        blocked=frozenset(sanitizer.node for sanitizer in on_a_path),
        max_call_depth=max_call_depth,
    )


def _rule_applies(rule: CompiledFlowRule, path: str, language: str, root: str) -> bool:
    from emend.checks.rules_config import path_matches_glob
    return (
        rule.enabled
        and (not rule.languages or language in rule.languages)
        and (not rule.files or any(
            path_matches_glob(path, pattern, project_root=root) for pattern in rule.files
        ))
        and not any(path_matches_glob(path, pattern, project_root=root)
                    for pattern in rule.exclude_paths)
    )


def _event_text(event: "FlowEventFact", source: str) -> str:
    return source.encode()[event.start_byte:event.end_byte].decode(errors="replace")


def _add_cross_file_calls(
    events: dict[tuple[str, int], "FlowEventFact"],
    value_adjacency: dict[Any, list[Any]],
    control_adjacency: dict[Any, list[Any]],
    graph: "FactGraph",
) -> set[tuple[str, int]]:
    """Join resolved call facts to exact occurrence nodes.

    Rust can link calls whose callee is in the same parsed buffer.  Calls
    across files are deliberately linked here, after all files have been
    extracted, using the resolver's qualified callee identity.  A bare-name
    fallback would make two same-named functions (or two call sites on one
    line) contaminate one another, so ambiguous joins are omitted.
    """
    by_function: dict[tuple[str, str], list[tuple[tuple[str, int], FlowEventFact]]] = defaultdict(list)
    by_call: dict[tuple[str, int], list[tuple[tuple[str, int], FlowEventFact]]] = defaultdict(list)
    for node, event in events.items():
        function = (event.file_path, event.func_id)
        by_function[function].append((node, event))
        if event.call_id is not None:
            by_call[(event.file_path, event.call_id)].append((node, event))

    # These are already-resolved calls from the same FactGraph snapshot.  Do
    # not infer a new call target from occurrence text: that would reintroduce
    # the name-based cross-file false positives this linker exists to avoid.
    resolved_calls = {
        (events[source].file_path, events[source].call_id)
        for source, outgoing in value_adjacency.items()
        if source in events and events[source].role == "call_arg"
        and events[source].call_id is not None
        and any(kind == "call_arg" for _target, kind in outgoing)
    }
    try:
        calls = graph._all_calls()  # type: ignore[attr-defined]
        symbols = graph.symbols()
    except Exception:
        return resolved_calls
    symbols_by_qn: dict[str, list[Any]] = defaultdict(list)
    for symbol in symbols:
        symbols_by_qn[symbol.qualified_name].append(symbol)

    existing = {
        (source, target, kind)
        for source, outgoing in value_adjacency.items()
        for target, kind in outgoing
    }
    for call in calls:
        targets = symbols_by_qn.get(call.callee_qn, ())
        if len(targets) != 1:
            continue
        target = targets[0]
        callee_funcs = [function for function in by_function
                        if function[0] == target.file_path
                        and any(event.func_name == target.name
                                for _node, event in by_function[function])]
        if len(callee_funcs) != 1:
            continue
        callee_events = by_function[callee_funcs[0]]

        candidates = [
            (node, event) for node, event in events.items()
            if event.file_path == call.file_path
            and event.role == "call"
            and event.start_line == call.line - 1
            and event.start_col <= call.col < event.end_col
        ]
        if len(candidates) != 1:
            # Some language resolvers report the call token's start while a
            # grammar's call node starts one token earlier.  A unique line is
            # still safe; multiple same-line calls remain intentionally
            # unresolved.
            candidates = [
                (node, event) for node, event in events.items()
                if event.file_path == call.file_path
                and event.role == "call"
                and event.start_line == call.line - 1
            ]
        if len(candidates) != 1:
            continue
        _call_node, call_event = candidates[0]
        resolved_calls.add((call_event.file_path, call_event.event_id))
        caller_events = by_call.get((call.file_path, call_event.event_id), ())
        for argument_node, argument in caller_events:
            if argument.role != "call_arg" or argument.arg_index is None:
                continue
            for parameter_node, parameter in callee_events:
                argument_name = getattr(argument, "arg_name", None)
                parameter_name = getattr(parameter, "arg_name", None)
                matches_parameter = (
                    parameter_name == argument_name
                    if argument_name is not None
                    else parameter.arg_index == argument.arg_index
                )
                if parameter.role == "param_in" and matches_parameter:
                    edge = (argument_node, parameter_node, "call_arg")
                    if edge not in existing:
                        value_adjacency[argument_node].append((parameter_node, "call_arg"))
                        control_adjacency[argument_node].append((parameter_node, "call_arg"))
                        existing.add(edge)
        results = [node for node, event in caller_events if event.role == "call_result"]
        for return_node, returned in callee_events:
            if returned.role != "return_out":
                continue
            for result_node in results:
                edge = (return_node, result_node, "call_return")
                if edge not in existing:
                    value_adjacency[return_node].append((result_node, "call_return"))
                    control_adjacency[return_node].append((result_node, "call_return"))
                    existing.add(edge)
    return resolved_calls


def _add_container_mutations(
    events: dict[tuple[str, int], "FlowEventFact"],
    adjacency: dict[Any, list[Any]],
    language: str | None,
) -> None:
    """Connect append/extend inputs to later uses of the same container generation."""
    from emend.language_registry import detect_language, load_config

    languages = {path: language or detect_language(path) or "python"
                 for path in {event.file_path for event in events.values()}}
    methods = {lang: frozenset(load_config(lang).get("trace", {})
                              .get("container_mutations", {}).get("methods", ()))
               for lang in set(languages.values())}
    if not any(methods.values()):
        return
    incoming_reaching: dict[Any, set[Any]] = defaultdict(set)
    outgoing_reaching: dict[Any, set[Any]] = defaultdict(set)
    calls: dict[tuple[str, int], list[tuple[Any, FlowEventFact]]] = defaultdict(list)
    for source, outgoing in adjacency.items():
        for target, kind in outgoing:
            if kind == "reaching":
                incoming_reaching[target].add(source)
                outgoing_reaching[source].add(target)
    for node, event in events.items():
        if event.call_id is not None:
            calls[(event.file_path, event.call_id)].append((node, event))
    for call_rows in calls.values():
        call = next((row for row in call_rows if row[1].role == "call"), None)
        if call is None or not call[1].access_path:
            continue
        receiver, separator, method = call[1].access_path.rpartition(".")
        if not separator or method not in methods[languages[call[1].file_path]]:
            continue
        receiver_root = receiver.split(".", 1)[0].split("[", 1)[0]
        receiver_uses = [
            node for node, event in events.items()
            if event.file_path == call[1].file_path and event.func_id == call[1].func_id
            and event.ordinal <= call[1].ordinal
            and (event.access_path or event.var or "").split(".", 1)[0]
                .split("[", 1)[0] == receiver_root
            and incoming_reaching.get(node)
        ]
        defining = set().union(*(incoming_reaching[node] for node in receiver_uses)) \
            if receiver_uses else set()
        later_uses = {
            target for definition in defining for target in outgoing_reaching[definition]
            if events[target].ordinal > call[1].ordinal
        }
        for argument_node, argument in call_rows:
            if argument.role != "call_arg":
                continue
            adjacency[argument_node].extend(
                (target, "transfer") for target in later_uses
            )


def _type_filter(
    matches: list[_EndpointMatch],
    endpoint: CompiledEndpoint,
    graph: "FactGraph",
) -> list[_EndpointMatch]:
    from emend.pattern import is_oracle_type_constraint, parse_oracle_type_constraint, parse_pattern
    from emend.trace import evaluate_type_constraint

    constraints: list[tuple[str | None, str]] = []
    if endpoint.type_constraint:
        constraints.append((None, endpoint.type_constraint))
    try:
        constraints.extend(
            (metavar.name, metavar.type_constraint)
            for metavar in parse_pattern(endpoint.pattern).metavars
            if metavar.type_constraint and is_oracle_type_constraint(metavar.type_constraint)
        )
    except Exception:
        pass
    if not constraints:
        return matches

    kept: list[_EndpointMatch] = []
    for match in matches:
        accepted = True
        captures = {name: (text, line) for name, _sb, _eb, line, _sc, _el, _ec, text
                    in match.span.captures}
        for capture_name, raw_constraint in constraints:
            value, line = (match.variable, match.span.start_line) if capture_name is None \
                else captures.get(capture_name, ("", match.span.start_line))
            variable = value.split(".", 1)[0].split("[", 1)[0].strip()
            if not variable:
                continue
            facts = graph.types_for(variable)
            stored_path = graph.stored_path(match.span.file_path)
            eligible = [fact for fact in facts
                        if fact.file_path == stored_path and fact.line <= line + 1]
            if not eligible:
                continue  # Missing type information is conservative.
            binding = max(eligible, key=lambda fact: fact.line)
            kind, constraint = (
                parse_oracle_type_constraint(raw_constraint)
                if is_oracle_type_constraint(raw_constraint)
                else ("type", raw_constraint)
            )
            type_name = binding.type_str
            if kind == "returns":
                from emend.type_oracle import parse_type_string
                descriptor = parse_type_string(type_name)
                if descriptor.kind == "callable" and descriptor.return_type is not None:
                    type_name = descriptor.return_type.display()
                elif binding.binding_kind not in {"return", "returns"}:
                    continue  # The stored binding does not expose a return type.
            if not evaluate_type_constraint(constraint, type_name):
                accepted = False
                break
        if accepted:
            kept.append(match)
    return kept


def evaluate_compiled_flow(
    rules: Iterable[CompiledFlowRule],
    paths: list[str],
    *,
    interprocedural: bool,
    label_filter: str | None = None,
    language: str | None = None,
    project_path: str | None = None,
    graph: "FactGraph | None" = None,
    source_overrides: dict[str, str] | None = None,
    max_call_depth: int | None = None,
) -> list[EvaluatedFlow]:
    """Evaluate compiled rules over exact occurrence and control facts."""
    from emend.language_registry import detect_language
    all_rules = tuple(rules)
    for rule in all_rules:
        for endpoint in rule.sanitizers:
            if endpoint.effect not in {"", "returns"}:
                raise ValueError(f"Unknown sanitizer effect {endpoint.effect!r}; expected 'returns' or no effect")
    if not paths or not all_rules:
        return []
    overrides = {str(Path(path).resolve()): text
                 for path, text in (source_overrides or {}).items()}
    actual_paths = [str(Path(path).resolve()) for path in paths]
    inferred_root = os.path.commonpath(actual_paths)
    if len(actual_paths) == 1 or Path(inferred_root).is_file():
        inferred_root = str(Path(actual_paths[0]).parent)
    root_path = Path(project_path or inferred_root).resolve()
    if root_path.is_file():
        root_path = root_path.parent
    project_root = str(root_path)
    if graph is None:
        from emend.analysis_store import AnalysisStore
        needs_types = any(
            endpoint.type_constraint or ":type[" in endpoint.pattern
            or ":returns[" in endpoint.pattern
            for rule in all_rules
            for endpoint in (*rule.sources, *rule.sinks, *rule.sanitizers)
        )
        store = (AnalysisStore.existing_for_path(actual_paths[0])
                 if project_path is None else None)
        if store is None:
            if not Path(project_root).is_dir():
                return []
            store = AnalysisStore.open(project_root)
        project_root = str(store.project_root)
        graph = store.query_facts(include_types=needs_types)
        snapshot_paths = {revision.file_path for revision in graph.snapshot.files}
        actual_paths = [path for path in actual_paths
                        if path in snapshot_paths or path in overrides]
        if not actual_paths:
            return []

    actual_to_stored = {path: graph.stored_path(path) for path in actual_paths}
    stored_to_actual = {stored: actual for actual, stored in actual_to_stored.items()}
    contents = {path: overrides[path] if path in overrides else graph.source_text(path)
                for path in actual_paths}
    file_pairs = list(contents.items())
    file_languages = {path: language or detect_language(path) or "python" for path in actual_paths}

    all_events: dict[tuple[str, int], FlowEventFact] = {}
    events_by_actual: dict[str, list[FlowEventFact]] = defaultdict(list)
    for event in graph.flow_events():
        node = (event.file_path, event.event_id)
        all_events[node] = event
        actual = stored_to_actual.get(event.file_path)
        if actual is None:
            actual = str((Path(project_root) / event.file_path).resolve())
            stored_to_actual[event.file_path] = actual
        if actual in actual_to_stored:
            events_by_actual[actual].append(event)
    if not all_events:
        return []

    value_adjacency: dict[Any, list[Any]] = defaultdict(list)
    control_adjacency: dict[Any, list[Any]] = defaultdict(list)
    for edge in graph.flow_edges():
        source_node = (edge.file_path, edge.from_event)
        target_node = (edge.file_path, edge.to_event)
        if source_node not in all_events or target_node not in all_events:
            continue
        if edge.edge_kind == "control":
            control_adjacency[source_node].append((target_node, edge.edge_kind))
        else:
            value_adjacency[source_node].append((target_node, edge.edge_kind))
            if edge.edge_kind in _CALL_EDGES:
                control_adjacency[source_node].append((target_node, edge.edge_kind))
    resolved_calls = _add_cross_file_calls(
        all_events, value_adjacency, control_adjacency, graph,
    )
    _add_container_mutations(all_events, value_adjacency, language)
    if interprocedural:
        for source_node, outgoing in value_adjacency.items():
            source_event = all_events[source_node]
            marker = (source_event.file_path, source_event.call_id)
            if marker in resolved_calls:
                outgoing[:] = [edge for edge in outgoing
                               if edge[1] != _OPAQUE_CALL_EDGE]

    match_cache: dict[tuple[Any, ...], list[_PatternSpan]] = {}

    results: list[EvaluatedFlow] = []
    seen: set[tuple[Any, ...]] = set()
    for rule in all_rules:
        if label_filter and label_filter not in {rule.label, rule.rule_id}:
            continue
        rule_pairs = [pair for pair in file_pairs
                      if _rule_applies(rule, pair[0], file_languages[pair[0]], project_root)]
        if not rule_pairs:
            continue
        allowed_actual = {path for path, _source in rule_pairs}
        rule_events = {path: rows for path, rows in events_by_actual.items()
                       if path in allowed_actual}
        sources = [match for endpoint in rule.sources for match in _type_filter(
            _resolve_endpoints(
                endpoint, "source",
                rule_pairs, rule_events, actual_to_stored, language,
                match_cache,
            ), endpoint, graph,
        )]
        source_rows: list[_EndpointMatch] = []
        for source in sources:
            downstream_defs = [
                all_events[target] for target, kind in value_adjacency.get(source.node, ())
                if kind == "transfer" and all_events[target].role in {"def", "mutation"}
            ]
            if downstream_defs:
                source_rows.append(_EndpointMatch(
                    source.node, source.span,
                    downstream_defs[0].access_path or downstream_defs[0].var or source.variable,
                ))
            elif any(endpoint.options.get("source_capture") for endpoint in rule.sources):
                actual = str(Path(source.span.file_path).resolve())
                capture_event = _choose_event(
                    rule_events.get(actual, ()), source.span, "source_capture",
                )
                capture_node = (
                    (actual_to_stored.get(actual, capture_event.file_path), capture_event.event_id)
                    if capture_event is not None else source.node
                )
                reverse: dict[Any, list[tuple[Any, str]]] = defaultdict(list)
                for origin, outgoing in value_adjacency.items():
                    for target, kind in outgoing:
                        reverse[target].append((origin, kind))
                pending = [capture_node]
                visited = {capture_node}
                origins: list[Any] = []
                while pending:
                    target = pending.pop()
                    for origin, kind in reverse.get(target, ()):
                        if kind == "reaching":
                            origins.append(origin)
                        elif kind == "transfer" and origin not in visited:
                            visited.add(origin)
                            pending.append(origin)
                source_rows.extend(_EndpointMatch(node, source.span, source.variable)
                                   for node in origins or [capture_node])
            else:
                source_rows.append(source)
        sources = source_rows
        pattern_sinks = [
            (endpoint, match) for endpoint in rule.sinks if endpoint.pattern
            for match in _type_filter(_resolve_endpoints(
                endpoint, "sink", rule_pairs, rule_events, actual_to_stored, language,
                match_cache,
            ), endpoint, graph)
        ]
        sanitizers = [
            match for endpoint in rule.sanitizers
            if endpoint.effect != "returns"
            for match in _type_filter(_resolve_endpoints(
                endpoint, "sanitizer", rule_pairs, rule_events,
                actual_to_stored, language,
                match_cache,
            ), endpoint, graph)
        ]
        return_sanitizers = [
            match for endpoint in rule.sanitizers if endpoint.effect == "returns"
            for match in _type_filter(_resolve_endpoints(
                endpoint, "source", rule_pairs, rule_events,
                actual_to_stored, language, match_cache,
            ), endpoint, graph)
        ]
        scope_sanitizers = [
            match for endpoint in rule.scope_sanitizers
            for match in _resolve_endpoints(
                endpoint, "scope", rule_pairs, rule_events,
                actual_to_stored, language, match_cache,
            )
        ]
        if not sources:
            continue

        value_edges = _INTRA_VALUE_EDGES | frozenset({_OPAQUE_CALL_EDGE}) | (
            _CALL_EDGES if interprocedural else frozenset()
        )
        control_edges = frozenset({"control"}) | (
            _CALL_EDGES if interprocedural else frozenset()
        )
        for source in sources:
            states, predecessor = _walk(
                source.node, value_adjacency, all_events, value_edges,
                max_call_depth=max_call_depth,
            )
            states_by_node: dict[Any, list[Any]] = defaultdict(list)
            for state in states:
                states_by_node[state[0]].append(state)

            candidates = list(pattern_sinks)
            for endpoint in rule.sinks:
                if not endpoint.effect:
                    continue
                effect = endpoint.effect.strip().split("(", 1)[0]
                for node, event in all_events.items():
                    actual = stored_to_actual.get(event.file_path)
                    method_call = next((candidate for candidate in all_events.values()
                        if effect == "writes" and event.role == "use"
                        and "." in (event.access_path or "")
                        and candidate.file_path == event.file_path
                        and candidate.func_id == event.func_id
                        and candidate.role == "call"
                        and candidate.access_path == event.access_path
                        and candidate.start_byte <= event.start_byte
                        and event.end_byte <= candidate.end_byte), None)
                    accepts_effect = (
                        event.role == "mutation" or method_call is not None
                        if effect == "writes" else event.role == "use"
                    )
                    if not accepts_effect or node not in states_by_node \
                            or actual not in allowed_actual:
                        continue
                    location = method_call or event
                    text = _event_text(location, contents[actual])
                    candidates.append((endpoint, _EndpointMatch(
                        node,
                        _PatternSpan(
                            actual, location.start_byte, location.end_byte,
                            location.start_line, location.start_column,
                            location.end_line, location.end_column, text,
                        ),
                        event.access_path or event.var or text,
                    )))

            for sink_endpoint, sink in candidates:
                sink_states = states_by_node.get(sink.node, ())
                if not sink_states or _is_sanitized(
                    source, sink, sanitizers, scope_sanitizers, rule.quantifier,
                    value_adjacency, control_adjacency, all_events,
                    value_edges, control_edges,
                    max_call_depth,
                    return_sanitizers,
                ):
                    continue
                key = (rule.rule_id, source.span.file_path, source.span.start_byte,
                       sink.span.file_path, sink.span.start_byte, sink.span.end_byte)
                if key in seen:
                    continue
                seen.add(key)
                state = min(sink_states, key=lambda item: len(_path_to(item, predecessor)))
                event_path = _path_to(state, predecessor)
                from emend.trace import TraceStep
                trace = [TraceStep(
                    file_path=stored_to_actual.get(event.file_path, event.file_path),
                    line=(
                        source.span.start_line + 1 if node == source.node else
                        sink.span.start_line + 1 if node == sink.node else
                        event.start_line + 1
                    ),
                    col=(
                        source.span.start_col if node == source.node else
                        sink.span.start_col if node == sink.node else event.start_column
                    ),
                    description=(
                        f"source: {source.span.text}" if node == source.node else
                        f"sink: {sink.span.text}" if node == sink.node else
                        f"propagation: {event.access_path or event.var or event.role}"
                    ),
                    variable=(
                        source.variable if node == source.node else
                        sink.variable if node == sink.node else
                        event.access_path or event.var
                    ),
                ) for node in event_path for event in [all_events[node]]]
                if not trace or (
                    trace[0].file_path != source.span.file_path
                    or trace[0].line != source.span.start_line + 1
                    or trace[0].col != source.span.start_col
                ):
                    trace.insert(0, TraceStep(
                        source.span.file_path, source.span.start_line + 1,
                        source.span.start_col, f"source: {source.span.text}", source.variable,
                    ))
                if not trace[-1].description.startswith("sink:"):
                    trace.append(TraceStep(
                        sink.span.file_path, sink.span.start_line + 1,
                        sink.span.start_col, f"sink: {sink.span.text}", sink.variable,
                    ))
                results.append(EvaluatedFlow(
                    sink.span.file_path, sink.span.start_line + 1, sink.span.start_col,
                    rule.label, sink_endpoint.pattern or sink_endpoint.effect,
                    rule.message + (
                        " (via function call)"
                        if len({all_events[node].func_id for node in event_path}) > 1
                        else ""
                    ), trace, rule_id=rule.rule_id,
                    rule_name=rule.name, severity=rule.severity,
                ))
    return sorted(results, key=lambda row: (
        row.file_path, row.line, row.col, row.rule_id, row.sink_pattern,
    ))


def evaluate_flow_config(
    config: CompiledFlowConfig,
    paths: list[str],
    *,
    label_filter: str | None = None,
    language: str = "python",
    project_path: str | None = None,
    graph: "FactGraph | None" = None,
    source_overrides: dict[str, str] | None = None,
    interprocedural: bool = True,
) -> list[EvaluatedFlow]:
    return evaluate_compiled_flow(
        config.compiled_rules(), paths, interprocedural=interprocedural,
        label_filter=label_filter, language=language, project_path=project_path,
        graph=graph, source_overrides=source_overrides,
    )


def execute_flow_spec(
    spec: FlowSpec,
    file_path: str,
    source: str,
    language: str,
    fact_graph: "FactGraph | None" = None,
) -> list[FlowViolation]:
    sanitizers = ([spec.sanitizers] if isinstance(spec.sanitizers, str)
                  else list(spec.sanitizers or ()))
    if spec.through:
        sanitizers.append(spec.through)
    label = spec.label or spec.name
    rule_id = f"legacy:{spec.name}"
    rows = evaluate_compiled_flow((CompiledFlowRule(
        rule_id=rule_id, name=spec.name, label=label, message=spec.message,
        severity=spec.severity,
        sources=(CompiledEndpoint(spec.sources, options={"source_capture": True}),),
        sinks=(CompiledEndpoint(spec.sinks),),
        sanitizers=tuple(CompiledEndpoint(pattern) for pattern in sanitizers),
    ),), [file_path], language=language, interprocedural=False,
        project_path=str(Path(file_path).resolve().parent), graph=fact_graph,
        source_overrides={str(Path(file_path).resolve()): source},
    )
    result: list[FlowViolation] = []
    for row in rows:
        source_step = next((step for step in row.trace
                            if step.description.startswith("source:")), None)
        witness = [WitnessStep(
            step.file_path, "", 0, step.line, step.col, step.variable,
            "source" if step.description.startswith("source:") else
            "sink" if step.description.startswith("sink:") else "propagation",
        ) for step in row.trace]
        sink_text = next((
            step.description.removeprefix("sink:").strip()
            for step in reversed(row.trace) if step.description.startswith("sink:")
        ), row.sink_pattern)
        result.append(FlowViolation(
            spec.name, row.message, spec.severity, row.file_path, row.line, row.col,
            source_step.line if source_step else 0,
            source_step.description.removeprefix("source:").strip() if source_step else "",
            sink_text, witness,
        ))
    return result
