"""Intraprocedural trace (data-flow) analysis engine for emend.

Tracks labeled value flow from sources to sinks within individual
functions, with sanitizers and path traces.  Supports Datalog-based
propagation via :class:`~emend.fact_graph.FactGraph` when
``use_datalog=True`` (the default), falling back to the regex-based
Python simulation when fact graph construction fails or is unavailable.

Formerly named "taint analysis"; renamed to "trace" because the engine
is a general labeled data-flow tracer — not only for security taint.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from emend.transform import find_pattern, PatternMatch
from emend.rules_config import (
    LEGACY_PATTERNS_PATH,
    as_list,
    expand_macros,
    load_rules_document,
    yaml_key,
)

if TYPE_CHECKING:
    from emend.fact_graph import FactGraph

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TraceSource:
    """A pattern that introduces taint."""
    pattern: str  # emend pattern string
    label: str  # taint label name
    type_constraint: str = ""  # e.g. "!int & !float & !bool & !str"


@dataclass
class TraceSink:
    """A pattern that should not receive tainted values.

    Either ``pattern`` or ``effect`` (or both) must be set.  An ``effect``
    key like ``"writes($OBJ)"`` resolves via the fact graph instead of
    pattern matching — see :meth:`FactGraph.trace_propagation_datalog`.
    """
    pattern: str  # emend pattern string (may be "" when effect is set)
    label: str  # taint label name (which labels are forbidden)
    message: str  # violation message
    effect: str = ""  # e.g. "writes($OBJ)", "reads($OBJ)"
    type_constraint: str = ""  # e.g. "!int & !float"


@dataclass
class TraceSanitizer:
    """A pattern that removes taint."""
    pattern: str  # emend pattern string
    label: str  # which label is sanitized
    quantifier: str = "all_paths"  # "all_paths" or "some_path"
    type_constraint: str = ""  # e.g. "!int & !float"


@dataclass
class TraceScopeSanitizer:
    """A pattern that kills ALL taint for a label within a scope.

    Unlike regular sanitizers which kill taint for a specific matched
    variable, scope sanitizers kill all taint carrying the given label
    when the pattern is matched. This models scope/context boundaries
    like transaction commits or session closes.

    Known limitation: nested scopes (e.g., ``with db.begin(): with
    db.begin():``) kill all taint for the label, not tracking
    per-session. This is acceptable for the TOCTOU use case but means
    inner scope kills will also clear outer scope's taint (false negatives).
    """
    pattern: str  # emend pattern string
    label: str    # which label is killed


@dataclass
class TraceConfig:
    """Configuration for taint analysis from patterns.yaml."""
    labels: list[str] = field(default_factory=list)
    sources: list[TraceSource] = field(default_factory=list)
    sinks: list[TraceSink] = field(default_factory=list)
    sanitizers: list[TraceSanitizer] = field(default_factory=list)
    scope_sanitizers: list[TraceScopeSanitizer] = field(default_factory=list)


@dataclass
class TraceStep:
    """A step in a taint propagation trace."""
    file_path: str
    line: int
    col: int
    description: str  # e.g. "source: user_input via request.args.get($X)"
    variable: str  # the variable name at this step


@dataclass
class TraceViolation:
    """A taint violation: tainted value reached a sink."""
    file_path: str
    line: int
    col: int
    label: str
    sink_pattern: str
    message: str
    trace: list[TraceStep] = field(default_factory=list)
    engine: str = ""  # "python" or "datalog" — which engine produced this violation


# ---------------------------------------------------------------------------
# Type constraint evaluation
# ---------------------------------------------------------------------------


def evaluate_type_constraint(constraint: str, type_name: str) -> bool:
    """Evaluate a boolean type constraint against a top-level type name.

    Constraint syntax:
    - Bare name: matches if ``type_name == name``
    - ``!name``: negation
    - ``expr & expr``: conjunction (AND)
    - ``expr | expr``: disjunction (OR)

    ``&`` binds tighter than ``|``.  Parentheses are not supported.
    Matching is against the **top-level** type constructor only (e.g.
    ``int`` matches ``int`` but not ``Optional[int]``).

    Returns ``True`` if the type satisfies the constraint, ``False`` otherwise.
    An empty constraint always returns ``True``.
    """
    parsed = _parse_type_constraint(constraint)
    if parsed is None:
        return True
    return any(
        all(_eval_atom(atom, type_name) for atom in and_group)
        for and_group in parsed
    )


@functools.lru_cache(maxsize=256)
def _parse_type_constraint(constraint: str) -> tuple[tuple[tuple[str, bool], ...], ...] | None:
    """Parse a type constraint string into a cached tuple of OR-groups.

    Returns ``None`` for empty constraints.  Each OR-group is a tuple of
    ``(name, negated)`` atoms.
    """
    constraint = constraint.strip()
    if not constraint:
        return None
    or_parts = [p.strip() for p in constraint.split("|")]
    return tuple(
        tuple(_parse_atom(a.strip()) for a in part.split("&"))
        for part in or_parts
    )


def _parse_atom(atom: str) -> tuple[str, bool]:
    """Parse a single atom into (name, negated)."""
    atom = atom.strip()
    if atom.startswith("!"):
        return (atom[1:].strip(), True)
    return (atom, False)


def _eval_atom(atom: tuple[str, bool], type_name: str) -> bool:
    """Evaluate a parsed type constraint atom."""
    name, negated = atom
    if negated:
        return type_name != name
    return type_name == name


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_EFFECT_PREDICATE_RE = re.compile(r"^\s*(writes|reads)\s*\(.*\)\s*$")


def _trace_config_from_trace_section(raw: dict[str, Any] | None) -> TraceConfig:
    """Parse legacy ``trace:``/``taint:`` section format."""
    if raw is None:
        return TraceConfig()
    if not isinstance(raw, dict):
        return TraceConfig()

    labels = [str(v) for v in as_list(raw.get("labels"))]

    sources = []
    for s in raw.get("sources", []) or []:
        if not isinstance(s, dict):
            continue
        if "pattern" not in s or "label" not in s:
            continue
        sources.append(TraceSource(
            pattern=str(s["pattern"]),
            label=str(s["label"]),
            type_constraint=str(s.get("type_constraint", "")),
        ))

    sinks = []
    for s in raw.get("sinks", []) or []:
        if not isinstance(s, dict):
            continue
        if "label" not in s:
            continue
        sinks.append(TraceSink(
            pattern=str(s.get("pattern", "")),
            label=str(s["label"]),
            message=str(s.get("message", "Traced value reaches sink")),
            effect=str(s.get("effect", "")),
            type_constraint=str(s.get("type_constraint", "")),
        ))

    sanitizers = []
    for s in raw.get("sanitizers", []) or []:
        if not isinstance(s, dict):
            continue
        if "pattern" not in s or "label" not in s:
            continue
        sanitizers.append(TraceSanitizer(
            pattern=str(s["pattern"]),
            label=str(s["label"]),
            quantifier=str(s.get("quantifier", "all_paths")),
            type_constraint=str(s.get("type_constraint", "")),
        ))

    scope_sanitizers = []
    for s in raw.get("scope_sanitizers", []) or []:
        if not isinstance(s, dict):
            continue
        if "pattern" not in s or "label" not in s:
            continue
        scope_sanitizers.append(TraceScopeSanitizer(
            pattern=str(s["pattern"]),
            label=str(s["label"]),
        ))

    return TraceConfig(
        labels=labels,
        sources=sources,
        sinks=sinks,
        sanitizers=sanitizers,
        scope_sanitizers=scope_sanitizers,
    )


def _trace_config_from_unified_rules(config: dict[str, Any]) -> TraceConfig:
    """Parse unified ``rules: {name: {flow: ...}}`` entries into TraceConfig."""
    raw_rules = config.get("rules", {}) or {}
    if not isinstance(raw_rules, dict):
        return TraceConfig()

    macros = config.get("macros", {}) or {}
    labels: list[str] = []
    sources: list[TraceSource] = []
    sinks: list[TraceSink] = []
    sanitizers: list[TraceSanitizer] = []
    scope_sanitizers: list[TraceScopeSanitizer] = []

    for name, rule_def in raw_rules.items():
        if not isinstance(rule_def, dict):
            continue
        if rule_def.get("enabled") is False:
            continue

        flow_def = rule_def.get("flow")
        if not isinstance(flow_def, dict):
            continue

        flow_from = yaml_key(flow_def, "from", "flows_from")
        flow_to = yaml_key(flow_def, "to", "flows_to")
        if not flow_from or not flow_to:
            continue

        label = str(flow_def.get("label") or rule_def.get("label") or name)
        labels.append(label)

        source_pattern = expand_macros(str(flow_from), macros)
        sink_pattern = expand_macros(str(flow_to), macros)
        sources.append(TraceSource(
            pattern=source_pattern,
            label=label,
            type_constraint=str(flow_def.get("type_constraint", "")),
        ))

        sink_message = str(rule_def.get("message", "Traced value reaches sink"))
        if _EFFECT_PREDICATE_RE.match(sink_pattern):
            sinks.append(TraceSink(
                pattern="",
                label=label,
                message=sink_message,
                effect=sink_pattern,
                type_constraint=str(flow_def.get("type_constraint", "")),
            ))
        else:
            sinks.append(TraceSink(
                pattern=sink_pattern,
                label=label,
                message=sink_message,
                effect=str(flow_def.get("effect", "")),
                type_constraint=str(flow_def.get("type_constraint", "")),
            ))

        for sanitizer in as_list(yaml_key(flow_def, "not_through")):
            sanitizer_pattern = expand_macros(str(sanitizer), macros)
            if sanitizer_pattern:
                sanitizers.append(TraceSanitizer(
                    pattern=sanitizer_pattern,
                    label=label,
                    quantifier=str(flow_def.get("quantifier", "all_paths")),
                    type_constraint=str(flow_def.get("type_constraint", "")),
                ))

        for scope_def in as_list(yaml_key(flow_def, "not_through_scope")):
            scope_pattern = expand_macros(str(scope_def), macros)
            if scope_pattern:
                scope_sanitizers.append(TraceScopeSanitizer(
                    pattern=scope_pattern,
                    label=label,
                ))

    return TraceConfig(
        labels=labels,
        sources=sources,
        sinks=sinks,
        sanitizers=sanitizers,
        scope_sanitizers=scope_sanitizers,
    )


def load_trace_config(config_path: str) -> TraceConfig:
    """Load trace analysis configuration from a YAML file.

    Reads the ``trace`` section from the given YAML config file.

    Args:
        config_path: Path to the YAML config file (typically .emend/patterns.yaml).

    Returns:
        A TraceConfig with sources, sinks, sanitizers, and labels.

    Raises:
        FileNotFoundError: If the config file does not exist.
    """
    config, _path = load_rules_document(
        config_path,
        fallbacks=(LEGACY_PATTERNS_PATH,),
    )

    raw_section = config.get("trace")
    if raw_section is None:
        raw_section = config.get("taint")

    explicit_config = _trace_config_from_trace_section(raw_section)
    rules_flow_config = _trace_config_from_unified_rules(config)

    # Support presets in both old trace-section and unified top-level forms.
    preset_names: list[str] = []
    preset_names.extend(str(v) for v in as_list(config.get("presets")))
    if isinstance(raw_section, dict):
        preset_names.extend(str(v) for v in as_list(raw_section.get("presets")))
    if preset_names:
        # Deduplicate while preserving order.
        seen: set[str] = set()
        unique_preset_names: list[str] = []
        for name in preset_names:
            if name not in seen:
                seen.add(name)
                unique_preset_names.append(name)

        from emend.trace_presets import get_preset, merge_configs

        preset_configs = [get_preset(name) for name in unique_preset_names]
        return merge_configs(*preset_configs, explicit_config, rules_flow_config)

    if (
        explicit_config.labels
        or explicit_config.sources
        or explicit_config.sinks
        or explicit_config.sanitizers
        or explicit_config.scope_sanitizers
        or rules_flow_config.labels
        or rules_flow_config.sources
        or rules_flow_config.sinks
        or rules_flow_config.sanitizers
        or rules_flow_config.scope_sanitizers
    ):
        from emend.trace_presets import merge_configs
        return merge_configs(explicit_config, rules_flow_config)

    return TraceConfig()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z_0-9]*)\b")
_QUALIFIED_IDENT_RE = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\b")

# Augmented assignment regex: ``x += expr``, ``obj.field -= expr``, etc.
_AUG_ASSIGN_RE = re.compile(
    r"^([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*"
    r"(\+|-|\*|/|//|%|\*\*|&|\||\^|>>|<<)=\s*(.+)",
    re.DOTALL,
)


def _extract_identifiers(expr: str) -> set[str]:
    """Return all simple identifiers appearing in *expr*."""
    _KEYWORDS = frozenset({
        "False", "None", "True", "and", "as", "assert", "async", "await",
        "break", "class", "continue", "def", "del", "elif", "else",
        "except", "finally", "for", "from", "global", "if", "import",
        "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise",
        "return", "try", "while", "with", "yield",
    })
    return {m for m in _IDENT_RE.findall(expr) if m not in _KEYWORDS}


def _extract_qualified_identifiers(expr: str) -> set[str]:
    """Return both simple identifiers and dotted attribute access patterns in *expr*.

    For example, ``obj.field`` and ``obj.field.subfield`` are returned as-is,
    in addition to all simple identifiers found by ``_extract_identifiers``.
    This enables field-sensitive taint tracking where ``obj.dirty`` is distinct
    from ``obj.clean``.
    """
    simple = _extract_identifiers(expr)
    qualified = set(_QUALIFIED_IDENT_RE.findall(expr))
    return simple | qualified


def _find_assignments_in_source(source: str, ext: str = "py") -> list[tuple[int, str, str]]:
    """Find assignments in source using tree-sitter statement ranges.

    Returns a list of (line, target_name, rhs_text) tuples for simple
    assignments like ``x = expr`` or ``x = func(expr)``, as well as dotted
    attribute assignments like ``obj.field = expr`` (returned as
    ``"obj.field"`` in the target position for field-sensitive tracking).
    """
    from emend import emend_core

    assignments: list[tuple[int, str, str]] = []
    lines = source.split("\n")
    ranges = emend_core.get_statement_ranges(source, ext=ext)

    for start, end in ranges:
        # Extract the statement text (1-based lines from tree-sitter)
        stmt_lines = lines[start - 1 : end]
        stmt_text = "\n".join(stmt_lines).strip()

        # Simple assignment: target = value (skip augmented assignments +=, etc.)
        # Match patterns like ``x = ...`` or ``x: type = ...``
        m = re.match(
            r"^([A-Za-z_][A-Za-z_0-9]*)\s*(?::\s*[^=]+)?\s*=\s*(?!=)(.+)",
            stmt_text,
            re.DOTALL,
        )
        if m:
            target = m.group(1)
            rhs = m.group(2).strip()
            assignments.append((start, target, rhs))
            continue

        # Dotted attribute assignment: obj.field = value (field-sensitive)
        # Matches ``obj.field = ...`` or ``obj.field.sub = ...``
        m_dotted = re.match(
            r"^([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\s*=\s*(?!=)(.+)",
            stmt_text,
            re.DOTALL,
        )
        if m_dotted:
            target = m_dotted.group(1)
            rhs = m_dotted.group(2).strip()
            assignments.append((start, target, rhs))
            continue

        # Augmented assignment: target op= value (+=, -=, *=, etc.)
        m_aug = _AUG_ASSIGN_RE.match(stmt_text)
        if m_aug:
            target = m_aug.group(1)
            rhs = m_aug.group(3).strip()
            assignments.append((start, target, rhs))

    return assignments


# Regex for list/dict container mutations
_CONTAINER_APPEND_RE = re.compile(
    r"^\s*([A-Za-z_]\w*)\.(?:append|extend|update)\s*\((.+)\)\s*$"
)
_CONTAINER_SUBSCRIPT_ASSIGN_RE = re.compile(
    r"^\s*([A-Za-z_]\w*)\s*\[.*?\]\s*=\s*(.+)$"
)
# Regex for subscript read on RHS: items[...] or d[...]
_SUBSCRIPT_READ_RE = re.compile(r"^([A-Za-z_]\w*)\s*\[")
# Regex for for-loop iteration
_FOR_LOOP_RE = re.compile(r"^\s*for\s+([A-Za-z_]\w*)\s+in\s+(.+?)\s*:")


def _find_container_mutations(source: str) -> list[tuple[int, str, str]]:
    """Find container mutation statements in source.

    Returns a list of (line, container_name, rhs_text) for:
    - ``items.append(expr)``  -> container=items, rhs=expr
    - ``items.extend(other)`` -> container=items, rhs=other
    - ``d.update(other)``     -> container=d, rhs=other
    - ``d[key] = expr``       -> container=d, rhs=expr

    Line numbers are 1-based.
    """
    mutations: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(source.split("\n"), start=1):
        m = _CONTAINER_APPEND_RE.match(line)
        if m:
            mutations.append((lineno, m.group(1), m.group(2).strip()))
            continue
        m = _CONTAINER_SUBSCRIPT_ASSIGN_RE.match(line)
        if m:
            mutations.append((lineno, m.group(1), m.group(2).strip()))
    return mutations


def _find_for_loops(source: str) -> list[tuple[int, str, str]]:
    """Find for-loop iteration statements in source.

    Returns a list of (line, loop_var, iterable_expr) for ``for VAR in EXPR:``.
    Line numbers are 1-based.
    """
    loops: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(source.split("\n"), start=1):
        m = _FOR_LOOP_RE.match(line)
        if m:
            loops.append((lineno, m.group(1), m.group(2).strip()))
    return loops


# ---------------------------------------------------------------------------
# Type constraint helpers
# ---------------------------------------------------------------------------


def _has_type_constraints(config: TraceConfig) -> bool:
    """Check if any source/sink/sanitizer has a type_constraint."""
    for s in config.sources:
        if s.type_constraint:
            return True
    for s in config.sinks:
        if s.type_constraint:
            return True
    for s in config.sanitizers:
        if s.type_constraint:
            return True
    return False


def _maybe_create_type_oracle(config: TraceConfig) -> Any | None:
    """Create a type oracle if any rule has a type_constraint."""
    if not _has_type_constraints(config):
        return None
    try:
        from emend.type_oracle import create_type_oracle
        oracle = create_type_oracle(engine="auto")
        if oracle.is_available():
            return oracle
    except Exception:
        logger.debug("Could not create type oracle for type constraints", exc_info=True)
    return None


def _filter_vars_by_type(
    vars: set[str],
    constraint: str,
    type_oracle: Any,
    file_path: str,
    line: int,
) -> set[str]:
    """Filter variables by type constraint using the type oracle.

    For each variable, query the type oracle for its inferred type at the
    given line.  Keep the variable only if its top-level type constructor
    satisfies the constraint.  Variables whose types cannot be determined
    are kept (conservative: don't suppress taint when type is unknown).
    """
    from emend.type_oracle import parse_type_string, FileTypes

    kept: set[str] = set()
    try:
        file_types: FileTypes = type_oracle.infer_file(Path(file_path))
        file_types.build_index()
    except Exception:
        logger.debug("Type oracle failed for %s, keeping all vars", file_path, exc_info=True)
        return vars

    for var in vars:
        # Try to find this variable's type binding by name at the right line
        bindings = file_types.types_for_name(var)
        type_name = ""
        if bindings:
            # Find the binding closest to (at or before) the match line
            best = None
            for b in bindings:
                if b.line <= line:
                    if best is None or b.line > best.line:
                        best = b
            if best is None:
                best = bindings[0]
            td = parse_type_string(best.raw_type)
            type_name = td.name
        if not type_name:
            # Unknown type — conservatively keep taint
            kept.add(var)
            continue
        if evaluate_type_constraint(constraint, type_name):
            kept.add(var)
    return kept


def _filter_by_receiver_type(
    matches: list[tuple[str, str, str, int, str]],
    type_constraint: str,
    graph: "FactGraph",
) -> list[tuple[str, str, str, int, str]]:
    """Filter source/sink locations by receiver type via object-sensitive dispatch.

    For method-call patterns (``obj.method($X)``), resolves ``obj``'s type
    using the ``type_binding`` relation in the fact graph and filters by the
    ``type_constraint`` on the source/sink definition.

    Non-method-call matches and matches with unknown receiver types are kept
    (conservative).
    """
    if not type_constraint:
        return matches

    try:
        resolved_types = graph.method_call_types()
    except Exception:
        return matches

    # Build a lookup: (file_path, func_qn, receiver) -> type_str
    receiver_types: dict[tuple[str, str, str], str] = {}
    for fp, fq, rcv, _meth, ts in resolved_types:
        receiver_types[(fp, fq, rcv)] = ts

    kept = []
    for fp, fq, var, bid, lbl in matches:
        # Check if var looks like a receiver (has a dot — e.g., "obj" for "obj.method()")
        rtype = receiver_types.get((fp, fq, var))
        if rtype is None:
            # Unknown receiver type — conservatively keep
            kept.append((fp, fq, var, bid, lbl))
            continue
        if evaluate_type_constraint(type_constraint, rtype):
            kept.append((fp, fq, var, bid, lbl))
    return kept


# ---------------------------------------------------------------------------
# Core trace analysis
# ---------------------------------------------------------------------------

def _analyze_function(
    file_path: str,
    source: str,
    func_start: int,
    func_end: int,
    config: TraceConfig,
    label_filter: str | None = None,
    language: str = "python",
    type_oracle: Any | None = None,
) -> list[TraceViolation]:
    """Analyze a single function body for taint violations.

    Args:
        file_path: Path to the source file.
        source: Full file source text.
        func_start: 1-based start line of the function.
        func_end: 1-based end line of the function.
        config: Trace configuration with sources, sinks, sanitizers.
        label_filter: If set, only check this specific taint label.
        language: Source language.

    Returns:
        List of taint violations found in this function.
    """
    lines = source.split("\n")

    # We use the full file source for find_pattern calls so tree-sitter can
    # parse it correctly, then filter matches to the function's line range.
    # For assignment analysis we extract the body (excluding the def line),
    # dedent it, and parse that as top-level statements.
    import textwrap

    # Build dedented body for assignment analysis
    body_start = func_start + 1  # skip the def line
    if body_start > func_end:
        body_start = func_start  # module-level scope, no def line to skip
    body_text_lines = lines[body_start - 1 : func_end]
    body_text = "\n".join(body_text_lines) + "\n"
    body_dedented = textwrap.dedent(body_text)

    def _in_range(match_line: int) -> bool:
        """Check if a match line falls within this function."""
        return func_start <= match_line <= func_end

    # Taint state: variable_name -> {label: TraceStep (where it was tainted)}
    taint_state: dict[str, dict[str, TraceStep]] = {}

    # Step 1: Find source pattern matches and establish initial taint
    for src_def in config.sources:
        if label_filter and src_def.label != label_filter:
            continue
        try:
            matches = find_pattern(
                src_def.pattern, file_path,
                source_override=source,
                language=language,
            )
        except Exception:
            logger.debug("find_pattern failed for source pattern %s", src_def.pattern, exc_info=True)
            continue

        for match in matches:
            match_line = match.line or 1
            match_col = match.col or 0
            if not _in_range(match_line):
                continue

            # Find the assignment target for this source match.
            # Check if this match is on the RHS of an assignment.
            # Also check captures for variables introduced by the pattern.
            tainted_vars: set[str] = set()

            # Look for assignments on this line in the original source
            stmt_line = lines[match_line - 1] if match_line <= len(lines) else ""
            assign_match = re.match(
                r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=\s*",
                stmt_line,
            )
            if assign_match:
                tainted_vars.add(assign_match.group(1))

            # Also check captures - any metavar capture that looks like a
            # variable is tainted at the source site
            for cap_name, cap_val in (match.captures or {}).items():
                if cap_val and re.match(r"^[A-Za-z_][A-Za-z_0-9]*$", cap_val):
                    tainted_vars.add(cap_val)

            # If no assignment target found, the match text itself may name
            # a variable or the whole expression is the source
            if not tainted_vars and match.matched_text:
                # Use identifiers from matched text as tainted
                for ident in _extract_identifiers(match.matched_text):
                    tainted_vars.add(ident)

            # Type constraint check: skip this source if the assignment
            # target's inferred type does not satisfy the constraint.
            if src_def.type_constraint and type_oracle and tainted_vars:
                tainted_vars = _filter_vars_by_type(
                    tainted_vars, src_def.type_constraint,
                    type_oracle, file_path, match_line,
                )
                if not tainted_vars:
                    continue

            step = TraceStep(
                file_path=file_path,
                line=match_line,
                col=match_col,
                description=f"source: {src_def.label} via {src_def.pattern}",
                variable=", ".join(sorted(tainted_vars)) or "?",
            )
            for var in tainted_vars:
                if var not in taint_state:
                    taint_state[var] = {}
                taint_state[var][src_def.label] = step

    # Step 2: Propagate taint through assignments, container mutations, and
    # for-loops in line order.  Merging all three into a single sorted pass
    # ensures that a container mutated on line N is seen as tainted when a
    # subscript read occurs on line N+1.

    body_assignments = _find_assignments_in_source(body_dedented)
    container_mutations = _find_container_mutations(body_dedented)
    for_loops = _find_for_loops(body_dedented)

    # Build unified (line, kind, payload) list sorted by line.
    _ops: list[tuple[int, str, tuple]] = []
    for line_rel, target, rhs in body_assignments:
        _ops.append((line_rel, "assign", (target, rhs)))
    for line_rel, container, rhs in container_mutations:
        _ops.append((line_rel, "container", (container, rhs)))
    for line_rel, loop_var, iterable_expr in for_loops:
        _ops.append((line_rel, "for", (loop_var, iterable_expr)))
    _ops.sort(key=lambda t: t[0])

    for op_line_rel, op_kind, op_payload in _ops:
        op_line_abs = op_line_rel + body_start - 1

        if op_kind == "assign":
            target, rhs = op_payload
            rhs_idents = _extract_qualified_identifiers(rhs)

            propagated_labels: dict[str, TraceStep] = {}
            for ident in rhs_idents:
                if ident in taint_state:
                    for label, origin_step in taint_state[ident].items():
                        if label_filter and label != label_filter:
                            continue
                        propagated_labels[label] = TraceStep(
                            file_path=file_path,
                            line=op_line_abs,
                            col=0,
                            description=f"propagation: {target} = ... {ident} ...",
                            variable=target,
                        )
                elif "." in ident:
                    base = ident.split(".")[0]
                    if base in taint_state:
                        for label, origin_step in taint_state[base].items():
                            if label_filter and label != label_filter:
                                continue
                            propagated_labels[label] = TraceStep(
                                file_path=file_path,
                                line=op_line_abs,
                                col=0,
                                description=f"propagation: {target} = ... {ident} ...",
                                variable=target,
                            )

            # Subscript reads: target = container[key]
            rhs_stripped = rhs.strip()
            subscript_m = _SUBSCRIPT_READ_RE.match(rhs_stripped)
            if subscript_m:
                container_name = subscript_m.group(1)
                if container_name in taint_state:
                    for label, origin_step in taint_state[container_name].items():
                        if label_filter and label != label_filter:
                            continue
                        if label not in propagated_labels:
                            propagated_labels[label] = TraceStep(
                                file_path=file_path,
                                line=op_line_abs,
                                col=0,
                                description=f"propagation: {target} = {container_name}[...]",
                                variable=target,
                            )

            if propagated_labels:
                if target not in taint_state:
                    taint_state[target] = {}
                for lbl, step in propagated_labels.items():
                    if lbl not in taint_state[target]:
                        taint_state[target][lbl] = step

        elif op_kind == "container":
            container, rhs = op_payload
            rhs_idents = _extract_identifiers(rhs)
            for ident in rhs_idents:
                if ident in taint_state:
                    for label, origin_step in taint_state[ident].items():
                        if label_filter and label != label_filter:
                            continue
                        if container not in taint_state:
                            taint_state[container] = {}
                        if label not in taint_state[container]:
                            taint_state[container][label] = TraceStep(
                                file_path=file_path,
                                line=op_line_abs,
                                col=0,
                                description=f"propagation: {container} mutated with tainted {ident}",
                                variable=container,
                            )

        elif op_kind == "for":
            loop_var, iterable_expr = op_payload
            iterable_idents = _extract_identifiers(iterable_expr)
            for ident in iterable_idents:
                if ident in taint_state:
                    for label, origin_step in taint_state[ident].items():
                        if label_filter and label != label_filter:
                            continue
                        if loop_var not in taint_state:
                            taint_state[loop_var] = {}
                        if label not in taint_state[loop_var]:
                            taint_state[loop_var][label] = TraceStep(
                                file_path=file_path,
                                line=op_line_abs,
                                col=0,
                                description=f"propagation: for {loop_var} in {ident}",
                                variable=loop_var,
                            )

    # Step 3: Apply sanitizers to remove taint.
    # Path-sensitive: only remove taint if sanitizer(s) cover all paths from
    # the source to the function exit.  When a sanitizer is in a conditional
    # branch but the other branch is uncovered, taint is preserved.

    # Build CFG for path-sensitive sanitizer analysis.
    _cfg_for_func = None
    _cfg_edges: dict[int, list[int]] | None = None
    try:
        from emend.cfg import build_cfgs_for_source
        func_source = "\n".join(lines[func_start - 1 : func_end])
        cfgs = build_cfgs_for_source(func_source, ext="py")
        if cfgs:
            _cfg_for_func = cfgs[0]
            # Build adjacency list for BFS
            _cfg_edges = {}
            for edge in _cfg_for_func.get_edges():
                src_b = edge["from"]
                _cfg_edges.setdefault(src_b, []).append(edge["to"])
    except Exception:
        logger.debug("CFG construction failed for sanitizer path analysis", exc_info=True)

    def _find_block_for_line(match_line: int) -> int | None:
        """Find the most specific CFG block containing match_line."""
        if _cfg_for_func is None:
            return None
        rel_line = match_line - func_start
        best_block_id = None
        best_size = float("inf")
        for block in _cfg_for_func.get_blocks():
            if block["start_line"] <= rel_line <= block["end_line"]:
                size = block["end_line"] - block["start_line"]
                if size < best_size:
                    best_size = size
                    best_block_id = block["id"]
        return best_block_id

    def _source_to_sink_sanitized(
        source_block: int | None,
        sink_block: int | None,
        san_blocks: set[int],
        source_line: int = 0,
        sink_line: int = 0,
        san_lines_by_block: dict[int, int] | None = None,
    ) -> bool:
        """Check if all CFG paths from source_block to sink_block pass through
        a sanitizer block.

        Uses BFS from source_block avoiding sanitizer blocks. If the sink_block
        is unreachable from source_block without passing through a sanitizer,
        all paths are sanitized.

        For same-block cases (source_block == sink_block), uses line-number
        ordering as a tiebreaker: the sanitizer must appear between source_line
        and sink_line.

        Returns False (not sanitized = violation) when CFG is unavailable, so
        that missing CFG info never silently suppresses violations (fail-closed).
        """
        if _cfg_for_func is None or _cfg_edges is None:
            # Bug 2 fix: fail closed — report violation when CFG unavailable
            logger.debug(
                "CFG unavailable for source-to-sink sanitizer check; "
                "assuming NOT sanitized (fail-closed)"
            )
            return False
        if source_block is None or sink_block is None:
            # Can't determine block positions — fail closed
            return False

        if source_block == sink_block:
            # Intra-block: source and sink in same block.
            # Sanitized only if a sanitizer is in the same block AND
            # its line is between source_line and sink_line.
            if source_block in san_blocks:
                san_line = (san_lines_by_block or {}).get(source_block, 0)
                if san_line == 0:
                    # No line info for the sanitizer — conservatively sanitized
                    return True
                if source_line <= san_line <= sink_line:
                    return True
            return False

        # Different blocks: BFS from source_block, avoiding sanitizer blocks,
        # checking if sink_block is reachable without hitting a sanitizer.
        visited: set[int] = set()
        queue = [source_block]
        while queue:
            block = queue.pop(0)
            if block in visited:
                continue
            visited.add(block)
            if block == sink_block:
                return False  # sink reachable without going through sanitizer
            if block in san_blocks:
                # If this sanitizer block is the same as the source block,
                # only stop propagation if the sanitizer line is after the
                # source line (otherwise the sanitizer precedes the source).
                if block == source_block and san_lines_by_block:
                    san_line = san_lines_by_block.get(block, source_line + 1)
                    if san_line <= source_line:
                        # Sanitizer before source in same block — doesn't help
                        pass
                    else:
                        continue  # sanitizer after source, stops propagation
                else:
                    continue  # sanitizer in a later block, stops propagation
            for succ in _cfg_edges.get(block, []):
                if succ not in visited:
                    queue.append(succ)
        return True  # sink not reachable without going through sanitizers

    # Collect source blocks: (var, label) -> block_id where taint was introduced.
    # Used for source-to-sink path checking in Step 4.
    _taint_source_blocks: dict[tuple[str, str], int | None] = {}
    for var, labels in taint_state.items():
        for label, step in labels.items():
            src_line = step.line or 1
            _taint_source_blocks[(var, label)] = _find_block_for_line(src_line)

    # Step 3: Collect sanitizer info (lazy — applied during sink check in Step 4).
    # Maps (var, label) -> (set of sanitizer block IDs, any_some_path flag,
    #   block_id->min_line mapping for intra-block line ordering).
    # Path-sensitive: only remove taint if sanitizer(s) cover all paths from
    # the source to the sink.  When a sanitizer is in a conditional branch but
    # the other branch is uncovered, taint is preserved.
    _san_info: dict[tuple[str, str], tuple[set[int], bool, dict[int, int]]] = {}
    # (var, label) -> (san_blocks, any_some_path, block_to_line)

    for san_def in config.sanitizers:
        if label_filter and san_def.label != label_filter:
            continue
        try:
            matches = find_pattern(
                san_def.pattern, file_path,
                source_override=source,
                language=language,
            )
        except Exception:
            logger.debug("find_pattern failed for sanitizer pattern %s", san_def.pattern, exc_info=True)
            continue

        for match in matches:
            match_line = match.line or 1
            if not _in_range(match_line):
                continue

            # Find assignment target on this line
            stmt_line = lines[match_line - 1] if match_line <= len(lines) else ""
            assign_match = re.match(
                r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=\s*",
                stmt_line,
            )
            sanitized_vars: set[str] = set()
            if assign_match:
                sanitized_vars.add(assign_match.group(1))

            for cap_name, cap_val in (match.captures or {}).items():
                if cap_val and re.match(r"^[A-Za-z_][A-Za-z_0-9]*$", cap_val):
                    sanitized_vars.add(cap_val)

            block_id = _find_block_for_line(match_line)
            for var in sanitized_vars:
                key = (var, san_def.label)
                if key not in _san_info:
                    _san_info[key] = (set(), False, {})
                san_blocks_set, any_some_path, block_to_line = _san_info[key]
                if san_def.quantifier == "some_path":
                    _san_info[key] = (san_blocks_set, True, block_to_line)
                elif block_id is not None:
                    san_blocks_set.add(block_id)
                    # Track earliest sanitizer line per block for same-block ordering
                    if block_id not in block_to_line or match_line < block_to_line[block_id]:
                        block_to_line[block_id] = match_line

    # Step 3b: Collect scope sanitizer blocks (lazy — applied during sink check in Step 4).
    # Scope sanitizers (e.g. session.commit()) clear ALL taint for a label.
    # Maps label -> (set of block IDs, block_id->min_line mapping).
    _scope_kill_blocks: dict[str, tuple[set[int], dict[int, int]]] = {}

    for scope_san in config.scope_sanitizers:
        if label_filter and scope_san.label != label_filter:
            continue
        try:
            matches = find_pattern(
                scope_san.pattern, file_path,
                source_override=source,
                language=language,
            )
        except Exception:
            logger.debug("find_pattern failed for scope sanitizer %s", scope_san.pattern, exc_info=True)
            continue

        for match in matches:
            match_line = match.line or 1
            if not _in_range(match_line):
                continue
            block_id = _find_block_for_line(match_line)
            if scope_san.label not in _scope_kill_blocks:
                _scope_kill_blocks[scope_san.label] = (set(), {})
            blocks_set, block_to_line = _scope_kill_blocks[scope_san.label]
            if block_id is not None:
                blocks_set.add(block_id)
                # Track earliest scope kill line per block for same-block ordering
                if block_id not in block_to_line or match_line < block_to_line[block_id]:
                    block_to_line[block_id] = match_line
            else:
                # No CFG block found — use line-number fallback: record the
                # match line so we can compare against sink lines later.
                # Store as negative value as a sentinel for line-number mode.
                blocks_set.add(-(match_line))

    def _is_sanitized(
        var: str, label: str, sink_block: int | None, sink_line: int
    ) -> bool:
        """Check whether the (var, label) taint is sanitized before reaching
        the given sink_block/sink_line.

        Uses the source-to-sink path check to ensure sanitizer blocks actually
        cover all paths between the taint origin and the sink.  For same-block
        cases (source, sanitizer, and sink in the same basic block), uses
        line-number ordering instead of BFS.
        """
        source_block = _taint_source_blocks.get((var, label))
        source_step = taint_state.get(var, {}).get(label)
        source_line = source_step.line if source_step else 1

        # Check regular sanitizers
        if (var, label) in _san_info:
            san_blocks_set, any_some_path, block_to_line = _san_info[(var, label)]
            if any_some_path:
                return True
            # Same-block case: source and sink in same block — use line ordering
            if (source_block is not None and source_block == sink_block
                    and source_block in san_blocks_set):
                san_line = block_to_line.get(source_block, 0)
                if source_line <= san_line <= sink_line:
                    return True
            # Inter-block case: BFS from source to sink avoiding sanitizer blocks
            elif _source_to_sink_sanitized(source_block, sink_block, san_blocks_set):
                return True

        # Check scope sanitizers (kill ALL taint for this label)
        if label in _scope_kill_blocks:
            scope_blocks_set, scope_block_to_line = _scope_kill_blocks[label]
            # Separate real block IDs from line-number sentinels
            real_scope_blocks = {b for b in scope_blocks_set if b >= 0}
            line_sentinels = {-b for b in scope_blocks_set if b < 0}
            if real_scope_blocks:
                # Same-block case: use line ordering
                if (source_block is not None and source_block == sink_block
                        and source_block in real_scope_blocks):
                    scope_line = scope_block_to_line.get(source_block, 0)
                    if source_line <= scope_line <= sink_line:
                        return True
                # Inter-block case: BFS
                elif _source_to_sink_sanitized(source_block, sink_block, real_scope_blocks):
                    return True
            # Line-number fallback: scope sanitizer must appear before sink line
            if line_sentinels:
                if any(source_line <= san_line <= sink_line for san_line in line_sentinels):
                    return True

        return False

    violations: list[TraceViolation] = []

    # Step 4: Check sinks for tainted values
    for sink_def in config.sinks:
        if label_filter and sink_def.label != label_filter:
            continue
        try:
            matches = find_pattern(
                sink_def.pattern, file_path,
                source_override=source,
                language=language,
            )
        except Exception:
            logger.debug("find_pattern failed for sink pattern %s", sink_def.pattern, exc_info=True)
            continue

        for match in matches:
            match_line = match.line or 1
            match_col = match.col or 0
            if not _in_range(match_line):
                continue

            # Check if any variable in the sink match is tainted with the forbidden label.
            # Use qualified identifiers so that obj.dirty in a sink is detected too.
            sink_idents: set[str] = set()
            for cap_name, cap_val in (match.captures or {}).items():
                if cap_val:
                    sink_idents |= _extract_qualified_identifiers(cap_val)
            if match.matched_text:
                sink_idents |= _extract_qualified_identifiers(match.matched_text)

            # Type constraint on sink: filter idents by type
            if sink_def.type_constraint and type_oracle and sink_idents:
                sink_idents = _filter_vars_by_type(
                    sink_idents, sink_def.type_constraint,
                    type_oracle, file_path, match_line,
                )

            sink_block = _find_block_for_line(match_line)
            for ident in sink_idents:
                if ident in taint_state and sink_def.label in taint_state[ident]:
                    # Check if all source-to-sink paths pass through a sanitizer
                    if _is_sanitized(ident, sink_def.label, sink_block, match_line):
                        continue

                    # Build trace
                    trace: list[TraceStep] = []
                    origin = taint_state[ident].get(sink_def.label)
                    if origin:
                        trace.append(origin)

                    # Add propagation steps by walking the chain
                    # (simplified: just show origin and sink)
                    trace.append(TraceStep(
                        file_path=file_path,
                        line=match_line,
                        col=match_col,
                        description=f"sink: {sink_def.label} via {sink_def.pattern}",
                        variable=ident,
                    ))

                    violations.append(TraceViolation(
                        file_path=file_path,
                        line=match_line,
                        col=match_col,
                        label=sink_def.label,
                        sink_pattern=sink_def.pattern,
                        message=sink_def.message,
                        trace=trace,
                    ))
                    # One violation per sink match is enough
                    break

    return violations


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_trace_analysis(
    paths: list[str],
    config: TraceConfig,
    label_filter: str | None = None,
    language: str = "python",
    project_path: str | None = None,
) -> list[TraceViolation]:
    """Run taint analysis on the given files using the Python intraprocedural engine.

    Args:
        paths: List of source file paths to analyze.
        config: Trace configuration (sources, sinks, sanitizers, labels).
        label_filter: If set, only check this specific taint label.
        language: Source language (default: "python").
        project_path: Project root for FactGraph construction (optional).

    Returns:
        List of TraceViolation objects.
    """
    if not config.sources or not config.sinks:
        return []

    # Python intraprocedural trace analysis
    logger.debug("Using Python intraprocedural trace engine for %d files", len(paths))
    from emend.ast_utils import find_nested_definitions

    # Create type oracle if any rule has a type_constraint
    type_oracle = _maybe_create_type_oracle(config)

    violations: list[TraceViolation] = []

    for file_path in paths:
        path_obj = Path(file_path)
        if not path_obj.exists():
            continue

        try:
            source = path_obj.read_text()
        except Exception:
            logger.debug("Could not read %s", file_path, exc_info=True)
            continue

        # Collect all function definitions (including nested)
        try:
            symbols = find_nested_definitions(file_path)
        except Exception:
            logger.debug("Could not parse %s", file_path, exc_info=True)
            continue

        functions = _collect_functions(symbols)
        module_ranges = _collect_module_level_ranges(symbols, len(source.split("\n")))
        functions.extend(
            ("__module__", module_start, module_end)
            for module_start, module_end in module_ranges
        )

        for func_name, func_start, func_end in functions:
            func_violations = _analyze_function(
                file_path=str(path_obj),
                source=source,
                func_start=func_start,
                func_end=func_end,
                config=config,
                label_filter=label_filter,
                language=language,
                type_oracle=type_oracle,
            )
            violations.extend(func_violations)

    for v in violations:
        if not v.engine:
            v.engine = "python"
    return _deduplicate_violations(violations)


def _resolve_match_to_location(
    graph: "FactGraph",  # type: ignore[name-defined]
    file_path: str,
    line: int,
) -> tuple[str, int]:
    """Resolve a pattern match line to (func_qn, block_id).

    Uses the :class:`~emend.location_resolver.LocationResolver` backed by
    FactGraph ``source_loc`` and ``cfg_block`` facts to find the innermost
    enclosing function and most specific CFG block.

    Falls back to ``(MODULE_LEVEL_FUNC, MODULE_LEVEL_BLOCK)`` for module-level
    code or when facts are unavailable.
    """
    from emend.location_resolver import MODULE_LEVEL_BLOCK, MODULE_LEVEL_FUNC

    try:
        return graph.resolve_location(file_path, line)
    except Exception:
        pass
    return MODULE_LEVEL_FUNC, MODULE_LEVEL_BLOCK


def _build_trace_fact_graph(
    paths: list[str],
    language: str,
    project_path: str,
) -> "FactGraph":  # type: ignore[name-defined]
    """Build a FactGraph for trace analysis from the given file list.

    Prefers ``build_from_files`` so small file sets (e.g. single-file test
    fixtures) get fully populated CFG/def-use facts.  Falls back to the
    project-wide ``_get_or_build_fact_graph`` when the direct build fails.
    """
    from emend.fact_graph import FactGraph
    from emend.transform import _get_or_build_fact_graph

    try:
        return FactGraph.build_from_files(paths, language=language)
    except Exception:
        return _get_or_build_fact_graph(project_path)


def _run_trace_datalog(
    paths: list[str],
    config: TraceConfig,
    label_filter: str | None,
    language: str,
    project_path: str,
) -> list[TraceViolation] | None:
    """Datalog-based taint: pattern-match sources/sinks, propagate via FactGraph.

    Returns None if the FactGraph is unavailable or the Datalog query fails,
    signalling the caller to fall back to Python simulation.
    """
    graph = _build_trace_fact_graph(paths, language, project_path)

    # Create type oracle for Python-side type constraint filtering
    type_oracle = _maybe_create_type_oracle(config)

    # Pattern-match sources and sinks across all files
    sources: list[tuple[str, str, str, int, str]] = []
    sinks: list[tuple[str, str, str, int, str]] = []
    sanitizers: list[tuple[str, str, str, int, str]] = []
    scope_kills: list[tuple[str, str, str, int]] = []
    sink_metadata: dict[tuple[str, str, str, str, int], tuple[int, str, str]] = {}

    # For intra-block line ordering
    sanitizer_lines: list[tuple[str, str, str, int, int]] = []
    sink_lines: list[tuple[str, str, str, int, int]] = []

    for file_path in paths:
        path_obj = Path(file_path)
        if not path_obj.exists():
            continue
        try:
            source_text = path_obj.read_text()
        except Exception:
            continue

        for src_def in config.sources:
            if label_filter and src_def.label != label_filter:
                continue
            matched_sources: list[tuple[str, str, str, int, str]] = []
            source_lines = source_text.split("\n")
            matches = find_pattern(src_def.pattern, file_path, source_override=source_text, language=language)
            for m in matches:
                if m.line is not None:
                    # Extract variable names from captures
                    var_names: set[str] = set()
                    for _cn, ct in m.captures.items():
                        var_names |= _extract_identifiers(ct)
                    # Also extract the assignment target on the same line,
                    # mirroring the Python engine's behaviour: for
                    # ``user_input = request.args.get("name")``, the
                    # tainted variable is ``user_input``, not ``name``.
                    if m.line <= len(source_lines):
                        stmt_line = source_lines[m.line - 1]
                        assign_match = re.match(
                            r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=\s*",
                            stmt_line,
                        )
                        if assign_match:
                            var_names.add(assign_match.group(1))
                    # Type constraint filtering on sources
                    if src_def.type_constraint and type_oracle and var_names:
                        var_names = _filter_vars_by_type(
                            var_names, src_def.type_constraint,
                            type_oracle, file_path, m.line or 1,
                        )
                    fq, bid = _resolve_match_to_location(graph, file_path, m.line)
                    for var in var_names:
                        matched_sources.append((file_path, fq, var, bid, src_def.label))
            if src_def.type_constraint and matched_sources:
                matched_sources = _filter_by_receiver_type(
                    matched_sources,
                    src_def.type_constraint,
                    graph,
                )
            sources.extend(matched_sources)

        for sink_def in config.sinks:
            if label_filter and sink_def.label != label_filter:
                continue
            if not sink_def.pattern:
                continue
            matched_sinks: list[tuple[str, str, str, int, str]] = []
            matched_sink_lines: list[tuple[str, str, str, int, int]] = []
            matched_sink_metadata: dict[tuple[str, str, str, str, int], tuple[int, str, str]] = {}
            matches = find_pattern(sink_def.pattern, file_path, source_override=source_text, language=language)
            for m in matches:
                if m.line is not None:
                    var_names = set()
                    for _cn, ct in m.captures.items():
                        var_names |= _extract_identifiers(ct)
                    fq, bid = _resolve_match_to_location(graph, file_path, m.line)
                    for var in var_names:
                        match_tuple = (file_path, fq, var, bid, sink_def.label)
                        matched_sinks.append(match_tuple)
                        matched_sink_lines.append((file_path, fq, sink_def.label, bid, m.line))
                        matched_sink_metadata.setdefault(
                            (file_path, fq, sink_def.label, var, bid),
                            (m.line, sink_def.pattern or sink_def.effect, sink_def.message),
                        )
            if sink_def.type_constraint and matched_sinks:
                matched_sinks = _filter_by_receiver_type(
                    matched_sinks,
                    sink_def.type_constraint,
                    graph,
                )
            allowed_sinks = set(matched_sinks)
            sinks.extend(matched_sinks)
            for fp, fq, lbl, bid, line in matched_sink_lines:
                if any(
                    sink_fp == fp and sink_fq == fq and sink_lbl == lbl and sink_bid == bid
                    for sink_fp, sink_fq, _sink_var, sink_bid, sink_lbl in allowed_sinks
                ):
                    sink_lines.append((fp, fq, lbl, bid, line))
            for sink_key, sink_info in matched_sink_metadata.items():
                fp, fq, lbl, var, bid = sink_key
                if (fp, fq, var, bid, lbl) in allowed_sinks:
                    sink_metadata.setdefault(sink_key, sink_info)

        for san_def in config.sanitizers:
            if label_filter and san_def.label != label_filter:
                continue
            matches = find_pattern(san_def.pattern, file_path, source_override=source_text, language=language)
            for m in matches:
                if m.line is not None:
                    var_names = set()
                    for _cn, ct in m.captures.items():
                        var_names |= _extract_identifiers(ct)
                    fq, bid = _resolve_match_to_location(graph, file_path, m.line)
                    for var in var_names:
                        sanitizers.append((file_path, fq, var, bid, san_def.label))
                        sanitizer_lines.append((file_path, fq, san_def.label, bid, m.line))

        # Scope sanitizers: match patterns and record (fp, fq, lbl, block_id)
        for scope_san in config.scope_sanitizers:
            if label_filter and scope_san.label != label_filter:
                continue
            matches = find_pattern(scope_san.pattern, file_path, source_override=source_text, language=language)
            for m in matches:
                if m.line is not None:
                    fq, bid = _resolve_match_to_location(graph, file_path, m.line)
                    scope_kills.append((file_path, fq, scope_san.label, bid))

    if not sources:
        return []

    # Build effect_sinks from config
    effect_sinks_list: list[tuple[str, str]] = []
    for sink_def in config.sinks:
        if sink_def.effect:
            import re as _re
            effect_m = _re.match(r'(writes|reads)\(\$\w+\)', sink_def.effect)
            if effect_m:
                effect_sinks_list.append((sink_def.label, effect_m.group(1)))

    if not sources or (not sinks and not effect_sinks_list):
        return []

    effect_defs_by_label: dict[str, list[TraceSink]] = {}
    for sink_def in config.sinks:
        if sink_def.effect:
            effect_defs_by_label.setdefault(sink_def.label, []).append(sink_def)

    label_quantifiers: dict[str, str] = {}
    for san_def in config.sanitizers:
        if label_filter and san_def.label != label_filter:
            continue
        if san_def.quantifier == "some_path":
            label_quantifiers[san_def.label] = "some_path"
        else:
            label_quantifiers.setdefault(san_def.label, "all_paths")

    active_labels = (
        {lbl for _fp, _fq, _var, _bid, lbl in sources}
        | {lbl for _fp, _fq, _var, _bid, lbl in sinks}
        | {lbl for lbl, _kind in effect_sinks_list}
    )
    taint_facts: list[TraceFlowFact] = []
    for san_quantifier in ("all_paths", "some_path"):
        group_labels = {
            lbl
            for lbl in active_labels
            if label_quantifiers.get(lbl, "all_paths") == san_quantifier
        }
        if not group_labels:
            continue

        group_sources = [source for source in sources if source[4] in group_labels]
        group_sinks = [sink for sink in sinks if sink[4] in group_labels]
        group_effect_sinks = [
            effect_sink
            for effect_sink in effect_sinks_list
            if effect_sink[0] in group_labels
        ]
        if not group_sources or (not group_sinks and not group_effect_sinks):
            continue

        group_sanitizers = [
            sanitizer for sanitizer in sanitizers if sanitizer[4] in group_labels
        ]
        group_sanitizer_lines = [
            sanitizer_line
            for sanitizer_line in sanitizer_lines
            if sanitizer_line[2] in group_labels
        ]
        group_sink_lines = [
            sink_line for sink_line in sink_lines if sink_line[2] in group_labels
        ]
        group_scope_kills = [
            scope_kill for scope_kill in scope_kills if scope_kill[2] in group_labels
        ]

        taint_facts.extend(graph.trace_propagation_datalog(
            sources=group_sources,
            sinks=group_sinks,
            sanitizers=group_sanitizers if group_sanitizers else None,
            effect_sinks=group_effect_sinks if group_effect_sinks else None,
            sanitizer_quantifier=san_quantifier,
            sanitizer_lines=group_sanitizer_lines if group_sanitizer_lines else None,
            sink_lines=group_sink_lines if group_sink_lines else None,
            scope_kills=group_scope_kills if group_scope_kills else None,
        ))

    def _resolve_effect_sink_line(
        file_path: str,
        func_qn: str,
        sink_var: str,
        sink_block: int,
        effect_kind: str,
    ) -> int:
        try:
            if effect_kind == "writes":
                for du in graph.def_uses(file_path=file_path, func_qn=func_qn):
                    if du.def_block != sink_block:
                        continue
                    if du.kind not in {"write", "aug_write"}:
                        continue
                    if du.var_name == sink_var or du.var_name.startswith(f"{sink_var}."):
                        return du.def_line or du.use_line or 0
                for mc in graph.method_calls(file_path=file_path, func_qn=func_qn):
                    if mc.block_id == sink_block and mc.receiver == sink_var:
                        return mc.line
            elif effect_kind == "reads":
                for du in graph.def_uses(file_path=file_path, func_qn=func_qn):
                    if du.use_block != sink_block or du.kind != "read":
                        continue
                    if du.var_name == sink_var or du.var_name.startswith(f"{sink_var}."):
                        return du.use_line or du.def_line or 0
        except Exception:
            logger.debug(
                "Failed to resolve effect sink line for %s:%s %s in block %s",
                file_path,
                func_qn,
                sink_var,
                sink_block,
                exc_info=True,
            )
        return 0

    # Convert TraceFlowFact -> TraceViolation
    violations: list[TraceViolation] = []
    for tf in taint_facts:
        sink_block = tf.sink_line
        line = 0
        sink_pattern = tf.sink_var
        msg = f"Tainted value reaches sink: {tf.sink_var}"

        metadata = sink_metadata.get(
            (tf.file_path, tf.func_qn, tf.label, tf.sink_var, sink_block)
        )
        if metadata is not None:
            line, sink_pattern, msg = metadata
        else:
            effect_defs = effect_defs_by_label.get(tf.label, [])
            if len(effect_defs) == 1:
                effect_def = effect_defs[0]
                sink_pattern = effect_def.effect or effect_def.pattern or tf.sink_var
                msg = effect_def.message or msg
                effect_kind = effect_def.effect.split("(", 1)[0] if effect_def.effect else ""
                if effect_kind:
                    line = _resolve_effect_sink_line(
                        tf.file_path,
                        tf.func_qn,
                        tf.sink_var,
                        sink_block,
                        effect_kind,
                    )
        violations.append(TraceViolation(
            file_path=tf.file_path,
            line=line,
            col=0,
            label=tf.label,
            sink_pattern=sink_pattern,
            message=msg,
            trace=[],
            engine="datalog",
        ))
    return _deduplicate_violations(violations)


def _deduplicate_violations(violations: list[TraceViolation]) -> list[TraceViolation]:
    """Remove duplicate violations keyed by (file, line, label, sink_pattern)."""
    seen: set[tuple[str, int, str, str]] = set()
    unique: list[TraceViolation] = []
    for v in violations:
        key = (v.file_path, v.line, v.label, v.sink_pattern)
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return unique


def _collect_functions(
    symbols: list,
) -> list[tuple[str, int, int]]:
    """Recursively collect (name, start_line, end_line) for all functions."""
    result: list[tuple[str, int, int]] = []
    for sym in symbols:
        if sym.kind in ("function", "async_function", "method", "async_method"):
            result.append((sym.name, sym.line_start, sym.line_end))
        if hasattr(sym, "children") and sym.children:
            result.extend(_collect_functions(sym.children))
    return result


def _collect_function_descriptors(
    symbols: list,
) -> list[tuple[tuple[str, ...], int, int, str]]:
    """Recursively collect ``(path, start_line, end_line, kind)`` for functions."""
    result: list[tuple[tuple[str, ...], int, int, str]] = []
    for sym in symbols:
        if sym.kind in ("function", "async_function", "method", "async_method"):
            raw_path = tuple(getattr(sym, "path", ()) or ())
            path = raw_path or (sym.name,)
            result.append((path, sym.line_start, sym.line_end, sym.kind))
        if hasattr(sym, "children") and sym.children:
            result.extend(_collect_function_descriptors(sym.children))
    return result


def _collect_module_level_ranges(
    symbols: list,
    total_lines: int,
) -> list[tuple[int, int]]:
    """Return true module-level line ranges outside top-level symbol bodies."""
    if total_lines <= 0:
        return []

    top_level_spans = sorted(
        (sym.line_start, sym.line_end)
        for sym in symbols
        if getattr(sym, "line_start", None) is not None
        and getattr(sym, "line_end", None) is not None
    )
    if not top_level_spans:
        return [(1, total_lines)]

    merged_spans: list[tuple[int, int]] = []
    for start, end in top_level_spans:
        if not merged_spans or start > merged_spans[-1][1] + 1:
            merged_spans.append((start, end))
        else:
            prev_start, prev_end = merged_spans[-1]
            merged_spans[-1] = (prev_start, max(prev_end, end))

    module_ranges: list[tuple[int, int]] = []
    cursor = 1
    for start, end in merged_spans:
        if cursor < start:
            module_ranges.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
    if cursor <= total_lines:
        module_ranges.append((cursor, total_lines))
    return module_ranges


def _format_interprocedural_qn(file_path: str, path: tuple[str, ...]) -> str:
    """Build a stable interprocedural summary key from a symbol path."""
    return f"{file_path}::{'::'.join(path)}"


def _select_interprocedural_callee_qns(
    caller_qn: str,
    callee_name: str,
    *,
    name_to_qn: dict[str, list[str]],
    func_paths: dict[str, tuple[str, ...]],
    func_kinds: dict[str, str],
    include_methods: bool,
) -> list[str]:
    """Resolve bare call names against the nearest lexical scope.

    This prevents sibling nested functions with the same short name from
    sharing summaries purely because they live in the same file.
    """
    candidates = name_to_qn.get(callee_name, [])
    if not candidates:
        return []

    if not include_methods:
        candidates = [
            qn for qn in candidates
            if func_kinds.get(qn) in {"function", "async_function"}
        ]
        if not candidates:
            return []

    caller_path = func_paths.get(caller_qn, ())
    scope_path = caller_path
    while True:
        scoped = [
            qn for qn in candidates
            if func_paths.get(qn, ())[:-1] == scope_path
        ]
        if scoped:
            return scoped
        if not scope_path:
            break
        scope_path = scope_path[:-1]
    return []


def format_violations(
    violations: list[TraceViolation],
    show_trace: bool = False,
    json_output: bool = False,
) -> str:
    """Format taint violations for display.

    Args:
        violations: List of violations to format.
        show_trace: If True, include propagation traces.
        json_output: If True, output as JSON.

    Returns:
        Formatted string.
    """
    if json_output:
        data = []
        for v in violations:
            entry: dict = {
                "file": v.file_path,
                "line": v.line,
                "col": v.col,
                "label": v.label,
                "sink_pattern": v.sink_pattern,
                "message": v.message,
            }
            if v.engine:
                entry["engine"] = v.engine
            if show_trace:
                entry["trace"] = [
                    {
                        "file": s.file_path,
                        "line": s.line,
                        "col": s.col,
                        "description": s.description,
                        "variable": s.variable,
                    }
                    for s in v.trace
                ]
            data.append(entry)
        return json.dumps(data, indent=2)

    lines: list[str] = []
    for v in violations:
        lines.append(f"{v.file_path}:{v.line}:{v.col}: [trace:{v.label}] {v.message}")
        if show_trace and v.trace:
            for step in v.trace:
                lines.append(
                    f"  {step.file_path}:{step.line}:{step.col}: "
                    f"{step.description} (variable: {step.variable})"
                )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Interprocedural taint analysis (Phase 5)
# ---------------------------------------------------------------------------

@dataclass
class FunctionSummary:
    """Summary of a function's taint behavior for interprocedural analysis.

    Maps parameter positions/names to return value taint and sink violations.
    """
    qualified_name: str
    file_path: str
    # Which parameters propagate taint to the return value
    # param_name -> set of labels that flow through
    param_to_return: dict[str, set[str]] = field(default_factory=dict)
    # Which parameters flow to sinks (violations if tainted)
    # param_name -> list of (label, sink_pattern, line)
    param_to_sink: dict[str, list[tuple[str, str, int]]] = field(default_factory=dict)
    # Which parameters propagate to other parameters (via mutation)
    param_to_param: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class InterproceduralResult:
    violations: list[TraceViolation]
    summaries: dict[str, FunctionSummary]  # qn -> summary
    iterations: int  # how many fixed-point iterations


def _collect_function_params(
    source: str,
    func_start: int,
    func_end: int,
) -> list[str]:
    """Extract parameter names from a function definition using tree-sitter.

    Parses the def line at *func_start* and returns the list of parameter
    names (excluding ``self`` and ``cls``).

    Args:
        source: Full file source text.
        func_start: 1-based start line of the function.
        func_end: 1-based end line of the function.

    Returns:
        List of parameter name strings.
    """
    from emend import emend_core

    lines = source.split("\n")
    if func_start < 1 or func_start > len(lines):
        return []

    # Use statement ranges on the def line to find parameters
    # The def line is at func_start (1-based)
    def_line = lines[func_start - 1].strip()

    # Quick regex parse of the def line for parameter names
    # Handles: def foo(a, b, c=1, *args, **kwargs, d: int = 5)
    m = re.match(r"(?:async\s+)?def\s+\w+\s*\(([^)]*)\)", def_line)
    if not m:
        # Multi-line signature: gather lines until we find the closing paren
        sig_lines = [lines[func_start - 1]]
        for i in range(func_start, min(func_end, len(lines))):
            sig_lines.append(lines[i])
            if ")" in lines[i]:
                break
        combined = " ".join(l.strip() for l in sig_lines)
        m = re.match(r"(?:async\s+)?def\s+\w+\s*\(([^)]*)\)", combined)
        if not m:
            return []

    params_str = m.group(1)
    if not params_str.strip():
        return []

    params: list[str] = []
    for part in params_str.split(","):
        part = part.strip()
        if not part:
            continue
        # Strip leading * or **
        name = part.lstrip("*")
        # Strip type annotation and default
        name = name.split(":")[0].split("=")[0].strip()
        if name and name not in ("self", "cls") and re.match(r"^[A-Za-z_]\w*$", name):
            params.append(name)

    return params


def _compute_function_summary(
    file_path: str,
    source: str,
    func_start: int,
    func_end: int,
    config: TraceConfig,
    func_qn: str,
    param_names: list[str],
    language: str = "python",
) -> FunctionSummary:
    """Compute a taint summary for a single function.

    For each parameter, simulates that parameter being tainted with each
    configured label, then runs the intraprocedural analysis to see which
    labels reach the return value or a sink.

    Args:
        file_path: Path to the source file.
        source: Full file source text.
        func_start: 1-based start line of the function.
        func_end: 1-based end line of the function.
        config: Trace configuration.
        func_qn: Qualified name of the function.
        param_names: Names of the function's parameters.
        language: Source language.

    Returns:
        A FunctionSummary describing the function's taint behavior.
    """
    import textwrap

    summary = FunctionSummary(qualified_name=func_qn, file_path=file_path)

    if not param_names or not config.labels:
        return summary

    lines = source.split("\n")
    body_start = func_start + 1
    if body_start > func_end:
        return summary

    body_text_lines = lines[body_start - 1 : func_end]
    body_text = "\n".join(body_text_lines) + "\n"
    body_dedented = textwrap.dedent(body_text)

    body_assignments = _find_assignments_in_source(body_dedented)
    assignments_by_line: dict[int, list[tuple[str, str]]] = {}
    for stmt_line_rel, target, rhs in body_assignments:
        stmt_line_abs = stmt_line_rel + body_start - 1
        assignments_by_line.setdefault(stmt_line_abs, []).append((target, rhs))

    returns_by_line: dict[int, set[str]] = {}
    for line_idx in range(func_start, func_end + 1):
        if line_idx - 1 >= len(lines):
            break
        stripped = lines[line_idx - 1].strip()
        ret_m = re.match(r"return\s+(.+)", stripped)
        if ret_m:
            returns_by_line.setdefault(line_idx, set()).update(
                _extract_identifiers(ret_m.group(1))
            )

    sinks_by_line: dict[int, list[tuple[TraceSink, set[str]]]] = {}
    for sink_def in config.sinks:
        try:
            matches = find_pattern(
                sink_def.pattern, file_path,
                source_override=source,
                language=language,
            )
        except Exception:
            continue

        for match in matches:
            match_line = match.line or 1
            if not (func_start <= match_line <= func_end):
                continue
            sink_idents: set[str] = set()
            for _cap_name, cap_val in (match.captures or {}).items():
                if cap_val:
                    sink_idents |= _extract_identifiers(cap_val)
            if match.matched_text:
                sink_idents |= _extract_identifiers(match.matched_text)
            if sink_idents:
                sinks_by_line.setdefault(match_line, []).append((sink_def, sink_idents))

    for param_name in param_names:
        for label in config.labels:
            # Simulate this param being tainted with this label
            taint_state: dict[str, dict[str, bool]] = {
                param_name: {label: True},
            }

            for line_idx in range(func_start, func_end + 1):
                for target, rhs in assignments_by_line.get(line_idx, []):
                    rhs_idents = _extract_identifiers(rhs)
                    for ident in rhs_idents:
                        if ident in taint_state and label in taint_state[ident]:
                            if target not in taint_state:
                                taint_state[target] = {}
                            taint_state[target][label] = True

                for ret_id in returns_by_line.get(line_idx, set()):
                    if ret_id in taint_state and label in taint_state[ret_id]:
                        if param_name not in summary.param_to_return:
                            summary.param_to_return[param_name] = set()
                        summary.param_to_return[param_name].add(label)
                        break

                for sink_def, sink_idents in sinks_by_line.get(line_idx, []):
                    if sink_def.label != label:
                        continue
                    for ident in sink_idents:
                        if ident in taint_state and label in taint_state[ident]:
                            if param_name not in summary.param_to_sink:
                                summary.param_to_sink[param_name] = []
                            entry = (label, sink_def.pattern, line_idx)
                            if entry not in summary.param_to_sink[param_name]:
                                summary.param_to_sink[param_name].append(entry)
                            break

            # Check param-to-param propagation (via assignments)
            for other_param in param_names:
                if other_param == param_name:
                    continue
                if other_param in taint_state and label in taint_state[other_param]:
                    if param_name not in summary.param_to_param:
                        summary.param_to_param[param_name] = set()
                    summary.param_to_param[param_name].add(other_param)

    return summary


def _compute_return_reachable_vars(
    source: str,
    func_start: int,
    func_end: int,
) -> set[str]:
    """Return variables whose values can still reach a later return."""
    lines = source.split("\n")
    body_start = func_start + 1
    if body_start > func_end:
        return set()

    import textwrap as _textwrap

    body_text_lines = lines[body_start - 1 : func_end]
    body_dedented = _textwrap.dedent("\n".join(body_text_lines) + "\n")
    body_assignments = _find_assignments_in_source(body_dedented)
    assignments_by_line: dict[int, list[tuple[str, str]]] = {}
    for stmt_line_rel, target, rhs in body_assignments:
        stmt_line_abs = stmt_line_rel + body_start - 1
        assignments_by_line.setdefault(stmt_line_abs, []).append((target, rhs))

    reachable: set[str] = set()
    for line_idx in range(func_end, func_start - 1, -1):
        if line_idx - 1 >= len(lines):
            continue
        stripped = lines[line_idx - 1].strip()
        ret_m = re.match(r"return\s+(.+)", stripped)
        if ret_m:
            reachable |= _extract_identifiers(ret_m.group(1))
        for target, rhs in reversed(assignments_by_line.get(line_idx, [])):
            if target in reachable:
                reachable.discard(target)
                reachable |= _extract_identifiers(rhs)

    return reachable


def _collect_param_to_return_dependencies(
    *,
    source: str,
    func_start: int,
    func_end: int,
    func_qn: str,
    param_names: list[str],
    func_info: dict[str, tuple[str, str, int, int, list[str]]],
    name_to_qn: dict[str, list[str]],
    func_paths: dict[str, tuple[str, ...]],
    func_kinds: dict[str, str],
) -> list[tuple[str, str, str, str]]:
    """Collect ``caller_param -> callee_param`` return-summary edges."""
    if not param_names:
        return []

    lines = source.split("\n")
    body_start = func_start + 1
    if body_start > func_end:
        return []

    import textwrap as _textwrap

    body_text_lines = lines[body_start - 1 : func_end]
    body_dedented = _textwrap.dedent("\n".join(body_text_lines) + "\n")
    body_assignments = _find_assignments_in_source(body_dedented)
    assignments_by_line: dict[int, list[tuple[str, str]]] = {}
    for stmt_line_rel, target, rhs in body_assignments:
        stmt_line_abs = stmt_line_rel + body_start - 1
        assignments_by_line.setdefault(stmt_line_abs, []).append((target, rhs))

    return_reachable = _compute_return_reachable_vars(source, func_start, func_end)
    deps: set[tuple[str, str, str, str]] = set()

    for param_name in param_names:
        tainted_vars: set[str] = {param_name}
        for line_idx in range(func_start, func_end + 1):
            for target, rhs in assignments_by_line.get(line_idx, []):
                rhs_idents = _extract_identifiers(rhs)
                if any(ident in tainted_vars for ident in rhs_idents):
                    tainted_vars.add(target)

                if target not in return_reachable:
                    continue

                call_m = re.match(r"([A-Za-z_]\w*)\s*\(", rhs)
                if not call_m:
                    continue
                callee_name = call_m.group(1)
                args_m = re.match(r"[A-Za-z_]\w*\s*\(([^)]*)\)", rhs)
                if not args_m:
                    continue
                arg_strs = [a.strip() for a in args_m.group(1).split(",") if a.strip()]
                callee_qns = _select_interprocedural_callee_qns(
                    func_qn,
                    callee_name,
                    name_to_qn=name_to_qn,
                    func_paths=func_paths,
                    func_kinds=func_kinds,
                    include_methods=False,
                )
                for callee_qn in callee_qns:
                    callee_params = func_info.get(callee_qn, ("", "", 0, 0, []))[4]
                    for arg_idx, arg_str in enumerate(arg_strs):
                        if arg_idx >= len(callee_params):
                            break
                        if any(ident in tainted_vars for ident in _extract_identifiers(arg_str)):
                            deps.add((func_qn, param_name, callee_qn, callee_params[arg_idx]))

    return sorted(deps)


def _run_interprocedural_return_datalog(
    summaries: dict[str, FunctionSummary],
    return_dependencies: list[tuple[str, str, str, str]],
    *,
    label_filter: str | None,
) -> dict[str, FunctionSummary]:
    """Use Datalog to compute the transitive ``param_to_return`` closure."""
    if not return_dependencies:
        return summaries

    from emend.fact_graph import FactGraph

    direct_returns: list[tuple[str, str, str]] = []
    for summary in summaries.values():
        for param_name, labels in summary.param_to_return.items():
            for label in labels:
                if label_filter and label != label_filter:
                    continue
                direct_returns.append((summary.qualified_name, param_name, label))

    graph = FactGraph()
    ir = graph._inline_relation
    query = (
        ir("direct_return", ["fq", "param", "lbl"], direct_returns)
        + ir(
            "return_dep",
            ["caller_fq", "caller_param", "callee_fq", "callee_param"],
            return_dependencies,
        )
        + "param_to_return[fq, param, lbl] := direct_return[fq, param, lbl]\n"
        + "param_to_return[caller_fq, caller_param, lbl] := "
        + "return_dep[caller_fq, caller_param, callee_fq, callee_param], "
        + "param_to_return[callee_fq, callee_param, lbl]\n"
        + "?[fq, param, lbl] := param_to_return[fq, param, lbl]"
    )
    result = graph._client.run(query)

    updated: dict[str, FunctionSummary] = {
        qn: FunctionSummary(
            qualified_name=summary.qualified_name,
            file_path=summary.file_path,
            param_to_return={k: set(v) for k, v in summary.param_to_return.items()},
            param_to_sink={k: list(v) for k, v in summary.param_to_sink.items()},
            param_to_param={k: set(v) for k, v in summary.param_to_param.items()},
        )
        for qn, summary in summaries.items()
    }
    for fq, param, lbl in result["rows"]:
        updated.setdefault(
            fq,
            FunctionSummary(qualified_name=fq, file_path=summaries.get(fq, FunctionSummary(fq, "")).file_path),
        ).param_to_return.setdefault(param, set()).add(lbl)
    return updated


def _run_interprocedural_trace_datalog(
    paths: list[str],
    config: TraceConfig,
    label_filter: str | None = None,
    language: str = "python",
    max_iterations: int = 10,
    project_path: str | None = None,
) -> InterproceduralResult:
    """Datalog interprocedural trace engine with public-style witnesses."""
    if not config.sources or not config.sinks:
        return InterproceduralResult(violations=[], summaries={}, iterations=0)

    from emend.ast_utils import find_nested_definitions

    func_info: dict[str, tuple[str, str, int, int, list[str]]] = {}
    func_paths: dict[str, tuple[str, ...]] = {}
    func_kinds: dict[str, str] = {}
    file_sources: dict[str, str] = {}

    for file_path in paths:
        path_obj = Path(file_path)
        if not path_obj.exists():
            continue
        try:
            source = path_obj.read_text()
        except Exception:
            logger.debug("Could not read %s", file_path, exc_info=True)
            continue

        file_sources[file_path] = source

        try:
            symbols = find_nested_definitions(file_path)
        except Exception:
            logger.debug("Could not parse %s", file_path, exc_info=True)
            continue

        functions = _collect_function_descriptors(symbols)
        for func_path, func_start, func_end, func_kind in functions:
            params = _collect_function_params(source, func_start, func_end)
            qn = _format_interprocedural_qn(file_path, func_path)
            func_info[qn] = (file_path, source, func_start, func_end, params)
            func_paths[qn] = func_path
            func_kinds[qn] = func_kind

    direct_summaries: dict[str, FunctionSummary] = {}
    for qn, (fp, src, fs, fe, params) in func_info.items():
        direct_summaries[qn] = _compute_function_summary(
            file_path=fp,
            source=src,
            func_start=fs,
            func_end=fe,
            config=config,
            func_qn=qn,
            param_names=params,
            language=language,
        )

    name_to_qn: dict[str, list[str]] = {}
    for qn in func_info:
        short_name = func_paths.get(qn, ())[-1]
        if short_name not in name_to_qn:
            name_to_qn[short_name] = []
        name_to_qn[short_name].append(qn)

    return_dependencies: list[tuple[str, str, str, str]] = []
    for qn, (_fp, src, fs, fe, params) in func_info.items():
        return_dependencies.extend(
            _collect_param_to_return_dependencies(
                source=src,
                func_start=fs,
                func_end=fe,
                func_qn=qn,
                param_names=params,
                func_info=func_info,
                name_to_qn=name_to_qn,
                func_paths=func_paths,
                func_kinds=func_kinds,
            )
        )

    summaries = _run_interprocedural_return_datalog(
        direct_summaries,
        return_dependencies,
        label_filter=label_filter,
    )
    violations: list[TraceViolation] = []

    for file_path in paths:
        if file_path not in file_sources:
            continue
        source = file_sources[file_path]
        try:
            symbols = find_nested_definitions(file_path)
        except Exception:
            continue

        functions = _collect_functions(symbols)
        module_ranges = _collect_module_level_ranges(symbols, len(source.split("\n")))
        functions.extend(
            ("__module__", module_start, module_end)
            for module_start, module_end in module_ranges
        )

        for _func_name, func_start, func_end in functions:
            func_violations = _analyze_function(
                file_path=file_path,
                source=source,
                func_start=func_start,
                func_end=func_end,
                config=config,
                label_filter=label_filter,
                language=language,
            )
            violations.extend(func_violations)

    import textwrap as _textwrap

    for caller_qn, (fp, src, fs, fe, _caller_params) in func_info.items():
        lines = src.split("\n")
        body_start = fs + 1
        if body_start > fe:
            continue

        taint_state: dict[str, dict[str, TraceStep]] = {}
        sources_by_line: dict[int, list[tuple[TraceSource, int, set[str]]]] = {}
        sanitizers_by_line: dict[int, list[tuple[TraceSanitizer, set[str]]]] = {}
        sinks_by_line: dict[int, list[tuple[TraceSink, int, set[str]]]] = {}

        for src_def in config.sources:
            if label_filter and src_def.label != label_filter:
                continue
            try:
                matches = find_pattern(
                    src_def.pattern, fp,
                    source_override=src,
                    language=language,
                )
            except Exception:
                continue
            for match in matches:
                match_line = match.line or 1
                match_col = match.col or 0
                if not (fs <= match_line <= fe):
                    continue
                tainted_vars: set[str] = set()
                stmt_line = lines[match_line - 1] if match_line <= len(lines) else ""
                assign_m = re.match(r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=\s*", stmt_line)
                if assign_m:
                    tainted_vars.add(assign_m.group(1))
                for _cn, cv in (match.captures or {}).items():
                    if cv and re.match(r"^[A-Za-z_][A-Za-z_0-9]*$", cv):
                        tainted_vars.add(cv)
                if not tainted_vars and match.matched_text:
                    tainted_vars |= _extract_identifiers(match.matched_text)
                if tainted_vars:
                    sources_by_line.setdefault(match_line, []).append(
                        (src_def, match_col, tainted_vars)
                    )

        for san_def in config.sanitizers:
            if label_filter and san_def.label != label_filter:
                continue
            try:
                matches = find_pattern(
                    san_def.pattern, fp,
                    source_override=src,
                    language=language,
                )
            except Exception:
                continue
            for match in matches:
                match_line = match.line or 1
                if not (fs <= match_line <= fe):
                    continue
                stmt_line = lines[match_line - 1] if match_line <= len(lines) else ""
                assign_m = re.match(r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=\s*", stmt_line)
                sanitized_vars: set[str] = set()
                if assign_m:
                    sanitized_vars.add(assign_m.group(1))
                for _cn, cv in (match.captures or {}).items():
                    if cv and re.match(r"^[A-Za-z_][A-Za-z_0-9]*$", cv):
                        sanitized_vars.add(cv)
                if sanitized_vars:
                    sanitizers_by_line.setdefault(match_line, []).append(
                        (san_def, sanitized_vars)
                    )

        for sink_def in config.sinks:
            if label_filter and sink_def.label != label_filter:
                continue
            try:
                matches = find_pattern(
                    sink_def.pattern, fp,
                    source_override=src,
                    language=language,
                )
            except Exception:
                continue
            for match in matches:
                match_line = match.line or 1
                match_col = match.col or 0
                if not (fs <= match_line <= fe):
                    continue
                sink_idents: set[str] = set()
                for _cn, cv in (match.captures or {}).items():
                    if cv:
                        sink_idents |= _extract_identifiers(cv)
                if match.matched_text:
                    sink_idents |= _extract_identifiers(match.matched_text)
                if sink_idents:
                    sinks_by_line.setdefault(match_line, []).append(
                        (sink_def, match_col, sink_idents)
                    )

        body_text_lines = lines[body_start - 1 : fe]
        body_text = "\n".join(body_text_lines) + "\n"
        body_dedented = _textwrap.dedent(body_text)
        body_assignments = _find_assignments_in_source(body_dedented)
        assignments_by_line: dict[int, list[tuple[str, str]]] = {}
        for stmt_line_rel, target, rhs in body_assignments:
            stmt_line_abs = stmt_line_rel + body_start - 1
            assignments_by_line.setdefault(stmt_line_abs, []).append((target, rhs))

        for line_idx in range(fs, fe + 1):
            if line_idx - 1 >= len(lines):
                break

            for src_def, match_col, tainted_vars in sources_by_line.get(line_idx, []):
                step = TraceStep(
                    file_path=fp,
                    line=line_idx,
                    col=match_col,
                    description=f"source: {src_def.label} via {src_def.pattern}",
                    variable=", ".join(sorted(tainted_vars)) or "?",
                )
                for var in tainted_vars:
                    taint_state.setdefault(var, {})[src_def.label] = step

            for target, rhs in assignments_by_line.get(line_idx, []):
                rhs_idents = _extract_identifiers(rhs)
                propagated: dict[str, TraceStep] = {}
                for ident in rhs_idents:
                    if ident in taint_state:
                        for lbl in taint_state[ident]:
                            if label_filter and lbl != label_filter:
                                continue
                            propagated[lbl] = TraceStep(
                                file_path=fp,
                                line=line_idx,
                                col=0,
                                description=f"propagation: {target} = ... {ident} ...",
                                variable=target,
                            )

                call_m = re.match(r"([A-Za-z_]\w*)\s*\(", rhs)
                if call_m:
                    callee_name = call_m.group(1)
                    args_m = re.match(r"[A-Za-z_]\w*\s*\(([^)]*)\)", rhs)
                    if args_m:
                        arg_strs = [a.strip() for a in args_m.group(1).split(",") if a.strip()]
                        for callee_qn in _select_interprocedural_callee_qns(
                            caller_qn,
                            callee_name,
                            name_to_qn=name_to_qn,
                            func_paths=func_paths,
                            func_kinds=func_kinds,
                            include_methods=False,
                        ):
                            callee_summary = summaries.get(callee_qn)
                            if not callee_summary:
                                continue
                            callee_params = func_info[callee_qn][4]
                            for arg_idx, arg_str in enumerate(arg_strs):
                                if arg_idx >= len(callee_params):
                                    break
                                callee_param = callee_params[arg_idx]
                                if callee_param not in callee_summary.param_to_return:
                                    continue
                                arg_idents = _extract_identifiers(arg_str)
                                for ai in arg_idents:
                                    if ai not in taint_state:
                                        continue
                                    for lbl in taint_state[ai]:
                                        if label_filter and lbl != label_filter:
                                            continue
                                        if lbl not in callee_summary.param_to_return[callee_param]:
                                            continue
                                        propagated[lbl] = TraceStep(
                                            file_path=fp,
                                            line=line_idx,
                                            col=0,
                                            description=(
                                                f"propagation: {target} = {callee_name}(...) "
                                                f"returns tainted '{ai}'"
                                            ),
                                            variable=target,
                                        )
                if propagated:
                    for lbl, step in propagated.items():
                        taint_state.setdefault(target, {})
                        if lbl not in taint_state[target]:
                            taint_state[target][lbl] = step

            for san_def, sanitized_vars in sanitizers_by_line.get(line_idx, []):
                for var in sanitized_vars:
                    if var in taint_state and san_def.label in taint_state[var]:
                        del taint_state[var][san_def.label]
                        if not taint_state[var]:
                            del taint_state[var]

            for sink_def, match_col, sink_idents in sinks_by_line.get(line_idx, []):
                for ident in sink_idents:
                    if ident not in taint_state:
                        continue
                    if sink_def.label not in taint_state[ident]:
                        continue
                    origin_step = taint_state[ident][sink_def.label]
                    violations.append(TraceViolation(
                        file_path=fp,
                        line=line_idx,
                        col=match_col,
                        label=sink_def.label,
                        sink_pattern=sink_def.pattern,
                        message=sink_def.message,
                        trace=[
                            origin_step,
                            TraceStep(
                                file_path=fp,
                                line=line_idx,
                                col=match_col,
                                description=f"sink: {sink_def.label} via {sink_def.pattern}",
                                variable=ident,
                            ),
                        ],
                    ))
                    break

            line_text = lines[line_idx - 1]
            for call_match in re.finditer(r"\b([A-Za-z_]\w*)\s*\(([^)]*)\)", line_text):
                callee_name = call_match.group(1)
                arg_list = [a.strip() for a in call_match.group(2).split(",") if a.strip()]
                callee_qns = _select_interprocedural_callee_qns(
                    caller_qn,
                    callee_name,
                    name_to_qn=name_to_qn,
                    func_paths=func_paths,
                    func_kinds=func_kinds,
                    include_methods=call_match.start() > 0 and line_text[call_match.start() - 1] == ".",
                )
                for callee_qn in callee_qns:
                    callee_summary = summaries.get(callee_qn)
                    if not callee_summary:
                        continue
                    callee_params = func_info[callee_qn][4]
                    for arg_idx, arg_str in enumerate(arg_list):
                        if arg_idx >= len(callee_params):
                            break
                        callee_param = callee_params[arg_idx]
                        arg_idents = _extract_identifiers(arg_str)
                        for ai in arg_idents:
                            if ai not in taint_state:
                                continue
                            for lbl, origin_step in taint_state[ai].items():
                                if label_filter and lbl != label_filter:
                                    continue
                                if callee_param not in callee_summary.param_to_sink:
                                    continue
                                for sink_label, sink_pat, sink_line in callee_summary.param_to_sink[callee_param]:
                                    if sink_label != lbl:
                                        continue
                                    trace = [
                                        origin_step,
                                        TraceStep(
                                            file_path=fp,
                                            line=line_idx,
                                            col=call_match.start(),
                                            description=(
                                                f"call: {callee_name}({arg_str}) passes tainted "
                                                f"'{ai}' as param '{callee_param}'"
                                            ),
                                            variable=ai,
                                        ),
                                        TraceStep(
                                            file_path=callee_summary.file_path,
                                            line=sink_line,
                                            col=0,
                                            description=(
                                                f"sink: {sink_label} via {sink_pat} "
                                                f"(in callee {callee_name})"
                                            ),
                                            variable=callee_param,
                                        ),
                                    ]
                                    sink_msg = "Traced value reaches sink via function call"
                                    for sd in config.sinks:
                                        if sd.pattern == sink_pat and sd.label == sink_label:
                                            sink_msg = sd.message + f" (via {callee_name})"
                                            break
                                    violations.append(TraceViolation(
                                        file_path=fp,
                                        line=line_idx,
                                        col=call_match.start(),
                                        label=lbl,
                                        sink_pattern=sink_pat,
                                        message=sink_msg,
                                        trace=trace,
                                    ))

    for violation in violations:
        violation.engine = "datalog"
    return InterproceduralResult(
        violations=_deduplicate_violations(violations),
        summaries=summaries,
        iterations=1 if summaries else 0,
    )


def run_interprocedural_trace(
    paths: list[str],
    config: TraceConfig,
    label_filter: str | None = None,
    language: str = "python",
    max_iterations: int = 10,
    project_path: str | None = None,
) -> InterproceduralResult:
    """Run interprocedural trace analysis using the Datalog engine."""
    return _run_interprocedural_trace_datalog(
        paths, config,
        label_filter=label_filter,
        language=language,
        max_iterations=max_iterations,
        project_path=project_path,
    )
