"""Phase 11 differential tests for interprocedural Python vs Datalog parity."""

from __future__ import annotations

from emend.trace import TraceViolation

from conftest import make_sql_injection_config, run_both_interprocedural_engines


def _violation_signature(v: TraceViolation) -> tuple:
    return (
        v.file_path,
        v.line,
        v.col,
        v.label,
        v.sink_pattern,
        v.message,
        tuple(
            (step.file_path, step.line, step.col, step.description, step.variable)
            for step in v.trace
        ),
    )


def _summary_signature(summary) -> tuple:
    return (
        tuple(sorted((k, tuple(sorted(v))) for k, v in summary.param_to_return.items())),
        tuple(sorted((k, tuple(sorted(v))) for k, v in summary.param_to_sink.items())),
    )


def _run_both(tmp_path, source: str, config=None):
    return run_both_interprocedural_engines(tmp_path, source, config or make_sql_injection_config())


def test_interprocedural_datalog_matches_python_for_cross_function_violation(tmp_path):
    source = (
        "def run_query(cursor, query):\n"
        "    cursor.execute(query)\n"
        "\n"
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    run_query(cursor, name)\n"
    )
    python_result, datalog_result = _run_both(tmp_path, source)

    assert { _violation_signature(v) for v in python_result.violations } == {
        _violation_signature(v) for v in datalog_result.violations
    }
    assert all(v.engine == "python" for v in python_result.violations)
    assert all(v.engine == "datalog" for v in datalog_result.violations)


def test_interprocedural_datalog_matches_python_for_returned_taint(tmp_path):
    source = (
        "def passthrough(value):\n"
        "    return value\n"
        "\n"
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    query = passthrough(name)\n"
        "    cursor.execute(query)\n"
    )
    python_result, datalog_result = _run_both(tmp_path, source)

    assert { _violation_signature(v) for v in python_result.violations } == {
        _violation_signature(v) for v in datalog_result.violations
    }
    assert {
        qn: _summary_signature(summary)
        for qn, summary in python_result.summaries.items()
    } == {
        qn: _summary_signature(summary)
        for qn, summary in datalog_result.summaries.items()
    }


def test_interprocedural_datalog_matches_python_for_late_sanitizer(tmp_path):
    source = (
        "def run_query(cursor, query):\n"
        "    cursor.execute(query)\n"
        "\n"
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    run_query(cursor, name)\n"
        "    name = escape(name)\n"
    )
    python_result, datalog_result = _run_both(tmp_path, source)

    assert { _violation_signature(v) for v in python_result.violations } == {
        _violation_signature(v) for v in datalog_result.violations
    }


def test_interprocedural_datalog_matches_python_for_nested_same_named_helpers(tmp_path):
    source = (
        "def outer_b(request):\n"
        "    def helper(value):\n"
        "        return value\n"
        "    name = request.args.get('name')\n"
        "    helper(name)\n"
        "\n"
        "def outer_a(request):\n"
        "    def helper(value):\n"
        "        cursor.execute(value)\n"
        "    return request.args.get('name')\n"
    )
    python_result, datalog_result = _run_both(tmp_path, source)

    assert python_result.violations == []
    assert datalog_result.violations == []
    assert set(python_result.summaries) == set(datalog_result.summaries)
