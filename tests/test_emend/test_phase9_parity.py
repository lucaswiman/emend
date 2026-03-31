"""Phase 9 parity tests: verify trace/flow/policy results are consistent across API, CLI, and MCP entry points.

MCP parity can be tested once MCP test infrastructure exists (direct tool invocation without
going through the stdio transport). For now we cover API vs CLI parity.
"""

import json
import textwrap

import pytest
import yaml

from emend.trace import (
    TraceConfig,
    TraceSink,
    TraceSource,
    format_violations,
    run_trace_analysis,
)
from emend.policy import (
    FlowCheck,
    Policy,
    format_policy_violations,
    load_policies,
    run_policy_checks,
)


# ---------------------------------------------------------------------------
# Shared fixture source code and config
# ---------------------------------------------------------------------------

SOURCE = textwrap.dedent("""\
    def handle_request(request, cursor):
        name = request.args.get('name')
        cursor.execute(name)
""")

CONFIG_YAML = textwrap.dedent("""\
    trace:
      labels: [user_input]
      sources:
        - pattern: "request.args.get($X)"
          label: user_input
      sinks:
        - pattern: "cursor.execute($QUERY)"
          label: user_input
          message: "SQL injection risk"
""")

POLICY_YAML = textwrap.dedent("""\
    policies:
      - name: no-sql-injection
        description: Prevent SQL injection
        severity: error
        checks:
          - name: sqli-flow
            type: flow
            flows-from: "request.args.get($X)"
            flows-to: "cursor.execute($QUERY)"
            label: user_input
            message: "SQL injection via flow policy"
""")


def _write_trace_config(tmp_path):
    """Write the trace config YAML and return path to it."""
    config_dir = tmp_path / ".emend"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "patterns.yaml"
    config_file.write_text(CONFIG_YAML)
    return str(config_file)


def _write_policy_config(tmp_path):
    """Write the policy YAML and return path to it."""
    config_dir = tmp_path / ".emend"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "policies.yaml"
    config_file.write_text(POLICY_YAML)
    return str(config_file)


def _make_trace_config():
    return TraceConfig(
        labels=["user_input"],
        sources=[TraceSource(pattern="request.args.get($X)", label="user_input")],
        sinks=[
            TraceSink(
                pattern="cursor.execute($QUERY)",
                label="user_input",
                message="SQL injection risk",
            )
        ],
    )


# ---------------------------------------------------------------------------
# TestTraceParityApiCli
# ---------------------------------------------------------------------------


class TestTraceParityApiCli:
    """Verify that the trace API and CLI produce consistent results."""

    def test_api_finds_violation(self, tmp_path):
        """API run_trace_analysis detects the SQL injection violation."""
        test_file = tmp_path / "app.py"
        test_file.write_text(SOURCE)

        config = _make_trace_config()
        violations = run_trace_analysis([str(test_file)], config)

        assert len(violations) >= 1
        v = violations[0]
        assert v.label == "user_input"
        assert "SQL injection" in v.message

    def test_cli_finds_violation(self, tmp_path, run_emend_cmd):
        """CLI `emend analyze trace --json` detects the SQL injection violation."""
        test_file = tmp_path / "app.py"
        test_file.write_text(SOURCE)
        config_path = _write_trace_config(tmp_path)

        result = run_emend_cmd(
            ["analyze", "trace", str(test_file), "--config", config_path, "--json"],
            check=False,
        )
        assert result.returncode in (0, 1), f"Unexpected exit code: {result.returncode}\nSTDERR: {result.stderr}"

        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) >= 1
        assert data[0]["label"] == "user_input"
        assert "SQL injection" in data[0]["message"]

    def test_api_cli_results_match(self, tmp_path, run_emend_cmd):
        """API and CLI violations agree on file, line, label, and message."""
        test_file = tmp_path / "app.py"
        test_file.write_text(SOURCE)
        config_path = _write_trace_config(tmp_path)

        # API result
        config = _make_trace_config()
        api_violations = run_trace_analysis([str(test_file)], config)
        assert len(api_violations) >= 1
        api_v = api_violations[0]

        # CLI result
        result = run_emend_cmd(
            ["analyze", "trace", str(test_file), "--config", config_path, "--json"],
            check=False,
        )
        cli_data = json.loads(result.stdout)
        assert len(cli_data) >= 1
        cli_v = cli_data[0]

        assert cli_v["file"] == api_v.file_path
        assert cli_v["line"] == api_v.line
        assert cli_v["label"] == api_v.label
        assert cli_v["message"] == api_v.message


# ---------------------------------------------------------------------------
# TestTracePolicyParity
# ---------------------------------------------------------------------------


class TestTracePolicyParity:
    """Verify that trace analysis and policy flow checks agree on violations."""

    def test_policy_api_finds_violation(self, tmp_path):
        """Policy API run_policy_checks detects the SQL injection via flow check."""
        test_file = tmp_path / "app.py"
        test_file.write_text(SOURCE)
        config_path = _write_policy_config(tmp_path)

        policies = load_policies(config_path)
        violations = run_policy_checks([str(test_file)], policies)

        assert len(violations) >= 1
        v = violations[0]
        assert v.policy_name == "no-sql-injection"
        assert v.severity == "error"

    def test_policy_cli_finds_violation(self, tmp_path, run_emend_cmd):
        """CLI `emend policy --json` detects the SQL injection via flow check."""
        test_file = tmp_path / "app.py"
        test_file.write_text(SOURCE)
        config_path = _write_policy_config(tmp_path)

        result = run_emend_cmd(
            ["policy", str(test_file), "--config", config_path, "--json"],
            check=False,
        )
        assert result.returncode in (0, 1), f"Unexpected exit: {result.returncode}\nSTDERR: {result.stderr}"

        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) >= 1
        assert data[0]["policy"] == "no-sql-injection"

    def test_trace_and_policy_agree_on_location(self, tmp_path):
        """Both trace API and policy API flag the same line."""
        test_file = tmp_path / "app.py"
        test_file.write_text(SOURCE)
        config_path = _write_policy_config(tmp_path)

        # Trace API
        trace_config = _make_trace_config()
        trace_violations = run_trace_analysis([str(test_file)], trace_config)
        assert len(trace_violations) >= 1
        trace_line = trace_violations[0].line

        # Policy API
        policies = load_policies(config_path)
        policy_violations = run_policy_checks([str(test_file)], policies)
        assert len(policy_violations) >= 1
        policy_line = policy_violations[0].line

        # Both should flag the same sink line (cursor.execute on line 3)
        assert trace_line == policy_line


# ---------------------------------------------------------------------------
# TestEngineFieldInJsonOutput
# ---------------------------------------------------------------------------


class TestEngineFieldInJsonOutput:
    """Verify that the 'engine' field appears in JSON output from both API and CLI."""

    def test_json_output_includes_engine(self, tmp_path):
        """format_violations JSON includes 'engine' for violations from run_trace_analysis."""
        test_file = tmp_path / "app.py"
        test_file.write_text(SOURCE)

        config = _make_trace_config()
        violations = run_trace_analysis([str(test_file)], config)
        assert len(violations) >= 1

        # run_trace_analysis sets engine="python" on all violations
        assert all(v.engine == "python" for v in violations)

        output = format_violations(violations, json_output=True)
        data = json.loads(output)
        assert len(data) >= 1
        assert "engine" in data[0], f"'engine' key missing from JSON output: {data[0]}"
        assert data[0]["engine"] == "python"

    def test_cli_json_output_includes_engine(self, tmp_path, run_emend_cmd):
        """CLI `emend analyze trace --json` output includes 'engine' field."""
        test_file = tmp_path / "app.py"
        test_file.write_text(SOURCE)
        config_path = _write_trace_config(tmp_path)

        result = run_emend_cmd(
            ["analyze", "trace", str(test_file), "--config", config_path, "--json"],
            check=False,
        )
        assert result.returncode in (0, 1)

        data = json.loads(result.stdout)
        assert len(data) >= 1
        assert "engine" in data[0], f"'engine' key missing from CLI JSON output: {data[0]}"
        assert data[0]["engine"] == "python"
