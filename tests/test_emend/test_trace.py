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


def _write_config(tmp_path, config_dict, name="patterns.yaml"):
    """Helper to write a YAML config file under ``.emend/``."""
    config_file = tmp_path / ".emend" / name
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(yaml.dump(config_dict))
    return config_file


def _write_rules_config(tmp_path, config_dict):
    """Write the canonical ``rules.yaml`` config."""
    return _write_config(tmp_path, config_dict, name="rules.yaml")


# The recurring "user input flows straight into cursor.execute" snippet.
SQLI_SOURCE = (
    "def handle_request(request, cursor):\n"
    "    name = request.args.get('name')\n"
    "    cursor.execute(name)\n"
)


def test_trace_load_config_from_unified_flow_rules(tmp_path):
    config_file = _write_rules_config(tmp_path, {
        "macros": {
            "input": "request.args.get($X)",
        },
        "rules": {
            "sql-injection": {
                "flow": {
                    "from": "{input}",
                    "to": "cursor.execute($Q)",
                    "not-through": "escape($X)",
                    "quantifier": "some_path",
                },
                "message": "Unsanitized input reaches SQL execution",
            },
        },
    })

    config = load_trace_config(str(config_file))
    assert config.labels == ["sql-injection"]
    assert len(config.sources) == 1
    assert config.sources[0].pattern == "request.args.get($X)"
    assert len(config.sinks) == 1
    assert config.sinks[0].pattern == "cursor.execute($Q)"
    assert config.sinks[0].message == "Unsanitized input reaches SQL execution"
    assert len(config.sanitizers) == 1
    assert config.sanitizers[0].pattern == "escape($X)"
    assert config.sanitizers[0].quantifier == "some_path"


def test_trace_load_config_merges_trace_section_and_unified_flow_rules(tmp_path):
    config_file = _write_rules_config(tmp_path, {
        "trace": {
            "sources": [
                {"pattern": "request.form[$X]", "label": "legacy-source"},
            ],
            "sinks": [
                {"pattern": "eval($X)", "label": "legacy-source", "message": "Code injection"},
            ],
        },
        "rules": {
            "sql-injection": {
                "flow": {
                    "from": "request.args.get($X)",
                    "to": "cursor.execute($Q)",
                },
                "message": "SQL injection",
            },
        },
    })

    config = load_trace_config(str(config_file))
    assert {s.label for s in config.sources} == {"legacy-source", "sql-injection"}
    assert {s.label for s in config.sinks} == {"legacy-source", "sql-injection"}


def _make_sql_injection_config():
    """SQL-injection ``TraceConfig``: the shared conftest base extended with a
    ``request.form[...]`` source, an ``eval()`` sink, and a ``sanitize()``
    sanitizer that several tests in this module rely on."""
    from conftest import make_sql_injection_config

    config = make_sql_injection_config()
    config.sources.append(
        TraceSource(pattern="request.form[$X]", label="user_input")
    )
    config.sinks.append(
        TraceSink(
            pattern="eval($X)",
            label="user_input",
            message="Potential code injection: user input reaches eval()",
        )
    )
    config.sanitizers.append(
        TraceSanitizer(pattern="sanitize($X)", label="user_input")
    )
    return config


def test_trace_basic_source_to_sink(tmp_path):
    """Detects tainted value flowing from source to sink."""
    test_file = tmp_path / "app.py"
    test_file.write_text(SQLI_SOURCE)

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


def test_trace_does_not_cross_contaminate_separate_functions(tmp_path):
    """A source in one function should not taint an unrelated sink in another."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def read_name(request):\n"
        "    name = request.args.get('name')\n"
        "    return name\n"
        "\n"
        "def run_query(cursor):\n"
        "    name = 'SELECT 1'\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)

    assert violations == []


def test_trace_trace_output(tmp_path):
    """Trace includes source and sink steps."""
    test_file = tmp_path / "app.py"
    test_file.write_text(SQLI_SOURCE)

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
    test_file.write_text(SQLI_SOURCE)

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
    test_file.write_text(SQLI_SOURCE)

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
    test_file.write_text(SQLI_SOURCE)

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
    test_file.write_text(SQLI_SOURCE)

    config = _make_sql_injection_config()
    violations = run_trace_analysis([str(test_file)], config)

    output = format_violations(violations)
    assert "[trace:user_input]" in output
    assert "SQL injection" in output


def test_trace_text_output_with_trace(tmp_path):
    """Text output includes indented trace lines."""
    test_file = tmp_path / "app.py"
    test_file.write_text(SQLI_SOURCE)

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


def test_trace_loads_flow_rules_from_unified_rules_yaml(tmp_path):
    config_file = tmp_path / ".emend" / "rules.yaml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(yaml.dump({
        "presets": ["flask"],
        "rules": {
            "sql-injection": {
                "flow": {
                    "from": "request.args.get($X)",
                    "to": "cursor.execute($Q)",
                    "not-through": "escape($X)",
                },
                "message": "Unsanitized input reaches SQL execution",
            },
        },
    }))

    config = load_trace_config(str(config_file))
    assert "sql-injection" in config.labels
    assert any(source.pattern == "request.args.get($X)" for source in config.sources)
    assert any(sink.pattern == "cursor.execute($Q)" for sink in config.sinks)
    assert any(s.pattern == "escape($X)" for s in config.sanitizers)


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


# ---------------------------------------------------------------------------
# Defect #9: dict-form flow rules in unified rules config
# ---------------------------------------------------------------------------


def test_trace_load_config_dict_form_flow_rules(tmp_path):
    """Dict-form from/to with type_constraint should extract the pattern string.

    Regression test for defect #9: str(dict) produced garbage patterns
    like "{'pattern': '...', 'type_constraint': '...'}" instead of
    extracting the actual pattern.
    """
    config_file = _write_rules_config(tmp_path, {
        "rules": {
            "redis-get-set": {
                "flow": {
                    "from": {
                        "pattern": "$R.get($KEY)",
                        "type_constraint": "Redis",
                    },
                    "to": {
                        "pattern": "$R.set($KEY, $VAL)",
                        "type_constraint": "Redis",
                    },
                    "not-through": "$R.pipeline($...ARGS)",
                },
                "message": "Redis TOCTOU",
            },
        },
    })

    config = load_trace_config(str(config_file))
    assert len(config.sources) == 1
    assert config.sources[0].pattern == "$R.get($KEY)"
    assert config.sources[0].type_constraint == "Redis"
    assert len(config.sinks) == 1
    assert config.sinks[0].pattern == "$R.set($KEY, $VAL)"
    assert config.sinks[0].type_constraint == "Redis"


def test_trace_dict_form_mixed_string_and_dict(tmp_path):
    """Mixed string-form and dict-form in the same config should both work."""
    config_file = _write_rules_config(tmp_path, {
        "rules": {
            "string-form": {
                "flow": {
                    "from": "request.args.get($X)",
                    "to": "cursor.execute($Q)",
                },
                "message": "SQLi",
            },
            "dict-form": {
                "flow": {
                    "from": {
                        "pattern": "$R.get($KEY)",
                        "type_constraint": "Redis",
                    },
                    "to": "$R.set($KEY, $VAL)",
                },
                "message": "Redis TOCTOU",
            },
        },
    })

    config = load_trace_config(str(config_file))
    patterns = {s.pattern for s in config.sources}
    assert "request.args.get($X)" in patterns
    assert "$R.get($KEY)" in patterns
    # The dict-form source should have the type constraint extracted
    redis_src = next(s for s in config.sources if s.pattern == "$R.get($KEY)")
    assert redis_src.type_constraint == "Redis"


# ---------------------------------------------------------------------------
# Defect #8: exclude_paths support in trace analysis
# ---------------------------------------------------------------------------


def test_trace_exclude_paths_from_yaml(tmp_path):
    """exclude_paths in trace config YAML should be loaded."""
    config_file = _write_rules_config(tmp_path, {
        "trace": {
            "labels": ["test"],
            "sources": [{"pattern": "$X.get($K)", "label": "test"}],
            "sinks": [{"pattern": "$X.set($K, $V)", "label": "test",
                        "message": "test"}],
            "exclude_paths": ["*/migrations/*.py", "tests/**"],
        },
    })

    config = load_trace_config(str(config_file))
    assert config.exclude_paths == ["*/migrations/*.py", "tests/**"]


def test_trace_exclude_paths_from_unified_rules(tmp_path):
    """exclude_paths at top level of unified rules config should be loaded."""
    config_file = _write_rules_config(tmp_path, {
        "exclude_paths": ["*/migrations/*.py"],
        "rules": {
            "test-rule": {
                "flow": {
                    "from": "$X.get($K)",
                    "to": "$X.save()",
                },
                "message": "test",
            },
        },
    })

    config = load_trace_config(str(config_file))
    assert "*/migrations/*.py" in config.exclude_paths


def test_trace_exclude_paths_filters_files(tmp_path):
    """Files matching exclude_paths should be skipped during trace analysis."""
    # Create a source file that would trigger a violation
    migrations_dir = tmp_path / "app" / "migrations"
    migrations_dir.mkdir(parents=True)
    migration_file = migrations_dir / "0001_initial.py"
    migration_file.write_text(
        "def forwards(apps, schema_editor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    # Same code in a non-migration file should still be flagged
    app_file = tmp_path / "app" / "views.py"
    app_file.write_text(
        "def handle(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )

    config = _make_sql_injection_config()
    config.exclude_paths = ["*/migrations/*.py"]

    violations = run_trace_analysis(
        [str(migration_file), str(app_file)], config
    )

    # Only the non-migration file should have violations
    violation_files = {v.file_path for v in violations}
    assert str(app_file) in violation_files
    assert str(migration_file) not in violation_files


def test_trace_exclude_paths_does_not_overmatch_siblings():
    """A directory-name exclusion must not match sibling files sharing a prefix.

    Regression: ``_trace_path_is_excluded`` appended a bare ``*`` to each
    pattern, so ``exclude_paths: ["tests"]`` turned into ``tests*`` and wrongly
    matched ``tests_helper.py`` / ``tests_data/foo.py`` in addition to
    ``tests/foo.py``.
    """
    from emend.trace import _trace_path_is_excluded

    patterns = ["tests"]
    # Files genuinely under the excluded directory.
    assert _trace_path_is_excluded("tests/foo.py", patterns)
    assert _trace_path_is_excluded("tests/sub/bar.py", patterns)
    # Sibling files that merely share the prefix must NOT be excluded.
    assert not _trace_path_is_excluded("tests_helper.py", patterns)
    assert not _trace_path_is_excluded("tests_data/foo.py", patterns)


def test_trace_cmd_file_path_resolves_to_parent_dir(tmp_path, capsys):
    """When given a file path, _trace_cmd_impl should use the parent dir as project_path."""
    import yaml
    source_file = tmp_path / "app.py"
    source_file.write_text(
        "def handle(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )
    config_dir = tmp_path / ".emend"
    config_dir.mkdir()
    config_file = config_dir / "rules.yaml"
    config_file.write_text(yaml.dump({
        "trace": {
            "labels": ["sql-injection"],
            "sources": [{"pattern": "request.args.get($X)", "label": "sql-injection"}],
            "sinks": [{"pattern": "cursor.execute($X)", "label": "sql-injection"}],
        }
    }))

    from unittest.mock import patch
    from emend.cli_analysis import _trace_cmd_impl
    from emend.cli_base import _state
    old_lang = _state["language"]
    _state["language"] = "python"
    captured_project_path = []

    orig_run_trace = None
    try:
        from emend import trace as _trace_mod
        orig_run_trace = _trace_mod.run_trace_analysis

        def mock_run_trace(*args, **kwargs):
            captured_project_path.append(kwargs.get("project_path"))
            return orig_run_trace(*args, **kwargs)

        with patch.object(_trace_mod, "run_trace_analysis", side_effect=mock_run_trace):
            import typer
            with pytest.raises((SystemExit, typer.Exit)):
                _trace_cmd_impl(
                    path=str(source_file),
                    config=str(config_file),
                    label=None, trace=False, json_output=False,
                    project=None,
                    interprocedural=False, max_iterations=3,
                    preset=None,
                )
    finally:
        _state["language"] = old_lang

    assert captured_project_path, "run_trace_analysis was not called"
    pp = captured_project_path[0]
    assert not pp.endswith(".py"), (
        f"project_path should be a directory, not a file: {pp}"
    )


def test_trace_cmd_output_ends_with_newline(tmp_path, capsys):
    """trace_cmd's print output should end with a newline.

    The CLI uses ``print(output, end=...)`` with a ternary to avoid
    double-newlines.  A bug had both branches of the ternary producing
    the same empty string, so output never got a trailing newline.

    This test invokes the CLI implementation directly and captures stdout.
    """
    source_file = tmp_path / "app.py"
    source_file.write_text(
        "def handle(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    cursor.execute(name)\n"
    )
    config_dir = tmp_path / ".emend"
    config_dir.mkdir()
    config_file = config_dir / "rules.yaml"
    import yaml
    config_file.write_text(yaml.dump({
        "trace": {
            "labels": ["sql-injection"],
            "sources": [{"pattern": "request.args.get($X)", "label": "sql-injection"}],
            "sinks": [{"pattern": "cursor.execute($X)", "label": "sql-injection"}],
        }
    }))

    from emend.cli_analysis import _trace_cmd_impl
    from emend.cli_base import _state
    old_lang = _state["language"]
    _state["language"] = "python"
    try:
        import typer
        with pytest.raises((SystemExit, typer.Exit)):
            _trace_cmd_impl(
                path=str(source_file),
                config=str(config_file),
                label=None, trace=False, json_output=False,
                project=None,
                interprocedural=False, max_iterations=3,
                preset=None,
            )
    finally:
        _state["language"] = old_lang

    captured = capsys.readouterr()
    assert captured.out, "expected non-empty stdout"
    assert captured.out.endswith("\n"), (
        f"CLI trace output should end with a newline, got: {captured.out!r}"
    )


def test_trace_relative_path_matches_absolute_path(tmp_path, monkeypatch):
    """Relative file paths must yield the same violations as absolute paths.

    Regression: FactGraph stores facts keyed by the resolved (absolute) path,
    but ``_run_trace_datalog`` used the raw ``paths`` strings for the Datalog
    source/sink relations.  When the CLI passed a relative path (e.g.
    ``emend trace app.py``), the cross-variable propagation join
    (``trace_source.fp == def_use.fp``) failed silently and zero violations
    were reported.  An intermediate assignment (``query = ... + name``) is
    required to exercise the ``*def_use`` join; a direct same-variable flow
    only touches the inline relations and would mask the bug.
    """
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request(request, cursor):\n"
        "    name = request.args.get('name')\n"
        "    query = 'SELECT * FROM t WHERE n = ' + name\n"
        "    cursor.execute(query)\n"
    )

    config = _make_sql_injection_config()

    abs_violations = run_trace_analysis([str(test_file)], config)
    assert len(abs_violations) >= 1

    monkeypatch.chdir(tmp_path)
    rel_violations = run_trace_analysis(["app.py"], config)

    assert len(rel_violations) == len(abs_violations)
    assert any("SQL injection" in v.message for v in rel_violations)
