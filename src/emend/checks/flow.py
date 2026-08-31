"""Shared IR and witness model for flow/path checking.

Every flow check is normalised to a :class:`FlowSpec`, executed via
:func:`execute_flow_spec`, and returned as :class:`FlowViolation` with a
shared :class:`WitnessStep` witness model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from emend.errors import BUG_EXCEPTIONS

if TYPE_CHECKING:
    from emend.fact_graph import FactGraph
    from emend.lint import LintRule, FlowWitness
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

def _flow_witness_to_steps(
    fw: "FlowWitness",
    file_path: str,
) -> list[WitnessStep]:
    """Convert a lint ``FlowWitness`` to a list of ``WitnessStep``."""
    steps: list[WitnessStep] = []
    steps.append(WitnessStep(
        file_path=file_path,
        func_qn="",
        block_id=0,
        line=fw.source_line,
        var_name=fw.source_text,
        kind="source",
    ))
    for chain_line, chain_var in fw.taint_chain:
        if chain_line == fw.source_line:
            continue  # already covered by source step
        steps.append(WitnessStep(
            file_path=file_path,
            func_qn="",
            block_id=0,
            line=chain_line,
            var_name=chain_var,
            kind="propagation",
        ))
    steps.append(WitnessStep(
        file_path=file_path,
        func_qn="",
        block_id=0,
        line=fw.sink_line,
        var_name=fw.sink_text,
        kind="sink",
    ))
    return steps


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
    regex taint tracker in ``lint._check_flow_rule()``.
    """
    if fact_graph is not None:
        return _execute_via_datalog(spec, file_path, source, language, fact_graph)
    return _execute_via_python(spec, file_path, source, language)


def _execute_via_python(
    spec: FlowSpec,
    file_path: str,
    source: str,
    language: str,
) -> list[FlowViolation]:
    """Fallback: run flow check via lint.py's Python taint tracker."""
    from emend.lint import LintRule, _check_flow_rule

    rule = LintRule(
        name=spec.name,
        find="",
        message=spec.message,
        flows_from=spec.sources,
        flows_to=spec.sinks,
        not_through=spec.sanitizers,
    )
    lint_violations = _check_flow_rule(rule, file_path, source, language)

    results: list[FlowViolation] = []
    for lv in lint_violations:
        witness_steps: list[WitnessStep] = []
        if lv.witness is not None:
            witness_steps = _flow_witness_to_steps(lv.witness, file_path)

        results.append(FlowViolation(
            spec_name=spec.name,
            message=lv.message or spec.message,
            severity=spec.severity,
            file_path=lv.file_path,
            line=lv.line,
            col=lv.col,
            source_line=lv.witness.source_line if lv.witness else 0,
            source_text=lv.witness.source_text if lv.witness else "",
            sink_text=lv.witness.sink_text if lv.witness else "",
            witness=witness_steps,
        ))
    return results


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

    def _remember_match(
        store: dict[tuple[str, str, str, int], dict[str, object]],
        fallback_store: dict[tuple[str, str, str], dict[str, object]],
        *,
        fp: str,
        fq: str,
        var_name: str,
        block_id: int,
        line: int,
        col: int,
        text: str,
    ) -> None:
        meta = {
            "line": line,
            "col": col,
            "text": text,
        }
        store.setdefault((fp, fq, var_name, block_id), meta)
        fallback_store.setdefault((fp, fq, var_name), meta)

    # Resolve source matches
    source_matches = find_pattern(
        spec.sources, file_path, source_override=source, language=language,
    )
    sink_matches = find_pattern(
        spec.sinks, file_path, source_override=source, language=language,
    )

    if not source_matches or not sink_matches:
        return []

    # Build resolved location tuples for Datalog
    source_locs: list[tuple[str, str, str, int]] = []
    source_lines: dict[tuple[str, str, int], int] = {}
    source_meta: dict[tuple[str, str, str, int], dict[str, object]] = {}
    source_meta_by_var: dict[tuple[str, str, str], dict[str, object]] = {}
    for m in source_matches:
        if m.line is None:
            continue
        loc = resolver.resolve(file_path, m.line, m.col or 0, m.captures)
        var_name = _var_name_from_match(m)
        source_locs.append((loc.file_path, loc.func_qn, var_name, loc.block_id))
        key = (loc.file_path, loc.func_qn, loc.block_id)
        if key not in source_lines:
            source_lines[key] = m.line
        _remember_match(
            source_meta,
            source_meta_by_var,
            fp=loc.file_path,
            fq=loc.func_qn,
            var_name=var_name,
            block_id=loc.block_id,
            line=m.line,
            col=m.col or 0,
            text=(m.matched_text or var_name).strip(),
        )

    sink_locs: list[tuple[str, str, str, int]] = []
    sink_lines: dict[tuple[str, str, int], int] = {}
    sink_meta: dict[tuple[str, str, str, int], dict[str, object]] = {}
    sink_meta_by_var: dict[tuple[str, str, str], dict[str, object]] = {}
    for m in sink_matches:
        if m.line is None:
            continue
        loc = resolver.resolve(file_path, m.line, m.col or 0, m.captures)
        var_name = _var_name_from_match(m)
        sink_locs.append((loc.file_path, loc.func_qn, var_name, loc.block_id))
        key = (loc.file_path, loc.func_qn, loc.block_id)
        if key not in sink_lines:
            sink_lines[key] = m.line
        _remember_match(
            sink_meta,
            sink_meta_by_var,
            fp=loc.file_path,
            fq=loc.func_qn,
            var_name=var_name,
            block_id=loc.block_id,
            line=m.line,
            col=m.col or 0,
            text=(m.matched_text or var_name).strip(),
        )

    # Resolve blocker (not-through) matches
    blocker_locs: list[tuple[str, str, str, int]] | None = None
    blocker_lines: dict[tuple[str, str, int], int] = {}
    if _normalise_sanitizers(spec.sanitizers):
        san_matches = []
        for sanitizer in _normalise_sanitizers(spec.sanitizers):
            san_matches.extend(find_pattern(
                sanitizer, file_path, source_override=source, language=language,
            ))
        if san_matches:
            blocker_locs = []
            for m in san_matches:
                if m.line is None:
                    continue
                loc = resolver.resolve(file_path, m.line, m.col or 0, m.captures)
                var_name = _var_name_from_match(m)
                blocker_locs.append((loc.file_path, loc.func_qn, var_name, loc.block_id))
                key = (loc.file_path, loc.func_qn, loc.block_id)
                if key not in blocker_lines:
                    blocker_lines[key] = m.line

    # Resolve through matches
    through_locs: list[tuple[str, str, str, int]] | None = None
    if spec.through:
        through_matches = find_pattern(
            spec.through, file_path, source_override=source, language=language,
        )
        if through_matches:
            through_locs = []
            for m in through_matches:
                if m.line is None:
                    continue
                loc = resolver.resolve(file_path, m.line, m.col or 0, m.captures)
                var_name = _var_name_from_match(m)
                through_locs.append((loc.file_path, loc.func_qn, var_name, loc.block_id))

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
