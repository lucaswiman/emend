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

        # Also analyze module-level code (treat the whole file as a "function")
        # by using the full file range
        functions.append(("__module__", 1, len(source.split("\n"))))

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
    from emend.transform import _get_or_build_fact_graph

    graph = _get_or_build_fact_graph(project_path)

    # Create type oracle for Python-side type constraint filtering
    type_oracle = _maybe_create_type_oracle(config)

    # Pattern-match sources and sinks across all files
    sources: list[tuple[str, str, str, int, str]] = []
    sinks: list[tuple[str, str, str, int, str]] = []
    sanitizers: list[tuple[str, str, str, int, str]] = []
    scope_kills: list[tuple[str, str, str, int]] = []

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
            matches = find_pattern(src_def.pattern, file_path, source_override=source_text, language=language)
            for m in matches:
                if m.line is not None:
                    # Extract variable names from captures
                    var_names: set[str] = set()
                    for _cn, ct in m.captures.items():
                        var_names |= _extract_identifiers(ct)
                    # Type constraint filtering on sources
                    if src_def.type_constraint and type_oracle and var_names:
                        var_names = _filter_vars_by_type(
                            var_names, src_def.type_constraint,
                            type_oracle, file_path, m.line or 1,
                        )
                    fq, bid = _resolve_match_to_location(graph, file_path, m.line)
                    for var in var_names:
                        sources.append((file_path, fq, var, bid, src_def.label))

        for sink_def in config.sinks:
            if label_filter and sink_def.label != label_filter:
                continue
            matches = find_pattern(sink_def.pattern, file_path, source_override=source_text, language=language)
            for m in matches:
                if m.line is not None:
                    var_names = set()
                    for _cn, ct in m.captures.items():
                        var_names |= _extract_identifiers(ct)
                    fq, bid = _resolve_match_to_location(graph, file_path, m.line)
                    for var in var_names:
                        sinks.append((file_path, fq, var, bid, sink_def.label))
                        sink_lines.append((file_path, fq, sink_def.label, bid, m.line))

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

    if not sources or not sinks:
        return []

    # Object-sensitive dispatch: filter by receiver type when type_constraint is set
    for src_def in config.sources:
        if src_def.type_constraint:
            sources = _filter_by_receiver_type(sources, src_def.type_constraint, graph)
    for sink_def in config.sinks:
        if sink_def.type_constraint:
            sinks = _filter_by_receiver_type(sinks, sink_def.type_constraint, graph)

    if not sources or not sinks:
        return []

    # Build effect_sinks from config
    effect_sinks_list: list[tuple[str, str]] = []
    for sink_def in config.sinks:
        if sink_def.effect:
            import re as _re
            effect_m = _re.match(r'(writes|reads)\(\$\w+\)', sink_def.effect)
            if effect_m:
                effect_sinks_list.append((sink_def.label, effect_m.group(1)))

    # Determine sanitizer quantifier from config
    san_quantifier = "all_paths"
    for san_def in config.sanitizers:
        if san_def.quantifier == "some_path":
            san_quantifier = "some_path"
            break

    # Build sink_messages lookup for per-sink messages
    sink_messages: dict[tuple[str, str], str] = {}
    for sink_def in config.sinks:
        sink_messages[(sink_def.label, sink_def.pattern)] = sink_def.message

    taint_facts = graph.trace_propagation_datalog(
        sources=sources,
        sinks=sinks,
        sanitizers=sanitizers if sanitizers else None,
        effect_sinks=effect_sinks_list if effect_sinks_list else None,
        sanitizer_quantifier=san_quantifier,
        sanitizer_lines=sanitizer_lines if sanitizer_lines else None,
        sink_lines=sink_lines if sink_lines else None,
        scope_kills=scope_kills if scope_kills else None,
    )

    # Convert TraceFlowFact -> TraceViolation
    violations: list[TraceViolation] = []
    for tf in taint_facts:
        msg = sink_messages.get((tf.label, tf.sink_var), f"Tainted value reaches sink: {tf.sink_var}")
        violations.append(TraceViolation(
            file_path=tf.file_path,
            line=tf.sink_line,
            col=0,
            label=tf.label,
            sink_pattern=tf.sink_var,
            message=msg,
            trace=[],
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

    # Collect return statements in the function body to check which variables
    # flow to the return value.
    return_idents: set[str] = set()
    for line_idx in range(func_start, func_end + 1):
        if line_idx - 1 >= len(lines):
            break
        stripped = lines[line_idx - 1].strip()
        ret_m = re.match(r"return\s+(.+)", stripped)
        if ret_m:
            return_idents |= _extract_identifiers(ret_m.group(1))

    body_assignments = _find_assignments_in_source(body_dedented)

    for param_name in param_names:
        for label in config.labels:
            # Simulate this param being tainted with this label
            taint_state: dict[str, dict[str, bool]] = {
                param_name: {label: True},
            }

            # Propagate through assignments
            for _stmt_line_rel, target, rhs in body_assignments:
                rhs_idents = _extract_identifiers(rhs)
                for ident in rhs_idents:
                    if ident in taint_state and label in taint_state[ident]:
                        if target not in taint_state:
                            taint_state[target] = {}
                        taint_state[target][label] = True

            # Check if taint reaches return
            for ret_id in return_idents:
                if ret_id in taint_state and label in taint_state[ret_id]:
                    if param_name not in summary.param_to_return:
                        summary.param_to_return[param_name] = set()
                    summary.param_to_return[param_name].add(label)
                    break

            # Check if taint reaches a sink
            for sink_def in config.sinks:
                if sink_def.label != label:
                    continue
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

                    for ident in sink_idents:
                        if ident in taint_state and label in taint_state[ident]:
                            if param_name not in summary.param_to_sink:
                                summary.param_to_sink[param_name] = []
                            summary.param_to_sink[param_name].append(
                                (label, sink_def.pattern, match_line)
                            )
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


def _snapshot_summary(s: FunctionSummary) -> tuple:
    """Return an immutable snapshot of a summary for convergence comparison."""
    return (
        tuple(sorted((k, frozenset(v)) for k, v in s.param_to_return.items())),
        tuple(sorted((k, tuple(sorted(v))) for k, v in s.param_to_sink.items())),
        tuple(sorted((k, frozenset(v)) for k, v in s.param_to_param.items())),
    )


def run_interprocedural_trace_analysis(
    paths: list[str],
    config: TraceConfig,
    label_filter: str | None = None,
    language: str = "python",
    max_iterations: int = 10,
    project_path: str | None = None,
) -> InterproceduralResult:
    """Run interprocedural taint analysis across the given files.

    Tries Datalog-based recursive propagation over the FactGraph first,
    falling back to the Python fixed-point iteration when the fact graph
    is unavailable.

    Args:
        paths: List of source file paths to analyze.
        config: Trace configuration (sources, sinks, sanitizers, labels).
        label_filter: If set, only check this specific taint label.
        language: Source language (default: "python").
        max_iterations: Maximum number of fixed-point iterations.
        project_path: Project root for FactGraph construction (optional).

    Returns:
        An InterproceduralResult with violations, summaries, and iteration count.
    """
    if not config.sources or not config.sinks:
        return InterproceduralResult(violations=[], summaries={}, iterations=0)

    # NOTE: Datalog interprocedural path is disabled (Phase 2 migration).
    # Python fixed-point iteration is the canonical engine.
    # Retained reference for Phase 6 implementation.

    from emend.ast_utils import find_nested_definitions

    # ------------------------------------------------------------------
    # Phase 1: Collect all function definitions and compute initial summaries
    # ------------------------------------------------------------------
    # func_info: qn -> (file_path, source, func_start, func_end, param_names)
    func_info: dict[str, tuple[str, str, int, int, list[str]]] = {}
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

        functions = _collect_functions(symbols)
        for func_name, func_start, func_end in functions:
            params = _collect_function_params(source, func_start, func_end)
            # Use file_path::func_name as qualified name
            qn = f"{file_path}::{func_name}"
            func_info[qn] = (file_path, source, func_start, func_end, params)

    # Compute initial summaries
    summaries: dict[str, FunctionSummary] = {}
    for qn, (fp, src, fs, fe, params) in func_info.items():
        summaries[qn] = _compute_function_summary(
            file_path=fp,
            source=src,
            func_start=fs,
            func_end=fe,
            config=config,
            func_qn=qn,
            param_names=params,
            language=language,
        )

    # ------------------------------------------------------------------
    # Phase 2: Fixed-point iteration
    # ------------------------------------------------------------------
    # Build a reverse map from short function name -> list of qns
    name_to_qn: dict[str, list[str]] = {}
    for qn in func_info:
        short_name = qn.rsplit("::", 1)[-1]
        if short_name not in name_to_qn:
            name_to_qn[short_name] = []
        name_to_qn[short_name].append(qn)

    # Precompute per-function invariants (unchanged across iterations)
    import textwrap as _textwrap

    _func_precomputed: dict[str, tuple[list[tuple[int, str, str]], set[str]]] = {}
    for qn, (fp, src, fs, fe, params) in func_info.items():
        if not params:
            continue
        lines = src.split("\n")
        body_start = fs + 1
        if body_start > fe:
            continue
        body_text_lines = lines[body_start - 1 : fe]
        body_dedented = _textwrap.dedent("\n".join(body_text_lines) + "\n")
        body_assignments = _find_assignments_in_source(body_dedented)
        return_idents: set[str] = set()
        for line_idx in range(fs, fe + 1):
            if line_idx - 1 >= len(lines):
                break
            stripped = lines[line_idx - 1].strip()
            ret_m = re.match(r"return\s+(.+)", stripped)
            if ret_m:
                return_idents |= _extract_identifiers(ret_m.group(1))
        _func_precomputed[qn] = (body_assignments, return_idents)

    iterations = 0
    for iteration in range(max_iterations):
        iterations = iteration + 1
        prev_snapshots = {qn: _snapshot_summary(s) for qn, s in summaries.items()}

        for qn, (fp, src, fs, fe, params) in func_info.items():
            if qn not in _func_precomputed:
                continue

            body_assignments, return_idents = _func_precomputed[qn]

            # For each param+label, simulate taint with callee summaries
            for param_name in params:
                labels_to_check = [label_filter] if label_filter else config.labels
                for label in labels_to_check:
                    if not label:
                        continue
                    taint_state: dict[str, dict[str, bool]] = {
                        param_name: {label: True},
                    }

                    # Propagate through assignments
                    for _stmt_line_rel, target, rhs in body_assignments:
                        rhs_idents = _extract_identifiers(rhs)

                        # Direct propagation from tainted variables
                        for ident in rhs_idents:
                            if ident in taint_state and label in taint_state[ident]:
                                if target not in taint_state:
                                    taint_state[target] = {}
                                taint_state[target][label] = True

                        # Interprocedural: check if the RHS is a call to a
                        # known function whose summary says param->return
                        call_m = re.match(r"([A-Za-z_]\w*)\s*\(", rhs)
                        if call_m:
                            callee_name = call_m.group(1)
                            # Extract call arguments (simplified)
                            args_m = re.match(r"[A-Za-z_]\w*\s*\(([^)]*)\)", rhs)
                            if args_m:
                                arg_strs = [
                                    a.strip()
                                    for a in args_m.group(1).split(",")
                                    if a.strip()
                                ]
                                # Look up callee summaries
                                callee_qns = name_to_qn.get(callee_name, [])
                                for callee_qn in callee_qns:
                                    callee_summary = summaries.get(callee_qn)
                                    if not callee_summary:
                                        continue
                                    callee_params = func_info[callee_qn][4]
                                    # Map positional args to callee params
                                    for arg_idx, arg_str in enumerate(arg_strs):
                                        if arg_idx >= len(callee_params):
                                            break
                                        callee_param = callee_params[arg_idx]
                                        arg_idents = _extract_identifiers(arg_str)
                                        # Check if any arg ident is tainted
                                        for ai in arg_idents:
                                            if ai in taint_state and label in taint_state[ai]:
                                                # If callee param flows to return, taint target
                                                if callee_param in callee_summary.param_to_return:
                                                    if label in callee_summary.param_to_return[callee_param]:
                                                        if target not in taint_state:
                                                            taint_state[target] = {}
                                                        taint_state[target][label] = True

                    # Update summary: check return
                    for ret_id in return_idents:
                        if ret_id in taint_state and label in taint_state[ret_id]:
                            if param_name not in summaries[qn].param_to_return:
                                summaries[qn].param_to_return[param_name] = set()
                            summaries[qn].param_to_return[param_name].add(label)
                            break

                    # Update summary: check sinks
                    for sink_def in config.sinks:
                        if sink_def.label != label:
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
                            if not (fs <= match_line <= fe):
                                continue
                            sink_idents: set[str] = set()
                            for _cn, cv in (match.captures or {}).items():
                                if cv:
                                    sink_idents |= _extract_identifiers(cv)
                            if match.matched_text:
                                sink_idents |= _extract_identifiers(match.matched_text)
                            for ident in sink_idents:
                                if ident in taint_state and label in taint_state[ident]:
                                    if param_name not in summaries[qn].param_to_sink:
                                        summaries[qn].param_to_sink[param_name] = []
                                    entry = (label, sink_def.pattern, match_line)
                                    if entry not in summaries[qn].param_to_sink[param_name]:
                                        summaries[qn].param_to_sink[param_name].append(entry)
                                    break

        # Check convergence via snapshots (avoids deepcopy)
        new_snapshots = {qn: _snapshot_summary(s) for qn, s in summaries.items()}
        if new_snapshots == prev_snapshots:
            break

    # ------------------------------------------------------------------
    # Phase 3: Use final summaries to find cross-function violations
    # ------------------------------------------------------------------
    violations: list[TraceViolation] = []

    # First, collect intraprocedural violations (existing behavior)
    for file_path in paths:
        if file_path not in file_sources:
            continue
        source = file_sources[file_path]
        try:
            symbols = find_nested_definitions(file_path)
        except Exception:
            continue

        functions = _collect_functions(symbols)
        functions.append(("__module__", 1, len(source.split("\n"))))

        for func_name, func_start, func_end in functions:
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

    # Now find interprocedural violations: at each call site, if a tainted
    # argument is passed to a callee whose summary says that param flows to
    # a sink, report a violation with a cross-function trace.
    for caller_qn, (fp, src, fs, fe, caller_params) in func_info.items():
        lines = src.split("\n")

        body_start = fs + 1
        if body_start > fe:
            continue

        # Run intraprocedural taint to know which variables are tainted
        # at each point (reuse the source-finding logic)
        taint_state: dict[str, dict[str, TraceStep]] = {}

        # Find sources in this function
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
                    for ident in _extract_identifiers(match.matched_text):
                        tainted_vars.add(ident)

                step = TraceStep(
                    file_path=fp,
                    line=match_line,
                    col=match_col,
                    description=f"source: {src_def.label} via {src_def.pattern}",
                    variable=", ".join(sorted(tainted_vars)) or "?",
                )
                for var in tainted_vars:
                    if var not in taint_state:
                        taint_state[var] = {}
                    taint_state[var][src_def.label] = step

        # Propagate through assignments
        body_text_lines = lines[body_start - 1 : fe]
        body_text = "\n".join(body_text_lines) + "\n"
        body_dedented = _textwrap.dedent(body_text)
        body_assignments = _find_assignments_in_source(body_dedented)

        for stmt_line_rel, target, rhs in body_assignments:
            stmt_line_abs = stmt_line_rel + body_start - 1
            rhs_idents = _extract_identifiers(rhs)
            propagated: dict[str, TraceStep] = {}
            for ident in rhs_idents:
                if ident in taint_state:
                    for lbl, origin_step in taint_state[ident].items():
                        if label_filter and lbl != label_filter:
                            continue
                        propagated[lbl] = TraceStep(
                            file_path=fp,
                            line=stmt_line_abs,
                            col=0,
                            description=f"propagation: {target} = ... {ident} ...",
                            variable=target,
                        )
            if propagated:
                if target not in taint_state:
                    taint_state[target] = {}
                for lbl, step in propagated.items():
                    if lbl not in taint_state[target]:
                        taint_state[target][lbl] = step

        # Apply sanitizers
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
                for var in sanitized_vars:
                    if var in taint_state and san_def.label in taint_state[var]:
                        del taint_state[var][san_def.label]
                        if not taint_state[var]:
                            del taint_state[var]

        # Now scan call sites in the function body for interprocedural flow
        for line_idx in range(fs, fe + 1):
            if line_idx - 1 >= len(lines):
                break
            line_text = lines[line_idx - 1]
            # Find function calls: name(args)
            for call_match in re.finditer(
                r"\b([A-Za-z_]\w*)\s*\(([^)]*)\)", line_text
            ):
                callee_name = call_match.group(1)
                args_str = call_match.group(2)
                arg_list = [a.strip() for a in args_str.split(",") if a.strip()]

                callee_qns = name_to_qn.get(callee_name, [])
                for callee_qn in callee_qns:
                    callee_summary = summaries.get(callee_qn)
                    if not callee_summary:
                        continue
                    callee_params = func_info[callee_qn][4]

                    for arg_idx, arg_str in enumerate(arg_list):
                        if arg_idx >= len(callee_params):
                            break
                        callee_param = callee_params[arg_idx]

                        # Check if this arg is tainted
                        arg_idents = _extract_identifiers(arg_str)
                        for ai in arg_idents:
                            if ai not in taint_state:
                                continue
                            for lbl, origin_step in taint_state[ai].items():
                                if label_filter and lbl != label_filter:
                                    continue
                                # Check if callee summary says this param goes to a sink
                                if callee_param in callee_summary.param_to_sink:
                                    for sink_label, sink_pat, sink_line in callee_summary.param_to_sink[callee_param]:
                                        if sink_label != lbl:
                                            continue
                                        # Build interprocedural trace
                                        trace = [
                                            origin_step,
                                            TraceStep(
                                                file_path=fp,
                                                line=line_idx,
                                                col=call_match.start(),
                                                description=f"call: {callee_name}({arg_str}) passes tainted '{ai}' as param '{callee_param}'",
                                                variable=ai,
                                            ),
                                            TraceStep(
                                                file_path=callee_summary.file_path,
                                                line=sink_line,
                                                col=0,
                                                description=f"sink: {sink_label} via {sink_pat} (in callee {callee_name})",
                                                variable=callee_param,
                                            ),
                                        ]

                                        # Find the sink message
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

    return InterproceduralResult(
        violations=_deduplicate_violations(violations),
        summaries=summaries,
        iterations=iterations,
    )
