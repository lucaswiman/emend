"""Tests for intraprocedural taint analysis."""

import json

import pytest
import yaml

from emend.taint import (
    TaintConfig,
    TaintSanitizer,
    TaintSink,
    TaintSource,
    TaintViolation,
    format_violations,
    load_taint_config,
    run_taint_analysis,
)


def _write_config(tmp_path, config_dict):
    """Helper to write a YAML config file."""
    config_file = tmp_path / ".emend" / "patterns.yaml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(yaml.dump(config_dict))
    return config_file


def _make_sql_injection_config():
    """Return a TaintConfig for SQL injection detection."""
    return TaintConfig(
        labels=["user_input"],
        sources=[
            TaintSource(pattern="request.args.get($X)", label="user_input"),
            TaintSource(pattern="request.form[$X]", label="user_input"),
        ],
        sinks=[
            TaintSink(
                pattern="cursor.execute($X)",
                label="user_input",
                message="Potential SQL injection: user input reaches cursor.execute()",
            ),
            TaintSink(
                pattern="eval($X)",
                label="user_input",
                message="Potential code injection: user input reaches eval()",
            ),
        ],
        sanitizers=[
            TaintSanitizer(pattern="escape($X)", label="user_input"),
            TaintSanitizer(pattern="sanitize($X)", label="user_input"),
        ],
    )


def test_taint_basic_source_to_sink(tmp_path):
    """Detects tainted value flowing from source to sink."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    violations = run_taint_analysis([str(test_file)], config)

    assert len(violations) >= 1
    v = violations[0]
    assert v.label == "user_input"
    assert "SQL injection" in v.message
    assert v.file_path == str(test_file)


def test_taint_sanitizer_removes_taint(tmp_path):
    """Sanitizer pattern removes taint so no violation is reported."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    name = escape(name)\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    violations = run_taint_analysis([str(test_file)], config)

    assert len(violations) == 0


def test_taint_propagation_through_assignments(tmp_path):
    """Taint propagates through variable assignments."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    query = name\n"
        "    cursor.execute(query)\n"
    )

    config = _make_sql_injection_config()
    violations = run_taint_analysis([str(test_file)], config)

    assert len(violations) >= 1
    assert violations[0].label == "user_input"


def test_taint_no_false_positive_clean_value(tmp_path):
    """No violation when a clean (non-tainted) value reaches a sink."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(cursor):\n"
        "    query = 'SELECT 1'\n"
        "    cursor.execute(query)\n"
    )

    config = _make_sql_injection_config()
    violations = run_taint_analysis([str(test_file)], config)

    assert len(violations) == 0


def test_taint_trace_output(tmp_path):
    """Trace includes source and sink steps."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    violations = run_taint_analysis([str(test_file)], config)

    assert len(violations) >= 1
    v = violations[0]
    assert len(v.trace) >= 1
    # Trace should mention "source" and "sink"
    descriptions = [s.description for s in v.trace]
    assert any("source" in d for d in descriptions)
    assert any("sink" in d for d in descriptions)


def test_taint_label_filtering(tmp_path):
    """--label filters to only check a specific taint label."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()

    # With matching label: should find violations
    violations = run_taint_analysis([str(test_file)], config, label_filter="user_input")
    assert len(violations) >= 1

    # With non-matching label: should find no violations
    violations = run_taint_analysis([str(test_file)], config, label_filter="other_label")
    assert len(violations) == 0


def test_taint_json_output(tmp_path):
    """JSON output format is valid and contains expected fields."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    violations = run_taint_analysis([str(test_file)], config)

    output = format_violations(violations, json_output=True)
    data = json.loads(output)
    assert isinstance(data, list)
    assert len(data) >= 1
    assert "file" in data[0]
    assert "line" in data[0]
    assert "label" in data[0]
    assert "message" in data[0]


def test_taint_json_output_with_trace(tmp_path):
    """JSON output includes trace when requested."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    violations = run_taint_analysis([str(test_file)], config)

    output = format_violations(violations, show_trace=True, json_output=True)
    data = json.loads(output)
    assert len(data) >= 1
    assert "trace" in data[0]
    assert isinstance(data[0]["trace"], list)


def test_taint_text_output_format(tmp_path):
    """Text output uses standard file:line:col format."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    violations = run_taint_analysis([str(test_file)], config)

    output = format_violations(violations)
    assert "[taint:user_input]" in output
    assert "SQL injection" in output


def test_taint_text_output_with_trace(tmp_path):
    """Text output includes indented trace lines."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    violations = run_taint_analysis([str(test_file)], config)

    output = format_violations(violations, show_trace=True)
    # Trace lines are indented with two spaces
    trace_lines = [l for l in output.split("\n") if l.startswith("  ")]
    assert len(trace_lines) >= 1


def test_taint_load_config(tmp_path):
    """load_taint_config parses YAML taint section correctly."""
    config_file = _write_config(tmp_path, {
        "taint": {
            "labels": ["user_input", "sensitive_data"],
            "sources": [
                {"pattern": "request.args.get($X)", "label": "user_input"},
            ],
            "sinks": [
                {
                    "pattern": "cursor.execute($X)",
                    "label": "user_input",
                    "message": "SQL injection",
                },
            ],
            "sanitizers": [
                {"pattern": "escape($X)", "label": "user_input"},
            ],
        }
    })

    config = load_taint_config(str(config_file))
    assert config.labels == ["user_input", "sensitive_data"]
    assert len(config.sources) == 1
    assert config.sources[0].pattern == "request.args.get($X)"
    assert len(config.sinks) == 1
    assert config.sinks[0].message == "SQL injection"
    assert len(config.sanitizers) == 1


def test_taint_load_config_missing_taint_section(tmp_path):
    """load_taint_config returns empty config when no taint section."""
    config_file = _write_config(tmp_path, {
        "rules": {"no-print": {"find": "print($X)", "message": "No print"}}
    })

    config = load_taint_config(str(config_file))
    assert config.labels == []
    assert config.sources == []
    assert config.sinks == []
    assert config.sanitizers == []


def test_taint_load_config_file_not_found(tmp_path):
    """load_taint_config raises FileNotFoundError for missing file."""
    with pytest.raises(FileNotFoundError):
        load_taint_config(str(tmp_path / "nonexistent.yaml"))


def test_taint_eval_sink(tmp_path):
    """Detects tainted value reaching eval()."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(request):\n"
        "    code = request.args.get('code')\n"
        "    eval(code)\n"
    )

    config = _make_sql_injection_config()
    violations = run_taint_analysis([str(test_file)], config)

    assert len(violations) >= 1
    assert any("code injection" in v.message for v in violations)


def test_taint_multiple_functions(tmp_path):
    """Each function is analyzed independently."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def safe_handler(cursor):\n"
        "    query = 'SELECT 1'\n"
        "    cursor.execute(query)\n"
        "\n"
        "def unsafe_handler(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    violations = run_taint_analysis([str(test_file)], config)

    # Only the unsafe handler should produce violations
    assert len(violations) >= 1
    assert all(v.line >= 5 for v in violations)


def test_taint_empty_config_no_violations(tmp_path):
    """No violations when config has no sources or sinks."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = TaintConfig()
    violations = run_taint_analysis([str(test_file)], config)
    assert len(violations) == 0


def test_taint_nonexistent_file():
    """Nonexistent files are silently skipped."""
    config = _make_sql_injection_config()
    violations = run_taint_analysis(["/nonexistent/file.py"], config)
    assert len(violations) == 0


def test_taint_propagation_through_string_concat(tmp_path):
    """Taint propagates through string operations."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(request, cursor):\n"
        "    name = request.args.get('name')\n"
        '    query = "SELECT * FROM users WHERE name = " + name\n'
        "    cursor.execute(query)\n"
    )

    config = _make_sql_injection_config()
    violations = run_taint_analysis([str(test_file)], config)

    assert len(violations) >= 1


def test_taint_sanitize_then_use(tmp_path):
    """After sanitization, using the variable in a sink is safe."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    clean = sanitize(name)\n"
        "    cursor.execute(clean)\n"
    )

    config = _make_sql_injection_config()
    violations = run_taint_analysis([str(test_file)], config)

    assert len(violations) == 0
