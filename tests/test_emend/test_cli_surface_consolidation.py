"""Focused tests for grouped CLI surface consolidation."""

from __future__ import annotations

from typer.testing import CliRunner

from emend.cli import app

runner = CliRunner()


def test_root_help_keeps_mcp_public_and_hides_query():
    """`mcp` remains a public command; raw query moves under `tool`."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "│ mcp" in result.stdout
    assert "│ tool" in result.stdout
    assert "│ query" not in result.stdout


def test_edit_group_has_subcommands():
    """Mutation workflow is grouped under `edit`."""
    result = runner.invoke(app, ["edit", "--help"])

    assert result.exit_code == 0
    assert "Commands" in result.stdout
    assert "rm" in result.stdout
    assert "replace" in result.stdout


def test_analyze_group_has_subcommands():
    """Read-only analysis workflow is grouped under `analyze`."""
    result = runner.invoke(app, ["analyze", "--help"])

    assert result.exit_code == 0
    assert "Commands" in result.stdout
    assert "refs" in result.stdout
    assert "graph" in result.stdout
    assert "impact" in result.stdout


def test_tool_group_no_query_subcommand():
    """Raw datalog query has been removed from the public CLI (Phase 1)."""
    result = runner.invoke(app, ["tool", "--help"])

    assert result.exit_code == 0
    assert "Commands" in result.stdout
    assert "query" not in result.stdout


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
