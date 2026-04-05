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
from emend.fact_graph import DefUseFact, FactGraph, FuncSummaryFact, TraceFlowFact
from emend.transform import PatternMatch


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


def test_run_trace_datalog_uses_exact_sink_metadata(monkeypatch, tmp_path):
    """Datalog trace adapter should preserve sink line, pattern, and message."""

    class _FakeGraph:
        def trace_propagation_datalog(self, **kwargs):
            return [
                TraceFlowFact(
                    source_var="raw",
                    sink_var="raw",
                    label="sqli",
                    file_path=str(src_file),
                    func_qn="app.handle",
                    source_line=1,
                    sink_line=7,  # block id from the Datalog engine, not a source line
                )
            ]

    src_file = tmp_path / "app.py"
    src_file.write_text("def handle():\n    pass\n")

    config = TraceConfig(
        labels=["sqli"],
        sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
        sinks=[TraceSink(
            pattern="cursor.execute($X)",
            label="sqli",
            message="SQL injection",
        )],
    )

    def _fake_find_pattern(pattern, file_path, **kwargs):
        if pattern == "request.args.get($X)":
            return [
                PatternMatch(
                    node_text="request.args.get(raw)",
                    captures={"X": "raw"},
                    line=2,
                    matched_text="request.args.get(raw)",
                    col=5,
                )
            ]
        if pattern == "cursor.execute($X)":
            return [
                PatternMatch(
                    node_text="cursor.execute(raw)",
                    captures={"X": "raw"},
                    line=17,
                    matched_text="cursor.execute(raw)",
                    col=5,
                )
            ]
        return []

    monkeypatch.setattr("emend.trace.find_pattern", _fake_find_pattern)
    monkeypatch.setattr(
        "emend.trace._resolve_match_to_location",
        lambda graph, file_path, line: ("app.handle", 7),
    )
    monkeypatch.setattr(
        "emend.trace._build_trace_fact_graph",
        lambda paths, language, project_path: _FakeGraph(),
    )

    result = _run_trace_datalog(
        paths=[str(src_file)],
        config=config,
        label_filter=None,
        language="python",
        project_path=str(tmp_path),
    )

    assert len(result) == 1
    violation = result[0]
    assert violation.line == 17
    assert violation.sink_pattern == "cursor.execute($X)"
    assert violation.message == "SQL injection"


def test_run_trace_datalog_supports_effect_only_sinks(monkeypatch, tmp_path):
    """Effect-only sinks should not be dropped before the Datalog query runs."""

    class _FakeGraph:
        def trace_propagation_datalog(self, **kwargs):
            assert kwargs["sinks"] == []
            assert kwargs["effect_sinks"] == [("toctou", "writes")]
            return [
                TraceFlowFact(
                    source_var="session",
                    sink_var="session",
                    label="toctou",
                    file_path=str(src_file),
                    func_qn="app.handle",
                    source_line=1,
                    sink_line=4,  # sink block id
                )
            ]

        def def_uses(self, **kwargs):
            return [
                DefUseFact(
                    file_path=str(src_file),
                    func_qn="app.handle",
                    var_name="session.value",
                    kind="write",
                    def_block=4,
                    use_block=4,
                    def_line=21,
                )
            ]

        def method_calls(self, **kwargs):
            return []

    src_file = tmp_path / "app.py"
    src_file.write_text("def handle():\n    pass\n")

    config = TraceConfig(
        labels=["toctou"],
        sources=[TraceSource(pattern="load_session($OBJ)", label="toctou")],
        sinks=[TraceSink(
            pattern="",
            label="toctou",
            message="TOCTOU write",
            effect="writes($OBJ)",
        )],
    )

    def _fake_find_pattern(pattern, file_path, **kwargs):
        if pattern == "load_session($OBJ)":
            return [
                PatternMatch(
                    node_text="load_session(session)",
                    captures={"OBJ": "session"},
                    line=2,
                    matched_text="load_session(session)",
                    col=5,
                )
            ]
        assert pattern == ""
        return []

    monkeypatch.setattr("emend.trace.find_pattern", _fake_find_pattern)
    monkeypatch.setattr(
        "emend.trace._resolve_match_to_location",
        lambda graph, file_path, line: ("app.handle", 4),
    )
    monkeypatch.setattr(
        "emend.trace._build_trace_fact_graph",
        lambda paths, language, project_path: _FakeGraph(),
    )

    result = _run_trace_datalog(
        paths=[str(src_file)],
        config=config,
        label_filter=None,
        language="python",
        project_path=str(tmp_path),
    )

    assert len(result) == 1
    violation = result[0]
    assert violation.line == 21
    assert violation.sink_pattern == "writes($OBJ)"
    assert violation.message == "TOCTOU write"


def test_run_trace_datalog_type_constrained_sink_does_not_filter_other_sinks(
    monkeypatch,
    tmp_path,
):
    """A constrained sink rule must not prune unrelated sink matches."""

    class _FakeGraph:
        def trace_propagation_datalog(self, **kwargs):
            seen_sinks.extend(kwargs["sinks"])
            return [
                TraceFlowFact(
                    source_var=sink_var,
                    sink_var=sink_var,
                    label=label,
                    file_path=file_path,
                    func_qn=func_qn,
                    source_line=1,
                    sink_line=block_id,
                )
                for file_path, func_qn, sink_var, block_id, label in kwargs["sinks"]
            ]

    seen_sinks: list[tuple[str, str, str, int, str]] = []
    src_file = tmp_path / "app.py"
    src_file.write_text(
        "def handle(req):\n"
        "    raw = source(req)\n"
        "    sink_a(raw)\n"
        "    sink_b(raw)\n"
    )

    config = TraceConfig(
        labels=["sqli"],
        sources=[TraceSource(pattern="source($X)", label="sqli")],
        sinks=[
            TraceSink(pattern="sink_a($X)", label="sqli", message="A sink"),
            TraceSink(
                pattern="sink_b($X)",
                label="sqli",
                message="B sink",
                type_constraint="only_b",
            ),
        ],
    )

    def _fake_filter_by_receiver_type(matches, constraint, graph):
        assert constraint == "only_b"
        return [m for m in matches if m[3] == 4]

    monkeypatch.setattr(
        "emend.trace._filter_by_receiver_type",
        _fake_filter_by_receiver_type,
    )
    monkeypatch.setattr(
        "emend.trace._resolve_match_to_location",
        lambda graph, file_path, line: ("app.handle", line),
    )
    monkeypatch.setattr(
        "emend.trace._build_trace_fact_graph",
        lambda paths, language, project_path: _FakeGraph(),
    )

    result = _run_trace_datalog(
        paths=[str(src_file)],
        config=config,
        label_filter=None,
        language="python",
        project_path=str(tmp_path),
    )

    assert seen_sinks == [
        (str(src_file), "app.handle", "raw", 3, "sqli"),
        (str(src_file), "app.handle", "raw", 4, "sqli"),
    ]
    assert {(v.line, v.message) for v in result} == {
        (3, "A sink"),
        (4, "B sink"),
    }


def test_run_trace_datalog_sanitizer_quantifier_is_scoped_per_label(
    monkeypatch,
    tmp_path,
):
    """A some_path sanitizer for one label must not relax another label."""

    class _FakeGraph:
        def __init__(self):
            self.calls = []

        def resolve_location(self, file_path, line):
            return ("app.f", 1)

        def trace_propagation_datalog(self, **kwargs):
            self.calls.append(kwargs)
            labels = {lbl for _fp, _fq, _var, _bid, lbl in kwargs["sources"]}
            if labels == {"b"} and kwargs["sanitizer_quantifier"] == "all_paths":
                return [
                    TraceFlowFact(
                        source_var="y",
                        sink_var="y",
                        label="b",
                        file_path=str(src_file),
                        func_qn="app.f",
                        source_line=2,
                        sink_line=1,
                    )
                ]
            return []

    src_file = tmp_path / "app.py"
    src_file.write_text(
        "def f(flag):\n"
        "    x = source_a()\n"
        "    y = source_b()\n"
        "    if flag:\n"
        "        x = san_a(x)\n"
        "        y = san_b(y)\n"
        "    sink_a(x)\n"
        "    sink_b(y)\n"
    )

    config = TraceConfig(
        labels=["a", "b"],
        sources=[
            TraceSource(pattern="$X = source_a()", label="a"),
            TraceSource(pattern="$X = source_b()", label="b"),
        ],
        sinks=[
            TraceSink(pattern="sink_a($X)", label="a", message="A sink"),
            TraceSink(pattern="sink_b($X)", label="b", message="B sink"),
        ],
        sanitizers=[
            TraceSanitizer(pattern="san_a($X)", label="a", quantifier="some_path"),
            TraceSanitizer(pattern="san_b($X)", label="b", quantifier="all_paths"),
        ],
    )

    fake_graph = _FakeGraph()

    def _fake_find_pattern(pattern, file_path, **kwargs):
        matches_by_pattern = {
            "$X = source_a()": [
                PatternMatch(
                    node_text="x = source_a()",
                    captures={"X": "x"},
                    line=2,
                    matched_text="x = source_a()",
                    col=4,
                )
            ],
            "$X = source_b()": [
                PatternMatch(
                    node_text="y = source_b()",
                    captures={"X": "y"},
                    line=3,
                    matched_text="y = source_b()",
                    col=4,
                )
            ],
            "sink_a($X)": [
                PatternMatch(
                    node_text="sink_a(x)",
                    captures={"X": "x"},
                    line=7,
                    matched_text="sink_a(x)",
                    col=4,
                )
            ],
            "sink_b($X)": [
                PatternMatch(
                    node_text="sink_b(y)",
                    captures={"X": "y"},
                    line=8,
                    matched_text="sink_b(y)",
                    col=4,
                )
            ],
            "san_a($X)": [
                PatternMatch(
                    node_text="san_a(x)",
                    captures={"X": "x"},
                    line=5,
                    matched_text="san_a(x)",
                    col=8,
                )
            ],
            "san_b($X)": [
                PatternMatch(
                    node_text="san_b(y)",
                    captures={"X": "y"},
                    line=6,
                    matched_text="san_b(y)",
                    col=8,
                )
            ],
        }
        return matches_by_pattern.get(pattern, [])

    monkeypatch.setattr(
        "emend.trace._build_trace_fact_graph",
        lambda paths, language, project_path: fake_graph,
    )
    monkeypatch.setattr("emend.trace.find_pattern", _fake_find_pattern)

    result = _run_trace_datalog(
        paths=[str(src_file)],
        config=config,
        label_filter=None,
        language="python",
        project_path=str(tmp_path),
    )

    assert [v.label for v in result] == ["b"]
    assert [call["sanitizer_quantifier"] for call in fake_graph.calls] == [
        "all_paths",
        "some_path",
    ]


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


# ---------------------------------------------------------------------------
# Bug regression: subscript notation in matched patterns (Bug #1)
# ---------------------------------------------------------------------------


def test_run_trace_datalog_subscript_notation_does_not_crash(tmp_path):
    """Subscript notation like request.POST["id"] must not crash CozoDB query.

    When a pattern captures a subscript expression such as ``request.POST["id"]``
    (e.g. sink pattern ``cursor.execute($Q)`` where $Q is the whole subscript),
    the resulting var_name ``request.POST["id"]`` (which contains brackets and
    quotes) gets inserted into a CozoDB inline-relation string.  Without proper
    escaping the generated query string becomes syntactically invalid, producing:

        RuntimeError('CozoDB query error: ... unexpected input at ...')
    """
    source = '''\
def handle(request):
    cursor.execute(request.POST["id"])
'''
    p = tmp_path / "views.py"
    p.write_text(source)

    config = TraceConfig(
        labels=["sqli"],
        sources=[TraceSource(pattern='request.POST[$X]', label="sqli")],
        sinks=[TraceSink(pattern="cursor.execute($Q)", label="sqli", message="SQL injection")],
    )

    # Must not raise a RuntimeError about CozoDB query parse errors
    result = _run_trace_datalog(
        paths=[str(p)],
        config=config,
        label_filter=None,
        language="python",
        project_path=str(tmp_path),
    )
    assert isinstance(result, list)


def test_inline_relation_escapes_quotes_in_strings():
    """_inline_relation must escape double-quotes inside string values.

    A var name like ``data["key"]`` must be serialised correctly so the
    resulting CozoDB query parses without errors.
    """
    from emend.fact_graph import FactGraph

    output = FactGraph._inline_relation(
        "trace_source",
        ["fp", "fq", "var", "bid", "lbl"],
        [("app.py", "handler", 'request.POST["id"]', 0, "sqli")],
    )
    g = FactGraph()
    query = output + "?[fp, fq, var, bid, lbl] := trace_source[fp, fq, var, bid, lbl]"
    result = g._client.run(query)
    rows = result["rows"]
    assert len(rows) == 1
    assert rows[0][2] == 'request.POST["id"]'
