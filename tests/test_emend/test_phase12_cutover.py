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
import textwrap
from pathlib import Path

import pytest

from typer.testing import CliRunner

from emend.cli import app
from emend.trace import (
    InterproceduralResult,
    TraceConfig,
    TraceSink,
    TraceSource,
    TraceSanitizer,
    run_interprocedural_trace,
    run_interprocedural_trace_analysis,
)


runner = CliRunner()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_sql_config():
    return TraceConfig(
        labels=["user_input"],
        sources=[
            TraceSource(pattern="request.args.get($X)", label="user_input"),
        ],
        sinks=[
            TraceSink(
                pattern="cursor.execute($X)",
                label="user_input",
                message="SQL injection: user input reaches cursor.execute()",
            ),
        ],
        sanitizers=[
            TraceSanitizer(pattern="escape($X)", label="user_input"),
        ],
    )


_SQL_CONFIG_YAML = textwrap.dedent("""\
    trace:
      labels:
        - user_input
      sources:
        - pattern: "request.args.get($X)"
          label: user_input
      sinks:
        - pattern: "cursor.execute($X)"
          label: user_input
          message: "SQL injection: user input reaches cursor.execute()"
      sanitizers:
        - pattern: "escape($X)"
          label: user_input
""")

_CROSS_FUNCTION_SOURCE = textwrap.dedent("""\
    def run_query(cursor, query):
        cursor.execute(query)

    def handle_request(request, cursor):
        name = request.args.get('name')
        run_query(cursor, name)
""")


def _setup_fixture(tmp_path: Path, source: str) -> tuple[Path, Path]:
    src = tmp_path / "app.py"
    src.write_text(source)
    cfg = tmp_path / "rules.yaml"
    cfg.write_text(_SQL_CONFIG_YAML)
    return src, cfg


# ---------------------------------------------------------------------------
# API-level: default engine is now datalog
# ---------------------------------------------------------------------------


class TestDefaultEngineIsDatalog:
    """``run_interprocedural_trace()`` uses datalog by default."""

    def test_default_engine_produces_datalog_violations(self, tmp_path):
        test_file = tmp_path / "app.py"
        test_file.write_text(_CROSS_FUNCTION_SOURCE)

        config = _make_sql_config()
        result = run_interprocedural_trace([str(test_file)], config)

        assert isinstance(result, InterproceduralResult)
        assert len(result.violations) >= 1
        assert all(v.engine == "datalog" for v in result.violations), (
            f"Expected engine='datalog' but got: {[v.engine for v in result.violations]}"
        )

    def test_explicit_python_still_works(self, tmp_path):
        test_file = tmp_path / "app.py"
        test_file.write_text(_CROSS_FUNCTION_SOURCE)

        config = _make_sql_config()
        result = run_interprocedural_trace(
            [str(test_file)], config, engine="python",
        )

        assert len(result.violations) >= 1
        assert all(v.engine == "python" for v in result.violations)

    def test_explicit_datalog_still_works(self, tmp_path):
        test_file = tmp_path / "app.py"
        test_file.write_text(_CROSS_FUNCTION_SOURCE)

        config = _make_sql_config()
        result = run_interprocedural_trace(
            [str(test_file)], config, engine="datalog",
        )

        assert len(result.violations) >= 1
        assert all(v.engine == "datalog" for v in result.violations)

    def test_unknown_engine_raises(self, tmp_path):
        test_file = tmp_path / "app.py"
        test_file.write_text(_CROSS_FUNCTION_SOURCE)

        config = _make_sql_config()
        with pytest.raises(ValueError, match="Unknown interprocedural engine"):
            run_interprocedural_trace(
                [str(test_file)], config, engine="magic",
            )


# ---------------------------------------------------------------------------
# CLI-level: default --engine is datalog
# ---------------------------------------------------------------------------


class TestCLIDefaultEngine:
    """CLI ``trace --interprocedural`` without ``--engine`` uses datalog."""

    def test_cli_default_engine_is_datalog(self, tmp_path):
        src, cfg = _setup_fixture(tmp_path, _CROSS_FUNCTION_SOURCE)
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
        src, cfg = _setup_fixture(tmp_path, _CROSS_FUNCTION_SOURCE)
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
        """The stderr status line should mention 'datalog' as the engine."""
        src, cfg = _setup_fixture(tmp_path, _CROSS_FUNCTION_SOURCE)
        result = runner.invoke(
            app,
            ["trace", str(src), "--config", str(cfg), "--interprocedural"],
        )
        # stderr output is mixed into result.output in CliRunner
        assert "datalog" in result.output.lower()


# ---------------------------------------------------------------------------
# Output shape preservation
# ---------------------------------------------------------------------------


class TestOutputShapePreservation:
    """Violations from the datalog engine preserve the public output shape."""

    def test_violation_fields_present(self, tmp_path):
        test_file = tmp_path / "app.py"
        test_file.write_text(_CROSS_FUNCTION_SOURCE)

        config = _make_sql_config()
        result = run_interprocedural_trace([str(test_file)], config)

        for v in result.violations:
            assert hasattr(v, "file_path")
            assert hasattr(v, "line")
            assert hasattr(v, "col")
            assert hasattr(v, "label")
            assert hasattr(v, "sink_pattern")
            assert hasattr(v, "message")
            assert hasattr(v, "trace")
            assert hasattr(v, "engine")
            assert v.engine == "datalog"

    def test_json_output_schema(self, tmp_path):
        src, cfg = _setup_fixture(tmp_path, _CROSS_FUNCTION_SOURCE)
        result = runner.invoke(
            app,
            ["trace", str(src), "--config", str(cfg), "--interprocedural", "--json"],
        )
        data = json.loads(result.output)
        for v in data:
            assert "file_path" in v or "file" in v
            assert "line" in v
            assert "label" in v
            assert "sink_pattern" in v
            assert "message" in v
            assert "engine" in v
            assert v["engine"] == "datalog"

    def test_result_has_summaries_and_iterations(self, tmp_path):
        test_file = tmp_path / "app.py"
        test_file.write_text(_CROSS_FUNCTION_SOURCE)

        config = _make_sql_config()
        result = run_interprocedural_trace([str(test_file)], config)

        assert isinstance(result.summaries, dict)
        assert len(result.summaries) > 0
        assert isinstance(result.iterations, int)
        assert result.iterations >= 1


# ---------------------------------------------------------------------------
# No silent fallback
# ---------------------------------------------------------------------------


class TestNoSilentFallback:
    """The datalog engine must not silently fall back to Python."""

    def test_python_engine_still_reports_python(self, tmp_path):
        """When explicitly requesting python, violations say python."""
        test_file = tmp_path / "app.py"
        test_file.write_text(_CROSS_FUNCTION_SOURCE)

        config = _make_sql_config()
        result = run_interprocedural_trace(
            [str(test_file)], config, engine="python",
        )

        assert all(v.engine == "python" for v in result.violations)

    def test_datalog_default_never_says_python(self, tmp_path):
        """Default engine violations must never report engine='python'."""
        test_file = tmp_path / "app.py"
        test_file.write_text(_CROSS_FUNCTION_SOURCE)

        config = _make_sql_config()
        result = run_interprocedural_trace([str(test_file)], config)

        python_violations = [v for v in result.violations if v.engine == "python"]
        assert python_violations == [], (
            f"Found {len(python_violations)} violations with engine='python' "
            f"on the default (datalog) path — silent fallback detected"
        )
