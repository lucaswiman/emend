"""Tests for impact analysis on Rust projects."""
from pathlib import Path

import pytest

from emend.component_selector import ExtendedSelector


class TestRustImpact:
    """Tests for impact analysis on Rust projects."""

    def test_rust_impact_direct_caller(self, tmp_path):
        """Impact analysis finds direct callers in Rust (single file)."""
        from emend.transform import find_impact

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.rs"
        lib.write_text(
            "pub fn helper(x: i32) -> i32 {\n"
            "    x + 1\n"
            "}\n"
            "\n"
            "pub fn run() -> i32 {\n"
            "    helper(42)\n"
            "}\n"
        )

        selector = ExtendedSelector(
            file_path=str(lib),
            symbol_path=["helper"],
            component=None,
            accessor=None,
        )

        result = find_impact(selectors=[selector], project_path=str(project))

        assert len(result.changed_symbols) == 1
        assert "helper" in result.changed_symbols[0]

        impacted_names = [s.split("::")[-1] for s in result.impacted_symbols]
        assert "run" in impacted_names

    def test_rust_impact_two_callers_same_file(self, tmp_path):
        """Impact analysis finds multiple direct callers in a Rust file."""
        from emend.transform import find_impact

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.rs"
        lib.write_text(
            "pub fn target(x: i32) -> i32 {\n"
            "    x + 1\n"
            "}\n"
            "\n"
            "pub fn caller_a() -> i32 {\n"
            "    target(10)\n"
            "}\n"
            "\n"
            "pub fn caller_b() -> i32 {\n"
            "    target(20)\n"
            "}\n"
        )

        selector = ExtendedSelector(
            file_path=str(lib),
            symbol_path=["target"],
            component=None,
            accessor=None,
        )

        result = find_impact(selectors=[selector], project_path=str(project))

        assert len(result.changed_symbols) == 1
        assert "target" in result.changed_symbols[0]

        impacted_names = [s.split("::")[-1] for s in result.impacted_symbols]
        assert "caller_a" in impacted_names
        assert "caller_b" in impacted_names

    def test_rust_impact_detects_tests_directory(self, tmp_path):
        """Impact analysis identifies Rust tests/ directory files."""
        from emend.transform import find_impact

        project = tmp_path / "project"
        project.mkdir()

        # Put the callee and caller in the same file inside tests/
        tests_dir = project / "tests"
        tests_dir.mkdir()
        test_file = tests_dir / "test_lib.rs"
        test_file.write_text(
            "pub fn compute(x: i32) -> i32 {\n"
            "    x * 2\n"
            "}\n"
            "\n"
            "fn test_compute() {\n"
            "    let result = compute(3);\n"
            "}\n"
        )

        selector = ExtendedSelector(
            file_path=str(test_file),
            symbol_path=["compute"],
            component=None,
            accessor=None,
        )

        result = find_impact(selectors=[selector], project_path=str(project))

        # test_compute should be in impacted_tests (file is in tests/ directory)
        assert len(result.impacted_tests) > 0

    def test_rust_impact_test_prefix_detection(self, tmp_path):
        """Impact analysis identifies Rust test_ prefixed functions as tests."""
        from emend.transform import find_impact

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.rs"
        # Note: test_add must call add directly (not inside a macro like assert_eq!)
        # so the Rust scope resolver can track the call reference.
        lib.write_text(
            "pub fn add(a: i32, b: i32) -> i32 {\n"
            "    a + b\n"
            "}\n"
            "\n"
            "fn test_add() {\n"
            "    let result = add(1, 2);\n"
            "}\n"
        )

        selector = ExtendedSelector(
            file_path=str(lib),
            symbol_path=["add"],
            component=None,
            accessor=None,
        )

        result = find_impact(selectors=[selector], project_path=str(project))

        # test_add should be in impacted_tests (name starts with test_)
        assert len(result.impacted_tests) > 0
        test_names = " ".join(result.impacted_tests)
        assert "test_add" in test_names

    def test_rust_impact_no_callers(self, tmp_path):
        """Impact analysis returns empty impacted set for Rust function with no callers."""
        from emend.transform import find_impact

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "isolated.rs"
        lib.write_text(
            "pub fn lonely_func() -> i32 {\n"
            "    42\n"
            "}\n"
        )

        selector = ExtendedSelector(
            file_path=str(lib),
            symbol_path=["lonely_func"],
            component=None,
            accessor=None,
        )

        result = find_impact(selectors=[selector], project_path=str(project))

        assert len(result.changed_symbols) == 1
        assert len(result.impacted_symbols) == 0
        assert len(result.impacted_tests) == 0

    def test_rust_impact_witness_edges(self, tmp_path):
        """Impact analysis returns witness edges for Rust (single file)."""
        from emend.transform import find_impact

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.rs"
        lib.write_text(
            "pub fn target() -> i32 {\n"
            "    1\n"
            "}\n"
            "\n"
            "pub fn use_target() -> i32 {\n"
            "    target()\n"
            "}\n"
        )

        selector = ExtendedSelector(
            file_path=str(lib),
            symbol_path=["target"],
            component=None,
            accessor=None,
        )

        result = find_impact(selectors=[selector], project_path=str(project))

        assert len(result.edges) > 0
        edge_kinds = {e.kind for e in result.edges}
        assert "calls" in edge_kinds

    def test_rust_impact_changed_symbols_format(self, tmp_path):
        """Impact analysis returns the selector string for the changed symbol."""
        from emend.transform import find_impact

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "util.rs"
        lib.write_text(
            "pub fn util_fn() -> i32 {\n"
            "    0\n"
            "}\n"
        )

        selector = ExtendedSelector(
            file_path=str(lib),
            symbol_path=["util_fn"],
            component=None,
            accessor=None,
        )

        result = find_impact(selectors=[selector], project_path=str(project))

        assert len(result.changed_symbols) == 1
        # The changed symbol selector should contain the file and function name
        changed = result.changed_symbols[0]
        assert "util_fn" in changed

    def test_rust_impact_hash_test_decorator(self, tmp_path):
        """Impact analysis identifies Rust #[test] decorated functions."""
        from emend.transform import find_impact

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.rs"
        lib.write_text(
            "pub fn multiply(a: i32, b: i32) -> i32 {\n"
            "    a * b\n"
            "}\n"
            "\n"
            "#[test]\n"
            "fn test_multiply() {\n"
            "    let result = multiply(3, 4);\n"
            "}\n"
        )

        selector = ExtendedSelector(
            file_path=str(lib),
            symbol_path=["multiply"],
            component=None,
            accessor=None,
        )

        result = find_impact(selectors=[selector], project_path=str(project))

        # test_multiply should be in impacted_tests (has #[test] decorator
        # or starts with test_)
        assert len(result.impacted_tests) > 0
        test_names = " ".join(result.impacted_tests)
        assert "test_multiply" in test_names
