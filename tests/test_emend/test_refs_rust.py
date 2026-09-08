"""Tests for cross-language references, callers, and callees on Rust projects."""
from pathlib import Path

import pytest

from emend.component_selector import ExtendedSelector


def _make_selector(file_path, *symbol_path):
    return ExtendedSelector(
        file_path=str(file_path),
        symbol_path=list(symbol_path),
        component=None,
        accessor=None,
    )


class TestFindReferencesRust:
    """find_references() works on Rust projects."""

    def test_refs_finds_function_definition_and_usage(self, tmp_path):
        """find_references finds a Rust function definition and its usage."""
        from emend.transform import find_references

        project = tmp_path / "project"
        project.mkdir()

        lib_rs = project / "lib.rs"
        lib_rs.write_text(
            "fn process(x: i32) -> i32 {\n"
            "    x + 1\n"
            "}\n"
            "\n"
            "fn main() {\n"
            "    let y = process(5);\n"
            "    println!(\"{}\", y);\n"
            "}\n"
        )

        selector = _make_selector(lib_rs, "process")
        refs = list(find_references(selector, project_path=str(project)))

        assert len(refs) >= 2, (
            f"Expected at least 2 refs (definition + usage), got {len(refs)}: {refs}"
        )
        ref_lines = {r.line for r in refs}
        # Definition on line 1, usage on line 6
        assert 1 in ref_lines, f"Expected definition on line 1, got lines {ref_lines}"
        assert 6 in ref_lines, f"Expected usage on line 6, got lines {ref_lines}"

    def test_refs_finds_struct_references(self, tmp_path):
        """find_references finds references to a Rust struct."""
        from emend.transform import find_references

        project = tmp_path / "project"
        project.mkdir()

        lib_rs = project / "lib.rs"
        lib_rs.write_text(
            "pub struct Config {\n"
            "    pub value: i32,\n"
            "}\n"
            "\n"
            "fn make_config() -> Config {\n"
            "    Config { value: 42 }\n"
            "}\n"
        )

        selector = _make_selector(lib_rs, "Config")
        refs = list(find_references(selector, project_path=str(project)))

        assert len(refs) >= 2, (
            f"Expected at least 2 refs (definition + usage), got {len(refs)}: {refs}"
        )

    def test_refs_finds_variable_references(self, tmp_path):
        """find_references finds references to a Rust variable."""
        from emend.transform import find_references

        project = tmp_path / "project"
        project.mkdir()

        lib_rs = project / "lib.rs"
        lib_rs.write_text(
            "fn compute() -> i32 {\n"
            "    let count = 0;\n"
            "    let result = count + 1;\n"
            "    result\n"
            "}\n"
        )

        selector = _make_selector(lib_rs, "count")
        refs = list(find_references(selector, project_path=str(project)))

        # count is defined on line 2 and read on line 3
        assert len(refs) >= 2, (
            f"Expected at least 2 refs (definition + usage), got {len(refs)}: {refs}"
        )

    def test_refs_writes_only(self, tmp_path):
        """--writes-only filters to write references."""
        from emend.transform import find_references

        project = tmp_path / "project"
        project.mkdir()

        lib_rs = project / "lib.rs"
        lib_rs.write_text(
            "fn mutate() {\n"
            "    let mut x = 0;\n"
            "    x = x + 1;\n"
            "    println!(\"{}\", x);\n"
            "}\n"
        )

        selector = _make_selector(lib_rs, "x")

        all_refs = list(find_references(selector, project_path=str(project)))
        writes_only = list(
            find_references(selector, project_path=str(project), writes_only=True)
        )

        assert len(writes_only) < len(all_refs), (
            f"writes_only ({len(writes_only)}) should be fewer than all refs ({len(all_refs)})"
        )
        assert all(r.is_write for r in writes_only), (
            f"All writes_only refs should have is_write=True, got {writes_only}"
        )

    def test_refs_reads_only(self, tmp_path):
        """--reads-only filters to read references."""
        from emend.transform import find_references

        project = tmp_path / "project"
        project.mkdir()

        lib_rs = project / "lib.rs"
        lib_rs.write_text(
            "fn mutate() {\n"
            "    let mut x = 0;\n"
            "    x = x + 1;\n"
            "    println!(\"{}\", x);\n"
            "}\n"
        )

        selector = _make_selector(lib_rs, "x")

        all_refs = list(find_references(selector, project_path=str(project)))
        reads_only = list(
            find_references(selector, project_path=str(project), reads_only=True)
        )

        assert len(reads_only) > 0, "Expected at least one read reference"
        assert len(reads_only) <= len(all_refs), (
            "reads_only should be a subset of all refs"
        )
        assert all(not r.is_write for r in reads_only), (
            f"All reads_only refs should have is_write=False, got {reads_only}"
        )

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason="Rust path-qualified references are not resolved across files",
    )
    def test_refs_cross_file(self, tmp_path):
        """find_references finds references to a Rust function across files."""
        from emend.transform import find_references

        project = tmp_path / "project"
        project.mkdir()

        lib_rs = project / "lib.rs"
        lib_rs.write_text(
            "pub fn helper(x: i32) -> i32 {\n"
            "    x * 2\n"
            "}\n"
        )

        main_rs = project / "main.rs"
        main_rs.write_text(
            "mod lib;\n"
            "\n"
            "fn main() {\n"
            "    let result = lib::helper(21);\n"
            "    println!(\"{}\", result);\n"
            "}\n"
        )

        selector = _make_selector(lib_rs, "helper")
        refs = list(find_references(selector, project_path=str(project)))

        ref_files = {Path(r.file_path).name for r in refs}
        assert {"lib.rs", "main.rs"} <= ref_files, (
            f"Expected definition and cross-file usage, got {ref_files}"
        )


class TestFindCallersRust:
    """find_callers() works on Rust projects."""

    def test_callers_finds_function_callers(self, tmp_path):
        """find_callers finds functions that call a Rust function."""
        from emend.transform import find_callers

        project = tmp_path / "project"
        project.mkdir()

        lib_rs = project / "lib.rs"
        lib_rs.write_text(
            "pub fn process(x: i32) -> i32 {\n"
            "    x + 1\n"
            "}\n"
            "\n"
            "fn run() -> i32 {\n"
            "    let y = process(42);\n"
            "    y\n"
            "}\n"
        )

        selector = _make_selector(lib_rs, "process")
        callers = list(find_callers(selector, project_path=str(project)))

        assert len(callers) > 0, f"Expected callers for 'process', got none"
        caller_lines = {r.line for r in callers}
        # process(42) is called on line 6
        assert 6 in caller_lines, (
            f"Expected caller on line 6, got caller lines {caller_lines}"
        )

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason="Rust method calls are not attributed to their impl methods",
    )
    def test_callers_finds_method_callers(self, tmp_path):
        """find_callers attributes obj.method() calls to the impl method."""
        from emend.transform import find_callers

        project = tmp_path / "project"
        project.mkdir()

        lib_rs = project / "lib.rs"
        lib_rs.write_text(
            "struct Counter {\n"
            "    value: i32,\n"
            "}\n"
            "\n"
            "impl Counter {\n"
            "    fn increment(&mut self) {\n"
            "        self.value += 1;\n"
            "    }\n"
            "}\n"
            "\n"
            "fn use_counter() {\n"
            "    let mut c = Counter { value: 0 };\n"
            "    c.increment();\n"
            "    c.increment();\n"
            "}\n"
        )

        selector = _make_selector(lib_rs, "Counter", "increment")
        callers = list(find_callers(selector, project_path=str(project)))

        assert {r.line for r in callers} == {13, 14}

    def test_callers_same_file(self, tmp_path):
        """find_callers finds calls to the target within its own file."""
        from emend.transform import find_callers

        project = tmp_path / "project"
        project.mkdir()

        lib_rs = project / "lib.rs"
        lib_rs.write_text(
            "fn helper() -> i32 {\n"
            "    42\n"
            "}\n"
            "\n"
            "fn main() {\n"
            "    let x = helper();\n"
            "    println!(\"{}\", x);\n"
            "}\n"
        )

        selector = _make_selector(lib_rs, "helper")
        callers = list(find_callers(selector, project_path=str(project)))

        assert len(callers) > 0, "Should find caller in same file"
        caller_lines = {r.line for r in callers}
        # helper() called on line 6
        assert 6 in caller_lines, (
            f"Expected call on line 6, got caller lines {caller_lines}"
        )

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason="Rust path-qualified calls are not attributed across files",
    )
    def test_callers_cross_file(self, tmp_path):
        """find_callers attributes a path-qualified cross-file call."""
        from emend.transform import find_callers

        project = tmp_path / "project"
        project.mkdir()

        utils_rs = project / "utils.rs"
        utils_rs.write_text(
            "pub fn compute(x: i32) -> i32 {\n"
            "    x * x\n"
            "}\n"
        )

        main_rs = project / "main.rs"
        main_rs.write_text(
            "mod utils;\n"
            "\n"
            "fn main() {\n"
            "    let result = utils::compute(5);\n"
            "    println!(\"{}\", result);\n"
            "}\n"
        )

        selector = _make_selector(utils_rs, "compute")
        callers = list(find_callers(selector, project_path=str(project)))

        assert [(Path(r.file_path).name, r.line) for r in callers] == [("main.rs", 4)]


class TestFindCalleesRust:
    """find_callees() works on Rust projects."""

    def test_callees_finds_called_functions(self, tmp_path):
        """find_callees lists functions called inside a Rust function."""
        from emend.transform import find_callees

        project = tmp_path / "project"
        project.mkdir()

        lib_rs = project / "lib.rs"
        lib_rs.write_text(
            "fn helper() -> i32 {\n"
            "    42\n"
            "}\n"
            "\n"
            "fn utility() -> i32 {\n"
            "    99\n"
            "}\n"
            "\n"
            "fn main() {\n"
            "    let a = helper();\n"
            "    let b = utility();\n"
            "    println!(\"{} {}\", a, b);\n"
            "}\n"
        )

        selector = _make_selector(lib_rs, "main")
        callees = find_callees(selector, project_path=str(project))

        callee_names = {c.name for c in callees}
        assert "helper" in callee_names, (
            f"Expected 'helper' in callees, got {callee_names}"
        )
        assert "utility" in callee_names, (
            f"Expected 'utility' in callees, got {callee_names}"
        )

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason="Rust method-call callees are reported as receiver names",
    )
    def test_callees_method_calls(self, tmp_path):
        """find_callees reports method names from obj.method() calls."""
        from emend.transform import find_callees

        project = tmp_path / "project"
        project.mkdir()

        lib_rs = project / "lib.rs"
        lib_rs.write_text(
            "fn process(items: &mut Vec<i32>) -> Vec<i32> {\n"
            "    let result = items.clone();\n"
            "    items.push(1);\n"
            "    result\n"
            "}\n"
        )

        selector = _make_selector(lib_rs, "process")
        callees = find_callees(selector, project_path=str(project))

        callee_names = {c.name for c in callees}
        assert {"clone", "push"} <= callee_names

    def test_callees_no_calls(self, tmp_path):
        """find_callees returns empty list for a Rust function with no calls."""
        from emend.transform import find_callees

        project = tmp_path / "project"
        project.mkdir()

        lib_rs = project / "lib.rs"
        lib_rs.write_text(
            "fn pure_fn(x: i32) -> i32 {\n"
            "    x * 2 + 1\n"
            "}\n"
        )

        selector = _make_selector(lib_rs, "pure_fn")
        callees = find_callees(selector, project_path=str(project))

        assert len(callees) == 0, (
            f"Expected no callees for pure arithmetic function, got {callees}"
        )

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason="Rust impl method bodies are not traversed for callees",
    )
    def test_callees_impl_method(self, tmp_path):
        """find_callees traverses a Rust impl method body."""
        from emend.transform import find_callees

        project = tmp_path / "project"
        project.mkdir()

        lib_rs = project / "lib.rs"
        lib_rs.write_text(
            "fn validate(x: i32) -> bool {\n"
            "    x > 0\n"
            "}\n"
            "\n"
            "struct Processor;\n"
            "\n"
            "impl Processor {\n"
            "    fn run(&self, x: i32) -> bool {\n"
            "        validate(x)\n"
            "    }\n"
            "}\n"
        )

        selector = _make_selector(lib_rs, "Processor", "run")
        callees = find_callees(selector, project_path=str(project))

        callee_names = {c.name for c in callees}
        assert "validate" in callee_names, (
            f"Expected 'validate' in callees of run(), got {callee_names}"
        )
