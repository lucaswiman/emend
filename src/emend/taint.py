"""Intraprocedural taint analysis engine for emend.

Tracks value flow from sources to sinks within individual functions,
with sanitizers and path traces.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from emend.transform import find_pattern, PatternMatch

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TaintLabel:
    """A taint label (e.g. 'user_input', 'sensitive_data')."""
    name: str


@dataclass
class TaintSource:
    """A pattern that introduces taint."""
    pattern: str  # emend pattern string
    label: str  # taint label name


@dataclass
class TaintSink:
    """A pattern that should not receive tainted values."""
    pattern: str  # emend pattern string
    label: str  # taint label name (which labels are forbidden)
    message: str  # violation message


@dataclass
class TaintSanitizer:
    """A pattern that removes taint."""
    pattern: str  # emend pattern string
    label: str  # which label is sanitized


@dataclass
class TaintConfig:
    """Configuration for taint analysis from patterns.yaml."""
    labels: list[str] = field(default_factory=list)
    sources: list[TaintSource] = field(default_factory=list)
    sinks: list[TaintSink] = field(default_factory=list)
    sanitizers: list[TaintSanitizer] = field(default_factory=list)


@dataclass
class TaintTraceStep:
    """A step in a taint propagation trace."""
    file_path: str
    line: int
    col: int
    description: str  # e.g. "source: user_input via request.args.get($X)"
    variable: str  # the variable name at this step


@dataclass
class TaintViolation:
    """A taint violation: tainted value reached a sink."""
    file_path: str
    line: int
    col: int
    label: str
    sink_pattern: str
    message: str
    trace: list[TaintTraceStep] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_taint_config(config_path: str) -> TaintConfig:
    """Load taint analysis configuration from a YAML file.

    Reads the ``taint`` section from the given YAML config file.

    Args:
        config_path: Path to the YAML config file (typically .emend/patterns.yaml).

    Returns:
        A TaintConfig with sources, sinks, sanitizers, and labels.

    Raises:
        FileNotFoundError: If the config file does not exist.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        config = yaml.safe_load(f)

    raw = config.get("taint")
    if raw is None:
        return TaintConfig()

    labels = raw.get("labels", []) or []

    sources = []
    for s in raw.get("sources", []) or []:
        sources.append(TaintSource(pattern=s["pattern"], label=s["label"]))

    sinks = []
    for s in raw.get("sinks", []) or []:
        sinks.append(TaintSink(
            pattern=s["pattern"],
            label=s["label"],
            message=s.get("message", "Tainted value reaches sink"),
        ))

    sanitizers = []
    for s in raw.get("sanitizers", []) or []:
        sanitizers.append(TaintSanitizer(pattern=s["pattern"], label=s["label"]))

    return TaintConfig(
        labels=labels,
        sources=sources,
        sinks=sinks,
        sanitizers=sanitizers,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Regex to extract simple identifiers from an expression string.
_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z_0-9]*)\b")


def _extract_identifiers(expr: str) -> set[str]:
    """Return all simple identifiers appearing in *expr*."""
    # Skip common Python keywords that aren't variable names
    _KEYWORDS = frozenset({
        "False", "None", "True", "and", "as", "assert", "async", "await",
        "break", "class", "continue", "def", "del", "elif", "else",
        "except", "finally", "for", "from", "global", "if", "import",
        "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise",
        "return", "try", "while", "with", "yield",
    })
    return {m for m in _IDENT_RE.findall(expr) if m not in _KEYWORDS}


def _find_assignments_in_source(source: str, ext: str = "py") -> list[tuple[int, str, str]]:
    """Find assignments in source using tree-sitter statement ranges.

    Returns a list of (line, target_name, rhs_text) tuples for simple
    assignments like ``x = expr`` or ``x = func(expr)``.
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

    return assignments


# ---------------------------------------------------------------------------
# Core taint analysis
# ---------------------------------------------------------------------------

def _analyze_function(
    file_path: str,
    source: str,
    func_start: int,
    func_end: int,
    config: TaintConfig,
    label_filter: str | None = None,
    language: str = "python",
) -> list[TaintViolation]:
    """Analyze a single function body for taint violations.

    Args:
        file_path: Path to the source file.
        source: Full file source text.
        func_start: 1-based start line of the function.
        func_end: 1-based end line of the function.
        config: Taint configuration with sources, sinks, sanitizers.
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

    # Taint state: variable_name -> {label: TaintTraceStep (where it was tainted)}
    taint_state: dict[str, dict[str, TaintTraceStep]] = {}

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

            step = TaintTraceStep(
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

    # Step 2: Propagate taint through assignments in order
    # We walk the dedented body looking for assignments and propagate taint labels
    body_assignments = _find_assignments_in_source(body_dedented)
    for stmt_line_rel, target, rhs in body_assignments:
        # Map relative line back to absolute file line
        stmt_line_abs = stmt_line_rel + body_start - 1
        rhs_idents = _extract_identifiers(rhs)

        # Collect labels from RHS identifiers
        propagated_labels: dict[str, TaintTraceStep] = {}
        for ident in rhs_idents:
            if ident in taint_state:
                for label, origin_step in taint_state[ident].items():
                    if label_filter and label != label_filter:
                        continue
                    propagated_labels[label] = TaintTraceStep(
                        file_path=file_path,
                        line=stmt_line_abs,
                        col=0,
                        description=f"propagation: {target} = ... {ident} ...",
                        variable=target,
                    )

        if propagated_labels:
            if target not in taint_state:
                taint_state[target] = {}
            # Only add labels that are not already present (don't overwrite
            # a source trace with a propagation trace for the same variable)
            for lbl, step in propagated_labels.items():
                if lbl not in taint_state[target]:
                    taint_state[target][lbl] = step

    # Step 3: Apply sanitizers to remove taint
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

            # Also sanitize any variables appearing in metavar captures
            for cap_name, cap_val in (match.captures or {}).items():
                if cap_val and re.match(r"^[A-Za-z_][A-Za-z_0-9]*$", cap_val):
                    sanitized_vars.add(cap_val)

            for var in sanitized_vars:
                if var in taint_state and san_def.label in taint_state[var]:
                    del taint_state[var][san_def.label]
                    if not taint_state[var]:
                        del taint_state[var]

    # Step 4: Check sinks for tainted values
    violations: list[TaintViolation] = []
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

            # Check if any variable in the sink match is tainted with the forbidden label
            sink_idents: set[str] = set()
            for cap_name, cap_val in (match.captures or {}).items():
                if cap_val:
                    sink_idents |= _extract_identifiers(cap_val)
            if match.matched_text:
                sink_idents |= _extract_identifiers(match.matched_text)

            for ident in sink_idents:
                if ident in taint_state and sink_def.label in taint_state[ident]:
                    # Build trace
                    trace: list[TaintTraceStep] = []
                    origin = taint_state[ident].get(sink_def.label)
                    if origin:
                        trace.append(origin)

                    # Add propagation steps by walking the chain
                    # (simplified: just show origin and sink)
                    trace.append(TaintTraceStep(
                        file_path=file_path,
                        line=match_line,
                        col=match_col,
                        description=f"sink: {sink_def.label} via {sink_def.pattern}",
                        variable=ident,
                    ))

                    violations.append(TaintViolation(
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

def run_taint_analysis(
    paths: list[str],
    config: TaintConfig,
    label_filter: str | None = None,
    language: str = "python",
) -> list[TaintViolation]:
    """Run taint analysis on the given files.

    For each file, iterates over function definitions and performs
    intraprocedural taint tracking (sources -> propagation -> sanitizers -> sinks).

    Args:
        paths: List of source file paths to analyze.
        config: Taint configuration (sources, sinks, sanitizers, labels).
        label_filter: If set, only check this specific taint label.
        language: Source language (default: "python").

    Returns:
        List of TaintViolation objects.
    """
    if not config.sources or not config.sinks:
        return []

    from emend.ast_utils import find_nested_definitions
    from emend.component_selector import NestedSymbol

    violations: list[TaintViolation] = []

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
            )
            violations.extend(func_violations)

    # De-duplicate violations (same file, line, label, sink_pattern)
    seen: set[tuple[str, int, str, str]] = set()
    unique: list[TaintViolation] = []
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
    violations: list[TaintViolation],
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
        lines.append(f"{v.file_path}:{v.line}:{v.col}: [taint:{v.label}] {v.message}")
        if show_trace and v.trace:
            for step in v.trace:
                lines.append(
                    f"  {step.file_path}:{step.line}:{step.col}: "
                    f"{step.description} (variable: {step.variable})"
                )
    return "\n".join(lines)
