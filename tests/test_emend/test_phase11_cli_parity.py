"""Phase 11/13 CLI tests: engine observability and result shape.

These tests exercise the trace CLI command with the Datalog engine and verify:
1. JSON output includes the ``engine`` field.
2. The engine is always ``"datalog"``.
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
# File-specific source fixtures
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
# Engine field in JSON output
# ---------------------------------------------------------------------------


class TestEngineInJsonOutput:
    """JSON output from ``trace --interprocedural`` always includes ``engine``."""

    def test_datalog_engine_in_json(self, tmp_path):
        src, cfg = setup_trace_fixture(tmp_path)
        result = runner.invoke(
            app,
            [
                "trace", str(src), "--config", str(cfg),
                "--interprocedural", "--json",
            ],
        )
        data = json.loads(result.output)
        for v in data:
            assert "engine" in v, "engine field missing from JSON output"
            assert v["engine"] == "datalog"

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


# ---------------------------------------------------------------------------
# Result shape on CLI-level fixtures
# ---------------------------------------------------------------------------


class TestResultShape:
    """Violations have the expected shape on CLI-level fixtures."""

    def _run_interprocedural(self, tmp_path, source):
        src, cfg = setup_trace_fixture(tmp_path, source)
        result = runner.invoke(
            app,
            [
                "trace", str(src), "--config", str(cfg),
                "--interprocedural", "--json",
            ],
        )
        assert result.exit_code in (0, 1), result.output
        return json.loads(result.output)

    def test_cross_function_produces_violations(self, tmp_path):
        data = self._run_interprocedural(tmp_path, CROSS_FUNCTION_SOURCE)
        assert len(data) > 0
        assert all(v["engine"] == "datalog" for v in data)

    def test_returned_taint_produces_violations(self, tmp_path):
        data = self._run_interprocedural(tmp_path, _RETURNED_TAINT_SOURCE)
        assert len(data) > 0

    def test_late_sanitizer_produces_violations(self, tmp_path):
        data = self._run_interprocedural(tmp_path, _LATE_SANITIZER_SOURCE)
        assert len(data) > 0
