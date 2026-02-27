"""Tests for lint command with pattern macros."""

import pytest
import yaml

from emend.lint import (
    load_rules,
    expand_macros,
    LintRule,
    LintViolation,
    run_lint,
    parse_noqa_comments,
)


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
    rules, macros, _ = load_rules(str(config_file))
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
    rules, macros, _ = load_rules(str(config_file))
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


# --- parse_noqa_comments unit tests ---


def test_parse_noqa_bare():
    """Bare # noqa returns None (suppress all)."""
    result = parse_noqa_comments("x = 1  # noqa\n")
    assert result == {1: None}


def test_parse_noqa_specific_emend_rule():
    """# noqa: emend:rule-name extracts rule name."""
    result = parse_noqa_comments("x = 1  # noqa: emend:no-print\n")
    assert result == {1: {"no-print"}}


def test_parse_noqa_multiple_emend_rules():
    """# noqa: emend:r1, emend:r2 extracts both rules."""
    result = parse_noqa_comments("x = 1  # noqa: emend:no-print, emend:no-assert\n")
    assert result == {1: {"no-print", "no-assert"}}


def test_parse_noqa_mixed_only_emend():
    """# noqa: E501, emend:no-print only picks up emend-prefixed rules."""
    result = parse_noqa_comments("x = 1  # noqa: E501, emend:no-print\n")
    assert result == {1: {"no-print"}}


def test_parse_noqa_non_emend_only():
    """# noqa: E501 alone has no effect on emend (not in result)."""
    result = parse_noqa_comments("x = 1  # noqa: E501\n")
    assert result == {}


def test_parse_noqa_inside_string():
    """# noqa inside a string literal is NOT detected."""
    result = parse_noqa_comments('x = "# noqa"\n')
    assert result == {}


def test_parse_noqa_case_insensitive():
    """# NOQA and # Noqa work too."""
    result = parse_noqa_comments("x = 1  # NOQA\n")
    assert result == {1: None}

    result2 = parse_noqa_comments("x = 1  # Noqa: emend:my-rule\n")
    assert result2 == {1: {"my-rule"}}


def test_parse_noqa_spacing_variations():
    """Various spacing around noqa is handled."""
    result = parse_noqa_comments("x = 1  #noqa\n")
    assert result == {1: None}

    result2 = parse_noqa_comments("x = 1  #  noqa:emend:r1\n")
    assert result2 == {1: {"r1"}}


# --- Integration tests (find mode) ---


def test_noqa_suppresses_all_rules(tmp_path):
    """Bare # noqa suppresses all lint rules on that line."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "print('hello')  # noqa\n"
        "print('world')\n"
    )

    rules = [
        LintRule(
            name="no-print",
            find="print($...ARGS)",
            message="No print",
        ),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 1
    assert violations[0].line == 2


def test_noqa_suppresses_specific_rule(tmp_path):
    """# noqa: emend:rule-name suppresses only that rule."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "print('hello')  # noqa: emend:no-print\n"
        "assert x == 1\n"
    )

    rules = [
        LintRule(name="no-print", find="print($...ARGS)", message="No print"),
        LintRule(name="no-assert", find="assert $X", message="No assert"),
    ]

    violations = run_lint(rules, [str(test_file)])
    # print suppressed by noqa, assert not suppressed
    assert len(violations) == 1
    assert violations[0].rule_name == "no-assert"


def test_noqa_specific_rule_does_not_suppress_other(tmp_path):
    """# noqa: emend:no-print does NOT suppress no-assert on the same line."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "print('hello')  # noqa: emend:other-rule\n"
    )

    rules = [
        LintRule(name="no-print", find="print($...ARGS)", message="No print"),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 1
    assert violations[0].rule_name == "no-print"


def test_noqa_partial_suppression(tmp_path):
    """One line noqa'd, another not — only unsuppressed line reported."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "print('a')  # noqa: emend:no-print\n"
        "x = 1\n"
        "print('b')\n"
    )

    rules = [
        LintRule(name="no-print", find="print($...ARGS)", message="No print"),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 1
    assert violations[0].line == 3


def test_noqa_multiline_statement(tmp_path):
    """# noqa on a multi-line statement suppresses matches on inner lines."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "result = (  # noqa: emend:no-print\n"
        "    print('hello')\n"
        ")\n"
    )

    rules = [
        LintRule(name="no-print", find="print($...ARGS)", message="No print"),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 0


def test_noqa_inside_string_not_suppressed(tmp_path):
    """# noqa inside a string literal does NOT suppress violations."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        'x = "# noqa"\n'
        "print('hello')\n"
    )

    rules = [
        LintRule(name="no-print", find="print($...ARGS)", message="No print"),
    ]

    violations = run_lint(rules, [str(test_file)])
    assert len(violations) == 1
    assert violations[0].line == 2


# --- Integration tests (fix mode) ---


def test_noqa_fix_suppresses_line(tmp_path):
    """# noqa prevents fix on that line; other lines still fixed."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "print('keep')  # noqa\n"
        "print('fix')\n"
    )

    rules = [
        LintRule(
            name="no-print",
            find="print($...ARGS)",
            message="No print",
            replace="logger.info($...ARGS)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)], fix=True)
    content = test_file.read_text()
    assert "print('keep')  # noqa" in content
    assert "logger.info('fix')" in content
    assert "print('fix')" not in content


def test_noqa_fix_all_suppressed(tmp_path):
    """When all matches are suppressed, file is unchanged."""
    test_file = tmp_path / "example.py"
    original = "print('a')  # noqa\nprint('b')  # noqa\n"
    test_file.write_text(original)

    rules = [
        LintRule(
            name="no-print",
            find="print($...ARGS)",
            message="No print",
            replace="logger.info($...ARGS)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)], fix=True)
    assert test_file.read_text() == original
    assert len(violations) == 0


def test_noqa_fix_no_noqa_unchanged(tmp_path):
    """Existing fix behavior unchanged when no noqa comments present."""
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
            message="No print",
            replace="logger.info($...ARGS)",
        ),
    ]

    violations = run_lint(rules, [str(test_file)], fix=True)
    assert len(violations) == 1
    content = test_file.read_text()
    assert "logger.info('hello')" in content
    assert "print('hello')" not in content


def test_lint_fix_union_to_pipe_simple(tmp_path):
    """lint --fix converts Union[X, Y] to X | Y (no string args)."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "from typing import Union\n"
        "x: Union[int, str]\n"
        "y: Union[float, bool]\n"
    )

    rules = [
        LintRule(
            name="union-to-pipe",
            find="Union[$X:!str, $Y:!str]",
            message="Use X | Y syntax instead of Union[X, Y]",
            replace="$X | $Y",
        ),
    ]

    violations = run_lint(rules, [str(test_file)], fix=True)
    assert len(violations) == 1
    content = test_file.read_text()
    assert "int | str" in content
    assert "float | bool" in content
    assert "Union[" not in content


def test_lint_fix_union_to_pipe_deferred_string_first(tmp_path):
    """lint --fix converts Union['MyClass', int] to 'MyClass | int'."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "from typing import Union\n"
        'x: Union["MyClass", int]\n'
    )

    rules = [
        LintRule(
            name="union-with-deferred",
            find="Union[$X:str, $Y]",
            message="Union with deferred annotation",
            replace='"${X.content} | $Y"',
        ),
    ]

    violations = run_lint(rules, [str(test_file)], fix=True)
    assert len(violations) == 1
    content = test_file.read_text()
    assert '"MyClass | int"' in content
    assert "Union[" not in content


def test_lint_fix_union_to_pipe_deferred_string_second(tmp_path):
    """lint --fix converts Union[int, 'MyClass'] to 'int | MyClass'."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "from typing import Union\n"
        'x: Union[int, "MyClass"]\n'
    )

    rules = [
        LintRule(
            name="union-with-deferred-2",
            find="Union[$X, $Y:str]",
            message="Union with deferred annotation",
            replace='"$X | ${Y.content}"',
        ),
    ]

    violations = run_lint(rules, [str(test_file)], fix=True)
    assert len(violations) == 1
    content = test_file.read_text()
    assert '"int | MyClass"' in content
    assert "Union[" not in content


def test_lint_fix_union_to_pipe_both_deferred(tmp_path):
    """lint --fix converts Union['Foo', 'Bar'] to 'Foo | Bar'."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "from typing import Union\n"
        'x: Union["Foo", "Bar"]\n'
    )

    rules = [
        LintRule(
            name="union-both-deferred",
            find="Union[$X:str, $Y:str]",
            message="Union with both deferred",
            replace='"${X.content} | ${Y.content}"',
        ),
    ]

    violations = run_lint(rules, [str(test_file)], fix=True)
    assert len(violations) == 1
    content = test_file.read_text()
    assert '"Foo | Bar"' in content
    assert "Union[" not in content


def test_lint_union_to_pipe_config_file(tmp_path):
    """Union-to-pipe rules work end-to-end loaded from a YAML config."""
    test_file = tmp_path / "example.py"
    test_file.write_text(
        "from typing import Union\n"
        "a: Union[int, str]\n"
        'b: Union["MyClass", int]\n'
    )

    config = {
        "rules": {
            "union-to-pipe": {
                "find": "Union[$X:!str, $Y:!str]",
                "message": "Use X | Y syntax instead of Union[X, Y]",
                "replace": "$X | $Y",
            },
            "union-to-pipe-deferred": {
                "find": "Union[$X:str, $Y]",
                "message": "Union with deferred annotation",
                "replace": '"${X.content} | $Y"',
            },
        },
    }

    config_file = _write_config(tmp_path, config)
    rules, macros, _ = load_rules(str(config_file))

    violations = run_lint(rules, [str(test_file)], fix=True)
    assert len(violations) >= 1
    content = test_file.read_text()
    assert "int | str" in content
    assert '"MyClass | int"' in content
