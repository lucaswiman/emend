"""Tests for the dead-code detection command."""
import json
from pathlib import Path

import pytest
import yaml

from emend.component_selector import ExtendedSelector


class TestFindDeadCode:
    """Tests for find_dead_code() in transform.py."""

    def test_finds_unreferenced_function(self, tmp_path):
        """A function with no references is flagged as dead."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def used_func():\n"
            "    return 1\n"
            "\n"
            "def unused_func():\n"
            "    return 2\n"
            "\n"
            "result = used_func()\n"
        )

        dead = list(find_dead_code(str(project)))
        dead_names = {d.name for d in dead}
        assert "unused_func" in dead_names
        assert "used_func" not in dead_names

    def test_finds_unreferenced_class(self, tmp_path):
        """A class with no references is flagged as dead."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "class UsedClass:\n"
            "    pass\n"
            "\n"
            "class UnusedClass:\n"
            "    pass\n"
            "\n"
            "obj = UsedClass()\n"
        )

        dead = list(find_dead_code(str(project)))
        dead_names = {d.name for d in dead}
        assert "UnusedClass" in dead_names
        assert "UsedClass" not in dead_names

    def test_cross_file_reference_not_dead(self, tmp_path):
        """A function referenced from another file is not dead."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        lib_file = project / "lib.py"
        lib_file.write_text(
            "def helper():\n"
            "    return 42\n"
            "\n"
            "def orphan():\n"
            "    return 0\n"
        )

        user_file = project / "user.py"
        user_file.write_text(
            "from lib import helper\n"
            "\n"
            "result = helper()\n"
        )

        dead = list(find_dead_code(str(project)))
        dead_names = {d.name for d in dead}
        assert "orphan" in dead_names
        assert "helper" not in dead_names

    def test_skips_dunder_methods(self, tmp_path):
        """Dunder methods like __init__ are never flagged."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def __init__():\n"
            "    pass\n"
        )

        dead = list(find_dead_code(str(project)))
        dead_names = {d.name for d in dead}
        assert "__init__" not in dead_names

    def test_skips_test_functions(self, tmp_path):
        """Functions starting with test_ are never flagged."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        test_file = project / "test_example.py"
        test_file.write_text(
            "def test_something():\n"
            "    assert True\n"
            "\n"
            "class TestSuite:\n"
            "    pass\n"
        )

        dead = list(find_dead_code(str(project)))
        dead_names = {d.name for d in dead}
        assert "test_something" not in dead_names
        assert "TestSuite" not in dead_names

    def test_skips_main(self, tmp_path):
        """The 'main' function is never flagged."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def main():\n"
            "    pass\n"
        )

        dead = list(find_dead_code(str(project)))
        dead_names = {d.name for d in dead}
        assert "main" not in dead_names

    def test_skips_private_by_default(self, tmp_path):
        """Private symbols (_name) are skipped by default."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def _private_helper():\n"
            "    return 1\n"
            "\n"
            "def public_unused():\n"
            "    return 2\n"
        )

        dead = list(find_dead_code(str(project)))
        dead_names = {d.name for d in dead}
        assert "_private_helper" not in dead_names
        assert "public_unused" in dead_names

    def test_include_private(self, tmp_path):
        """With include_private=True, _private symbols are checked."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def _private_helper():\n"
            "    return 1\n"
        )

        dead = list(find_dead_code(str(project), include_private=True))
        dead_names = {d.name for d in dead}
        assert "_private_helper" in dead_names

    def test_skips_all_exports(self, tmp_path):
        """Symbols listed in __all__ are not flagged."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "__all__ = ['exported_func']\n"
            "\n"
            "def exported_func():\n"
            "    return 1\n"
            "\n"
            "def not_exported():\n"
            "    return 2\n"
        )

        dead = list(find_dead_code(str(project)))
        dead_names = {d.name for d in dead}
        assert "exported_func" not in dead_names
        assert "not_exported" in dead_names

    def test_kind_filter_function(self, tmp_path):
        """With kind='function', only functions are checked."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def unused_func():\n"
            "    return 1\n"
            "\n"
            "class UnusedClass:\n"
            "    pass\n"
        )

        dead = list(find_dead_code(str(project), kind="function"))
        dead_names = {d.name for d in dead}
        assert "unused_func" in dead_names
        assert "UnusedClass" not in dead_names

    def test_kind_filter_class(self, tmp_path):
        """With kind='class', only classes are checked."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def unused_func():\n"
            "    return 1\n"
            "\n"
            "class UnusedClass:\n"
            "    pass\n"
        )

        dead = list(find_dead_code(str(project), kind="class"))
        dead_names = {d.name for d in dead}
        assert "UnusedClass" in dead_names
        assert "unused_func" not in dead_names

    def test_returns_correct_fields(self, tmp_path):
        """DeadSymbol has correct file_path, name, kind, line, selector."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def orphan():\n"
            "    return 42\n"
        )

        dead = list(find_dead_code(str(project)))
        assert len(dead) == 1
        d = dead[0]
        assert d.name == "orphan"
        assert d.kind == "function"
        assert d.line == 1
        assert "main.py" in d.file_path
        assert "orphan" in d.selector
        assert d.reason == "no references found"

    def test_empty_project(self, tmp_path):
        """An empty project returns no dead code."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        dead = list(find_dead_code(str(project)))
        assert dead == []

    def test_skips_decorated_entry_points(self, tmp_path):
        """Functions decorated with framework decorators are skipped."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def route(f):\n"
            "    return f\n"
            "\n"
            "@route\n"
            "def my_handler():\n"
            "    return 'hello'\n"
            "\n"
            "def truly_unused():\n"
            "    return 'bye'\n"
        )

        dead = list(find_dead_code(str(project)))
        dead_names = {d.name for d in dead}
        assert "my_handler" not in dead_names
        assert "truly_unused" in dead_names

    def test_skips_fastapi_router_decorators(self, tmp_path):
        """FastAPI-style @router.get/post/etc decorators are skipped."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "class Router:\n"
            "    def get(self, path): return lambda f: f\n"
            "    def post(self, path): return lambda f: f\n"
            "\n"
            "router = Router()\n"
            "\n"
            "@router.get('/users')\n"
            "def list_users():\n"
            "    return []\n"
            "\n"
            "@router.post('/users')\n"
            "def create_user():\n"
            "    return {}\n"
            "\n"
            "def truly_unused():\n"
            "    return 'bye'\n"
        )

        dead = list(find_dead_code(str(project)))
        dead_names = {d.name for d in dead}
        assert "list_users" not in dead_names
        assert "create_user" not in dead_names
        assert "truly_unused" in dead_names

    def test_sorted_output(self, tmp_path):
        """Results are sorted by file path then line number."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        file_b = project / "b.py"
        file_b.write_text(
            "def zeta():\n"
            "    pass\n"
            "\n"
            "def alpha():\n"
            "    pass\n"
        )

        file_a = project / "a.py"
        file_a.write_text(
            "def gamma():\n"
            "    pass\n"
        )

        dead = list(find_dead_code(str(project)))
        # Should be sorted: a.py first, then b.py, then by line within file
        assert len(dead) >= 3
        file_paths = [d.file_path for d in dead]
        assert file_paths == sorted(file_paths) or all(
            (file_paths[i], dead[i].line) <= (file_paths[i + 1], dead[i + 1].line)
            for i in range(len(dead) - 1)
        )

    def test_only_top_level_symbols(self, tmp_path):
        """Only top-level symbols are checked, not nested methods."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "class MyClass:\n"
            "    def unused_method(self):\n"
            "        pass\n"
            "\n"
            "obj = MyClass()\n"
        )

        dead = list(find_dead_code(str(project)))
        dead_names = {d.name for d in dead}
        # Methods are nested (depth > 1), so should not be checked
        assert "unused_method" not in dead_names
        # MyClass is used, so not dead
        assert "MyClass" not in dead_names


class TestDeadCodeCLI:
    """Tests for the dead-code CLI command."""

    def test_cli_finds_dead_code(self, tmp_path, run_emend_cmd):
        """CLI command reports dead code."""
        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def used_func():\n"
            "    return 1\n"
            "\n"
            "def dead_func():\n"
            "    return 2\n"
            "\n"
            "result = used_func()\n"
        )

        result = run_emend_cmd(["deadcode", str(project)])
        assert "dead_func" in result.stdout
        assert "used_func" not in result.stdout

    def test_cli_json_output(self, tmp_path, run_emend_cmd):
        """CLI --json produces valid JSON output."""
        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def orphan():\n"
            "    return 42\n"
        )

        result = run_emend_cmd(["deadcode", str(project), "--json"])
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "orphan"
        assert data[0]["kind"] == "function"
        assert data[0]["line"] == 1

    def test_cli_kind_filter(self, tmp_path, run_emend_cmd):
        """CLI --kind filters to specific symbol types."""
        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def unused_func():\n"
            "    pass\n"
            "\n"
            "class UnusedClass:\n"
            "    pass\n"
        )

        result = run_emend_cmd(["deadcode", str(project), "--kind", "function"])
        assert "unused_func" in result.stdout
        assert "UnusedClass" not in result.stdout

    def test_cli_no_dead_code(self, tmp_path, run_emend_cmd):
        """CLI reports 'No dead code found.' when everything is used."""
        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def helper():\n"
            "    return 1\n"
            "\n"
            "result = helper()\n"
        )

        result = run_emend_cmd(["deadcode", str(project)])
        assert "No dead code found" in result.stdout

    def test_cli_include_private(self, tmp_path, run_emend_cmd):
        """CLI --include-private flag includes private symbols."""
        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def _private_unused():\n"
            "    return 1\n"
        )

        # Without --include-private
        result = run_emend_cmd(["deadcode", str(project)])
        assert "_private_unused" not in result.stdout

        # With --include-private
        result = run_emend_cmd(["deadcode", str(project), "--include-private"])
        assert "_private_unused" in result.stdout


class TestExcludeReferencesFrom:
    """Tests for --exclude-references-from."""

    def test_exclude_references_from_directory(self, tmp_path):
        """References in excluded directories are ignored."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        tests_dir = project / "tests"
        tests_dir.mkdir()

        lib_file = project / "lib.py"
        lib_file.write_text(
            "def only_tested():\n"
            "    return 42\n"
        )

        test_file = tests_dir / "test_lib.py"
        test_file.write_text(
            "from lib import only_tested\n"
            "\n"
            "def test_it():\n"
            "    assert only_tested() == 42\n"
        )

        # Without exclusion: not dead (test references it)
        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead}
        assert "only_tested" not in dead_names

        # With exclusion: dead (test dir references are ignored)
        dead = list(find_dead_code(
            str(project),
            exclude_references_from=[str(tests_dir)],
            show_last_reference=False,
        ))
        dead_names = {d.name for d in dead}
        assert "only_tested" in dead_names

    def test_cli_exclude_references_from(self, tmp_path, run_emend_cmd):
        """CLI --exclude-references-from works."""
        project = tmp_path / "project"
        project.mkdir()
        tests_dir = project / "tests"
        tests_dir.mkdir()

        lib_file = project / "lib.py"
        lib_file.write_text(
            "def only_tested():\n"
            "    return 42\n"
        )

        test_file = tests_dir / "test_lib.py"
        test_file.write_text(
            "from lib import only_tested\n"
            "\n"
            "def test_it():\n"
            "    assert only_tested() == 42\n"
        )

        result = run_emend_cmd([
            "deadcode", str(project),
            "--exclude-references-from", str(tests_dir),
            "--no-last-reference",
        ])
        assert "only_tested" in result.stdout


class TestStringsCountAsReferences:
    """Tests for --strings-count-as-references / --no-strings."""

    def test_string_literal_counts_as_reference(self, tmp_path):
        """String containing symbol name prevents dead-code flagging."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def dynamic_handler():\n"
            "    return 'handled'\n"
            "\n"
            "registry = {'dynamic_handler': True}\n"
        )

        # With strings (default): not flagged
        dead = list(find_dead_code(
            str(project), strings_count_as_references=True,
            show_last_reference=False,
        ))
        dead_names = {d.name for d in dead}
        assert "dynamic_handler" not in dead_names

        # Without strings: flagged
        dead = list(find_dead_code(
            str(project), strings_count_as_references=False,
            show_last_reference=False,
        ))
        dead_names = {d.name for d in dead}
        assert "dynamic_handler" in dead_names

    def test_short_names_not_string_matched(self, tmp_path):
        """Names <= 3 chars are not matched in strings to avoid noise."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def foo():\n"
            "    return 1\n"
            "\n"
            "x = 'foo bar'\n"
        )

        dead = list(find_dead_code(
            str(project), strings_count_as_references=True,
            show_last_reference=False,
        ))
        dead_names = {d.name for d in dead}
        # "foo" is only 3 chars — string matching should not protect it
        assert "foo" in dead_names

    def test_cli_no_strings(self, tmp_path, run_emend_cmd):
        """CLI --no-strings disables string-based reference detection."""
        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def dynamic_handler():\n"
            "    return 'handled'\n"
            "\n"
            "registry = {'dynamic_handler': True}\n"
        )

        # Default: not flagged because string contains the name
        result = run_emend_cmd([
            "deadcode", str(project), "--no-last-reference",
        ])
        assert "dynamic_handler" not in result.stdout or "No dead code" in result.stdout

        # --no-strings: flagged
        result = run_emend_cmd([
            "deadcode", str(project), "--no-strings", "--no-last-reference",
        ])
        assert "dynamic_handler" in result.stdout


class TestNoqaDeadcode:
    """Tests for # noqa: emend:deadcode annotation."""

    def test_noqa_suppresses_deadcode(self, tmp_path):
        """# noqa: emend:deadcode on the definition line suppresses flagging."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def suppressed():  # noqa: emend:deadcode\n"
            "    return 1\n"
            "\n"
            "def not_suppressed():\n"
            "    return 2\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead}
        assert "suppressed" not in dead_names
        assert "not_suppressed" in dead_names

    def test_bare_noqa_also_suppresses(self, tmp_path):
        """A bare # noqa suppresses all rules including deadcode."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def suppressed():  # noqa\n"
            "    return 1\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead}
        assert "suppressed" not in dead_names


class TestShowLastReference:
    """Tests for --show-last-reference."""

    def test_last_reference_disabled(self, tmp_path):
        """With show_last_reference=False, no git info is attached."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def orphan():\n"
            "    return 42\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        assert len(dead) == 1
        assert dead[0].last_reference_commit is None

    def test_last_reference_in_git_repo(self, tmp_path):
        """In a git repo, last_reference_commit is populated."""
        import subprocess
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        env = {
            "HOME": str(tmp_path),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
            "PATH": "/usr/bin:/bin",
        }
        subprocess.run(["git", "init"], cwd=str(project),
                        capture_output=True, check=True, env=env)

        main_file = project / "main.py"
        main_file.write_text(
            "def orphan():\n"
            "    return 42\n"
        )
        subprocess.run(["git", "add", "."], cwd=str(project),
                        capture_output=True, check=True, env=env)
        subprocess.run(["git", "commit", "-m", "initial"],
                        cwd=str(project), capture_output=True, check=True,
                        env=env)

        dead = list(find_dead_code(str(project), show_last_reference=True))
        assert len(dead) == 1
        assert dead[0].last_reference_commit is not None
        assert "initial" in dead[0].last_reference_commit


class TestDeadCodeLint:
    """Tests for deadcode integration in the lint engine."""

    def test_lint_deadcode_config(self, tmp_path):
        """Lint config with deadcode section triggers dead code analysis."""
        from emend.lint import load_rules, run_lint, DeadCodeConfig

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def orphan_func():\n"
            "    return 42\n"
        )

        config_file = project / ".emend" / "patterns.yaml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(yaml.dump({
            "deadcode": {
                "enabled": True,
            },
        }))

        rules, macros, dc_config = load_rules(str(config_file))
        assert dc_config is not None
        assert dc_config.enabled is True

        violations = run_lint(
            rules, [str(main_file)],
            deadcode_config=dc_config,
            project_path=str(project),
        )

        dc_violations = [v for v in violations if v.rule_name == "deadcode"]
        assert len(dc_violations) == 1
        assert "orphan_func" in dc_violations[0].message

    def test_lint_deadcode_boolean_shorthand(self, tmp_path):
        """deadcode: true in config enables with defaults."""
        from emend.lint import load_rules

        config_file = tmp_path / "patterns.yaml"
        config_file.write_text(yaml.dump({
            "deadcode": True,
        }))

        rules, macros, dc_config = load_rules(str(config_file))
        assert dc_config is not None
        assert dc_config.enabled is True

    def test_lint_deadcode_with_options(self, tmp_path):
        """deadcode config supports all options."""
        from emend.lint import load_rules

        config_file = tmp_path / "patterns.yaml"
        config_file.write_text(yaml.dump({
            "deadcode": {
                "enabled": True,
                "kind": "function",
                "include-private": True,
                "exclude-references-from": ["tests/"],
                "strings-count-as-references": False,
                "message": "Custom dead code message",
            },
        }))

        rules, macros, dc_config = load_rules(str(config_file))
        assert dc_config.kind == "function"
        assert dc_config.include_private is True
        assert dc_config.exclude_references_from == ["tests/"]
        assert dc_config.strings_count_as_references is False
        assert dc_config.message == "Custom dead code message"

    def test_lint_deadcode_disabled(self, tmp_path):
        """deadcode: {enabled: false} does not run analysis."""
        from emend.lint import load_rules, run_lint

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def orphan_func():\n"
            "    return 42\n"
        )

        config_file = project / ".emend" / "patterns.yaml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(yaml.dump({
            "deadcode": {"enabled": False},
        }))

        rules, macros, dc_config = load_rules(str(config_file))
        violations = run_lint(
            rules, [str(main_file)],
            deadcode_config=dc_config,
            project_path=str(project),
        )

        dc_violations = [v for v in violations if v.rule_name == "deadcode"]
        assert len(dc_violations) == 0

    def test_lint_deadcode_noqa_suppresses(self, tmp_path):
        """# noqa: emend:deadcode suppresses lint violations too."""
        from emend.lint import load_rules, run_lint

        project = tmp_path / "project"
        project.mkdir()

        main_file = project / "main.py"
        main_file.write_text(
            "def suppressed():  # noqa: emend:deadcode\n"
            "    return 1\n"
            "\n"
            "def flagged():\n"
            "    return 2\n"
        )

        config_file = project / ".emend" / "patterns.yaml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(yaml.dump({
            "deadcode": True,
        }))

        rules, macros, dc_config = load_rules(str(config_file))
        violations = run_lint(
            rules, [str(main_file)],
            deadcode_config=dc_config,
            project_path=str(project),
        )

        dc_violations = [v for v in violations if v.rule_name == "deadcode"]
        names = [v.message for v in dc_violations]
        assert any("flagged" in m for m in names)
        assert not any("suppressed" in m for m in names)
