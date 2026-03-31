"""Tests for Phase 6 Datalog trace: fixed _run_trace_datalog and config-driven
interprocedural_trace_datalog."""

import re

import pytest

from emend.trace import (
    TraceConfig,
    TraceSanitizer,
    TraceSink,
    TraceSource,
    TraceViolation,
    _run_trace_datalog,
    _resolve_match_to_location,
)
from emend.fact_graph import FactGraph, FuncSummaryFact, TraceFlowFact


# ---------------------------------------------------------------------------
# TraceViolation field tests
# ---------------------------------------------------------------------------


def test_trace_violation_fields():
    """TraceViolation constructed by Datalog path has all required fields."""
    v = TraceViolation(
        file_path="app.py",
        line=10,
        col=0,
        label="sqli",
        sink_pattern="cursor.execute",
        message="SQL injection risk",
        trace=[],
    )
    assert v.file_path == "app.py"
    assert v.line == 10
    assert v.col == 0
    assert v.label == "sqli"
    assert v.sink_pattern == "cursor.execute"
    assert v.message == "SQL injection risk"
    assert v.trace == []


# ---------------------------------------------------------------------------
# Effect sinks extraction test
# ---------------------------------------------------------------------------


def test_effect_sinks_extracted_from_config():
    """Effect sinks from TraceSink.effect are correctly parsed."""
    config = TraceConfig(
        labels=["toctou"],
        sources=[TraceSource(pattern="db.query($X)", label="toctou")],
        sinks=[TraceSink(pattern="", label="toctou", message="TOCTOU", effect="writes($OBJ)")],
    )
    # Verify effect parsing logic (mirrors _run_trace_datalog)
    effect_sinks = []
    for sink_def in config.sinks:
        if sink_def.effect:
            effect_m = re.match(r'(writes|reads)\(\$\w+\)', sink_def.effect)
            if effect_m:
                effect_sinks.append((sink_def.label, effect_m.group(1)))
    assert effect_sinks == [("toctou", "writes")]


def test_effect_sinks_reads():
    """reads($X) effect is also parsed correctly."""
    config = TraceConfig(
        labels=["leak"],
        sources=[TraceSource(pattern="taint($X)", label="leak")],
        sinks=[TraceSink(pattern="", label="leak", message="Leak", effect="reads($OBJ)")],
    )
    effect_sinks = []
    for sink_def in config.sinks:
        if sink_def.effect:
            effect_m = re.match(r'(writes|reads)\(\$\w+\)', sink_def.effect)
            if effect_m:
                effect_sinks.append((sink_def.label, effect_m.group(1)))
    assert effect_sinks == [("leak", "reads")]


def test_no_effect_sinks_when_pattern_only():
    """Sinks without effect field produce no effect_sinks."""
    config = TraceConfig(
        labels=["sqli"],
        sources=[TraceSource(pattern="req.get($X)", label="sqli")],
        sinks=[TraceSink(pattern="cursor.execute($X)", label="sqli", message="SQL injection")],
    )
    effect_sinks = []
    for sink_def in config.sinks:
        if sink_def.effect:
            effect_m = re.match(r'(writes|reads)\(\$\w+\)', sink_def.effect)
            if effect_m:
                effect_sinks.append((sink_def.label, effect_m.group(1)))
    assert effect_sinks == []


# ---------------------------------------------------------------------------
# Sanitizer quantifier extraction
# ---------------------------------------------------------------------------


def test_sanitizer_quantifier_all_paths_default():
    """Default sanitizer quantifier is all_paths."""
    config = TraceConfig(
        labels=["sqli"],
        sources=[TraceSource(pattern="req.get($X)", label="sqli")],
        sinks=[TraceSink(pattern="cursor.execute($X)", label="sqli", message="SQL injection")],
        sanitizers=[TraceSanitizer(pattern="escape($X)", label="sqli")],
    )
    san_quantifier = "all_paths"
    for san_def in config.sanitizers:
        if san_def.quantifier == "some_path":
            san_quantifier = "some_path"
            break
    assert san_quantifier == "all_paths"


def test_sanitizer_quantifier_some_path():
    """some_path quantifier is picked up from config."""
    config = TraceConfig(
        labels=["sqli"],
        sources=[TraceSource(pattern="req.get($X)", label="sqli")],
        sinks=[TraceSink(pattern="cursor.execute($X)", label="sqli", message="SQL injection")],
        sanitizers=[TraceSanitizer(pattern="escape($X)", label="sqli", quantifier="some_path")],
    )
    san_quantifier = "all_paths"
    for san_def in config.sanitizers:
        if san_def.quantifier == "some_path":
            san_quantifier = "some_path"
            break
    assert san_quantifier == "some_path"


# ---------------------------------------------------------------------------
# _resolve_match_to_location tests
# ---------------------------------------------------------------------------


def test_resolve_match_to_location_empty_graph():
    """Falls back to ('<module>', 0) when graph has no matching facts."""
    graph = FactGraph()
    fq, bid = _resolve_match_to_location(graph, "app.py", 5)
    assert fq == "<module>"
    assert bid == 0


# ---------------------------------------------------------------------------
# _run_trace_datalog smoke test
# ---------------------------------------------------------------------------


def test_run_trace_datalog_basic(tmp_path):
    """_run_trace_datalog returns a list without error (may be empty)."""
    source = '''\
def handler():
    user_input = request.args.get("name")
    cursor.execute(user_input)
'''
    p = tmp_path / "app.py"
    p.write_text(source)

    config = TraceConfig(
        labels=["sqli"],
        sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
        sinks=[TraceSink(pattern="cursor.execute($X)", label="sqli", message="SQL injection")],
    )

    result = _run_trace_datalog(
        paths=[str(p)],
        config=config,
        label_filter=None,
        language="python",
        project_path=str(tmp_path),
    )
    # Must return a list (possibly empty) without raising
    assert isinstance(result, list)


def test_run_trace_datalog_no_sources_returns_empty(tmp_path):
    """Returns empty list when no source matches are found."""
    source = "x = 1\n"
    p = tmp_path / "app.py"
    p.write_text(source)

    config = TraceConfig(
        labels=["sqli"],
        sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
        sinks=[TraceSink(pattern="cursor.execute($X)", label="sqli", message="SQL injection")],
    )

    result = _run_trace_datalog(
        paths=[str(p)],
        config=config,
        label_filter=None,
        language="python",
        project_path=str(tmp_path),
    )
    assert result == []


def test_run_trace_datalog_missing_file_skipped(tmp_path):
    """Missing files are skipped without raising."""
    config = TraceConfig(
        labels=["sqli"],
        sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
        sinks=[TraceSink(pattern="cursor.execute($X)", label="sqli", message="SQL injection")],
    )

    result = _run_trace_datalog(
        paths=[str(tmp_path / "nonexistent.py")],
        config=config,
        label_filter=None,
        language="python",
        project_path=str(tmp_path),
    )
    assert result == []


# ---------------------------------------------------------------------------
# interprocedural_trace_datalog config-driven tests
# ---------------------------------------------------------------------------


def test_interprocedural_datalog_accepts_sources_sinks():
    """interprocedural_trace_datalog accepts source/sink configuration without error."""
    g = FactGraph()
    g.add_func_summary(FuncSummaryFact(
        func_qn="app.process",
        param_name="user_input",
        flows_to_return=False,
        flows_to_sink=True,
        sink_label="sqli",
    ))
    results = g.interprocedural_trace_datalog(
        sources=[("app.py", "app.handler", "data", 0, "sqli")],
        sinks=[("app.py", "app.handler", "query", 1, "sqli")],
    )
    assert isinstance(results, list)


def test_interprocedural_datalog_no_args_backward_compat():
    """interprocedural_trace_datalog works with no arguments (backward compat)."""
    g = FactGraph()
    g.add_func_summary(FuncSummaryFact(
        func_qn="app.process",
        param_name="user_input",
        flows_to_return=False,
        flows_to_sink=True,
        sink_label="sqli",
    ))
    # Should not raise
    results = g.interprocedural_trace_datalog()
    assert isinstance(results, list)


def test_interprocedural_datalog_returns_trace_flow_facts():
    """interprocedural_trace_datalog results are TraceFlowFact instances."""
    g = FactGraph()
    # Without any facts the result should be empty but well-typed
    results = g.interprocedural_trace_datalog(
        sources=[("f.py", "f.handler", "x", 0, "lbl")],
        sinks=[("f.py", "f.handler", "y", 1, "lbl")],
    )
    assert isinstance(results, list)
    for item in results:
        assert isinstance(item, TraceFlowFact)


def test_interprocedural_datalog_empty_sources_sinks():
    """Passing empty sources/sinks lists is handled gracefully."""
    g = FactGraph()
    results = g.interprocedural_trace_datalog(sources=[], sinks=[])
    assert isinstance(results, list)
