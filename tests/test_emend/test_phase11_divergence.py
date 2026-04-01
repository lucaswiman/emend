"""Phase 11 divergence enumeration tests.

Systematically compares Python and Datalog interprocedural engines on edge
cases.  Each test either asserts parity (the engines agree) or documents an
accepted divergence with an explicit reason.

This satisfies the Phase 11 requirement:
  "Enumerate every currently accepted divergence and either fix it or mark it
   as intentional in tests/docs."
"""

from __future__ import annotations

import pytest

from emend.trace import (
    TraceConfig,
    TraceSanitizer,
    TraceSink,
    TraceSource,
    TraceViolation,
    _run_interprocedural_trace_datalog,
    run_interprocedural_trace_analysis,
)

from conftest import make_sql_injection_config, run_both_interprocedural_engines


def _violation_locs(violations: list[TraceViolation]) -> set[tuple[int, str, str]]:
    """Extract (line, label, sink_pattern) for comparison."""
    return {(v.line, v.label, v.sink_pattern) for v in violations}


def _run_both(tmp_path, source: str, config: TraceConfig | None = None):
    return run_both_interprocedural_engines(tmp_path, source, config)


# ---------------------------------------------------------------------------
# Parity cases: engines MUST agree
# ---------------------------------------------------------------------------


class TestParity:
    """Cases where both engines must produce identical results."""

    def test_no_sources_no_violations(self, tmp_path):
        """Both engines return empty when config has no sources."""
        source = "def f(x):\n    cursor.execute(x)\n"
        config = TraceConfig(
            labels=["user_input"],
            sources=[],
            sinks=[TraceSink(pattern="cursor.execute($X)", label="user_input", message="sqli")],
            sanitizers=[],
        )
        py, dl = _run_both(tmp_path, source, config)
        assert py.violations == []
        assert dl.violations == []

    def test_no_sinks_no_violations(self, tmp_path):
        """Both engines return empty when config has no sinks."""
        source = "def f(x):\n    return x\n"
        config = TraceConfig(
            labels=["user_input"],
            sources=[TraceSource(pattern="request.args.get($X)", label="user_input")],
            sinks=[],
            sanitizers=[],
        )
        py, dl = _run_both(tmp_path, source, config)
        assert py.violations == []
        assert dl.violations == []

    def test_single_function_intraprocedural(self, tmp_path):
        """Intraprocedural violation within a single function."""
        source = (
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    cursor.execute(name)\n"
        )
        py, dl = _run_both(tmp_path, source)
        assert _violation_locs(py.violations) == _violation_locs(dl.violations)

    def test_sanitizer_before_sink(self, tmp_path):
        """Sanitizer applied before sink — no violation from either engine."""
        source = (
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    name = escape(name)\n"
            "    cursor.execute(name)\n"
        )
        py, dl = _run_both(tmp_path, source)
        assert py.violations == []
        assert dl.violations == []

    def test_two_labels_only_one_tainted(self, tmp_path):
        """Only the configured label triggers violations."""
        source = (
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    cursor.execute(name)\n"
        )
        config = TraceConfig(
            labels=["user_input", "other_label"],
            sources=[TraceSource(pattern="request.args.get($X)", label="user_input")],
            sinks=[
                TraceSink(pattern="cursor.execute($X)", label="user_input", message="sqli"),
                TraceSink(pattern="cursor.execute($X)", label="other_label", message="other"),
            ],
            sanitizers=[],
        )
        py, dl = _run_both(tmp_path, source, config)
        py_labels = {v.label for v in py.violations}
        dl_labels = {v.label for v in dl.violations}
        assert py_labels == dl_labels
        assert "other_label" not in py_labels

    def test_chain_of_two_calls(self, tmp_path):
        """Taint flows through two successive function calls."""
        source = (
            "def step1(value):\n"
            "    return value\n"
            "\n"
            "def step2(value):\n"
            "    return value\n"
            "\n"
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    x = step1(name)\n"
            "    y = step2(x)\n"
            "    cursor.execute(y)\n"
        )
        py, dl = _run_both(tmp_path, source)
        assert _violation_locs(py.violations) == _violation_locs(dl.violations)
        assert len(py.violations) > 0

    def test_multiple_params_only_tainted_one_flows(self, tmp_path):
        """Only the tainted parameter flows to sink, not the clean one."""
        source = (
            "def run_query(cursor, query):\n"
            "    cursor.execute(query)\n"
            "\n"
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    safe = 'SELECT 1'\n"
            "    run_query(cursor, name)\n"
        )
        py, dl = _run_both(tmp_path, source)
        assert _violation_locs(py.violations) == _violation_locs(dl.violations)
        assert len(py.violations) > 0

    def test_label_filter(self, tmp_path):
        """Label filter restricts which violations are reported."""
        source = (
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    cursor.execute(name)\n"
        )
        config = TraceConfig(
            labels=["user_input", "admin_input"],
            sources=[
                TraceSource(pattern="request.args.get($X)", label="user_input"),
                TraceSource(pattern="request.args.get($X)", label="admin_input"),
            ],
            sinks=[
                TraceSink(pattern="cursor.execute($X)", label="user_input", message="sqli-user"),
                TraceSink(pattern="cursor.execute($X)", label="admin_input", message="sqli-admin"),
            ],
            sanitizers=[],
        )
        test_file = tmp_path / "app.py"
        test_file.write_text(source)
        paths = [str(test_file)]
        py = run_interprocedural_trace_analysis(paths, config, label_filter="user_input")
        dl = _run_interprocedural_trace_datalog(paths, config, label_filter="user_input")
        py_labels = {v.label for v in py.violations}
        dl_labels = {v.label for v in dl.violations}
        assert py_labels == {"user_input"}
        assert dl_labels == {"user_input"}

    def test_summary_parity_passthrough(self, tmp_path):
        """Function summaries agree for a simple passthrough function."""
        source = (
            "def passthrough(value):\n"
            "    return value\n"
            "\n"
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    q = passthrough(name)\n"
            "    cursor.execute(q)\n"
        )
        py, dl = _run_both(tmp_path, source)
        # Both should have a passthrough summary with param_to_return
        for qn in py.summaries:
            if "passthrough" in qn:
                py_ptr = py.summaries[qn].param_to_return
                dl_ptr = dl.summaries[qn].param_to_return
                assert py_ptr == dl_ptr, f"param_to_return mismatch: {py_ptr} vs {dl_ptr}"


# ---------------------------------------------------------------------------
# Accepted divergence cases (documented)
# ---------------------------------------------------------------------------


class TestFullParity:
    """Cases previously expected to diverge, now confirmed at parity.

    These were originally documented as accepted divergences but testing
    showed both engines already agree.  They remain as regression guards.
    """

    def test_iteration_count_matches(self, tmp_path):
        """Both engines report the same iteration count on multi-hop chains."""
        source = (
            "def step1(value):\n"
            "    return value\n"
            "\n"
            "def step2(value):\n"
            "    return step1(value)\n"
            "\n"
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    q = step2(name)\n"
            "    cursor.execute(q)\n"
        )
        py, dl = _run_both(tmp_path, source)
        assert _violation_locs(py.violations) == _violation_locs(dl.violations)
        assert py.iterations == dl.iterations

    def test_trace_step_descriptions_match(self, tmp_path):
        """Trace step descriptions are identical across engines."""
        source = (
            "def run_query(cursor, query):\n"
            "    cursor.execute(query)\n"
            "\n"
            "def handle_request(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    run_query(cursor, name)\n"
        )
        py, dl = _run_both(tmp_path, source)
        assert len(py.violations) == len(dl.violations)

        for pv, dv in zip(
            sorted(py.violations, key=lambda v: v.line),
            sorted(dl.violations, key=lambda v: v.line),
        ):
            assert pv.line == dv.line
            assert pv.label == dv.label
            assert pv.sink_pattern == dv.sink_pattern
            assert len(pv.trace) == len(dv.trace)
            for ps, ds in zip(pv.trace, dv.trace):
                assert ps.line == ds.line
                assert ps.variable == ds.variable
                assert ps.description == ds.description
