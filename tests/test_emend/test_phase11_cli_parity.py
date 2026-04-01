"""Phase 11 CLI/API tests: engine choice observability and result equivalence.

These tests exercise the trace CLI command with both Python and Datalog engines
and verify:
1. The ``--engine`` flag routes to the correct implementation.
2. JSON output includes the ``engine`` field so callers can observe which
   engine produced each violation.
3. Both engines produce equivalent results on the same fixtures.
"""

from __future__ import annotations

import json
import textwrap

from typer.testing import CliRunner

from emend.cli import app

from conftest import (
    CROSS_FUNCTION_SOURCE,
    setup_trace_fixture,
)


runner = CliRunner()


# ---------------------------------------------------------------------------
# File-specific source fixtures (not shared because they are only used here)
# ---------------------------------------------------------------------------

_RETURNED_TAINT_SOURCE = textwrap.dedent("""\
    def passthrough(value):
        return value

    def handle_request(request, cursor):
        name = request.args.get('name')
        query = passthrough(name)
        cursor.execute(query)
""")


_LATE_SANITIZER_SOURCE = textwrap.dedent("""\
    def run_query(cursor, query):
        cursor.execute(query)

    def handle_request(request, cursor):
        name = request.args.get('name')
        run_query(cursor, name)
        name = escape(name)
""")


# ---------------------------------------------------------------------------
# Engine flag routing
# ---------------------------------------------------------------------------


class TestEngineFlag:
    """The ``--engine`` flag on ``trace --interprocedural`` selects the engine."""

    def test_default_engine_is_datalog(self, tmp_path):
        src, cfg = setup_trace_fixture(tmp_path)
        result = runner.invoke(
            app,
            ["trace", str(src), "--config", str(cfg), "--interprocedural", "--json"],
        )
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)
        assert len(data) > 0
        assert all(v["engine"] == "datalog" for v in data)

    def test_engine_python_explicit(self, tmp_path):
        src, cfg = setup_trace_fixture(tmp_path)
        result = runner.invoke(
            app,
            [
                "trace", str(src), "--config", str(cfg),
                "--interprocedural", "--engine", "python", "--json",
            ],
        )
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)
        assert len(data) > 0
        assert all(v["engine"] == "python" for v in data)

    def test_engine_datalog(self, tmp_path):
        src, cfg = setup_trace_fixture(tmp_path)
        result = runner.invoke(
            app,
            [
                "trace", str(src), "--config", str(cfg),
                "--interprocedural", "--engine", "datalog", "--json",
            ],
        )
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)
        assert len(data) > 0
        assert all(v["engine"] == "datalog" for v in data)


# ---------------------------------------------------------------------------
# JSON output includes engine field
# ---------------------------------------------------------------------------


class TestEngineInJsonOutput:
    """JSON output from ``trace --interprocedural`` always includes ``engine``."""

    def test_python_engine_in_json(self, tmp_path):
        src, cfg = setup_trace_fixture(tmp_path)
        result = runner.invoke(
            app,
            [
                "trace", str(src), "--config", str(cfg),
                "--interprocedural", "--engine", "python", "--json",
            ],
        )
        data = json.loads(result.output)
        for v in data:
            assert "engine" in v, "engine field missing from JSON output"
            assert v["engine"] == "python"

    def test_datalog_engine_in_json(self, tmp_path):
        src, cfg = setup_trace_fixture(tmp_path)
        result = runner.invoke(
            app,
            [
                "trace", str(src), "--config", str(cfg),
                "--interprocedural", "--engine", "datalog", "--json",
            ],
        )
        data = json.loads(result.output)
        for v in data:
            assert "engine" in v, "engine field missing from JSON output"
            assert v["engine"] == "datalog"


# ---------------------------------------------------------------------------
# Result equivalence across engines
# ---------------------------------------------------------------------------


def _normalize_violations(data: list[dict]) -> set[tuple]:
    """Extract a comparable signature from JSON violation dicts."""
    result = set()
    for v in data:
        result.add((
            v["line"],
            v["label"],
            v["sink_pattern"],
            v["message"],
        ))
    return result


class TestResultEquivalence:
    """Both engines produce equivalent violations on CLI-level fixtures."""

    def _run_both_engines(self, tmp_path, source):
        src, cfg = setup_trace_fixture(tmp_path, source)
        py_result = runner.invoke(
            app,
            [
                "trace", str(src), "--config", str(cfg),
                "--interprocedural", "--engine", "python", "--json",
            ],
        )
        dl_result = runner.invoke(
            app,
            [
                "trace", str(src), "--config", str(cfg),
                "--interprocedural", "--engine", "datalog", "--json",
            ],
        )
        assert py_result.exit_code in (0, 1), py_result.output
        assert dl_result.exit_code in (0, 1), dl_result.output
        py_data = json.loads(py_result.output)
        dl_data = json.loads(dl_result.output)
        return py_data, dl_data

    def test_cross_function_equivalence(self, tmp_path):
        py, dl = self._run_both_engines(tmp_path, CROSS_FUNCTION_SOURCE)
        assert _normalize_violations(py) == _normalize_violations(dl)

    def test_returned_taint_equivalence(self, tmp_path):
        py, dl = self._run_both_engines(tmp_path, _RETURNED_TAINT_SOURCE)
        assert _normalize_violations(py) == _normalize_violations(dl)

    def test_late_sanitizer_equivalence(self, tmp_path):
        py, dl = self._run_both_engines(tmp_path, _LATE_SANITIZER_SOURCE)
        assert _normalize_violations(py) == _normalize_violations(dl)
