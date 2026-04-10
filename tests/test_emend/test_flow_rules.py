"""Tests for flow-based lint rules (flows-from / flows-to / not-through)."""

import pytest
import yaml

from emend.lint import (
    FlowWitness,
    LintRule,
    LintViolation,
    load_rules,
    run_lint,
    _assignments_from_cfgs,
    _check_flow_rule,
    _extract_names_from_text,
)


def _write_config(tmp_path, config_dict):
    """Helper to write a YAML config file."""
    config_file = tmp_path / ".emend" / "patterns.yaml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(yaml.dump(config_dict))
    return config_file


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


class TestExtractNames:
    def test_simple_identifiers(self):
        names = _extract_names_from_text("foo + bar")
        assert "foo" in names
        assert "bar" in names

    def test_filters_keywords(self):
        names = _extract_names_from_text("if x and y")
        assert "x" in names
        assert "y" in names
        assert "if" not in names
        assert "and" not in names

    def test_dotted_access(self):
        names = _extract_names_from_text("request.args.get(key)")
        assert "request" in names
        assert "args" in names
        assert "get" in names
        assert "key" in names


class TestFindAssignments:
    """Test tree-sitter CFG-backed assignment extraction via _assignments_from_cfgs."""

    def test_simple_assignment(self, tmp_path):
        source = "def f():\n    x = 1\n    y = foo(x)\n"
        test_file = tmp_path / "test.py"
        test_file.write_text(source)
        assignments = _assignments_from_cfgs(
            source, str(test_file),
            func_start=1, func_end=3,
        )
        assert len(assignments) >= 2
        targets = [a[1] for a in assignments]
        assert "x" in targets
        assert "y" in targets

    def test_no_assignments(self, tmp_path):
        source = "def f():\n    print(hello)\n    foo()\n"
        test_file = tmp_path / "test.py"
        test_file.write_text(source)
        assignments = _assignments_from_cfgs(
            source, str(test_file),
            func_start=1, func_end=3,
        )
        assert assignments == []


# ---------------------------------------------------------------------------
# Basic flow detection
# ---------------------------------------------------------------------------


def test_flow_basic_detection(tmp_path):
    """Flow rule detects taint flowing from source to sink within a function."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request():\n"
        "    user_input = request.args.get('name')\n"
        "    cursor.execute(user_input)\n"
    )

    rules = [
        LintRule(
            name="sql-injection",
            find="",
            message="User input may flow to SQL execution",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) >= 1
    v = violations[0]
    assert v.rule_name == "sql-injection"
    assert v.message == "User input may flow to SQL execution"
    assert v.file_path == str(test_file)


def test_flow_taint_through_assignment(tmp_path):
    """Taint propagates through variable assignments."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    query = raw\n"
        "    cursor.execute(query)\n"
    )

    rules = [
        LintRule(
            name="sql-injection",
            find="",
            message="SQL injection",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) >= 1


def test_flow_taint_through_function_call(tmp_path):
    """Taint propagates through function calls in assignments: y = f(tainted)."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    prepared = build_query(raw)\n"
        "    cursor.execute(prepared)\n"
    )

    rules = [
        LintRule(
            name="sql-injection",
            find="",
            message="SQL injection via function",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) >= 1


# ---------------------------------------------------------------------------
# not-through sanitizer
# ---------------------------------------------------------------------------


def test_flow_not_through_sanitizer_suppresses(tmp_path):
    """A sanitizer (not-through) between source and sink prevents reporting."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    safe = sanitize(raw)\n"
        "    cursor.execute(safe)\n"
    )

    rules = [
        LintRule(
            name="sql-injection",
            find="",
            message="SQL injection",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
            not_through="sanitize($Y)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 0


def test_flow_not_through_absent_still_reports(tmp_path):
    """Without the sanitizer pattern present, violation is still reported."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    cursor.execute(raw)\n"
    )

    rules = [
        LintRule(
            name="sql-injection",
            find="",
            message="SQL injection",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
            not_through="sanitize($Y)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) >= 1


# ---------------------------------------------------------------------------
# Scoping: flow is intraprocedural only
# ---------------------------------------------------------------------------


def test_flow_across_functions_not_detected(tmp_path):
    """Taint in one function does not bleed to another function."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def get_input():\n"
        "    raw = request.args.get('q')\n"
        "    return raw\n"
        "\n"
        "def execute_query():\n"
        "    cursor.execute(query)\n"
    )

    rules = [
        LintRule(
            name="sql-injection",
            find="",
            message="SQL injection",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 0


# ---------------------------------------------------------------------------
# YAML config loading
# ---------------------------------------------------------------------------


def test_load_flow_rule_from_yaml(tmp_path):
    """load_rules parses flows-from, flows-to, not-through from YAML."""
    config = {
        "rules": {
            "sql-injection": {
                "flows-from": "request.args.get($X)",
                "flows-to": "cursor.execute($QUERY)",
                "not-through": "sanitize($Y)",
                "message": "SQL injection risk",
            }
        }
    }
    config_file = _write_config(tmp_path, config)

    rules, macros, _ = load_rules(str(config_file))
    assert len(rules) == 1
    r = rules[0]
    assert r.name == "sql-injection"
    assert r.flows_from == "request.args.get($X)"
    assert r.flows_to == "cursor.execute($QUERY)"
    assert r.not_through == "sanitize($Y)"
    assert r.message == "SQL injection risk"
    assert r.find == ""  # flow rules don't need find


def test_load_mixed_rules_from_yaml(tmp_path):
    """Config with both pattern and flow rules loads correctly."""
    config = {
        "rules": {
            "no-print": {
                "find": "print($X)",
                "message": "Use logging",
            },
            "sql-injection": {
                "flows-from": "request.args.get($X)",
                "flows-to": "cursor.execute($QUERY)",
                "message": "SQL injection",
            },
        }
    }
    config_file = _write_config(tmp_path, config)

    rules, _, _ = load_rules(str(config_file))
    assert len(rules) == 2

    pattern_rules = [r for r in rules if not (r.flows_from and r.flows_to)]
    flow_rules = [r for r in rules if r.flows_from and r.flows_to]
    assert len(pattern_rules) == 1
    assert len(flow_rules) == 1


def test_flow_rule_with_macros(tmp_path):
    """Flow rules support macro expansion."""
    config = {
        "macros": {
            "user_input": "request.args.get($X)",
        },
        "rules": {
            "sql-injection": {
                "flows-from": "{user_input}",
                "flows-to": "cursor.execute($QUERY)",
                "message": "SQL injection",
            },
        },
    }
    config_file = _write_config(tmp_path, config)

    rules, _, _ = load_rules(str(config_file))
    assert rules[0].flows_from == "request.args.get($X)"


# ---------------------------------------------------------------------------
# Combined pattern + flow rules in one lint run
# ---------------------------------------------------------------------------


def test_mixed_pattern_and_flow_rules(tmp_path):
    """Pattern rules and flow rules both produce violations in one lint run."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handler():\n"
        "    print('debug')\n"
        "    raw = request.args.get('q')\n"
        "    cursor.execute(raw)\n"
    )

    rules = [
        LintRule(
            name="no-print",
            find="print($X)",
            message="Use logging",
        ),
        LintRule(
            name="sql-injection",
            find="",
            message="SQL injection",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    rule_names = {v.rule_name for v in violations}
    assert "no-print" in rule_names
    assert "sql-injection" in rule_names


# ---------------------------------------------------------------------------
# Witness traces
# ---------------------------------------------------------------------------


def test_flow_violation_has_witness(tmp_path):
    """Flow violations include a FlowWitness with source/sink info."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    cursor.execute(raw)\n"
    )

    rules = [
        LintRule(
            name="sql-injection",
            find="",
            message="SQL injection",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) >= 1
    v = violations[0]
    assert v.witness is not None
    assert v.witness.source_line == 2
    assert v.witness.sink_line == 3
    assert "request.args.get" in v.witness.source_text
    assert "cursor.execute" in v.witness.sink_text
    assert len(v.witness.taint_chain) >= 1


def test_flow_match_text_format(tmp_path):
    """Flow violation match_text shows source -> sink."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    cursor.execute(raw)\n"
    )

    rules = [
        LintRule(
            name="sql-injection",
            find="",
            message="SQL injection",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) >= 1
    assert "flow:" in violations[0].match_text
    assert "->" in violations[0].match_text


# ---------------------------------------------------------------------------
# Unsafe logging example from spec
# ---------------------------------------------------------------------------


def test_flow_unsafe_logging(tmp_path):
    """Sensitive data flowing to logging is detected."""
    test_file = tmp_path / "auth.py"
    test_file.write_text(
        "def login(user, pwd):\n"
        "    password = pwd\n"
        "    logger.info(password)\n"
    )

    rules = [
        LintRule(
            name="unsafe-logging",
            find="",
            message="Sensitive data may reach logging without redaction",
            flows_from="password = $X",
            flows_to="logger.info($MSG)",
            not_through="redact($X)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) >= 1
    assert violations[0].rule_name == "unsafe-logging"


def test_flow_unsafe_logging_with_redact(tmp_path):
    """Sanitized (redacted) data does not trigger the rule."""
    test_file = tmp_path / "auth.py"
    test_file.write_text(
        "def login(user, pwd):\n"
        "    password = pwd\n"
        "    password = redact(password)\n"
        "    logger.info(password)\n"
    )

    rules = [
        LintRule(
            name="unsafe-logging",
            find="",
            message="Sensitive data may reach logging without redaction",
            flows_from="password = $X",
            flows_to="logger.info($MSG)",
            not_through="redact($X)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_flow_no_source_match(tmp_path):
    """No violation when source pattern is absent."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    cursor.execute('SELECT 1')\n"
    )

    rules = [
        LintRule(
            name="sql-injection",
            find="",
            message="SQL injection",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 0


def test_flow_no_sink_match(tmp_path):
    """No violation when sink pattern is absent."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    print(raw)\n"
    )

    rules = [
        LintRule(
            name="sql-injection",
            find="",
            message="SQL injection",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 0


def test_flow_sink_before_source_not_detected(tmp_path):
    """Sink appearing before source in the same function is not a violation."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    cursor.execute(q)\n"
        "    raw = request.args.get('q')\n"
    )

    rules = [
        LintRule(
            name="sql-injection",
            find="",
            message="SQL injection",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 0


def test_flow_multiple_functions_only_tainted_reports(tmp_path):
    """Only the function with both source and sink reports a violation."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def safe():\n"
        "    cursor.execute('SELECT 1')\n"
        "\n"
        "def unsafe():\n"
        "    raw = request.args.get('q')\n"
        "    cursor.execute(raw)\n"
    )

    rules = [
        LintRule(
            name="sql-injection",
            find="",
            message="SQL injection",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 1
    assert violations[0].line >= 4  # in the unsafe function


def test_flow_method_in_class(tmp_path):
    """Flow detection works for methods inside classes."""
    test_file = tmp_path / "views.py"
    test_file.write_text(
        "class UserView:\n"
        "    def post(self):\n"
        "        raw = request.args.get('q')\n"
        "        cursor.execute(raw)\n"
    )

    rules = [
        LintRule(
            name="sql-injection",
            find="",
            message="SQL injection",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) >= 1


def test_flow_rule_filter(tmp_path):
    """rule_filter restricts which flow rules are checked."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    cursor.execute(raw)\n"
    )

    rules = [
        LintRule(
            name="sql-injection",
            find="",
            message="SQL injection",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
        ),
        LintRule(
            name="other-flow",
            find="",
            message="Other",
            flows_from="request.args.get($X)",
            flows_to="logger.info($MSG)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)], rule_filter="sql-injection")
    assert all(v.rule_name == "sql-injection" for v in violations)


def test_flow_empty_file(tmp_path):
    """Flow rules on empty files produce no violations."""
    test_file = tmp_path / "empty.py"
    test_file.write_text("")

    rules = [
        LintRule(
            name="sql-injection",
            find="",
            message="SQL injection",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 0


def test_flow_multiline_chain(tmp_path):
    """Taint propagates through a chain of assignments."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    step1 = raw\n"
        "    step2 = transform(step1)\n"
        "    step3 = step2\n"
        "    cursor.execute(step3)\n"
    )

    rules = [
        LintRule(
            name="sql-injection",
            find="",
            message="SQL injection",
            flows_from="request.args.get($X)",
            flows_to="cursor.execute($QUERY)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) >= 1
    # The witness should capture the chain
    w = violations[0].witness
    assert w is not None
    assert w.source_line == 2
    assert w.sink_line == 6
