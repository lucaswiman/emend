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
    ("command", "present", "absent"),
    [
        pytest.param([], ("│ mcp", "│ tool"), ("│ query",), id="root"),
        pytest.param(["edit"], ("Commands", "rm", "replace"), (), id="edit"),
        pytest.param(
            ["analyze"], ("Commands", "refs", "graph", "impact"), (), id="analyze"
        ),
        pytest.param(["tool"], ("Commands",), ("query",), id="tool"),
    ],
)
def test_grouped_help_contract(command, present, absent):
    """Each command group exposes its promised surface and hides removals."""
    result = runner.invoke(app, [*command, "--help"])
    stdout = _strip_ansi(result.stdout)

    assert result.exit_code == 0
    for text in present:
        assert text in stdout
    for text in absent:
        assert text not in stdout


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
