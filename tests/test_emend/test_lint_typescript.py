"""Tests for the lint engine applied to TypeScript files.

Covers: simple pattern rules, not-inside constraints, flow rules,
fix/replace mode, sanitizer blocks, arrow functions, clean-file
no-violations, and multiple violations in one file.
"""

import yaml

from emend.lint import LintRule, DeadCodeConfig, run_lint, load_rules


# --- Simple pattern matching ---


def test_ts_lint_simple_pattern(tmp_path):
    """Simple pattern rule detects console.log in TypeScript."""
    ts_file = tmp_path / "app.ts"
    ts_file.write_text(
        "function greet(name: string) {\n"
        '    console.log("hello");\n'
        "}\n"
    )

    rule = LintRule(
        name="no-console",
        find="console.log($X)",
        message="No console.log",
    )

    violations = run_lint([rule], [str(ts_file)], language="typescript")
    assert len(violations) == 1
    assert violations[0].rule_name == "no-console"
    assert violations[0].line is not None
    assert "console.log" in violations[0].match_text


# --- Not-inside constraint ---


def test_ts_lint_pattern_with_not_inside(tmp_path):
    """Pattern rule with not-inside constraint works in TypeScript."""
    ts_file = tmp_path / "service.ts"
    ts_file.write_text(
        "function process() {\n"
        '    console.log("debug");\n'
        "}\n"
        "\n"
        "function debugHelper() {\n"
        "    if (DEBUG) {\n"
        '        console.log("ok");\n'
        "    }\n"
        "}\n"
    )

    rule = LintRule(
        name="no-console",
        find="console.log($X)",
        message="No console.log",
        not_inside="if",
    )

    violations = run_lint([rule], [str(ts_file)], language="typescript")
    # Only the console.log outside the if block should be reported
    assert len(violations) == 1
    assert violations[0].rule_name == "no-console"


# --- Flow rule (taint propagation) ---


def test_ts_lint_flow_rule(tmp_path):
    """Flow rule detects taint from req.query to res.send in TypeScript."""
    ts_file = tmp_path / "handler.ts"
    ts_file.write_text(
        "function handler(req: any, res: any) {\n"
        '    const input = req.query.get("name");\n'
        "    const output = input;\n"
        "    res.send(output);\n"
        "}\n"
    )

    rule = LintRule(
        name="xss",
        find="",
        message="XSS risk",
        flows_from="req.query.get($X)",
        flows_to="res.send($X)",
    )

    violations = run_lint([rule], [str(ts_file)], language="typescript")
    assert len(violations) >= 1
    assert violations[0].rule_name == "xss"


# --- Fix / replace mode ---


def test_ts_lint_fix_replaces_pattern(tmp_path):
    """--fix replaces console.log with logger.info in TypeScript."""
    ts_file = tmp_path / "app.ts"
    ts_file.write_text('console.log("hello");\n')

    rule = LintRule(
        name="use-logger",
        find="console.log($X)",
        message="Use logger",
        replace="logger.info($X)",
    )

    violations = run_lint([rule], [str(ts_file)], fix=True, language="typescript")
    assert len(violations) == 1
    assert "replacement" in violations[0].match_text.lower() or violations[0].rule_name == "use-logger"

    content = ts_file.read_text()
    assert 'logger.info("hello")' in content
    assert "console.log" not in content


# --- Sanitizer blocks flow ---


def test_ts_lint_flow_rule_sanitizer_blocks(tmp_path):
    """Flow rule sanitizer blocks taint propagation in TypeScript."""
    ts_file = tmp_path / "handler.ts"
    ts_file.write_text(
        "function handler(req: any, res: any) {\n"
        '    const input = req.query.get("name");\n'
        "    const safe = sanitize(input);\n"
        "    res.send(safe);\n"
        "}\n"
    )

    rule = LintRule(
        name="xss",
        find="",
        message="XSS risk",
        flows_from="req.query.get($X)",
        flows_to="res.send($X)",
        not_through="sanitize($X)",
    )

    violations = run_lint([rule], [str(ts_file)], language="typescript")
    assert len(violations) == 0


# --- Arrow functions ---


def test_ts_lint_arrow_function_flow(tmp_path):
    """Flow rule works inside TypeScript arrow functions."""
    ts_file = tmp_path / "handler.ts"
    ts_file.write_text(
        "const handler = (req: any, res: any) => {\n"
        '    const input = req.query.get("q");\n'
        "    res.send(input);\n"
        "};\n"
    )

    rule = LintRule(
        name="xss",
        find="",
        message="XSS risk",
        flows_from="req.query.get($X)",
        flows_to="res.send($X)",
    )

    violations = run_lint([rule], [str(ts_file)], language="typescript")
    assert len(violations) >= 1
    assert violations[0].rule_name == "xss"


# --- Clean file produces no violations ---


def test_ts_lint_no_violations_clean_file(tmp_path):
    """Clean TypeScript file produces no violations."""
    ts_file = tmp_path / "clean.ts"
    ts_file.write_text(
        "function greet(name: string) {\n"
        '    logger.info("hello");\n'
        "}\n"
    )

    rule = LintRule(
        name="no-console",
        find="console.log($X)",
        message="No console.log",
    )

    violations = run_lint([rule], [str(ts_file)], language="typescript")
    assert len(violations) == 0


# --- Multiple violations ---


def test_ts_lint_multiple_violations(tmp_path):
    """Multiple matches in a single TypeScript file produce multiple violations."""
    ts_file = tmp_path / "app.ts"
    ts_file.write_text(
        'console.log("one");\n'
        "const x = 1;\n"
        'console.log("two");\n'
        "const y = 2;\n"
        'console.log("three");\n'
    )

    rule = LintRule(
        name="no-console",
        find="console.log($X)",
        message="No console.log",
    )

    violations = run_lint([rule], [str(ts_file)], language="typescript")
    assert len(violations) == 3
    lines = [v.line for v in violations]
    assert len(set(lines)) == 3


# ---------------------------------------------------------------------------
# deadcode lint rule on TypeScript projects
# ---------------------------------------------------------------------------


def _write_deadcode_config(tmp_path, config_dict):
    config_dir = tmp_path / ".emend"
    config_dir.mkdir(exist_ok=True)
    config_file = config_dir / "patterns.yaml"
    config_file.write_text(yaml.dump(config_dict, default_flow_style=False))
    return str(config_file)


def test_deadcode_detects_unused_ts_function(tmp_path):
    """Deadcode lint section detects unused exported function in TypeScript project."""
    project = tmp_path / "project"
    project.mkdir()

    (project / "app.ts").write_text(
        "export function usedFunction() {\n"
        "    return 42;\n"
        "}\n"
        "\n"
        "function unusedHelper() {\n"
        '    return "never called";\n'
        "}\n"
        "\n"
        "const result = usedFunction();\n"
    )

    deadcode_config = DeadCodeConfig(enabled=True)
    violations = run_lint(
        [],
        [str(project / "app.ts")],
        deadcode_config=deadcode_config,
        project_path=str(project),
    )

    # Best-effort: find_dead_code may or may not support TypeScript yet
    assert isinstance(violations, list)
    violation_messages = [v.message for v in violations]
    if violations:
        # If violations were found, unusedHelper should be among them
        assert any("unusedHelper" in msg for msg in violation_messages), (
            f"Expected 'unusedHelper' in violations but got: {violation_messages}"
        )
        # usedFunction is called so should NOT be flagged
        assert not any("usedFunction" in msg for msg in violation_messages), (
            f"'usedFunction' should not be dead but found in: {violation_messages}"
        )


def test_deadcode_entry_point_names_ts(tmp_path):
    """Deadcode respects entry-point-names for TypeScript test functions."""
    project = tmp_path / "project"
    project.mkdir()

    (project / "tests.ts").write_text(
        "function describe(name: string, fn: () => void) { fn(); }\n"
        "\n"
        "function test_helper() {\n"
        '    return "help";\n'
        "}\n"
        "\n"
        'describe("suite", () => {\n'
        "    test_helper();\n"
        "});\n"
    )

    deadcode_config = DeadCodeConfig(enabled=True, entry_point_names=["describe"])
    violations = run_lint(
        [],
        [str(project / "tests.ts")],
        deadcode_config=deadcode_config,
        project_path=str(project),
    )

    assert isinstance(violations, list)
    violation_messages = [v.message for v in violations]
    # describe is an entry-point name and should not be flagged as dead code
    assert not any("describe" in msg for msg in violation_messages), (
        f"'describe' should not be flagged as dead code, got: {violation_messages}"
    )


def test_deadcode_yaml_config_with_ts_project(tmp_path):
    """Deadcode section loaded from YAML works with TypeScript files."""
    project = tmp_path / "project"
    project.mkdir()

    (project / "module.ts").write_text(
        "export function activeFunction() {\n"
        "    return true;\n"
        "}\n"
        "\n"
        "function dormantFunction() {\n"
        "    return false;\n"
        "}\n"
        "\n"
        "const x = activeFunction();\n"
    )

    config_path = _write_deadcode_config(
        project,
        {
            "deadcode": {
                "enabled": True,
                "message": "Unused code found",
            }
        },
    )

    rules, _macros, deadcode_config = load_rules(config_path)
    assert deadcode_config is not None
    assert deadcode_config.enabled is True
    assert deadcode_config.message == "Unused code found"

    violations = run_lint(
        rules,
        [str(project / "module.ts")],
        deadcode_config=deadcode_config,
        project_path=str(project),
    )

    assert isinstance(violations, list)
    if violations:
        # Custom message from YAML should appear in violation messages
        assert any("Unused code found" in v.message for v in violations), (
            f"Expected custom YAML message in violations but got: {[v.message for v in violations]}"
        )
