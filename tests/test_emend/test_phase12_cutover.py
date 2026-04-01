"""Phase 12/13: Interprocedural trace uses the Datalog engine.

These tests verify that:
1. The interprocedural engine produces ``engine == "datalog"`` violations.
2. The CLI produces ``engine == "datalog"`` in JSON output.
3. All existing violation shapes are preserved.
"""

from __future__ import annotations

import json

import pytest

from typer.testing import CliRunner

from emend.cli import app
from emend.trace import (
    InterproceduralResult,
    run_interprocedural_trace,
)

from conftest import (
    CROSS_FUNCTION_SOURCE,
    make_sql_injection_config,
    setup_trace_fixture,
)


runner = CliRunner()


# ---------------------------------------------------------------------------
# API-level: engine is datalog
# ---------------------------------------------------------------------------


class TestDefaultEngineIsDatalog:
    """``run_interprocedural_trace()`` uses datalog."""

    def test_default_engine_produces_datalog_violations(self, tmp_path):
        test_file = tmp_path / "app.py"
        test_file.write_text(CROSS_FUNCTION_SOURCE)

        result = run_interprocedural_trace(
            [str(test_file)], make_sql_injection_config(),
        )

        assert isinstance(result, InterproceduralResult)
        assert len(result.violations) >= 1
        assert all(v.engine == "datalog" for v in result.violations), (
            f"Expected engine='datalog' but got: {[v.engine for v in result.violations]}"
        )


# ---------------------------------------------------------------------------
# CLI-level: engine is datalog
# ---------------------------------------------------------------------------


class TestCLIDefaultEngine:
    """CLI ``trace --interprocedural`` uses datalog."""

    def test_cli_default_engine_is_datalog(self, tmp_path):
        src, cfg = setup_trace_fixture(tmp_path)
        result = runner.invoke(
            app,
            ["trace", str(src), "--config", str(cfg), "--interprocedural", "--json"],
        )
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)
        assert len(data) > 0
        assert all(v["engine"] == "datalog" for v in data), (
            f"Expected engine='datalog' but got: {[v['engine'] for v in data]}"
        )

    def test_cli_stderr_reports_engine(self, tmp_path):
        src, cfg = setup_trace_fixture(tmp_path)
        result = runner.invoke(
            app,
            ["trace", str(src), "--config", str(cfg), "--interprocedural"],
            catch_exceptions=False,
        )
        assert "Interprocedural analysis:" in result.output


# ---------------------------------------------------------------------------
# Output shape preservation
# ---------------------------------------------------------------------------


class TestOutputShapePreservation:
    """Violations from the datalog engine preserve the public output shape."""

    def test_json_output_schema(self, tmp_path):
        src, cfg = setup_trace_fixture(tmp_path)
        result = runner.invoke(
            app,
            ["trace", str(src), "--config", str(cfg), "--interprocedural", "--json"],
        )
        data = json.loads(result.output)
        for v in data:
            assert "file" in v
            assert "line" in v
            assert "label" in v
            assert "sink_pattern" in v
            assert "message" in v
            assert "engine" in v
            assert v["engine"] == "datalog"

    def test_result_has_summaries_and_iterations(self, tmp_path):
        test_file = tmp_path / "app.py"
        test_file.write_text(CROSS_FUNCTION_SOURCE)

        result = run_interprocedural_trace(
            [str(test_file)], make_sql_injection_config(),
        )

        assert isinstance(result.summaries, dict)
        assert len(result.summaries) > 0
        assert isinstance(result.iterations, int)
        assert result.iterations >= 1
