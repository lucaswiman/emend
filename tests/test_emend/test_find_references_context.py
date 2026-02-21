"""Tests for --writes-only and --reads-only flags on find-references."""
from pathlib import Path

import pytest

from emend.transform import find_references
from emend.component_selector import ExtendedSelector


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
            "find-references", f"{file}::config",
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
            "find-references", f"{file}::config",
            "--reads-only"
        ])
        lines = result.stdout.strip().split("\n")
        line_nums = [l.split(":")[-1].strip() for l in lines if ":" in l]
        assert "2" in line_nums
