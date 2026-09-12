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


def test_method_identity_survives_fact_caching(tmp_path, monkeypatch):
    from emend import analysis_extraction
    from emend.analysis_store import AnalysisStore
    from emend.transform import find_callers, find_callees, generate_graph
    import json

    source = """struct A;
struct B;
fn alpha() {}
fn beta() {}
impl A { fn run(&self) { alpha(); } }
impl B { fn run(&self) { beta(); } }
fn entry(a: &A, b: &B) {
    a.run();
    b.run();
}
"""
    path = tmp_path / "lib.rs"
    path.write_text(source)
    store = AnalysisStore.open(tmp_path)
    original = store.query_facts()
    for owner, line, callee in [("A", 8, "alpha"), ("B", 9, "beta")]:
        selector = _make_selector(path, owner, "run")
        assert [r.line for r in find_callers(selector)] == [line]
        assert {c.name for c in find_callees(selector)} == {callee}
    assert {(m.receiver, m.method) for m in original.method_calls()
            if m.func_qn == "lib.entry"} == {("a", "run"), ("b", "run")}
    graph = json.loads(generate_graph(str(path), format="json"))
    assert graph["lib.A.run"] == ["alpha"]
    assert graph["lib.B.run"] == ["beta"]

    path.write_text(source.replace("a.run();", "b.run();"))
    assert not store.query_facts().calls_to("lib.A.run")
    monkeypatch.setattr(analysis_extraction, "_extract_file_facts",
                        lambda *a, **kw: pytest.fail("unchanged content was re-extracted"))
    path.write_text(source)
    assert store.query_facts().calls_to("lib.A.run") == original.calls_to("lib.A.run")


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

    @pytest.mark.parametrize("filters, lines", [
        ({}, {2, 3, 4}), ({"writes_only": True}, {2, 3}), ({"reads_only": True}, {3, 4}),
    ])
    def test_local_refs_keep_function_scope_and_tail_reads(self, tmp_path, filters, lines):
        from emend.transform import find_references

        path = tmp_path / "lib.rs"
        path.write_text(
            "fn mutate() -> i32 {\n"
            "    let mut x = 0;\n"
            "    x = x + 1;\n"
            "    x\n"
            "}\n"
            "fn other() -> i32 { let x = 2; x }\n"
        )
        refs = list(find_references(
            _make_selector(path, "mutate", "x"), project_path=str(tmp_path), **filters,
        ))
        assert {r.line for r in refs} == lines
        if filters:
            assert all(r.is_write == filters.get("writes_only", False) for r in refs)

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
