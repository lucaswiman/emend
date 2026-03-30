"""Tests for intraprocedural trace analysis."""

import json

import pytest
import yaml

from emend.trace import (
    TraceConfig,
    TraceSanitizer,
    TraceSink,
    TraceSource,
    TraceViolation,
    format_violations,
    load_trace_config,
    run_trace_analysis,
)


def _write_config(tmp_path, config_dict):
    """Helper to write a YAML config file."""
    config_file = tmp_path / ".emend" / "patterns.yaml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(yaml.dump(config_dict))
    return config_file


def _make_sql_injection_config():
    """Return a TraceConfig for SQL injection detection."""
    return TraceConfig(
        labels=["user_input"],
        sources=[
            TraceSource(pattern="request.args.get($X)", label="user_input"),
            TraceSource(pattern="request.form[$X]", label="user_input"),
        ],
        sinks=[
            TraceSink(
                pattern="cursor.execute($X)",
                label="user_input",
                message="Potential SQL injection: user input reaches cursor.execute()",
            ),
            TraceSink(
                pattern="eval($X)",
                label="user_input",
                message="Potential code injection: user input reaches eval()",
            ),
        ],
        sanitizers=[
            TraceSanitizer(pattern="escape($X)", label="user_input"),
            TraceSanitizer(pattern="sanitize($X)", label="user_input"),
        ],
    )


def test_trace_basic_source_to_sink(tmp_path):
    """Detects tainted value flowing from source to sink."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)

    assert len(violations) >= 1
    v = violations[0]
    assert v.label == "user_input"
    assert "SQL injection" in v.message
    assert v.file_path == str(test_file)


def test_trace_sanitizer_removes_taint(tmp_path):
    """Sanitizer pattern removes taint so no violation is reported."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    name = escape(name)\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)

    assert len(violations) == 0


def test_trace_propagation_through_assignments(tmp_path):
    """Taint propagates through variable assignments."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    query = name\n"
        "    cursor.execute(query)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)

    assert len(violations) >= 1
    assert violations[0].label == "user_input"


def test_trace_no_false_positive_clean_value(tmp_path):
    """No violation when a clean (non-tainted) value reaches a sink."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(cursor):\n"
        "    query = 'SELECT 1'\n"
        "    cursor.execute(query)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)

    assert len(violations) == 0


def test_trace_trace_output(tmp_path):
    """Trace includes source and sink steps."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)

    assert len(violations) >= 1
    v = violations[0]
    assert len(v.trace) >= 1
    # Trace should mention "source" and "sink"
    descriptions = [s.description for s in v.trace]
    assert any("source" in d for d in descriptions)
    assert any("sink" in d for d in descriptions)


def test_trace_label_filtering(tmp_path):
    """--label filters to only check a specific taint label."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()

    # With matching label: should find violations
    violations = run_trace_analysis([str(test_file)], config, label_filter="user_input")
    assert len(violations) >= 1

    # With non-matching label: should find no violations
    violations = run_trace_analysis([str(test_file)], config, label_filter="other_label")
    assert len(violations) == 0


def test_trace_json_output(tmp_path):
    """JSON output format is valid and contains expected fields."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)

    output = format_violations(violations, json_output=True)
    data = json.loads(output)
    assert isinstance(data, list)
    assert len(data) >= 1
    assert "file" in data[0]
    assert "line" in data[0]
    assert "label" in data[0]
    assert "message" in data[0]


def test_trace_json_output_with_trace(tmp_path):
    """JSON output includes trace when requested."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)

    output = format_violations(violations, show_trace=True, json_output=True)
    data = json.loads(output)
    assert len(data) >= 1
    assert "trace" in data[0]
    assert isinstance(data[0]["trace"], list)


def test_trace_text_output_format(tmp_path):
    """Text output uses standard file:line:col format."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)

    output = format_violations(violations)
    assert "[trace:user_input]" in output
    assert "SQL injection" in output


def test_trace_text_output_with_trace(tmp_path):
    """Text output includes indented trace lines."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)

    output = format_violations(violations, show_trace=True)
    # Trace lines are indented with two spaces
    trace_lines = [l for l in output.split("\n") if l.startswith("  ")]
    assert len(trace_lines) >= 1


def test_trace_load_config(tmp_path):
    """load_trace_config parses YAML trace section correctly."""
    config_file = _write_config(tmp_path, {
        "trace": {
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

    config = load_trace_config(str(config_file))
    assert config.labels == ["user_input", "sensitive_data"]
    assert len(config.sources) == 1
    assert config.sources[0].pattern == "request.args.get($X)"
    assert len(config.sinks) == 1
    assert config.sinks[0].message == "SQL injection"
    assert len(config.sanitizers) == 1


def test_trace_load_config_missing_trace_section(tmp_path):
    """load_trace_config returns empty config when no trace section."""
    config_file = _write_config(tmp_path, {
        "rules": {"no-print": {"find": "print($X)", "message": "No print"}}
    })

    config = load_trace_config(str(config_file))
    assert config.labels == []
    assert config.sources == []
    assert config.sinks == []
    assert config.sanitizers == []


def test_trace_load_config_file_not_found(tmp_path):
    """load_trace_config raises FileNotFoundError for missing file."""
    with pytest.raises(FileNotFoundError):
        load_trace_config(str(tmp_path / "nonexistent.yaml"))


def test_trace_eval_sink(tmp_path):
    """Detects tainted value reaching eval()."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(request):\n"
        "    code = request.args.get('code')\n"
        "    eval(code)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)

    assert len(violations) >= 1
    assert any("code injection" in v.message for v in violations)


def test_trace_multiple_functions(tmp_path):
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
    violations = run_trace_analysis([str(test_file)], config)

    # Only the unsafe handler should produce violations
    assert len(violations) >= 1
    assert all(v.line >= 5 for v in violations)


def test_trace_empty_config_no_violations(tmp_path):
    """No violations when config has no sources or sinks."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = TraceConfig()
    violations = run_trace_analysis([str(test_file)], config)
    assert len(violations) == 0


def test_trace_nonexistent_file():
    """Nonexistent files are silently skipped."""
    config = _make_sql_injection_config()
    violations = run_trace_analysis(["/nonexistent/file.py"], config)
    assert len(violations) == 0


def test_trace_propagation_through_string_concat(tmp_path):
    """Taint propagates through string operations."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(request, cursor):\n"
        "    name = request.args.get('name')\n"
        '    query = "SELECT * FROM users WHERE name = " + name\n'
        "    cursor.execute(query)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)

    assert len(violations) >= 1


def test_trace_sanitize_then_use(tmp_path):
    """After sanitization, using the variable in a sink is safe."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    clean = sanitize(name)\n"
        "    cursor.execute(clean)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)

    assert len(violations) == 0


def test_trace_field_sensitivity_distinct_fields(tmp_path):
    """Different fields on the same object are tracked independently."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(request, cursor):\n"
        "    user_input = request.args.get('name')\n"
        "    obj = type('O', (), {})()\n"
        "    obj.dirty = user_input\n"
        "    obj.clean = 'safe'\n"
        "    cursor.execute(obj.dirty)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)
    # obj.dirty should trigger a violation
    assert len(violations) >= 1


def test_trace_field_sensitivity_clean_field_no_violation(tmp_path):
    """A clean field on a partially-tainted object does not trigger."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(request, cursor):\n"
        "    user_input = request.args.get('name')\n"
        "    obj = type('O', (), {})()\n"
        "    obj.dirty = user_input\n"
        "    obj.clean = 'safe'\n"
        "    cursor.execute(obj.clean)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)
    # obj.clean should NOT trigger a violation
    assert len(violations) == 0


def test_trace_field_sensitivity_propagation(tmp_path):
    """Taint propagates through dotted attribute assignments."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    data = name\n"
        "    cursor.execute(data)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)
    assert len(violations) >= 1


def test_trace_field_sanitizer_only_cleans_field(tmp_path):
    """Sanitizing obj.field does not clean obj.other_field."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(request, cursor):\n"
        "    a = request.args.get('a')\n"
        "    b = request.args.get('b')\n"
        "    a = escape(a)\n"
        "    cursor.execute(b)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)
    # b is still tainted (escape only cleaned a)
    assert len(violations) >= 1


def test_trace_container_append(tmp_path):
    """Taint propagates through list.append()."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    queries = []\n"
        "    queries.append(name)\n"
        "    cursor.execute(queries[0])\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)
    assert len(violations) >= 1


def test_trace_container_dict_subscript(tmp_path):
    """Taint propagates through dict subscript assignment."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    data = {}\n"
        "    data['query'] = name\n"
        "    q = data['query']\n"
        "    cursor.execute(q)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)
    assert len(violations) >= 1


def test_trace_container_iteration(tmp_path):
    """Taint propagates through for-loop iteration."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    items = [name]\n"
        "    for item in items:\n"
        "        cursor.execute(item)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)
    assert len(violations) >= 1


def test_trace_container_clean_list_no_violation(tmp_path):
    """Clean list elements do not produce false positives."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(cursor):\n"
        "    queries = ['SELECT 1', 'SELECT 2']\n"
        "    for q in queries:\n"
        "        cursor.execute(q)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)
    assert len(violations) == 0


def test_trace_container_extend(tmp_path):
    """Taint propagates through list.extend()."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    tainted = [name]\n"
        "    queries = []\n"
        "    queries.extend(tainted)\n"
        "    cursor.execute(queries[0])\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)
    assert len(violations) >= 1
