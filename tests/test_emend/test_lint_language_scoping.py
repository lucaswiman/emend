"""Tests for language-scoped lint rules (Phase 6: Cross-Language Lint & Flow Rules)."""

import yaml
from pathlib import Path

from emend.lint import LintRule, run_lint, load_rules


def _write_config(tmp_path, config_dict):
    config_dir = tmp_path / ".emend"
    config_dir.mkdir(exist_ok=True)
    config_file = config_dir / "patterns.yaml"
    config_file.write_text(yaml.dump(config_dict, default_flow_style=False))
    return str(config_file)


# --- LintRule dataclass field tests ---


def test_language_field_on_lint_rule():
    """LintRule accepts language field as string."""
    rule = LintRule(name="no-console", find="console.log($X)", message="No console.log", language="typescript")
    assert rule.language == "typescript"


def test_language_field_list_on_lint_rule():
    """LintRule accepts language field as list of strings."""
    rule = LintRule(name="no-foo", find="foo($X)", message="No foo", language=["python", "typescript"])
    assert rule.language == ["python", "typescript"]


# --- Language-scoped rule filtering tests ---


def test_language_scoped_rule_only_applies_to_matching_files(tmp_path):
    """Rule with language='typescript' only fires on .ts files, not .py files."""
    py_file = tmp_path / "app.py"
    py_file.write_text("console.log('x')\n")

    ts_file = tmp_path / "app.ts"
    ts_file.write_text("console.log('x')\n")

    rule = LintRule(
        name="no-console",
        find="console.log($X)",
        message="No console.log",
        language="typescript",
    )

    violations = run_lint([rule], [str(py_file), str(ts_file)])

    violated_files = {v.file_path for v in violations}
    assert str(ts_file) in violated_files
    assert str(py_file) not in violated_files


def test_language_scoped_rule_list_applies_to_multiple_languages(tmp_path):
    """Rule with language=['python', 'typescript'] applies to both .py and .ts files."""
    py_file = tmp_path / "app.py"
    py_file.write_text("def f():\n    foo(1)\n")

    ts_file = tmp_path / "app.ts"
    ts_file.write_text("function f() { foo(2) }\n")

    rs_file = tmp_path / "app.rs"
    rs_file.write_text("fn f() { foo(3); }\n")

    rule = LintRule(
        name="no-foo",
        find="foo($X)",
        message="No foo",
        language=["python", "typescript"],
    )

    violations = run_lint([rule], [str(py_file), str(ts_file), str(rs_file)])

    violated_files = {v.file_path for v in violations}
    assert str(py_file) in violated_files
    assert str(ts_file) in violated_files
    assert str(rs_file) not in violated_files


def test_no_language_field_applies_to_all(tmp_path):
    """Rule with no language field applies to files of any language."""
    py_file = tmp_path / "app.py"
    py_file.write_text("def f():\n    foo(1)\n")

    ts_file = tmp_path / "app.ts"
    ts_file.write_text("function f() { foo(2) }\n")

    rs_file = tmp_path / "app.rs"
    rs_file.write_text("fn f() { foo(3); }\n")

    rule = LintRule(
        name="no-foo",
        find="foo($X)",
        message="No foo",
    )

    violations = run_lint([rule], [str(py_file), str(ts_file), str(rs_file)])

    violated_files = {v.file_path for v in violations}
    assert str(py_file) in violated_files
    assert str(ts_file) in violated_files
    assert str(rs_file) in violated_files


# --- load_rules() language key parsing tests ---


def test_load_rules_parses_language_string(tmp_path):
    """load_rules() parses language key as string from YAML."""
    config_path = _write_config(tmp_path, {
        "rules": {
            "no-console": {
                "find": "console.log($X)",
                "message": "No console.log",
                "language": "typescript",
            },
        },
    })

    rules, _macros, _deadcode = load_rules(config_path)

    assert len(rules) == 1
    assert rules[0].language == "typescript"


def test_load_rules_parses_language_list(tmp_path):
    """load_rules() parses language key as list from YAML."""
    config_path = _write_config(tmp_path, {
        "rules": {
            "no-foo": {
                "find": "foo($X)",
                "message": "No foo",
                "language": ["python", "typescript"],
            },
        },
    })

    rules, _macros, _deadcode = load_rules(config_path)

    assert len(rules) == 1
    assert rules[0].language == ["python", "typescript"]


def test_load_rules_no_language_key(tmp_path):
    """load_rules() sets language to None when key is absent."""
    config_path = _write_config(tmp_path, {
        "rules": {
            "no-print": {
                "find": "print($X)",
                "message": "No print",
            },
        },
    })

    rules, _macros, _deadcode = load_rules(config_path)

    assert len(rules) == 1
    assert rules[0].language is None


# --- Integration: mixed-language rules in same config ---


def test_mixed_language_rules_in_same_config(tmp_path):
    """Config with rules for different languages applies each to the right files."""
    py_file = tmp_path / "app.py"
    py_file.write_text("print('hello')\n")

    ts_file = tmp_path / "app.ts"
    ts_file.write_text("console.log('world')\n")

    config_path = _write_config(tmp_path, {
        "rules": {
            "no-console": {
                "find": "console.log($X)",
                "message": "No console.log",
                "language": "typescript",
            },
            "no-print": {
                "find": "print($X)",
                "message": "No print",
                "language": "python",
            },
        },
    })

    rules, _macros, _deadcode = load_rules(config_path)
    violations = run_lint(rules, [str(py_file), str(ts_file)])

    no_print_files = {v.file_path for v in violations if v.rule_name == "no-print"}
    no_console_files = {v.file_path for v in violations if v.rule_name == "no-console"}

    assert no_print_files == {str(py_file)}
    assert no_console_files == {str(ts_file)}
