"""Phase 16: Cut Over Intraprocedural Trace to Datalog.

Verifies that ``run_trace_analysis()`` now routes through the Datalog engine
by default, that the ``--engine python`` escape hatch works, and that
``TraceViolation`` output shape is preserved.
"""

from __future__ import annotations

import json

import pytest

from emend.trace import (
    TraceConfig,
    TraceSanitizer,
    TraceScopeSanitizer,
    TraceSink,
    TraceSource,
    TraceViolation,
    format_violations,
    run_trace_analysis,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SQL_CONFIG = TraceConfig(
    labels=["sqli"],
    sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
    sinks=[TraceSink(
        pattern="cursor.execute($QUERY)",
        label="sqli",
        message="SQL injection",
    )],
)

_SIMPLE_APP = (
    "def handle(request, cursor):\n"
    "    name = request.args.get('name')\n"
    "    cursor.execute(name)\n"
)


# ---------------------------------------------------------------------------
# Tests: default engine is now Datalog
# ---------------------------------------------------------------------------

class TestDefaultEngineIsDatalog:
    """After Phase 16 cutover, run_trace_analysis uses Datalog by default."""

    def test_violations_tagged_datalog(self, tmp_path):
        """All violations from run_trace_analysis have engine='datalog'."""
        test_file = tmp_path / "app.py"
        test_file.write_text(_SIMPLE_APP)

        violations = run_trace_analysis([str(test_file)], _SQL_CONFIG)
        assert len(violations) >= 1
        for v in violations:
            assert v.engine == "datalog", (
                f"Expected engine='datalog', got {v.engine!r}"
            )

    def test_format_violations_json_engine_datalog(self, tmp_path):
        """JSON output includes engine='datalog'."""
        test_file = tmp_path / "app.py"
        test_file.write_text(_SIMPLE_APP)

        violations = run_trace_analysis([str(test_file)], _SQL_CONFIG)
        assert violations

        json_str = format_violations(violations, json_output=True)
        data = json.loads(json_str)
        for entry in data:
            assert entry.get("engine") == "datalog"

    def test_violation_output_shape(self, tmp_path):
        """Violations preserve file_path, line, label, sink_pattern, message."""
        test_file = tmp_path / "app.py"
        test_file.write_text(_SIMPLE_APP)

        violations = run_trace_analysis([str(test_file)], _SQL_CONFIG)
        assert violations
        v = violations[0]
        assert v.file_path == str(test_file)
        assert v.label == "sqli"
        assert v.message == "SQL injection"
        assert v.sink_pattern == "cursor.execute($QUERY)"

    def test_label_filter(self, tmp_path):
        """Label filter still works after cutover."""
        test_file = tmp_path / "app.py"
        test_file.write_text(_SIMPLE_APP)

        violations = run_trace_analysis(
            [str(test_file)], _SQL_CONFIG, label_filter="sqli",
        )
        assert len(violations) >= 1

        violations = run_trace_analysis(
            [str(test_file)], _SQL_CONFIG, label_filter="nonexistent",
        )
        assert len(violations) == 0

    def test_sanitizer_suppresses_violation(self, tmp_path):
        """Sanitizers still suppress violations after cutover."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    name = escape(name)\n"
            "    cursor.execute(name)\n"
        )
        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
            sinks=[TraceSink(
                pattern="cursor.execute($QUERY)",
                label="sqli",
                message="SQL injection",
            )],
            sanitizers=[TraceSanitizer(pattern="escape($X)", label="sqli")],
        )
        violations = run_trace_analysis([str(test_file)], config)
        assert len(violations) == 0

    def test_empty_sources_returns_empty(self, tmp_path):
        test_file = tmp_path / "app.py"
        test_file.write_text(_SIMPLE_APP)
        config = TraceConfig(labels=["sqli"], sources=[], sinks=_SQL_CONFIG.sinks)
        violations = run_trace_analysis([str(test_file)], config)
        assert violations == []

    def test_nonexistent_file(self, tmp_path):
        violations = run_trace_analysis(["/nonexistent/file.py"], _SQL_CONFIG)
        assert violations == []


# ---------------------------------------------------------------------------
# Tests: --engine escape hatch
# ---------------------------------------------------------------------------

class TestEngineEscapeHatch:
    """The engine= parameter allows forcing python or datalog."""

    def test_engine_python_forces_python(self, tmp_path):
        """engine='python' forces the Python engine."""
        test_file = tmp_path / "app.py"
        test_file.write_text(_SIMPLE_APP)

        violations = run_trace_analysis(
            [str(test_file)], _SQL_CONFIG, engine="python",
        )
        assert len(violations) >= 1
        for v in violations:
            assert v.engine == "python", (
                f"Expected engine='python', got {v.engine!r}"
            )

    def test_engine_datalog_forces_datalog(self, tmp_path):
        """engine='datalog' explicitly selects Datalog (same as default)."""
        test_file = tmp_path / "app.py"
        test_file.write_text(_SIMPLE_APP)

        violations = run_trace_analysis(
            [str(test_file)], _SQL_CONFIG, engine="datalog",
        )
        assert len(violations) >= 1
        for v in violations:
            assert v.engine == "datalog"


# ---------------------------------------------------------------------------
# Tests: scope sanitizers still work
# ---------------------------------------------------------------------------

class TestScopeSanitizersAfterCutover:
    def test_scope_sanitizer_kills_taint(self, tmp_path):
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def process(session):\n"
            "    session.user = get_input()\n"
            "    session.commit()\n"
            "    use_data(session.user)\n"
        )
        config = TraceConfig(
            labels=["toctou"],
            sources=[TraceSource(pattern="get_input()", label="toctou")],
            sinks=[TraceSink(
                pattern="use_data($X)",
                label="toctou",
                message="Use after commit",
            )],
            scope_sanitizers=[TraceScopeSanitizer(
                pattern="session.commit()", label="toctou",
            )],
        )
        violations = run_trace_analysis([str(test_file)], config)
        assert len(violations) == 0
