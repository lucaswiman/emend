"""Tests for the unified 'search' command."""

import json
import pytest
from typer.testing import CliRunner

from emend.cli import app

runner = CliRunner()


class TestSearchPatternMode:
    """Tests for search in pattern-matching mode (auto-detected by $ in query)."""

    def test_search_detects_pattern_mode(self, tmp_path):
        """Search with $X triggers pattern mode (like find)."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "x = 5\n"
            "print('world')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file)])

        assert result.exit_code == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 2
        assert f"{test_file}:1" in lines[0]
        assert f"{test_file}:3" in lines[1]

    def test_search_pattern_count(self, tmp_path):
        """Search pattern mode with --count."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('a')\n"
            "print('b')\n"
            "print('c')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file), "--count"])

        assert result.exit_code == 0
        assert "3" in result.stdout

    def test_search_pattern_json(self, tmp_path):
        """Search pattern mode with --json."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "x = 5\n"
            "print('world')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file), "--json"])

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["count"] == 2
        assert len(data["matches"]) == 2

    def test_search_pattern_with_scope(self, tmp_path):
        """Search pattern mode with --in scope filter."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('global')\n"
            "def my_func():\n"
            "    print('inside')\n"
            "def other_func():\n"
            "    print('other')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file), "--in", "my_func"])

        assert result.exit_code == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 1
        assert ":3" in lines[0]

    def test_search_pattern_ellipsis_capture(self, tmp_path):
        """Search with $...REST ellipsis capture."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "func(1, 2, 3)\n"
        )

        result = runner.invoke(app, ["search", "func($...ARGS)", str(test_file)])

        assert result.exit_code == 0
        assert f"{test_file}:1" in result.stdout

    def test_search_pattern_directory(self, tmp_path):
        """Search pattern mode across directory."""
        file1 = tmp_path / "a.py"
        file1.write_text("print('a')\n")
        file2 = tmp_path / "b.py"
        file2.write_text("print('b')\n")

        result = runner.invoke(app, ["search", "print($X)", str(tmp_path)])

        assert result.exit_code == 0
        assert "a.py" in result.stdout
        assert "b.py" in result.stdout


class TestSearchLookupMode:
    """Tests for search in lookup mode (auto-detected by :: in query)."""

    def test_search_detects_lookup_mode(self, tmp_path):
        """Search with :: triggers lookup mode."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def func(x: int, y: str) -> bool:\n"
            "    return True\n"
        )

        result = runner.invoke(app, ["search", f"{test_file}::func[params]"])

        assert result.exit_code == 0
        assert "x: int" in result.stdout
        assert "y: str" in result.stdout

    def test_search_lookup_returns(self, tmp_path):
        """Search lookup mode for return type."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def func() -> str | None:\n"
            "    pass\n"
        )

        result = runner.invoke(app, ["search", f"{test_file}::func[returns]"])

        assert result.exit_code == 0
        assert "str | None" in result.stdout

    def test_search_lookup_with_filters(self, tmp_path):
        """Search lookup mode with --kind filter."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def func():\n"
            "    pass\n"
            "class MyClass:\n"
            "    pass\n"
        )

        result = runner.invoke(app, ["search", str(test_file), "--kind", "function"])

        assert result.exit_code == 0
        assert "func" in result.stdout

    def test_search_lookup_json(self, tmp_path):
        """Search lookup mode with --json and filters."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def func(x: int, y: str) -> bool:\n"
            "    return True\n"
            "class MyClass:\n"
            "    pass\n"
        )

        result = runner.invoke(app, ["search", str(test_file), "--kind", "function", "--json"])

        assert result.exit_code == 0
        # JSON output should be valid JSON
        data = json.loads(result.stdout)
        assert data is not None

    def test_search_lookup_count(self, tmp_path):
        """Search lookup mode with --count."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def func1():\n"
            "    pass\n"
            "def func2():\n"
            "    pass\n"
            "class MyClass:\n"
            "    pass\n"
        )

        result = runner.invoke(app, ["search", str(test_file), "--kind", "function", "--count"])

        assert result.exit_code == 0
        assert "2" in result.stdout


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
