"""Tests for the callers command."""
import json
import pytest

from emend.component_selector import ExtendedSelector


class TestFindCallers:
    """Tests for find_callers() in transform.py."""

    def test_callers_finds_direct_calls(self, tmp_path):
        """callers finds functions that directly call the target."""
        from emend.transform import find_callers

        project = tmp_path / "project"
        project.mkdir()

        target = project / "target.py"
        target.write_text(
            "def process(x):\n"
            "    return x + 1\n"
        )

        caller = project / "caller.py"
        caller.write_text(
            "from target import process\n"
            "\n"
            "def run():\n"
            "    return process(42)\n"
        )

        selector = ExtendedSelector(
            file_path=str(target),
            symbol_path=["process"],
            component=None,
            accessor=None,
        )

        callers = find_callers(selector, project_path=str(project))
        # Should find the call in caller.py
        caller_files = {r.file_path for r in callers}
        assert str(caller) in caller_files or any(
            str(caller.resolve()) == str(Path(f).resolve())
            for f in caller_files
        ), f"Expected caller.py in callers, got {caller_files}"

    def test_callers_excludes_non_call_references(self, tmp_path):
        """callers should not include mere references (not calls)."""
        from emend.transform import find_callers

        project = tmp_path / "project"
        project.mkdir()

        target = project / "target.py"
        target.write_text(
            "def process(x):\n"
            "    return x + 1\n"
        )

        ref_file = project / "ref.py"
        ref_file.write_text(
            "from target import process\n"
            "\n"
            "# Just referencing, not calling\n"
            "func_ref = process\n"
        )

        caller_file = project / "caller.py"
        caller_file.write_text(
            "from target import process\n"
            "\n"
            "result = process(10)\n"
        )

        selector = ExtendedSelector(
            file_path=str(target),
            symbol_path=["process"],
            component=None,
            accessor=None,
        )

        callers = find_callers(selector, project_path=str(project))
        caller_files = {r.file_path for r in callers}
        # caller.py should be there (actual call)
        assert any("caller.py" in f for f in caller_files)
        # ref.py should NOT be there (just a reference, not a call)
        assert not any("ref.py" in f for f in caller_files), \
            "ref.py only references process, doesn't call it"

    def test_callers_scope_aware(self, tmp_path):
        """callers ignores same-named functions in different scopes."""
        from emend.transform import find_callers

        project = tmp_path / "project"
        project.mkdir()

        file_a = project / "file_a.py"
        file_a.write_text(
            "def process():\n"
            "    return 'A'\n"
        )

        file_b = project / "file_b.py"
        file_b.write_text(
            "def process():\n"
            "    return 'B'\n"
            "\n"
            "def run():\n"
            "    # This calls file_b's own process, not file_a's\n"
            "    return process()\n"
        )

        file_c = project / "file_c.py"
        file_c.write_text(
            "from file_a import process\n"
            "\n"
            "def invoke():\n"
            "    return process()\n"
        )

        selector = ExtendedSelector(
            file_path=str(file_a),
            symbol_path=["process"],
            component=None,
            accessor=None,
        )

        callers = find_callers(selector, project_path=str(project))
        caller_files = {r.file_path for r in callers}
        # file_c calls file_a's process
        assert any("file_c.py" in f for f in caller_files)
        # file_b calls its own process, not file_a's
        assert not any("file_b.py" in f for f in caller_files), \
            "file_b has its own process() -- should not appear"

    def test_callers_in_same_file(self, tmp_path):
        """callers finds calls to the target within its own file."""
        from emend.transform import find_callers

        project = tmp_path / "project"
        project.mkdir()

        target = project / "module.py"
        target.write_text(
            "def helper():\n"
            "    return 42\n"
            "\n"
            "def main():\n"
            "    return helper()\n"
        )

        selector = ExtendedSelector(
            file_path=str(target),
            symbol_path=["helper"],
            component=None,
            accessor=None,
        )

        callers = list(find_callers(selector, project_path=str(project)))
        assert len(callers) > 0, "Should find caller in same file"


class TestCallersCommand:
    """Tests for the refs --calls-only CLI command."""

    def test_callers_command_basic(self, tmp_path, run_emend_cmd):
        """CLI refs --calls-only command works."""
        project = tmp_path / "project"
        project.mkdir()

        target = project / "target.py"
        target.write_text(
            "def process(x):\n"
            "    return x + 1\n"
        )

        caller = project / "caller.py"
        caller.write_text(
            "from target import process\n"
            "\n"
            "def run():\n"
            "    return process(42)\n"
        )

        result = run_emend_cmd([
            "refs", f"{target}::process",
            "--calls-only",
            "--project", str(project),
        ], check=False)
        assert result.returncode == 0
        assert "caller.py" in result.stdout

    def test_callers_command_json(self, tmp_path, run_emend_cmd):
        """CLI refs --calls-only command with --json."""
        project = tmp_path / "project"
        project.mkdir()

        target = project / "target.py"
        target.write_text(
            "def process(x):\n"
            "    return x + 1\n"
        )

        caller = project / "caller.py"
        caller.write_text(
            "from target import process\n"
            "\n"
            "def run():\n"
            "    return process(42)\n"
        )

        result = run_emend_cmd([
            "refs", f"{target}::process",
            "--calls-only",
            "--project", str(project),
            "--json",
        ], check=False)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) > 0


# Needed for Path usage in assertions
from pathlib import Path
