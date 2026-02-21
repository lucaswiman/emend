"""Tests for lint command with pattern macros."""

import pytest
import yaml

from emend.lint import load_rules, expand_macros, LintRule, LintViolation, run_lint


def _write_config(tmp_path, config_dict):
    """Helper to write a YAML config file."""
    config_file = tmp_path / ".emend" / "patterns.yaml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(yaml.dump(config_dict))
    return config_file


def test_lint_basic_rule(tmp_path):
    """lint reports violations for basic pattern rules."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "x = 1\n"
        "print('hello')\n"
        "y = 2\n"
        "print('world')\n"
    )

    rules = [
        LintRule(
            name="no-print",
            find="print($...ARGS)",
            message="Use logger instead of print",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 2
    assert all(v.rule_name == "no-print" for v in violations)
    assert all(v.message == "Use logger instead of print" for v in violations)
    assert violations[0].line == 2
    assert violations[1].line == 4
    assert all(v.file_path == str(test_file) for v in violations)


def test_lint_macro_expansion(tmp_path):
    """lint expands macros in rule patterns."""
    macros = {
        "api_call": "requests.$METHOD($URL, $...KWARGS)",
    }

    pattern = "{api_call}"
    expanded = expand_macros(pattern, macros)
    assert expanded == "requests.$METHOD($URL, $...KWARGS)"

    # Multiple macros in one pattern
    macros2 = {
        "print_call": "print($...ARGS)",
    }
    pattern2 = "{print_call}"
    expanded2 = expand_macros(pattern2, macros2)
    assert expanded2 == "print($...ARGS)"


def test_lint_macro_in_rules(tmp_path):
    """lint rule patterns use macro expansion from config."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "print('debug info')\n"
        "x = 1\n"
    )

    config = {
        "macros": {
            "print_call": "print($...ARGS)",
        },
        "rules": {
            "no-print": {
                "find": "{print_call}",
                "message": "Use logger instead of print",
            },
        },
    }

    config_file = _write_config(tmp_path, config)
    rules, macros = load_rules(str(config_file))
    assert len(rules) == 1
    assert rules[0].find == "print($...ARGS)"

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 1
    assert violations[0].rule_name == "no-print"


def test_lint_not_inside(tmp_path):
    """lint respects not-inside constraints."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "def process():\n"
        "    print('processing')\n"
        "\n"
        "def test_process():\n"
        "    print('test output')\n"
    )

    rules = [
        LintRule(
            name="no-print",
            find="print($...ARGS)",
            message="Use logger instead of print",
            not_inside="def",
        ),
    ]

    # not_inside="def" means find prints NOT inside any def — module level only
    # Both prints are inside def, so no violations
    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 0

    # Now test with a module-level print
    test_file2 = tmp_path / "example2.py"
    test_file2.write_text(
        "print('module level')\n"
        "def func():\n"
        "    print('in func')\n"
    )

    violations2 = run_lint(rules, [str(test_file2)])
    assert len(violations2) == 1
    assert violations2[0].line == 1


def test_lint_fix(tmp_path):
    """lint --fix applies replace rules."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "x = 1\n"
        "print('hello')\n"
        "y = 2\n"
    )

    rules = [
        LintRule(
            name="no-print",
            find="print($...ARGS)",
            message="Use logger instead of print",
            replace="logger.info($...ARGS)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)], fix=True)
    # Should still report violations even when fixing
    assert len(violations) == 1
    assert violations[0].rule_name == "no-print"

    # File should be modified
    content = test_file.read_text()
    assert "logger.info('hello')" in content
    assert "print('hello')" not in content
    # Other lines untouched
    assert "x = 1" in content
    assert "y = 2" in content


def test_lint_custom_config(tmp_path):
    """lint reads config from --config path."""
    test_file = tmp_path / "code.py"
    test_file.write_text("assert x == 1\n")

    config = {
        "rules": {
            "no-bare-assert": {
                "find": "assert $X",
                "message": "Use pytest assertions instead",
            },
        },
    }

    config_file = _write_config(tmp_path, config)
    rules, macros = load_rules(str(config_file))
    assert len(rules) == 1
    assert rules[0].name == "no-bare-assert"
    assert rules[0].message == "Use pytest assertions instead"

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 1
    assert violations[0].rule_name == "no-bare-assert"


def test_lint_multiple_files(tmp_path):
    """lint processes multiple files."""
    file1 = tmp_path / "a.py"
    file1.write_text("print('a')\n")

    file2 = tmp_path / "b.py"
    file2.write_text("print('b')\nx = 1\nprint('c')\n")

    rules = [
        LintRule(
            name="no-print",
            find="print($...ARGS)",
            message="No print",
        ),
    ]

    violations = run_lint(rules, [str(file1), str(file2)])
    assert len(violations) == 3


def test_lint_multiple_rules(tmp_path):
    """lint can check multiple rules at once."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "print('debug')\n"
        "assert x == 1\n"
    )

    rules = [
        LintRule(
            name="no-print",
            find="print($...ARGS)",
            message="No print",
        ),
        LintRule(
            name="no-bare-assert",
            find="assert $X",
            message="Use pytest assertions",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 2
    rule_names = {v.rule_name for v in violations}
    assert rule_names == {"no-print", "no-bare-assert"}


def test_lint_no_violations(tmp_path):
    """lint returns empty list when no violations."""
    test_file = tmp_path / "clean.py"
    test_file.write_text("x = 1\ny = 2\n")

    rules = [
        LintRule(
            name="no-print",
            find="print($...ARGS)",
            message="No print",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 0


def test_lint_filter_by_rule(tmp_path):
    """run_lint can filter to only specific rules."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "print('debug')\n"
        "assert x == 1\n"
    )

    rules = [
        LintRule(
            name="no-print",
            find="print($...ARGS)",
            message="No print",
        ),
        LintRule(
            name="no-bare-assert",
            find="assert $X",
            message="Use pytest assertions",
        ),
    ]

    violations = run_lint(rules, [str(test_file)], rule_filter="no-print")
    assert len(violations) == 1
    assert violations[0].rule_name == "no-print"


def test_load_rules_missing_file():
    """load_rules raises error for missing config file."""
    with pytest.raises(FileNotFoundError):
        load_rules("/nonexistent/path/patterns.yaml")


def test_lint_violation_fields(tmp_path):
    """LintViolation contains all expected fields."""
    test_file = tmp_path / "example.py"
    test_file.write_text("print('hello')\n")

    rules = [
        LintRule(
            name="no-print",
            find="print($...ARGS)",
            message="Use logger instead",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 1
    v = violations[0]
    assert v.rule_name == "no-print"
    assert v.message == "Use logger instead"
    assert v.file_path == str(test_file)
    assert v.line == 1
    assert "print" in v.match_text
