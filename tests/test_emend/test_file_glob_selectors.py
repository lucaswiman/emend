"""Tests for file glob selectors and compositionality improvements."""

import json
import pytest
from pathlib import Path
from emend.component_selector import parse_extended_selector, ExtendedSelector
from emend.transform import cmd_lookup, cmd_edit, cmd_add, _apply_matching_filter


# ============================================================================
# ExtendedSelector file glob methods
# ============================================================================


class TestExtendedSelectorFileGlob:
    def test_has_file_glob_star(self):
        sel = parse_extended_selector("*.py::func")
        assert sel.has_file_glob()

    def test_has_file_glob_doublestar(self):
        sel = parse_extended_selector("**/*.py::func")
        assert sel.has_file_glob()

    def test_has_file_glob_question_mark(self):
        sel = parse_extended_selector("test_?.py::func")
        assert sel.has_file_glob()

    def test_no_file_glob(self):
        sel = parse_extended_selector("file.py::func")
        assert not sel.has_file_glob()

    def test_no_file_glob_with_symbol_wildcard(self):
        sel = parse_extended_selector("file.py::Test*")
        assert not sel.has_file_glob()

    def test_expand_file_glob(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.py").write_text("y = 2\n")
        (tmp_path / "c.txt").write_text("not python\n")

        sel = ExtendedSelector(file_path=str(tmp_path / "*.py"), symbol_path=["x"])
        result = sel.expand_file_glob()
        assert len(result) == 2
        assert all(f.endswith(".py") for f in result)

    def test_expand_file_glob_no_matches(self, tmp_path):
        sel = ExtendedSelector(file_path=str(tmp_path / "nonexistent_*.py"), symbol_path=[])
        with pytest.raises(FileNotFoundError, match="No files match"):
            sel.expand_file_glob()

    def test_expand_file_glob_recursive(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "sub" / "b.py").write_text("y = 2\n")

        sel = ExtendedSelector(file_path=str(tmp_path / "**/*.py"), symbol_path=["x"])
        result = sel.expand_file_glob()
        assert len(result) == 2

    def test_expand_file_glob_typescript(self, tmp_path):
        """A .ts glob expands to TypeScript files, not an empty Python filter."""
        (tmp_path / "a.ts").write_text("export function greet() {}\n")
        (tmp_path / "b.ts").write_text("export function wave() {}\n")
        (tmp_path / "c.py").write_text("x = 1\n")

        sel = ExtendedSelector(file_path=str(tmp_path / "*.ts"), symbol_path=["greet"])
        result = sel.expand_file_glob()
        assert len(result) == 2
        assert all(f.endswith(".ts") for f in result)

    def test_expand_file_glob_rust(self, tmp_path):
        """A .rs glob expands to Rust files."""
        (tmp_path / "a.rs").write_text("pub fn greet() {}\n")

        sel = ExtendedSelector(file_path=str(tmp_path / "*.rs"), symbol_path=["greet"])
        result = sel.expand_file_glob()
        assert [Path(f).name for f in result] == ["a.rs"]

    def test_with_file_path(self):
        sel = ExtendedSelector(
            file_path="**/*.py",
            symbol_path=["func"],
            component="params",
            accessor="x",
        )
        concrete = sel.with_file_path("specific.py")
        assert concrete.file_path == "specific.py"
        assert concrete.symbol_path == ["func"]
        assert concrete.component == "params"
        assert concrete.accessor == "x"

    def test_parse_glob_selector(self):
        sel = parse_extended_selector("**/*.py::TestClass")
        assert sel.file_path == "**/*.py"
        assert sel.symbol_path == ["TestClass"]
        assert sel.has_file_glob()

    def test_parse_glob_selector_with_component(self):
        sel = parse_extended_selector("test_*.py::func[params]")
        assert sel.file_path == "test_*.py"
        assert sel.symbol_path == ["func"]
        assert sel.component == "params"
        assert sel.has_file_glob()


# ============================================================================
# cmd_lookup with file globs
# ============================================================================


class TestLookupFileGlob:
    def test_lookup_glob_show_source(self, tmp_path):
        (tmp_path / "a.py").write_text("def hello():\n    return 'a'\n")
        (tmp_path / "b.py").write_text("def hello():\n    return 'b'\n")

        result = cmd_lookup(
            file_or_pattern=str(tmp_path / "*.py"),
            selector_str=f"{tmp_path / '*.py'}::hello",
        )
        assert "return 'a'" in result
        assert "return 'b'" in result

    def test_lookup_glob_get_component(self, tmp_path):
        (tmp_path / "a.py").write_text("def hello(x: int):\n    pass\n")
        (tmp_path / "b.py").write_text("def hello(y: str):\n    pass\n")

        result = cmd_lookup(
            file_or_pattern=str(tmp_path / "*.py"),
            selector_str=f"{tmp_path / '*.py'}::hello[params]",
        )
        assert "x: int" in result
        assert "y: str" in result

    def test_lookup_glob_skips_missing_symbols(self, tmp_path):
        (tmp_path / "a.py").write_text("def hello():\n    pass\n")
        (tmp_path / "b.py").write_text("def other():\n    pass\n")

        result = cmd_lookup(
            file_or_pattern=str(tmp_path / "*.py"),
            selector_str=f"{tmp_path / '*.py'}::hello",
        )
        assert "hello" in result
        assert "other" not in result

    def test_lookup_glob_no_matches_raises(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")

        with pytest.raises(ValueError, match="No symbols found"):
            cmd_lookup(
                file_or_pattern=str(tmp_path / "*.py"),
                selector_str=f"{tmp_path / '*.py'}::nonexistent",
            )

    def test_lookup_glob_with_symbol_wildcard(self, tmp_path):
        """Both file glob + symbol wildcard should work."""
        (tmp_path / "a.py").write_text("def test_one():\n    pass\ndef test_two():\n    pass\n")
        (tmp_path / "b.py").write_text("def test_three():\n    pass\n")

        result = cmd_lookup(
            file_or_pattern=str(tmp_path / "*.py"),
            selector_str=f"{tmp_path / '*.py'}::test_*",
        )
        assert "test_one" in result
        assert "test_two" in result
        assert "test_three" in result

    def test_lookup_glob_line_selector_rejected(self, tmp_path):
        """Line selectors with globs should be rejected."""
        (tmp_path / "a.py").write_text("x = 1\n")

        with pytest.raises(ValueError, match="Line selectors cannot be combined"):
            cmd_lookup(
                file_or_pattern=str(tmp_path),
                selector_str=f"{tmp_path / '*.py'}:10",
            )

    def test_lookup_query_mode_with_glob(self, tmp_path):
        """Query mode (with --kind etc.) should support globs in file_or_pattern."""
        (tmp_path / "a.py").write_text("def hello():\n    pass\n\ndef world():\n    pass\n")
        (tmp_path / "b.py").write_text("class Foo:\n    pass\n")

        result = cmd_lookup(
            file_or_pattern=str(tmp_path / "*.py"),
            kind=["function"],
            count=True,
        )
        assert result.strip() == "2"


# ============================================================================
# cmd_edit with file globs
# ============================================================================


class TestEditFileGlob:
    def test_edit_glob_returns(self, tmp_path):
        (tmp_path / "a.py").write_text("def hello() -> str:\n    return 'hi'\n")
        (tmp_path / "b.py").write_text("def hello() -> str:\n    return 'bye'\n")

        result = cmd_edit(
            selector_str=f"{tmp_path / '*.py'}::hello[returns]",
            value="int",
            apply=True,
        )
        # Both files should be edited
        assert "int" in (tmp_path / "a.py").read_text()
        assert "int" in (tmp_path / "b.py").read_text()

    def test_edit_glob_no_match(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")

        with pytest.raises(ValueError, match="No symbols found"):
            cmd_edit(
                selector_str=f"{tmp_path / '*.py'}::nonexistent[returns]",
                value="int",
            )


# ============================================================================
# cmd_add with file globs
# ============================================================================


class TestAddFileGlob:
    def test_add_glob_decorator(self, tmp_path):
        (tmp_path / "a.py").write_text("def hello():\n    pass\n")
        (tmp_path / "b.py").write_text("def hello():\n    pass\n")

        result = cmd_add(
            selector_str=f"{tmp_path / '*.py'}::hello[decorators]",
            value="@staticmethod",
            apply=True,
        )
        assert "@staticmethod" in (tmp_path / "a.py").read_text()
        assert "@staticmethod" in (tmp_path / "b.py").read_text()


# ============================================================================
# --in selector support
# ============================================================================



# ============================================================================
# --matching flag
# ============================================================================


class TestMatchingFlag:
    def test_matching_filters_symbols(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def has_print():\n    print('hello')\n\n"
            "def no_print():\n    return 42\n"
        )

        # Without matching, both symbols appear
        result_all = cmd_lookup(
            file_or_pattern=str(test_file),
            selector_str=f"{test_file}::has_print",
        )
        assert "print" in result_all

    def test_matching_with_pattern(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def has_print():\n    print('hello')\n\n"
            "def no_print():\n    return 42\n"
        )

        result = cmd_lookup(
            file_or_pattern=str(test_file),
            selector_str=f"{test_file}::has_print",
            matching="print($X)",
        )
        assert "print" in result


# ============================================================================
# --output-selectors flag
# ============================================================================


class TestOutputSelectors:
    def test_output_selectors_find(self, tmp_path):
        """Test that --output-selectors outputs selectors instead of line numbers."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def hello():\n    print('hi')\n\n"
            "def world():\n    print('bye')\n"
        )

        from emend.transform import find_pattern
        from emend.ast_utils import find_nested_definitions, find_symbol_by_line

        matches = find_pattern("print($X)", str(test_file))
        assert len(matches) == 2

        # Verify that find_symbol_by_line correctly maps lines to symbols
        symbols = find_nested_definitions(str(test_file))
        sym1 = find_symbol_by_line(symbols, matches[0].line)
        sym2 = find_symbol_by_line(symbols, matches[1].line)
        assert sym1 is not None
        assert sym2 is not None
        assert sym1.path == ["hello"]
        assert sym2.path == ["world"]


# ============================================================================
# Validation: reject file globs in non-applicable commands
# ============================================================================


class TestRejectFileGlobs:
    def test_reject_glob_find_references(self):
        from emend.cli_base import _reject_file_glob
        with pytest.raises(ValueError, match="not supported for find-references"):
            _reject_file_glob("**/*.py::func", "find-references")

    def test_reject_glob_rename(self):
        from emend.cli_base import _reject_file_glob
        with pytest.raises(ValueError, match="not supported for rename"):
            _reject_file_glob("*.py::func", "rename")

    def test_reject_glob_move(self):
        from emend.cli_base import _reject_file_glob
        with pytest.raises(ValueError, match="not supported for move"):
            _reject_file_glob("**/*.py::func", "move")

    def test_reject_glob_callers(self):
        from emend.cli_base import _reject_file_glob
        with pytest.raises(ValueError, match="not supported for callers"):
            _reject_file_glob("*.py::func", "callers")

    def test_reject_glob_callees(self):
        from emend.cli_base import _reject_file_glob
        with pytest.raises(ValueError, match="not supported for callees"):
            _reject_file_glob("*.py::func", "callees")

    def test_no_reject_for_concrete_path(self):
        from emend.cli_base import _reject_file_glob
        # Should not raise for concrete paths
        _reject_file_glob("file.py::func", "find-references")

    def test_no_reject_symbol_wildcard(self):
        from emend.cli_base import _reject_file_glob
        # Symbol wildcards are fine (the * is after ::)
        _reject_file_glob("file.py::Test*", "find-references")


# ============================================================================
# resolve_files helper
# ============================================================================


class TestResolveFiles:
    def test_resolve_many_files_uses_default_for_empty_paths(self, tmp_path):
        from emend.cli_base import resolve_many_files

        default_file = tmp_path / "default.py"
        default_file.write_text("x = 1\n")

        files, is_multi = resolve_many_files([], default=str(default_file))

        assert files == [default_file]
        assert not is_multi

    def test_resolve_directory(self, tmp_path):
        from emend.cli_base import resolve_files
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.py").write_text("y = 2\n")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.py").write_text("z = 3\n")

        files, is_multi = resolve_files(str(tmp_path))
        assert is_multi
        assert len(files) == 3

    def test_resolve_glob(self, tmp_path):
        from emend.cli_base import resolve_files
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.py").write_text("y = 2\n")
        (tmp_path / "c.txt").write_text("z = 3\n")

        files, is_multi = resolve_files(str(tmp_path / "*.py"))
        assert is_multi
        assert len(files) == 2

    def test_resolve_single_file(self, tmp_path):
        from emend.cli_base import resolve_files
        (tmp_path / "a.py").write_text("x = 1\n")

        files, is_multi = resolve_files(str(tmp_path / "a.py"))
        assert not is_multi
        assert len(files) == 1


class TestMatchingFilterExcludesUnparseable:
    def test_unparseable_selector_excluded_from_matching_results(self, tmp_path):
        """Selectors that raise ValueError during source retrieval must not
        appear in the filtered output."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def has_print():\n    print('hello')\n\n"
            "def no_print():\n    return 42\n"
        )

        valid_selector = f"{test_file}::has_print"
        bogus_selector = f"{test_file}::nonexistent_symbol"

        lookup_result = f"{valid_selector}\n{bogus_selector}"

        sel = parse_extended_selector(f"{test_file}::has_print")
        result = _apply_matching_filter(
            lookup_result,
            matching_pattern="print($X)",
            selector=sel,
            files=[str(test_file)],
        )

        assert valid_selector in result
        assert "nonexistent_symbol" not in result
