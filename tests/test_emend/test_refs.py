"""Tests for reference queries: ``refs`` filters, callers and callees.

Covers ``find_references`` with ``--writes-only`` / ``--reads-only``, nested
symbol qualified names, ``find_callers`` and ``find_callees``, and their CLI
commands.
"""

import json
from pathlib import Path

import pytest

from emend.component_selector import ExtendedSelector
from emend.transform import find_references


# ---------------------------------------------------------------------------
# refs --writes-only / --reads-only
# ---------------------------------------------------------------------------


def _make_selector(file_path, symbol_name):
    return ExtendedSelector(
        file_path=str(file_path),
        symbol_path=[symbol_name],
        component=None,
        accessor=None,
    )


class TestWritesOnlyFilter:
    """find_references with writes_only should return only write references."""

    def test_writes_only_simple_assignment(self, tmp_path):
        """Simple assignment x = 1 should count as a write."""
        project = tmp_path / "project"
        project.mkdir()
        file = project / "module.py"
        file.write_text(
            "config = 'default'\n"
            "print(config)\n"
            "config = 'updated'\n"
        )

        selector = _make_selector(file, "config")
        refs = find_references(
            selector, project_path=str(project), writes_only=True
        )

        lines = [r.line for r in refs]
        assert 1 in lines, "First assignment should be a write"
        assert 3 in lines, "Second assignment should be a write"
        assert 2 not in lines, "print(config) should not be a write"

    def test_writes_only_augmented_assignment(self, tmp_path):
        """Augmented assignment (+=) should count as a write."""
        project = tmp_path / "project"
        project.mkdir()
        file = project / "module.py"
        file.write_text(
            "count = 0\n"
            "count += 1\n"
            "print(count)\n"
        )

        selector = _make_selector(file, "count")
        refs = find_references(
            selector, project_path=str(project), writes_only=True
        )

        lines = [r.line for r in refs]
        assert 1 in lines, "Initial assignment should be a write"
        assert 2 in lines, "Augmented assignment should be a write"
        assert 3 not in lines, "print(count) should not be a write"

    def test_writes_only_annotated_assignment(self, tmp_path):
        """Annotated assignment (x: int = 1) should count as a write."""
        project = tmp_path / "project"
        project.mkdir()
        file = project / "module.py"
        file.write_text(
            "value: int = 42\n"
            "print(value)\n"
        )

        selector = _make_selector(file, "value")
        refs = find_references(
            selector, project_path=str(project), writes_only=True
        )

        lines = [r.line for r in refs]
        assert 1 in lines, "Annotated assignment should be a write"
        assert 2 not in lines, "print(value) should not be a write"

    def test_writes_only_for_target(self, tmp_path):
        """For loop target should count as a write."""
        project = tmp_path / "project"
        project.mkdir()
        file = project / "module.py"
        file.write_text(
            "item = None\n"
            "for item in [1, 2, 3]:\n"
            "    print(item)\n"
        )

        selector = _make_selector(file, "item")
        refs = find_references(
            selector, project_path=str(project), writes_only=True
        )

        lines = [r.line for r in refs]
        assert 1 in lines, "Initial assignment should be a write"
        assert 2 in lines, "For target should be a write"
        assert 3 not in lines, "print(item) should not be a write"


class TestReadsOnlyFilter:
    """find_references with reads_only should return only read references."""

    def test_reads_only_excludes_assignments(self, tmp_path):
        """Read-only filter should exclude assignment targets."""
        project = tmp_path / "project"
        project.mkdir()
        file = project / "module.py"
        file.write_text(
            "config = 'default'\n"
            "print(config)\n"
            "x = config + 'suffix'\n"
        )

        selector = _make_selector(file, "config")
        refs = find_references(
            selector, project_path=str(project), reads_only=True
        )

        lines = [r.line for r in refs]
        assert 1 not in lines, "Assignment target should not be a read"
        assert 2 in lines, "print(config) should be a read"
        assert 3 in lines, "config in expression should be a read"

    def test_reads_only_includes_function_args(self, tmp_path):
        """Reading a variable as a function argument should be a read."""
        project = tmp_path / "project"
        project.mkdir()
        file = project / "module.py"
        file.write_text(
            "data = [1, 2, 3]\n"
            "result = len(data)\n"
            "data = [4, 5, 6]\n"
        )

        selector = _make_selector(file, "data")
        refs = find_references(
            selector, project_path=str(project), reads_only=True
        )

        lines = [r.line for r in refs]
        assert 2 in lines, "len(data) should be a read"
        assert 1 not in lines, "First assignment should not be a read"
        assert 3 not in lines, "Second assignment should not be a read"


class TestReadsWritesMutualExclusion:
    """writes_only and reads_only should be mutually exclusive."""

    def test_cannot_use_both_flags(self, tmp_path):
        """Using both writes_only and reads_only should raise ValueError."""
        project = tmp_path / "project"
        project.mkdir()
        file = project / "module.py"
        file.write_text("x = 1\n")

        selector = _make_selector(file, "x")
        with pytest.raises(ValueError, match="Cannot specify both"):
            find_references(
                selector, project_path=str(project),
                writes_only=True, reads_only=True
            )


class TestFindReferencesContextCLI:
    """Test --writes-only and --reads-only via CLI."""

    def test_writes_only_cli(self, tmp_path, run_emend_cmd):
        """CLI --writes-only should filter to writes."""
        project = tmp_path / "project"
        project.mkdir()
        file = project / "module.py"
        file.write_text(
            "config = 'default'\n"
            "print(config)\n"
            "config = 'updated'\n"
        )

        result = run_emend_cmd([
            "refs", f"{file}::config",
            "--writes-only"
        ])
        lines = result.stdout.strip().split("\n")
        # Should include lines 1 and 3 (assignments), not line 2 (read)
        line_nums = [l.split(":")[-1].strip() for l in lines if ":" in l]
        assert "1" in line_nums
        assert "3" in line_nums

    def test_reads_only_cli(self, tmp_path, run_emend_cmd):
        """CLI --reads-only should filter to reads."""
        project = tmp_path / "project"
        project.mkdir()
        file = project / "module.py"
        file.write_text(
            "config = 'default'\n"
            "print(config)\n"
            "config = 'updated'\n"
        )

        result = run_emend_cmd([
            "refs", f"{file}::config",
            "--reads-only"
        ])
        lines = result.stdout.strip().split("\n")
        line_nums = [l.split(":")[-1].strip() for l in lines if ":" in l]
        assert "2" in line_nums


# ---------------------------------------------------------------------------
# Nested symbol qualified names (Class.method selectors)
# ---------------------------------------------------------------------------


def _make_nested_selector(file_path, *symbol_parts):
    """Create a selector with a multi-part symbol path (e.g. Class.method)."""
    return ExtendedSelector(
        file_path=str(file_path),
        symbol_path=list(symbol_parts),
        component=None,
        accessor=None,
    )


class TestNestedSymbolReferences:
    """find_references should use the full qualified name for nested symbols."""

    def test_refs_distinguishes_same_named_methods(self, tmp_path):
        """Searching for ClassA.process should not return ClassB.process refs."""
        from emend.transform import find_references

        project = tmp_path / "project"
        project.mkdir()

        module = project / "module.py"
        module.write_text(
            "class ClassA:\n"
            "    def process(self):\n"
            "        return 'A'\n"
            "\n"
            "class ClassB:\n"
            "    def process(self):\n"
            "        return 'B'\n"
            "\n"
            "a = ClassA()\n"
            "a.process()\n"
            "b = ClassB()\n"
            "b.process()\n"
        )

        selector = _make_nested_selector(module, "ClassA", "process")
        refs = list(find_references(selector, project_path=str(project)))

        ref_lines = {r.line for r in refs}

        assert 2 in ref_lines, (
            f"Expected ClassA.process definition on line 2, got lines {ref_lines}"
        )
        assert 6 not in ref_lines, (
            f"ClassB.process definition (line 6) should not appear when searching "
            f"for ClassA.process, got lines {ref_lines}"
        )


class TestNestedSymbolCallers:
    """find_callers should use the full qualified name for nested symbols."""

    def test_callers_distinguishes_same_named_methods(self, tmp_path):
        """Searching callers of ClassA.process should not return ClassB.process callers."""
        from emend.transform import find_callers

        project = tmp_path / "project"
        project.mkdir()

        module = project / "module.py"
        module.write_text(
            "class ClassA:\n"
            "    def process(self):\n"
            "        return 'A'\n"
            "\n"
            "class ClassB:\n"
            "    def process(self):\n"
            "        return 'B'\n"
            "\n"
            "def call_a():\n"
            "    a = ClassA()\n"
            "    a.process()\n"
            "\n"
            "def call_b():\n"
            "    b = ClassB()\n"
            "    b.process()\n"
        )

        selector = _make_nested_selector(module, "ClassA", "process")
        callers = list(find_callers(selector, project_path=str(project)))

        caller_lines = {r.line for r in callers}

        assert 15 not in caller_lines, (
            f"ClassB.process call (line 15) should not appear as a caller of "
            f"ClassA.process, got lines {caller_lines}"
        )


class TestNestedSymbolCallees:
    """find_callees should use the full qualified name for nested symbols."""

    def test_callees_distinguishes_same_named_methods(self, tmp_path):
        """Searching callees of ClassA.run should use full qualified name."""
        from emend.transform import find_callees

        project = tmp_path / "project"
        project.mkdir()

        module = project / "module.py"
        module.write_text(
            "def helper():\n"
            "    return 1\n"
            "\n"
            "class ClassA:\n"
            "    def run(self):\n"
            "        return helper()\n"
            "\n"
            "class ClassB:\n"
            "    def run(self):\n"
            "        return 999\n"
        )

        selector = _make_nested_selector(module, "ClassA", "run")
        callees = find_callees(selector, project_path=str(project))

        callee_names = {c.name for c in callees}
        assert "helper" in callee_names, (
            f"Expected 'helper' in callees of ClassA.run, got {callee_names}"
        )


# ---------------------------------------------------------------------------
# Callers
# ---------------------------------------------------------------------------


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

    def test_callers_find_nested_method_closure_calls_module_function(self, tmp_path):
        """callers includes same-module calls from nested functions inside methods."""
        from emend.transform import find_callers

        project = tmp_path / "project"
        project.mkdir()

        target = project / "module.py"
        target.write_text(
            "def foo():\n"
            "    return 1\n"
            "\n"
            "class E:\n"
            "    def method(self):\n"
            "        def bar():\n"
            "            return foo()\n"
            "        return bar()\n"
        )

        selector = ExtendedSelector(
            file_path=str(target),
            symbol_path=["foo"],
            component=None,
            accessor=None,
        )

        callers = list(find_callers(selector, project_path=str(project)))
        caller_lines = {(r.file_path, r.line) for r in callers}
        assert any(
            Path(f).resolve() == target.resolve() and line == 7
            for f, line in caller_lines
        ), f"Expected nested foo() call on line 7, got {caller_lines}"


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


# ---------------------------------------------------------------------------
# Callees
# ---------------------------------------------------------------------------


class TestFindCallees:
    """Tests for find_callees() in transform.py."""

    def test_callees_basic(self, tmp_path):
        """callees finds functions called by target."""
        from emend.transform import find_callees

        project = tmp_path / "project"
        project.mkdir()

        module = project / "module.py"
        module.write_text(
            "def helper():\n"
            "    return 42\n"
            "\n"
            "def utility():\n"
            "    return 99\n"
            "\n"
            "def main():\n"
            "    a = helper()\n"
            "    b = utility()\n"
            "    return a + b\n"
        )

        selector = ExtendedSelector(
            file_path=str(module),
            symbol_path=["main"],
            component=None,
            accessor=None,
        )

        callees = find_callees(selector, project_path=str(project))
        callee_names = {c.name for c in callees}
        assert "helper" in callee_names, f"Expected 'helper' in callees, got {callee_names}"
        assert "utility" in callee_names, f"Expected 'utility' in callees, got {callee_names}"

    def test_callees_cross_module(self, tmp_path):
        """callees finds imported functions called by the target."""
        from emend.transform import find_callees

        project = tmp_path / "project"
        project.mkdir()

        helpers = project / "helpers.py"
        helpers.write_text(
            "def compute(x):\n"
            "    return x * 2\n"
        )

        main = project / "main.py"
        main.write_text(
            "from helpers import compute\n"
            "\n"
            "def run():\n"
            "    return compute(21)\n"
        )

        selector = ExtendedSelector(
            file_path=str(main),
            symbol_path=["run"],
            component=None,
            accessor=None,
        )

        callees = find_callees(selector, project_path=str(project))
        callee_names = {c.name for c in callees}
        assert "compute" in callee_names, f"Expected 'compute' in callees, got {callee_names}"

    def test_callees_no_calls(self, tmp_path):
        """callees returns empty list for function with no calls."""
        from emend.transform import find_callees

        project = tmp_path / "project"
        project.mkdir()

        module = project / "module.py"
        module.write_text(
            "def pure():\n"
            "    return 42\n"
        )

        selector = ExtendedSelector(
            file_path=str(module),
            symbol_path=["pure"],
            component=None,
            accessor=None,
        )

        callees = find_callees(selector, project_path=str(project))
        assert len(callees) == 0

    def test_callees_method_calls(self, tmp_path):
        """callees finds method calls inside a function."""
        from emend.transform import find_callees

        project = tmp_path / "project"
        project.mkdir()

        module = project / "module.py"
        module.write_text(
            "def process(items):\n"
            "    result = items.copy()\n"
            "    result.append(42)\n"
            "    return result\n"
        )

        selector = ExtendedSelector(
            file_path=str(module),
            symbol_path=["process"],
            component=None,
            accessor=None,
        )

        callees = find_callees(selector, project_path=str(project))
        callee_names = {c.name for c in callees}
        assert "copy" in callee_names or "append" in callee_names, \
            f"Expected method calls in callees, got {callee_names}"
