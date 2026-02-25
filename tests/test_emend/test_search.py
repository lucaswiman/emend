"""Tests for the unified 'search' command.

Covered elsewhere:
- Pattern mode basics (detect, count, json, scope): test_cli_transform.py::TestFindCommand
- Lookup mode basics (detect, returns, json, count, filters): test_component_operations.py::test_get_cli
- Filter combinations (--kind, --name, etc.): test_query.py
"""

from typer.testing import CliRunner

from emend.cli import app

runner = CliRunner()


def test_search_pattern_ellipsis_capture(tmp_path):
    """Search with $...REST ellipsis capture."""
    test_file = tmp_path / "test.py"
    test_file.write_text(
        "func(1, 2, 3)\n"
    )

    result = runner.invoke(app, ["search", "func($...ARGS)", str(test_file)])

    assert result.exit_code == 0
    assert f"{test_file}:1" in result.stdout


def test_search_pattern_directory(tmp_path):
    """Search pattern mode across directory."""
    file1 = tmp_path / "a.py"
    file1.write_text("print('a')\n")
    file2 = tmp_path / "b.py"
    file2.write_text("print('b')\n")

    result = runner.invoke(app, ["search", "print($X)", str(tmp_path)])

    assert result.exit_code == 0
    assert "a.py" in result.stdout
    assert "b.py" in result.stdout


class TestSearchEdgeCases:
    """Edge cases for mode detection in search command."""

    def test_search_no_dollar_no_colon_is_lookup(self, tmp_path):
        """Query without $ or :: uses lookup mode (file path only)."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def func():\n"
            "    pass\n"
        )

        result = runner.invoke(app, ["search", str(test_file), "--kind", "function"])

        assert result.exit_code == 0
        assert "func" in result.stdout

    def test_search_nonexistent_file(self, tmp_path):
        """Error for nonexistent file."""
        result = runner.invoke(app, ["search", f"{tmp_path}/nonexistent.py::func"])

        assert result.exit_code != 0

    def test_search_empty_results(self, tmp_path):
        """Empty results for no matches in pattern mode."""
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 5\n")

        result = runner.invoke(app, ["search", "print($X)", str(test_file)])

        assert result.exit_code == 0
        assert result.stdout.strip() == ""
