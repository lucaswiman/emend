"""Tests for flow-based lint rules (flows-from / flows-to / not-through)."""

import pytest
import yaml

from emend.lint import (
    LintRule,
    load_rules,
    run_lint,
)


@pytest.fixture
def sql_rule():
    def make_rule(message="SQL injection", not_through=None):
        return LintRule(
            name="sql-injection", find="", message=message,
            flows_from="request.args.get($X)", flows_to="cursor.execute($QUERY)",
            not_through=not_through,
        )
    return make_rule


def _write_config(tmp_path, config_dict):
    """Helper to write a YAML config file."""
    config_file = tmp_path / ".emend" / "patterns.yaml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(yaml.dump(config_dict))
    return config_file


# ---------------------------------------------------------------------------
# Basic flow detection
# ---------------------------------------------------------------------------


def test_flow_basic_detection(tmp_path, sql_rule):
    """Flow rule detects taint flowing from source to sink within a function."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def handle_request():\n"
        "    user_input = request.args.get('name')\n"
        "    cursor.execute(user_input)\n"
    )

    rules = [sql_rule(message="User input may flow to SQL execution")]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) >= 1
    v = violations[0]
    assert v.rule_name == "sql-injection"
    assert v.message == "User input may flow to SQL execution"
    assert v.file_path == str(test_file)


def test_flow_taint_through_assignment(tmp_path, sql_rule):
    """Taint propagates through variable assignments."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    query = raw\n"
        "    cursor.execute(query)\n"
    )

    rules = [sql_rule()]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) >= 1


def test_flow_taint_through_function_call(tmp_path, sql_rule):
    """Taint propagates through function calls in assignments: y = f(tainted)."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    prepared = build_query(raw)\n"
        "    cursor.execute(prepared)\n"
    )

    rules = [sql_rule(message="SQL injection via function")]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) >= 1


# ---------------------------------------------------------------------------
# not-through sanitizer
# ---------------------------------------------------------------------------


def test_flow_not_through_sanitizer_suppresses(tmp_path, sql_rule):
    """A sanitizer (not-through) between source and sink prevents reporting."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    safe = sanitize(raw)\n"
        "    cursor.execute(safe)\n"
    )

    rules = [sql_rule(not_through="sanitize($Y)")]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 0


def test_flow_not_through_absent_still_reports(tmp_path, sql_rule):
    """Without the sanitizer pattern present, violation is still reported."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    cursor.execute(raw)\n"
    )

    rules = [sql_rule(not_through="sanitize($Y)")]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) >= 1


def test_flow_any_not_through_alternative_blocks(tmp_path, sql_rule):
    """Either configured sanitizer alternative blocks the source-to-sink flow."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def first():\n"
        "    raw = request.args.get('q')\n"
        "    safe = escape(raw)\n"
        "    cursor.execute(safe)\n"
        "def second():\n"
        "    raw = request.args.get('q')\n"
        "    safe = sanitize(raw)\n"
        "    cursor.execute(safe)\n"
        "def unsafe():\n"
        "    raw = request.args.get('q')\n"
        "    cursor.execute(raw)\n"
    )
    rules = [sql_rule(not_through=["escape($X)", "sanitize($X)"])]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 1


# ---------------------------------------------------------------------------
# Scoping: flow is intraprocedural only
# ---------------------------------------------------------------------------


def test_flow_across_functions_not_detected(tmp_path, sql_rule):
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

    rules = [sql_rule()]

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


def test_mixed_pattern_and_flow_rules(tmp_path, sql_rule):
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
        sql_rule(),
    ]

    violations = run_lint(rules, [str(test_file)])
    rule_names = {v.rule_name for v in violations}
    assert "no-print" in rule_names
    assert "sql-injection" in rule_names


# ---------------------------------------------------------------------------
# Witness traces
# ---------------------------------------------------------------------------


def test_flow_violation_has_witness(tmp_path, sql_rule):
    """Flow violations include a FlowWitness with source/sink info."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    cursor.execute(raw)\n"
    )

    rules = [sql_rule()]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) >= 1
    v = violations[0]
    assert v.witness is not None
    assert v.witness.source_line == 2
    assert v.witness.sink_line == 3
    assert "request.args.get" in v.witness.source_text
    assert "cursor.execute" in v.witness.sink_text
    assert len(v.witness.taint_chain) >= 1


def test_flow_match_text_format(tmp_path, sql_rule):
    """Flow violation match_text shows source -> sink."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    cursor.execute(raw)\n"
    )

    rules = [sql_rule()]

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


def test_flow_no_source_match(tmp_path, sql_rule):
    """No violation when source pattern is absent."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    cursor.execute('SELECT 1')\n"
    )

    rules = [sql_rule()]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 0


def test_flow_no_sink_match(tmp_path, sql_rule):
    """No violation when sink pattern is absent."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    print(raw)\n"
    )

    rules = [sql_rule()]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 0


def test_flow_sink_before_source_not_detected(tmp_path, sql_rule):
    """Sink appearing before source in the same function is not a violation."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    cursor.execute(q)\n"
        "    raw = request.args.get('q')\n"
    )

    rules = [sql_rule()]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 0


def test_flow_multiple_functions_only_tainted_reports(tmp_path, sql_rule):
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

    rules = [sql_rule()]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 1
    assert violations[0].line >= 4  # in the unsafe function


def test_flow_method_in_class(tmp_path, sql_rule):
    """Flow detection works for methods inside classes."""
    test_file = tmp_path / "views.py"
    test_file.write_text(
        "class UserView:\n"
        "    def post(self):\n"
        "        raw = request.args.get('q')\n"
        "        cursor.execute(raw)\n"
    )

    rules = [sql_rule()]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) >= 1


def test_flow_rule_filter(tmp_path, sql_rule):
    """rule_filter restricts which flow rules are checked."""
    test_file = tmp_path / "app.py"
    test_file.write_text(
        "def process():\n"
        "    raw = request.args.get('q')\n"
        "    cursor.execute(raw)\n"
    )

    rules = [
        sql_rule(),
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


def test_flow_empty_file(tmp_path, sql_rule):
    """Flow rules on empty files produce no violations."""
    test_file = tmp_path / "empty.py"
    test_file.write_text("")

    rules = [sql_rule()]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 0


def test_flow_multiline_chain(tmp_path, sql_rule):
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

    rules = [sql_rule()]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) >= 1
    # The witness should capture the chain
    w = violations[0].witness
    assert w is not None
    assert w.source_line == 2
    assert w.sink_line == 6


# ---------------------------------------------------------------------------
# A rules document in the wrong shape is an error, not an empty rule set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("document, complaint", [
    # Rules written as a list of entries, the shape of many other linters.
    ({"rules": [{"id": "no-eval", "find": "eval($X)", "message": "eval"}]},
     r"'rules' must map each rule name to its settings"),
    ({"rules": []}, r"'rules' must map each rule name to its settings"),
    ({"rules": ""}, r"'rules' must map each rule name to its settings"),
    ({"rules": "eval($X)"}, r"'rules' must map each rule name to its settings"),
    # A rule name with nothing under it.
    ({"rules": {"no-eval": None}}, r"rule 'no-eval' must be a mapping"),
    ({"rules": {"no-eval": "eval($X)"}}, r"rule 'no-eval' must be a mapping"),
    # Policies written as a mapping instead of a list, or holding a non-mapping.
    ({"policies": {"my-policy": {"checks": []}}}, r"'policies' must be a list"),
    ({"policies": ["not-a-mapping"]}, r"policy entry 0 must be a mapping"),
])
def test_misshapen_rules_are_rejected(document, complaint):
    """Each of these used to load as no rules at all, so a lint run printed
    nothing and exited 0."""
    from emend.checks.rules_config import compile_rules_document

    with pytest.raises(ValueError, match=complaint):
        compile_rules_document(document)


def test_lint_reports_a_misshapen_rules_file(tmp_path):
    from typer.testing import CliRunner

    from emend.cli import app

    (tmp_path / "app.py").write_text("eval(input())\n")
    rules = tmp_path / "rules.yaml"
    rules.write_text("rules:\n  - id: no-eval\n    find: eval($X)\n    message: eval\n")

    result = CliRunner().invoke(app, ["lint", str(tmp_path), "--config", str(rules)])

    assert result.exit_code != 0, result.output
    assert "'rules' must map each rule name to its settings" in result.output
    assert "not hold a list" in result.output


def test_empty_rules_section_is_still_no_rules():
    from emend.checks.rules_config import compile_rules_document

    for document in ({}, {"rules": None}, {"rules": {}}):
        compiled = compile_rules_document(document)
        assert not compiled.pattern_rules and not compiled.flow_rules


# ---------------------------------------------------------------------------
# A sink or sanitizer whose metavariable is the receiver of a method call
# ---------------------------------------------------------------------------


def _flow_lines(tmp_path, source, *, flows_to, not_through=None, flows_from="input($X)"):
    path = tmp_path / "app.py"
    path.write_text(source)
    rule = LintRule(
        name="flow", find="", message="tainted", flows_from=flows_from,
        flows_to=flows_to, not_through=not_through,
    )
    return sorted(violation.line for violation in run_lint([rule], [str(path)]))


def test_receiver_sink_fires_on_a_tainted_receiver(tmp_path):
    """``$P.strip()`` names the value the method is called on.  A use of a
    variable as a receiver is recorded for the whole ``data.strip``, so the
    sink found no occurrence inside ``data`` and was never placed."""
    source = (
        "def handler():\n"
        "    data = input('x')\n"
        "    clean = 'constant'\n"
        "    data.strip()\n"
        "    clean.strip()\n"
        "    eval(data)\n"
    )
    assert _flow_lines(tmp_path, source, flows_to="$P.strip()") == [4]
    # An argument sink is placed as before.
    assert _flow_lines(tmp_path, source, flows_to="eval($CODE)") == [6]


def test_subscript_base_sink_fires_on_a_tainted_container(tmp_path):
    source = (
        "def handler():\n"
        "    data = input('x')\n"
        "    clean = 'constant'\n"
        "    _ = data[0]\n"
        "    _ = clean[0]\n"
    )
    assert _flow_lines(tmp_path, source, flows_to="$P[0]") == [4]


def test_receiver_sink_fires_through_an_attribute_of_the_tainted_value(tmp_path):
    source = (
        "def handler():\n"
        "    payload = input('x')\n"
        "    payload.body.decode()\n"
        "    other.body.decode()\n"
    )
    assert _flow_lines(tmp_path, source, flows_to="$P.decode()") == [3]


def test_taint_follows_a_method_call_on_the_tainted_value(tmp_path):
    """``data.strip()`` is made of ``data``.  The receiver was not an input of
    its call, so any method call laundered the value."""
    source = (
        "def handler():\n"
        "    data = input('x')\n"
        "    eval(data.strip())\n"
        "    trimmed = data.strip().lower()\n"
        "    eval(trimmed)\n"
        "    eval(str(data).encode())\n"
        "    eval('constant'.upper())\n"
        "    eval(other.strip())\n"
        "    eval(helpers.clean('constant'))\n"
    )
    assert _flow_lines(tmp_path, source, flows_to="eval($CODE)") == [3, 5, 6]


def test_receiver_sanitizer_works_like_an_argument_sanitizer(tmp_path):
    """``escape(data)`` and ``data.escaped()`` both leave ``data`` clean."""
    by_argument = (
        "def handler():\n"
        "    data = input('x')\n"
        "    safe = escape(data)\n"
        "    eval(safe)\n"
        "    eval(data)\n"
    )
    by_receiver = by_argument.replace("escape(data)", "data.escaped()")
    for source in (by_argument, by_receiver):
        assert _flow_lines(tmp_path, source, flows_to="eval($CODE)") == [4, 5]
    assert _flow_lines(
        tmp_path, by_argument, flows_to="eval($CODE)", not_through=["escape($X)"],
    ) == []
    assert _flow_lines(
        tmp_path, by_receiver, flows_to="eval($CODE)", not_through=["$P.escaped()"],
    ) == []
    # A sanitizer called on something else leaves the value tainted.
    unrelated = by_receiver.replace("data.escaped()", "other.escaped()")
    assert _flow_lines(
        tmp_path, unrelated, flows_to="eval($CODE)", not_through=["$P.escaped()"],
    ) == [5]


def test_receiver_return_sanitizer_cleans_only_the_returned_copy(tmp_path):
    """``{pattern: "$P.escaped()", effect: returns}`` cleans what the call
    returns and leaves the value it was called on tainted, as ``escape($X)``
    does for its argument."""
    from emend.trace import (
        TraceConfig, TraceSanitizer, TraceSink, TraceSource, run_trace_analysis,
    )

    by_argument = (
        "def handler():\n"
        "    data = input('x')\n"
        "    safe = escape(data)\n"
        "    eval(safe)\n"
        "    eval(data)\n"
    )
    by_receiver = by_argument.replace("escape(data)", "data.escaped()")
    path = tmp_path / "app.py"
    for source, pattern in ((by_argument, "escape($X)"), (by_receiver, "$P.escaped()")):
        path.write_text(source)
        config = TraceConfig(
            labels=["value"],
            sources=[TraceSource(pattern="input($X)", label="value")],
            sinks=[TraceSink(pattern="eval($CODE)", label="value", message="tainted")],
            sanitizers=[TraceSanitizer(pattern, "value", effect="returns")],
        )
        found = run_trace_analysis([str(path)], config)
        assert sorted({violation.line for violation in found}) == [5], pattern
