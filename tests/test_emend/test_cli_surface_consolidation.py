"""Focused tests for grouped CLI surface consolidation."""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from emend.cli import app

runner = CliRunner()


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences so assertions work regardless of color."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


@pytest.mark.parametrize(
    ("command", "present"),
    [
        pytest.param([], ("│ mcp", "│ tool"), id="root"),
        pytest.param(["edit"], ("Commands", "rm", "replace"), id="edit"),
        pytest.param(
            ["analyze"], ("Commands", "refs", "graph", "impact"), id="analyze"
        ),
        pytest.param(["tool"], ("Commands",), id="tool"),
    ],
)
def test_grouped_help_contract(command, present):
    """Each command group exposes its promised surface."""
    result = runner.invoke(app, [*command, "--help"])
    stdout = _strip_ansi(result.stdout)

    assert result.exit_code == 0
    for text in present:
        assert text in stdout


def test_rm_alias_still_works_without_edit_prefix(tmp_path):
    """Users can omit `edit` and still call `rm` directly."""
    file_via_group = tmp_path / "group_path.py"
    file_via_alias = tmp_path / "alias_path.py"

    content = "def keep():\n    pass\n\ndef old():\n    pass\n"
    file_via_group.write_text(content)
    file_via_alias.write_text(content)

    grouped = runner.invoke(app, ["edit", "rm", f"{file_via_group}::old", "--apply"])
    alias = runner.invoke(app, ["rm", f"{file_via_alias}::old", "--apply"])

    assert grouped.exit_code == 0
    assert alias.exit_code == 0
    assert file_via_group.read_text() == file_via_alias.read_text()
    assert "def old" not in file_via_alias.read_text()


class TestCLIAliases:
    """Test that CLI aliases work correctly."""

    def test_grep_alias(self, tmp_path):
        """grep is an alias for search (pattern mode)."""
        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')\n")

        result = runner.invoke(app, ["grep", "print($X)", str(test_file)])
        assert result.exit_code == 0
        assert str(test_file) in result.stdout

    def test_ls_alias(self, tmp_path):
        """ls is an alias for search (summary mode)."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def greet():\n    pass\n")

        result = runner.invoke(app, ["ls", str(test_file)])
        assert result.exit_code == 0
        assert "greet" in result.stdout

    @pytest.mark.parametrize("command", ["remove", "delete"])
    def test_remove_aliases(self, tmp_path, command):
        """remove and delete remove a symbol (rm is pinned against `edit rm` above)."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def keep():\n    pass\n\ndef old():\n    pass\n")

        result = runner.invoke(app, [command, f"{test_file}::old", "--apply"])
        assert result.exit_code == 0
        content = test_file.read_text()
        assert "keep" in content
        assert "def old" not in content

    def test_set_alias(self, tmp_path):
        """set is an alias for edit."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def greet() -> str:\n    return 'hi'\n")

        result = runner.invoke(app, ["set", f"{test_file}::greet[returns]", "int", "--apply"])
        assert result.exit_code == 0
        assert "int" in test_file.read_text()

    def test_insert_alias(self, tmp_path):
        """insert is an alias for add."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def greet(name: str) -> str:\n    return f'Hello, {name}'\n")

        result = runner.invoke(app, ["insert", f"{test_file}::greet[params]", "ctx: Context", "--apply"])
        assert result.exit_code == 0
        assert "ctx" in test_file.read_text()

    @pytest.mark.parametrize("command", ["copy", "cp"])
    def test_copy_aliases(self, tmp_path, command):
        """copy and cp are aliases for copy-to."""
        src = tmp_path / "source.py"
        src.write_text("def helper():\n    return 42\n")
        dest = tmp_path / "dest.py"

        result = runner.invoke(app, [command, f"{src}::helper", str(dest), "--apply"])
        assert result.exit_code == 0
        assert "helper" in dest.read_text()

    def test_mv_alias(self, tmp_path):
        """mv is an alias for move."""
        src = tmp_path / "source.py"
        src.write_text("def helper():\n    return 42\n")
        dest = tmp_path / "dest.py"
        dest.write_text("")

        result = runner.invoke(app, ["mv", f"{src}::helper", str(dest), "--apply"])
        assert result.exit_code == 0
        assert "helper" in dest.read_text()

    def test_references_alias(self, tmp_path):
        """references is an alias for refs."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def greet():\n    pass\n\ngreet()\n")

        result = runner.invoke(app, ["references", f"{test_file}::greet"])
        assert result.exit_code == 0
        assert str(test_file) in result.stdout
