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

    def test_search_where_pattern_with_dir_activates_pattern_mode(self, tmp_path):
        """When query is a directory and --where has a $-pattern, treat as pattern search."""
        file1 = tmp_path / "a.py"
        file1.write_text("Union[int, str]\n")
        file2 = tmp_path / "b.py"
        file2.write_text("x = 1\n")

        result = runner.invoke(app, ["search", str(tmp_path), "--where", "Union[$X, $Y]"])

        assert result.exit_code == 0
        assert "a.py" in result.stdout
        assert "b.py" not in result.stdout

    def test_search_where_pattern_with_file_glob_activates_pattern_mode(self, tmp_path):
        """When query is a file glob and --where has a $-pattern, treat as pattern search."""
        file1 = tmp_path / "a.py"
        file1.write_text("Optional[int]\n")
        file2 = tmp_path / "b.py"
        file2.write_text("x = 1\n")

        result = runner.invoke(app, ["search", f"{tmp_path}/*.py", "--where", "Optional[$X]"])

        assert result.exit_code == 0
        assert "a.py" in result.stdout
        assert "b.py" not in result.stdout

    def test_search_nonexistent_bare_name_errors(self, tmp_path, monkeypatch):
        """A bare non-file name that doesn't match any symbol gives an error."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "mod.py").write_text("def foo(): pass\n")

        result = runner.invoke(app, ["search", "SomeNonExistentName"])

        assert result.exit_code != 0

    def test_search_bare_name_finds_symbol(self, tmp_path, monkeypatch):
        """A bare name searches for the symbol across Python files."""
        monkeypatch.chdir(tmp_path)
        test_file = tmp_path / "mymod.py"
        test_file.write_text("def process_encounter(): pass\n")

        result = runner.invoke(
            app,
            ["search", "process_encounter"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "process_encounter" in result.stdout

    def test_search_bare_name_with_path_scopes_search(self, tmp_path, monkeypatch):
        """Bare name + path arg scopes the search to that path."""
        monkeypatch.chdir(tmp_path)
        subdir = tmp_path / "pkg"
        subdir.mkdir()
        (subdir / "a.py").write_text("def target_func(): return 'a'\n")
        (tmp_path / "b.py").write_text("def other_func(): return 'b'\n")

        result = runner.invoke(
            app,
            ["search", "target_func", str(subdir)],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "target_func" in result.stdout
        # other_func from b.py should not appear (scoped to subdir)
        assert "other_func" not in result.stdout

    def test_search_double_colon_prefix(self, tmp_path, monkeypatch):
        """::name syntax searches all files for that symbol."""
        monkeypatch.chdir(tmp_path)
        test_file = tmp_path / "mod.py"
        test_file.write_text("class Foo:\n    def bar(self): pass\n")

        result = runner.invoke(
            app,
            ["search", "::Foo"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "Foo" in result.stdout

    def test_search_bare_dotted_name(self, tmp_path, monkeypatch):
        """A bare dotted name like Class.method searches as a symbol path."""
        monkeypatch.chdir(tmp_path)
        test_file = tmp_path / "mod.py"
        test_file.write_text("class Foo:\n    def bar(self): pass\n")

        result = runner.invoke(
            app,
            ["search", "Foo.bar"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "bar" in result.stdout

    def test_search_file_path_still_works(self, tmp_path):
        """A query ending in .py is still treated as a file path, not a symbol."""
        result = runner.invoke(app, ["search", "nonexistent.py"])

        assert result.exit_code != 0
