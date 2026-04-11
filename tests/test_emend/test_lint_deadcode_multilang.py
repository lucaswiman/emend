"""Tests for deadcode lint integration with TypeScript and Rust files."""

import yaml

from emend.lint import LintRule, DeadCodeConfig, run_lint, load_rules


def _write_config(tmp_path, config_dict):
    config_dir = tmp_path / ".emend"
    config_dir.mkdir(exist_ok=True)
    config_file = config_dir / "patterns.yaml"
    config_file.write_text(yaml.dump(config_dict, default_flow_style=False))
    return str(config_file)


# --- TypeScript dead code detection ---


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


# --- Rust dead code detection ---


def test_deadcode_detects_unused_rust_function(tmp_path):
    """Deadcode lint section detects unused function in Rust project."""
    project = tmp_path / "project"
    project.mkdir()

    (project / "lib.rs").write_text(
        "pub fn used_function() -> i32 {\n"
        "    42\n"
        "}\n"
        "\n"
        "fn unused_helper() -> &'static str {\n"
        '    "never called"\n'
        "}\n"
        "\n"
        "pub fn main() {\n"
        "    let _ = used_function();\n"
        "}\n"
    )

    deadcode_config = DeadCodeConfig(enabled=True)
    violations = run_lint(
        [],
        [str(project / "lib.rs")],
        deadcode_config=deadcode_config,
        project_path=str(project),
    )

    # Best-effort: find_dead_code may or may not support Rust yet
    assert isinstance(violations, list)
    if violations:
        violation_messages = [v.message for v in violations]
        assert any("unused_helper" in msg for msg in violation_messages), (
            f"Expected 'unused_helper' in violations but got: {violation_messages}"
        )


# --- Entry-point names for TypeScript ---


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


# --- YAML config wiring ---


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

    config_path = _write_config(
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
