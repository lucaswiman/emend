"""Phase 17: Remove Legacy Python Intraprocedural Trace.

Verifies that the legacy Python intraprocedural trace engine has been removed
and that there is one canonical (Datalog) execution path.
"""

from __future__ import annotations

import inspect

import pytest

from emend.trace import (
    TraceConfig,
    TraceSink,
    TraceSource,
    TraceViolation,
    run_trace_analysis,
)


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


class TestLegacyPythonEngineRemoved:
    """Phase 17: legacy Python intraprocedural trace helpers are gone."""

    def test_no_run_trace_python(self):
        """_run_trace_python() should not exist."""
        import emend.trace as mod
        assert not hasattr(mod, "_run_trace_python"), (
            "_run_trace_python should have been removed in Phase 17"
        )

    def test_no_analyze_function(self):
        """_analyze_function() should not exist."""
        import emend.trace as mod
        assert not hasattr(mod, "_analyze_function"), (
            "_analyze_function should have been removed in Phase 17"
        )

    def test_no_find_container_mutations(self):
        """_find_container_mutations() should not exist."""
        import emend.trace as mod
        assert not hasattr(mod, "_find_container_mutations"), (
            "_find_container_mutations should have been removed in Phase 17"
        )

    def test_no_find_for_loops(self):
        """_find_for_loops() should not exist."""
        import emend.trace as mod
        assert not hasattr(mod, "_find_for_loops"), (
            "_find_for_loops should have been removed in Phase 17"
        )

    def test_no_extract_qualified_identifiers(self):
        """_extract_qualified_identifiers() should not exist."""
        import emend.trace as mod
        assert not hasattr(mod, "_extract_qualified_identifiers"), (
            "_extract_qualified_identifiers should have been removed in Phase 17"
        )

    def test_no_engine_parameter(self):
        """run_trace_analysis() should not accept an engine parameter."""
        sig = inspect.signature(run_trace_analysis)
        assert "engine" not in sig.parameters, (
            "run_trace_analysis() should no longer accept an 'engine' parameter"
        )

    def test_run_trace_analysis_uses_datalog(self, tmp_path):
        """run_trace_analysis() always uses Datalog."""
        test_file = tmp_path / "app.py"
        test_file.write_text(_SIMPLE_APP)
        violations = run_trace_analysis([str(test_file)], _SQL_CONFIG)
        assert len(violations) >= 1
        for v in violations:
            assert v.engine == "datalog"

    def test_shared_helpers_preserved(self):
        """Shared helpers used by Datalog engine are still available."""
        from emend.trace import _extract_identifiers
        assert callable(_extract_identifiers)
