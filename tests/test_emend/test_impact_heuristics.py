"""Tests for cross-language impact analysis heuristics (Phase 8)."""
from pathlib import Path

import pytest


class TestIsTestFileCrossLanguage:
    """Tests for _is_test_file with TypeScript and Rust conventions."""

    def test_is_test_file_ts_dot_test(self):
        from emend.transform import _is_test_file
        assert _is_test_file("math.test.ts")
        assert _is_test_file("/project/src/math.test.ts")

    def test_is_test_file_ts_dot_spec(self):
        from emend.transform import _is_test_file
        assert _is_test_file("utils.spec.ts")
        assert _is_test_file("/project/src/utils.spec.ts")

    def test_is_test_file_tsx_dot_test(self):
        from emend.transform import _is_test_file
        assert _is_test_file("Component.test.tsx")

    def test_is_test_file_js_dot_spec(self):
        from emend.transform import _is_test_file
        assert _is_test_file("helper.spec.js")

    def test_is_test_file_dunder_tests_dir(self):
        from emend.transform import _is_test_file
        assert _is_test_file("__tests__/math.ts")
        assert _is_test_file("/project/__tests__/helper.tsx")

    def test_is_test_file_rust_test_prefix(self):
        from emend.transform import _is_test_file
        assert _is_test_file("test_lib.rs")

    def test_is_test_file_rust_test_suffix(self):
        from emend.transform import _is_test_file
        # The _test suffix check is now extension-agnostic
        assert _is_test_file("lib_test.rs")

    def test_is_test_file_rust_tests_dir(self):
        from emend.transform import _is_test_file
        assert _is_test_file("tests/integration.rs")
        assert _is_test_file("/project/tests/test_api.rs")

    def test_is_test_file_python_test_suffix_still_works(self):
        from emend.transform import _is_test_file
        # Regression: the old _test.py check should still work
        assert _is_test_file("foo_test.py")
        assert _is_test_file("/path/to/bar_test.py")

    def test_is_not_test_file_regular_ts(self):
        from emend.transform import _is_test_file
        assert not _is_test_file("app.ts")
        assert not _is_test_file("/src/utils.ts")

    def test_is_not_test_file_regular_rs(self):
        from emend.transform import _is_test_file
        assert not _is_test_file("lib.rs")
        assert not _is_test_file("/src/main.rs")


class TestIsTestSymbolCrossLanguage:
    """Tests for _is_test_symbol with TypeScript and Rust conventions."""

    def test_is_test_symbol_ts_describe(self):
        from emend.transform import _is_test_symbol
        assert _is_test_symbol("math.test.ts::describe")

    def test_is_test_symbol_ts_it(self):
        from emend.transform import _is_test_symbol
        assert _is_test_symbol("utils.spec.ts::it")

    def test_is_test_symbol_ts_test(self):
        from emend.transform import _is_test_symbol
        assert _is_test_symbol("app.test.ts::test")

    def test_is_test_symbol_rust_test_prefix(self):
        from emend.transform import _is_test_symbol
        assert _is_test_symbol("lib.rs::test_add")
        assert _is_test_symbol("main.rs::test_compute")

    def test_is_test_symbol_python_still_works(self):
        """Regression: Python test naming conventions still detected."""
        from emend.transform import _is_test_symbol
        assert _is_test_symbol("test_foo.py::test_something")
        assert _is_test_symbol("foo.py::TestClass")

    def test_is_not_test_symbol_regular_ts(self):
        from emend.transform import _is_test_symbol
        assert not _is_test_symbol("utils.ts::formatName")
        assert not _is_test_symbol("app.ts::handleRequest")

    def test_is_not_test_symbol_regular_rust(self):
        from emend.transform import _is_test_symbol
        assert not _is_test_symbol("lib.rs::compute")
        assert not _is_test_symbol("main.rs::run")

    def test_is_not_test_symbol_no_selector(self):
        from emend.transform import _is_test_symbol
        assert not _is_test_symbol("plain_string")
        assert not _is_test_symbol("")


class TestParseDiffCrossLanguage:
    """Tests for diff parsing with non-Python files."""

    def test_parse_diff_typescript_files(self):
        from emend.transform import _parse_diff_to_changed_files

        diff_text = (
            "diff --git a/src/utils.ts b/src/utils.ts\n"
            "index abc..def 100644\n"
            "--- a/src/utils.ts\n"
            "+++ b/src/utils.ts\n"
            "@@ -5,2 +5,3 @@\n"
            " export function format() {\n"
            "+    // new comment\n"
        )

        result = _parse_diff_to_changed_files(diff_text)
        assert len(result) == 1
        file_path, lines = result[0]
        assert file_path == "src/utils.ts"
        assert 5 in lines

    def test_parse_diff_rust_files(self):
        from emend.transform import _parse_diff_to_changed_files

        diff_text = (
            "diff --git a/src/lib.rs b/src/lib.rs\n"
            "index abc..def 100644\n"
            "--- a/src/lib.rs\n"
            "+++ b/src/lib.rs\n"
            "@@ -10,1 +10,2 @@\n"
            "+    let x = 42;\n"
        )

        result = _parse_diff_to_changed_files(diff_text)
        assert len(result) == 1
        file_path, lines = result[0]
        assert file_path == "src/lib.rs"
        assert 10 in lines

    def test_parse_diff_mixed_languages(self):
        from emend.transform import _parse_diff_to_changed_files

        diff_text = (
            "diff --git a/lib.py b/lib.py\n"
            "--- a/lib.py\n"
            "+++ b/lib.py\n"
            "@@ -1,2 +1,3 @@\n"
            "+new line\n"
            "diff --git a/utils.ts b/utils.ts\n"
            "--- a/utils.ts\n"
            "+++ b/utils.ts\n"
            "@@ -5,1 +5,2 @@\n"
            "+ts line\n"
            "diff --git a/lib.rs b/lib.rs\n"
            "--- a/lib.rs\n"
            "+++ b/lib.rs\n"
            "@@ -3,1 +3,2 @@\n"
            "+rs line\n"
        )

        result = _parse_diff_to_changed_files(diff_text)
        files = {r[0] for r in result}
        assert files == {"lib.py", "utils.ts", "lib.rs"}


class TestMixedProjectImpact:
    """Test that impact analysis in a mixed-language project doesn't produce
    spurious cross-language results."""

    def test_mixed_project_python_change_no_ts_impact(self, tmp_path):
        """Changing a Python file should not produce TypeScript impact results."""
        from emend.transform import find_impact
        from emend.component_selector import ExtendedSelector

        project = tmp_path / "project"
        project.mkdir()

        # Python file
        py_lib = project / "lib.py"
        py_lib.write_text(
            "def py_helper():\n"
            "    return 42\n"
        )

        py_app = project / "app.py"
        py_app.write_text(
            "from lib import py_helper\n"
            "\n"
            "def py_main():\n"
            "    return py_helper()\n"
        )

        # TypeScript file (unrelated)
        ts_lib = project / "utils.ts"
        ts_lib.write_text(
            "export function tsHelper(): number {\n"
            "    return 42;\n"
            "}\n"
        )

        selector = ExtendedSelector(
            file_path=str(py_lib),
            symbol_path=["py_helper"],
            component=None,
            accessor=None,
        )

        result = find_impact(selectors=[selector], project_path=str(project))

        # Python callers should be impacted
        impacted_names = [s.split("::")[-1] for s in result.impacted_symbols]
        assert "py_main" in impacted_names

        # TypeScript symbols should NOT be impacted
        assert "tsHelper" not in impacted_names
