"""Phase 12: Cut over public interprocedural trace to Datalog.

These tests verify that:
1. The default interprocedural engine is now ``"datalog"``.
2. The CLI default (no ``--engine`` flag) produces ``engine == "datalog"``.
3. Explicit ``--engine python`` still routes to the Python engine.
4. Failures are explicit — no silent fallback to the Python engine.
5. All existing violation shapes are preserved.
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
# API-level: default engine is now datalog
# ---------------------------------------------------------------------------


class TestDefaultEngineIsDatalog:
    """``run_interprocedural_trace()`` uses datalog by default."""

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

    def test_explicit_python_still_works(self, tmp_path):
        test_file = tmp_path / "app.py"
        test_file.write_text(CROSS_FUNCTION_SOURCE)

        result = run_interprocedural_trace(
            [str(test_file)], make_sql_injection_config(), engine="python",
        )

        assert len(result.violations) >= 1
        assert all(v.engine == "python" for v in result.violations)

    def test_unknown_engine_raises(self, tmp_path):
        test_file = tmp_path / "app.py"
        test_file.write_text(CROSS_FUNCTION_SOURCE)

        with pytest.raises(ValueError, match="Unknown interprocedural engine"):
            run_interprocedural_trace(
                [str(test_file)], make_sql_injection_config(), engine="magic",
            )


# ---------------------------------------------------------------------------
# CLI-level: default --engine is datalog
# ---------------------------------------------------------------------------


class TestCLIDefaultEngine:
    """CLI ``trace --interprocedural`` without ``--engine`` uses datalog."""

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

    def test_cli_explicit_python_engine(self, tmp_path):
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

    def test_cli_stderr_reports_datalog_engine(self, tmp_path):
        src, cfg = setup_trace_fixture(tmp_path)
        result = runner.invoke(
            app,
            ["trace", str(src), "--config", str(cfg), "--interprocedural"],
            catch_exceptions=False,
        )
        # CliRunner mixes stderr into output; check the status line mentions datalog
        assert "Interprocedural analysis (datalog)" in result.output


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
