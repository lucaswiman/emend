"""Shared IR and witness model for flow/path checking.

Every flow check is normalised to a :class:`FlowSpec`, executed via
:func:`execute_flow_spec`, and returned as :class:`FlowViolation` with a
shared :class:`WitnessStep` witness model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from emend.errors import BUG_EXCEPTIONS

if TYPE_CHECKING:
    from emend.fact_graph import FactGraph
    from emend.checks.pattern_rules import LintRule
    from emend.policy import FlowCheck

logger = logging.getLogger(__name__)


def _normalise_sanitizers(value: list[str] | str | None) -> list[str]:
    """Return independently executable not-through alternatives."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [pattern for pattern in value if pattern]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FlowSpec:
    """Canonical IR for a flow/path check."""
    name: str
    message: str
    sources: str          # pattern string for source matches
    sinks: str            # pattern string for sink matches
    sanitizers: list[str] | str | None = None   # not-through alternatives
    through: str | None = None      # must-pass-through pattern
    severity: str = "warning"
    label: str = ""


@dataclass(frozen=True)
class WitnessStep:
    """One hop in a flow witness trace."""
    file_path: str
    func_qn: str
    block_id: int
    line: int
    col: int = 0
    var_name: str = ""
    kind: str = ""   # "source", "propagation", "sink", "sanitizer"


@dataclass
class FlowViolation:
    """Unified result from any flow check engine."""
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


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------

def from_lint_rule(rule: "LintRule") -> FlowSpec:
    """Convert a ``LintRule`` with flow predicates to a :class:`FlowSpec`."""
    return FlowSpec(
        name=rule.name,
        message=rule.message,
        sources=rule.flows_from or "",
        sinks=rule.flows_to or "",
        sanitizers=rule.not_through,
        severity=rule.severity,
        label=rule.name,
    )


def from_flow_check(
    check: "FlowCheck",
    policy_name: str,
    policy_desc: str,
    severity: str = "warning",
) -> FlowSpec:
    """Convert a policy ``FlowCheck`` to a :class:`FlowSpec`."""
    return FlowSpec(
        name=policy_name,
        message=policy_desc,
        sources=check.flows_from,
        sinks=check.flows_to,
        sanitizers=check.not_through,
        severity=severity,
        label=check.label or policy_name,
    )


# ---------------------------------------------------------------------------
# Witness formatting
# ---------------------------------------------------------------------------

def format_witness(steps: list[WitnessStep]) -> list[str]:
    """Format witness steps into human-readable strings.

    Returns a list of strings, one per step, suitable for inclusion in
    ``PolicyViolation.witness`` or CLI output.
    """
    lines: list[str] = []
    for step in steps:
        prefix = step.kind or "step"
        loc = f"L{step.line}"
        if step.var_name:
            lines.append(f"{prefix} {loc}: {step.var_name}")
        else:
            lines.append(f"{prefix} {loc}")
    return lines


# ---------------------------------------------------------------------------
# Execution engine
# ---------------------------------------------------------------------------

def execute_flow_spec(
    spec: FlowSpec,
    file_path: str,
    source: str,
    language: str,
    fact_graph: "FactGraph | None" = None,
) -> list[FlowViolation]:
    """Execute a :class:`FlowSpec` and return unified :class:`FlowViolation` results.

    If *fact_graph* is provided, resolves matches via
    :class:`~emend.location_resolver.LocationResolver` and runs the Datalog
    ``flow_rule_check_datalog()`` engine.  Otherwise falls back to the Python
    line-order taint tracker.
    """
    if fact_graph is not None:
        return _execute_via_datalog(spec, file_path, source, language, fact_graph)
    return _execute_via_python(spec, file_path, source, language)


def _assignments_from_cfgs(
    source: str,
    file_path: str,
    func_start: int,
    func_end: int,
) -> list[tuple[int, str, str]]:
    """Find assignments via tree-sitter CFG block defs.

    Returns list of ``(abs_line, target_name, rhs_text)`` for writes/aug_writes
    within the given function's line range.
    """
    from emend.trace import _defs_from_cfgs, _extract_rhs_from_line

    ext = Path(file_path).suffix.lstrip('.') or 'py'
    source_lines = source.splitlines()
    assignments: list[tuple[int, str, str]] = []

    for abs_line, var_name in _defs_from_cfgs(source, func_start, func_end, ext=ext):
        rhs = _extract_rhs_from_line(source_lines, abs_line)
        if rhs is not None:
            assignments.append((abs_line, var_name, rhs))

    return sorted(assignments, key=lambda a: a[0])


def _execute_via_python(
    spec: FlowSpec,
    file_path: str,
    source: str,
    language: str,
) -> list[FlowViolation]:
    """Check a flow-based lint rule within each function in the file.

    For each function body, finds source and sink pattern matches, then
    propagates taint using the intraprocedural flow analysis engine.
    """
    from emend import emend_core
    from emend.ast_utils import _rust_dict_to_nested_symbol
    from emend.trace import _extract_identifiers
    from emend.transform import find_pattern

    violations: list[FlowViolation] = []

    # Get function definitions from source
    ext = Path(file_path).suffix.lstrip('.') or 'py'
    rust_syms = emend_core.collect_symbols_from_str(source, ext=ext)
    symbols = [
        _rust_dict_to_nested_symbol(d) for d in rust_syms
        if d.get("kind") not in ("variable", "reference")
    ]

    # Flatten to get all functions (including nested methods)
    def _all_functions(syms):
        for sym in syms:
            if sym.kind in ('function', 'async_function', 'method', 'async_method'):
                yield sym
            yield from _all_functions(sym.children)

    # Hoist pattern matching and line splitting out of the per-function loop
    all_source_matches = find_pattern(
        spec.sources, file_path, source_override=source, language=language
    )
    all_sink_matches = find_pattern(
        spec.sinks, file_path, source_override=source, language=language
    )
    all_sanitizer_matches = []
    sanitizers = (
        [spec.sanitizers]
        if isinstance(spec.sanitizers, str)
        else (spec.sanitizers or [])
    )
    for sanitizer in sanitizers:
        all_sanitizer_matches.extend(find_pattern(
            sanitizer, file_path, source_override=source, language=language
        ))

    # Intraprocedural flow analysis
    all_lines = source.splitlines()
    total_lines = len(all_lines)

    # Build the list of scopes to analyze.  When collect_symbols_from_str
    # doesn't detect any functions (e.g. TypeScript arrow functions or
    # top-level Rust code), fall back to analyzing the entire file as one
    # scope so that source→sink pairs are still checked.
    class _FakeScope:
        def __init__(self, start: int, end: int):
            self.line_start = start
            self.line_end = end

    function_scopes = list(_all_functions(symbols))
    if not function_scopes and all_source_matches and all_sink_matches:
        function_scopes = [_FakeScope(1, total_lines)]

    for sym in function_scopes:
        # Filter to matches within this function's line range
        func_sources = [
            m for m in all_source_matches
            if m.line is not None and sym.line_start <= m.line <= sym.line_end
        ]
        func_sinks = [
            m for m in all_sink_matches
            if m.line is not None and sym.line_start <= m.line <= sym.line_end
        ]

        if not func_sources or not func_sinks:
            continue

        # Find assignments within the function via tree-sitter CFG defs.
        assignments = _assignments_from_cfgs(
            source, file_path,
            func_start=sym.line_start,
            func_end=sym.line_end,
        )
        # Build a line -> [var_name, ...] lookup for O(1) assignment queries.
        assignments_lhs: dict[int, list[str]] = {}
        for assign_line, target, _rhs in assignments:
            assignments_lhs.setdefault(assign_line, []).append(target)

        for src_match in func_sources:
            src_line = src_match.line or 0
            tainted: dict[str, int] = {}  # name -> line where it became tainted

            for cap_name, cap_text in src_match.captures.items():
                for name in _extract_identifiers(cap_text):
                    tainted[name] = src_line

            if src_match.matched_text:
                # If this source line is an assignment, also taint the LHS variable.
                # Uses tree-sitter CFG defs (already computed above) instead of regex.
                for lhs_name in assignments_lhs.get(src_line, []):
                    tainted[lhs_name] = src_line

            taint_chain: list[tuple[int, str]] = [
                (src_line, ', '.join(sorted(tainted.keys())))
            ]

            for assign_line, target, rhs in sorted(assignments, key=lambda a: a[0]):
                if assign_line <= src_line:
                    continue
                rhs_names = _extract_identifiers(rhs)
                if rhs_names & set(tainted.keys()):
                    tainted[target] = assign_line
                    taint_chain.append((assign_line, target))

            for sink_match in func_sinks:
                sink_line = sink_match.line or 0
                if sink_line <= src_line:
                    continue

                sink_names: set[str] = set()
                for cap_name, cap_text in sink_match.captures.items():
                    sink_names |= _extract_identifiers(cap_text)
                if sink_match.matched_text:
                    sink_names |= _extract_identifiers(sink_match.matched_text or "")

                tainted_at_sink = {
                    name for name, line in tainted.items()
                    if line <= sink_line
                }

                if not (sink_names & tainted_at_sink):
                    continue

                sanitized = False
                if all_sanitizer_matches:
                    for san_match in all_sanitizer_matches:
                        san_line = san_match.line or 0
                        if src_line <= san_line < sink_line:
                            san_names: set[str] = set()
                            for cap_name, cap_text in san_match.captures.items():
                                san_names |= _extract_identifiers(cap_text)
                            if san_match.matched_text:
                                san_names |= _extract_identifiers(san_match.matched_text or "")
                            if san_names & tainted_at_sink:
                                sanitized = True
                                break
                            # If the sanitizer line is an assignment, check whether
                            # the LHS variable is tainted — uses tree-sitter CFG defs.
                            if any(
                                lhs_name in tainted_at_sink
                                for lhs_name in assignments_lhs.get(san_line, [])
                            ):
                                sanitized = True
                                break

                if sanitized:
                    continue

                src_text = (src_match.matched_text or "").strip()
                sink_text = (sink_match.matched_text or "").strip()
                witness = [
                    WitnessStep(file_path=file_path, func_qn="", block_id=0,
                                line=src_line, var_name=src_text, kind="source"),
                    *[WitnessStep(file_path=file_path, func_qn="", block_id=0,
                                  line=line, var_name=name, kind="propagation")
                      for line, name in taint_chain if src_line < line <= sink_line],
                    WitnessStep(file_path=file_path, func_qn="", block_id=0,
                                line=sink_line, var_name=sink_text, kind="sink"),
                ]
                violations.append(FlowViolation(
                    spec_name=spec.name, message=spec.message, severity=spec.severity,
                    file_path=file_path, line=sink_line, source_line=src_line,
                    source_text=src_text, sink_text=sink_text, witness=witness,
                ))

    return violations


def _var_name_from_match(m: Any) -> str:
    """Extract variable name from a pattern match: first capture, then matched text."""
    for _cap_name, cap_text in m.captures.items():
        return cap_text.strip()
    if m.matched_text:
        return m.matched_text.strip()
    return ""


def _execute_via_datalog(
    spec: FlowSpec,
    file_path: str,
    source: str,
    language: str,
    fact_graph: "FactGraph",
) -> list[FlowViolation]:
    """Run flow check via FactGraph Datalog engine with location resolution."""
    from emend.location_resolver import LocationResolver
    from emend.transform import find_pattern

    resolver = LocationResolver.from_fact_graph(fact_graph, file_path)

    def resolve_matches(patterns):
        locations, lines, metadata, metadata_by_var = [], {}, {}, {}
        for pattern in patterns:
            for match in find_pattern(
                pattern, file_path, source_override=source, language=language,
            ):
                if match.line is None:
                    continue
                loc = resolver.resolve(file_path, match.line, match.col or 0, match.captures)
                var_name = _var_name_from_match(match)
                key = (loc.file_path, loc.func_qn, var_name, loc.block_id)
                locations.append(key)
                lines.setdefault((loc.file_path, loc.func_qn, loc.block_id), match.line)
                meta = {"line": match.line, "col": match.col or 0,
                        "text": (match.matched_text or var_name).strip()}
                metadata.setdefault(key, meta)
                metadata_by_var.setdefault(key[:3], meta)
        return locations, lines, metadata, metadata_by_var

    source_locs, source_lines, source_meta, source_meta_by_var = resolve_matches([spec.sources])
    sink_locs, sink_lines, sink_meta, sink_meta_by_var = resolve_matches([spec.sinks])
    if not source_locs or not sink_locs:
        return []
    blocker_locs, blocker_lines, _, _ = resolve_matches(_normalise_sanitizers(spec.sanitizers))
    through_locs, _, _, _ = resolve_matches([spec.through] if spec.through else [])

    try:
        raw_violations = fact_graph.flow_rule_check_datalog(
            sources=source_locs,
            sinks=sink_locs,
            through=through_locs,
            not_through=blocker_locs,
            source_lines=source_lines,
            sink_lines=sink_lines,
            blocker_lines=blocker_lines,
            include_locations=True,
        )
    except BUG_EXCEPTIONS:
        raise
    except Exception:
        logger.debug(
            "Datalog flow check failed for %s; falling back to Python",
            file_path,
            exc_info=True,
        )
        return _execute_via_python(spec, file_path, source, language)

    results: list[FlowViolation] = []
    for fp, fq, src_var, sink_var, src_block, sink_block in raw_violations:
        src_info = source_meta.get((fp, fq, src_var, src_block))
        if src_info is None:
            src_info = source_meta_by_var.get((fp, fq, src_var), {})
        sink_info = sink_meta.get((fp, fq, sink_var, sink_block))
        if sink_info is None:
            sink_info = sink_meta_by_var.get((fp, fq, sink_var), {})

        src_line = int(src_info.get("line", 0))
        src_col = int(src_info.get("col", 0))
        src_text = str(src_info.get("text", src_var))
        sink_line = int(sink_info.get("line", 0))
        sink_col = int(sink_info.get("col", 0))
        sink_text = str(sink_info.get("text", sink_var))

        # Build minimal witness
        witness_steps = [
            WitnessStep(
                file_path=fp, func_qn=fq, block_id=src_block,
                line=src_line, col=src_col, var_name=src_text, kind="source",
            ),
            WitnessStep(
                file_path=fp, func_qn=fq, block_id=sink_block,
                line=sink_line, col=sink_col, var_name=sink_text, kind="sink",
            ),
        ]
        results.append(FlowViolation(
            spec_name=spec.name,
            message=spec.message,
            severity=spec.severity,
            file_path=fp,
            line=sink_line,
            col=sink_col,
            source_line=src_line,
            source_text=src_text,
            sink_text=sink_text,
            witness=witness_steps,
        ))
    return results
