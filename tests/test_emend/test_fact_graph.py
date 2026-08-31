"""Tests for the CozoDB-backed relational fact graph."""

import json

import pytest


def test_normalize_qn_collapses_relative_separators():
    from emend.fact_graph import _normalize_qn

    assert _normalize_qn("'../pkg'::Thing/method") == "pkg.Thing.method"

from emend.fact_graph import (
    CallFact,
    CfgBlockFact,
    CfgEdgeFact,
    DecoratorOnFact,
    DefUseFact,
    EntryPointDecoratorFact,
    EntryPointNameFact,
    ExportedSymbolFact,
    FactGraph,
    FuncSummaryFact,
    ImportFact,
    MethodCallFact,
    ReferenceFact,
    SourceLocFact,
    SymbolFact,
    TraceFlowFact,
    TypeFact,
    flows_from,
    flows_to,
    symbol_has_type,
)


def _make_graph() -> FactGraph:
    """Build a small graph for testing."""
    g = FactGraph()
    g.add_symbol(SymbolFact("app.py", "main", "app.main", "function", 1, 5, None))
    g.add_symbol(SymbolFact("app.py", "helper", "app.helper", "function", 7, 10, None))
    g.add_symbol(SymbolFact("lib.py", "compute", "lib.compute", "function", 1, 8, None))
    g.add_symbol(SymbolFact("lib.py", "MyClass", "lib.MyClass", "class", 10, 20, None))
    g.add_symbol(SymbolFact("lib.py", "method", "lib.MyClass.method", "method", 11, 15, "lib.MyClass"))

    g.add_call(CallFact("app.main", "app.helper", "app.py", 3, 4))
    g.add_call(CallFact("app.main", "lib.compute", "app.py", 4, 4))
    g.add_call(CallFact("app.helper", "lib.compute", "app.py", 8, 4))

    g.add_reference(ReferenceFact("lib.compute", "app.py", 4, 4, "call"))
    g.add_reference(ReferenceFact("lib.compute", "app.py", 8, 4, "call"))
    g.add_reference(ReferenceFact("lib.MyClass", "app.py", 2, 0, "import"))

    g.add_trace_flow(TraceFlowFact("user_input", "query", "sqli", "app.py", "app.main", 3, 5))
    g.add_type(TypeFact("app.main", "() -> None", "app.py", 1, "annotation"))
    g.add_type(TypeFact("lib.compute", "(int) -> int", "lib.py", 1, "inferred"))

    g.add_import(ImportFact("app.py", "lib", "compute", None, 1))
    g.add_import(ImportFact("app.py", "lib", "MyClass", None, 2))
    return g


class TestSymbolQueries:
    def test_symbols_all(self):
        g = _make_graph()
        assert len(g.symbols()) == 5

    def test_symbols_by_name(self):
        g = _make_graph()
        results = g.symbols(name="compute")
        assert len(results) == 1
        assert results[0].qualified_name == "lib.compute"

    def test_symbols_by_kind(self):
        g = _make_graph()
        funcs = g.symbols(kind="function")
        assert len(funcs) == 3
        classes = g.symbols(kind="class")
        assert len(classes) == 1

    def test_symbols_by_file(self):
        g = _make_graph()
        lib_syms = g.symbols(file_path="lib.py")
        assert len(lib_syms) == 3

    def test_symbols_multi_filter(self):
        g = _make_graph()
        results = g.symbols(kind="function", file_path="app.py")
        assert len(results) == 2


class TestCallQueries:
    def test_calls_from(self):
        g = _make_graph()
        calls = g.calls_from("app.main")
        assert len(calls) == 2
        callees = {c.callee_qn for c in calls}
        assert callees == {"app.helper", "lib.compute"}

    def test_calls_to(self):
        g = _make_graph()
        calls = g.calls_to("lib.compute")
        assert len(calls) == 2
        callers = {c.caller_qn for c in calls}
        assert callers == {"app.main", "app.helper"}

    def test_transitive_callers(self):
        g = _make_graph()
        callers = g.transitive_callers("lib.compute")
        assert "app.main" in callers
        assert "app.helper" in callers

    def test_transitive_callees(self):
        g = _make_graph()
        callees = g.transitive_callees("app.main")
        assert "app.helper" in callees
        assert "lib.compute" in callees


class TestReferenceQueries:
    def test_references_to(self):
        g = _make_graph()
        refs = g.references_to("lib.compute")
        assert len(refs) == 2
        assert all(r.ref_kind == "call" for r in refs)

    def test_references_import(self):
        g = _make_graph()
        refs = g.references_to("lib.MyClass")
        assert len(refs) == 1
        assert refs[0].ref_kind == "import"


class TestTraceFlowQueries:
    def test_trace_flows_all(self):
        g = _make_graph()
        flows = g.trace_flows()
        assert len(flows) == 1

    def test_trace_flows_by_label(self):
        g = _make_graph()
        flows = g.trace_flows(label="sqli")
        assert len(flows) == 1
        assert flows[0].source_var == "user_input"

    def test_trace_flows_by_file(self):
        g = _make_graph()
        flows = g.trace_flows(file_path="app.py")
        assert len(flows) == 1

    def test_trace_flows_no_match(self):
        g = _make_graph()
        assert g.trace_flows(label="nonexistent") == []


class TestTypeQueries:
    def test_types_for(self):
        g = _make_graph()
        types = g.types_for("app.main")
        assert len(types) == 1
        assert types[0].type_str == "() -> None"


class TestImportQueries:
    def test_imports_in(self):
        g = _make_graph()
        imports = g.imports_in("app.py")
        assert len(imports) == 2

    def test_imports_in_empty(self):
        g = _make_graph()
        assert g.imports_in("lib.py") == []


class TestRustImportExtraction:
    """Tests for Rust import extraction via tree-sitter (Phase 4 migration)."""

    def _extract(self, content: str) -> list[ImportFact]:
        from emend.fact_graph import _extract_imports_rust
        # Reset the cached resolver between tests to ensure isolation
        if hasattr(_extract_imports_rust, "_resolver"):
            del _extract_imports_rust._resolver
        return _extract_imports_rust("test.rs", content)

    def test_simple_use(self):
        """``use std::io;`` — plain scoped import."""
        facts = self._extract("use std::io;\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "std"
        assert f.imported_name == "io"
        assert f.alias is None
        assert f.line == 1

    def test_nested_use_tree(self):
        """``use std::{io, fmt::{self, Display}}`` — nested use tree."""
        src = "use std::{io, fmt::{self, Display}};\n"
        facts = self._extract(src)
        by_name = {f.imported_name: f for f in facts}
        # io from std
        assert "io" in by_name
        assert by_name["io"].imported_module == "std"
        # fmt (self) from std::fmt
        assert "fmt" in by_name
        assert by_name["fmt"].imported_module == "std"
        # Display from std::fmt
        assert "Display" in by_name
        assert by_name["Display"].imported_module == "std::fmt"

    def test_pub_use_reexport(self):
        """``pub use crate::foo::Bar`` — re-export."""
        facts = self._extract("pub use crate::foo::Bar;\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "crate::foo"
        assert f.imported_name == "Bar"
        assert f.alias is None

    def test_aliased_import(self):
        """``use std::collections::HashMap as HM`` — aliased import."""
        facts = self._extract("use std::collections::HashMap as HM;\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "std::collections"
        assert f.imported_name == "HashMap"
        assert f.alias == "HM"

    def test_glob_import(self):
        """``use std::io::*;`` — wildcard/glob import."""
        facts = self._extract("use std::io::*;\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "std::io"
        assert f.imported_name == "*"
        assert f.alias is None

    def test_mod_declaration(self):
        """``mod sub_module;`` — external module declaration."""
        facts = self._extract("mod sub_module;\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "sub_module"
        assert f.imported_name is None
        assert f.alias is None
        assert f.line == 1

    def test_aliased_relative_import(self):
        """``use super::baz as b;`` — aliased relative import."""
        facts = self._extract("use super::baz as b;\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "super"
        assert f.imported_name == "baz"
        assert f.alias == "b"

    def test_pub_crate_visibility(self):
        """``pub(crate) use std::sync::Arc;`` — visibility modifier ignored."""
        facts = self._extract("pub(crate) use std::sync::Arc;\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "std::sync"
        assert f.imported_name == "Arc"

    def test_line_numbers(self):
        """Line numbers are correctly reported for each import."""
        src = "use std::io;\nuse std::fmt;\nmod helper;\n"
        facts = self._extract(src)
        lines = {f.imported_module: f.line for f in facts}
        assert lines["std"] == 1 or lines.get("std") in (1, 2)
        # Check at least two different lines appear
        line_vals = [f.line for f in facts]
        assert len(set(line_vals)) >= 2

    def test_inline_mod_not_extracted(self):
        """``mod name { ... }`` inline modules are NOT extracted as imports."""
        src = "mod inner {\n    pub fn foo() {}\n}\n"
        facts = self._extract(src)
        assert len(facts) == 0


class TestSerialization:
    def test_roundtrip(self):
        g = _make_graph()
        json_str = g.to_json()
        g2 = FactGraph.from_json(json_str)

        assert len(g2.symbols()) == len(g.symbols())
        assert len(g2.calls_from("app.main")) == len(g.calls_from("app.main"))
        assert len(g2.references_to("lib.compute")) == len(g.references_to("lib.compute"))
        assert len(g2.trace_flows()) == len(g.trace_flows())
        assert len(g2.types_for("app.main")) == len(g.types_for("app.main"))
        assert len(g2.imports_in("app.py")) == len(g.imports_in("app.py"))

    def test_json_parseable(self):
        g = _make_graph()
        data = json.loads(g.to_json())
        assert isinstance(data, list)
        assert any(d["_type"] == "SymbolFact" for d in data)

    def test_exported_symbols_are_file_owned_and_serialized(self):
        g = FactGraph()
        g.add_exported_symbols_batch([
            ExportedSymbolFact("one.ts", "pkg.shared"),
            ExportedSymbolFact("two.ts", "pkg.shared"),
        ])

        assert {
            (fact.file_path, fact.qualified_name)
            for fact in g.exported_symbols()
        } == {("one.ts", "pkg.shared"), ("two.ts", "pkg.shared")}

        roundtrip = FactGraph.from_json(g.to_json())
        assert {
            (fact.file_path, fact.qualified_name)
            for fact in roundtrip.exported_symbols()
        } == {("one.ts", "pkg.shared"), ("two.ts", "pkg.shared")}

    def test_legacy_export_inputs_and_json_remain_global(self):
        g = FactGraph()
        g.add_exported_symbols_batch(["pkg.public"])
        legacy_json = '[{"qualified_name": "pkg.old", "_type": "ExportedSymbolFact"}]'

        assert g.exported_symbols() == [ExportedSymbolFact("", "pkg.public")]
        assert FactGraph.from_json(legacy_json).exported_symbols() == [
            ExportedSymbolFact("", "pkg.old")
        ]


class TestGenericQuery:
    def test_query_predicate(self):
        g = _make_graph()
        results = g.query(lambda f: isinstance(f, SymbolFact) and f.kind == "class")
        assert len(results) == 1
        assert results[0].name == "MyClass"

    def test_query_finds_method_call_facts(self):
        """query() must include MethodCallFact entries."""
        g = FactGraph()
        g.add_method_call(MethodCallFact(
            file_path="test.py", func_qn="foo", receiver="obj",
            method="bar", block_id=0, line=1,
        ))
        results = g.query(lambda f: isinstance(f, MethodCallFact))
        assert len(results) == 1
        assert results[0].method == "bar"


class TestPredicateHelpers:
    def test_flows_from(self):
        g = _make_graph()
        pred = flows_from("user_input")
        results = g.query(pred)
        assert len(results) == 1

    def test_flows_to(self):
        g = _make_graph()
        pred = flows_to("query")
        results = g.query(pred)
        assert len(results) == 1

    def test_symbol_has_type(self):
        g = _make_graph()
        pred = symbol_has_type(r"-> None")
        results = g.query(pred)
        assert len(results) == 1
        assert results[0].symbol_qn == "app.main"


class TestRstSectionParser:
    def test_facts_section_key_resolves(self):
        """The RST key_map must resolve the Fact graph heading to 'facts'."""
        import re as _re
        from pathlib import Path

        key_map = {
            "selector_syntax": "selectors",
            "pattern_syntax": "patterns",
            "commands": "commands",
            "cookbook_recipes": "recipes",
            "fact_graph_(``facts_query``)": "facts",
        }

        rst_path = Path(__file__).resolve().parents[2] / "src" / "emend" / "grammar_and_cookbook.rst"
        text = rst_path.read_text()
        lines = text.split("\n")
        section_keys = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if (
                i > 0
                and stripped
                and all(c == "-" for c in stripped)
                and len(stripped) >= 3
            ):
                heading = lines[i - 1].strip()
                if heading:
                    raw_key = _re.sub(r"\s+", "_", heading.lower())
                    key = key_map.get(raw_key, raw_key)
                    section_keys.append(key)

        assert "facts" in section_keys, (
            f"'facts' section not found. Derived keys: {section_keys}"
        )


# ---------------------------------------------------------------------------
# CozoDB-specific tests
# ---------------------------------------------------------------------------


class TestCozoScriptQueries:
    """Test raw CozoScript queries against the fact graph."""

    def test_raw_query_symbols(self):
        g = _make_graph()
        result = g.run_query(
            '?[name, kind] := *symbol[qn, fp, name, kind, line, end, parent]'
        )
        assert len(result["rows"]) == 5
        names = {r[0] for r in result["rows"]}
        assert "main" in names
        assert "compute" in names

    def test_raw_query_dead_code(self):
        g = _make_graph()
        result = g.run_query(
            'has_ref[qn] := *reference[qn, _, _, _, _, _, _]\n'
            'dead[name, qn] := *symbol[qn, _, name, _, _, _, _], not has_ref[qn]\n'
            '?[name, qn] := dead[name, qn]'
        )
        dead_names = {r[0] for r in result["rows"]}
        # main and helper have no direct references in our test data
        assert "main" in dead_names
        assert "helper" in dead_names
        # compute and MyClass have references
        assert "compute" not in dead_names
        assert "MyClass" not in dead_names

    def test_raw_query_transitive_closure(self):
        g = _make_graph()
        result = g.run_query(
            'reaches[b] := *call["app.main", b, _, _, _, _, _]\n'
            'reaches[b] := *call[mid, b, _, _, _, _, _], reaches[mid]\n'
            '?[b] := reaches[b]'
        )
        reached = {r[0] for r in result["rows"]}
        assert "app.helper" in reached
        assert "lib.compute" in reached

    def test_raw_query_with_params(self):
        g = _make_graph()
        result = g.run_query(
            '?[name, fp] := *symbol[qn, fp, name, kind, _, _, _], kind == "class"'
        )
        assert len(result["rows"]) == 1
        assert result["rows"][0][0] == "MyClass"


class TestDeadCodeDatalog:
    """Test the dead_code() Datalog query method."""

    def test_dead_code_finds_unreferenced(self):
        g = _make_graph()
        dead = g.dead_code()
        dead_qns = {s.qualified_name for s in dead}
        # main and helper have no incoming references
        assert "app.main" in dead_qns
        assert "app.helper" in dead_qns
        # method has no refs either
        assert "lib.MyClass.method" in dead_qns

    def test_dead_code_excludes_referenced(self):
        g = _make_graph()
        dead = g.dead_code()
        dead_qns = {s.qualified_name for s in dead}
        assert "lib.compute" not in dead_qns
        assert "lib.MyClass" not in dead_qns


class TestBatchOperations:
    """Test bulk insert operations."""

    def test_batch_symbols(self):
        g = FactGraph()
        facts = [
            SymbolFact("a.py", "foo", "a.foo", "function", 1, 5, None),
            SymbolFact("a.py", "bar", "a.bar", "function", 7, 10, None),
            SymbolFact("b.py", "baz", "b.baz", "class", 1, 20, None),
        ]
        g.add_symbols_batch(facts)
        assert len(g.symbols()) == 3
        assert len(g.symbols(file_path="a.py")) == 2

    def test_batch_calls(self):
        g = FactGraph()
        g.add_symbols_batch([
            SymbolFact("a.py", "foo", "a.foo", "function", 1, 5, None),
            SymbolFact("a.py", "bar", "a.bar", "function", 7, 10, None),
        ])
        g.add_calls_batch([
            CallFact("a.foo", "a.bar", "a.py", 3, 0),
        ])
        assert len(g.calls_from("a.foo")) == 1

    def test_batch_references(self):
        g = FactGraph()
        g.add_references_batch([
            ReferenceFact("mod.func", "app.py", 10, 0, "call"),
            ReferenceFact("mod.func", "app.py", 20, 0, "read"),
        ])
        refs = g.references_to("mod.func")
        assert len(refs) == 2

    def test_batch_imports(self):
        g = FactGraph()
        g.add_imports_batch([
            ImportFact("app.py", "os", "path", None, 1),
            ImportFact("app.py", "sys", None, None, 2),
        ])
        imports = g.imports_in("app.py")
        assert len(imports) == 2


class TestImpactClosure:
    """Test the impact_closure() Datalog query method."""

    def test_impact_direct_callers(self):
        """Changing a symbol finds its direct callers as impacted."""
        g = _make_graph()
        result = g.impact_closure({"lib.compute"})
        # app.main and app.helper both call lib.compute
        assert "app.main" in result["impacted"]
        assert "app.helper" in result["impacted"]

    def test_impact_transitive(self):
        """Impact propagates transitively through the call graph."""
        g = FactGraph()
        g.add_symbol(SymbolFact("a.py", "a", "a.a", "function", 1, 5, None))
        g.add_symbol(SymbolFact("b.py", "b", "b.b", "function", 1, 5, None))
        g.add_symbol(SymbolFact("c.py", "c", "c.c", "function", 1, 5, None))
        g.add_call(CallFact("a.a", "b.b", "a.py", 2, 0))
        g.add_call(CallFact("b.b", "c.c", "b.py", 2, 0))

        result = g.impact_closure({"c.c"})
        # b.b calls c.c directly, a.a calls b.b transitively
        assert "b.b" in result["impacted"]
        assert "a.a" in result["impacted"]
        assert len(result["edges"]) >= 2

    def test_impact_empty_changes(self):
        g = _make_graph()
        result = g.impact_closure(set())
        assert result["impacted"] == set()
        assert result["edges"] == []

    def test_impact_excludes_changed_symbols(self):
        """Changed symbols themselves are not in the impacted set."""
        g = _make_graph()
        result = g.impact_closure({"lib.compute"})
        assert "lib.compute" not in result["impacted"]

    def test_impact_edges_are_witness_pairs(self):
        """Each edge is a (caller, callee) witness for why the caller is impacted."""
        g = _make_graph()
        result = g.impact_closure({"lib.compute"})
        assert result["edges"]
        for src, tgt in result["edges"]:
            assert src in result["impacted"]


class TestCascadeDead:
    """Test the cascade_dead() Datalog query method."""

    def test_cascade_finds_orphaned_callees(self):
        """Deleting the only caller of a symbol marks the callee as cascade-dead."""
        g = FactGraph()
        g.add_symbol(SymbolFact("a.py", "caller", "a.caller", "function", 1, 5, None))
        g.add_symbol(SymbolFact("b.py", "callee", "b.callee", "function", 1, 5, None))
        g.add_call(CallFact("a.caller", "b.callee", "a.py", 2, 0))
        # b.callee's only caller is a.caller

        cascade = g.cascade_dead({"a.caller"})
        cascade_qns = {s.qualified_name for s in cascade}
        assert "b.callee" in cascade_qns

    def test_cascade_spares_externally_called(self):
        """If a callee has callers outside the delete set, it's not cascade-dead."""
        g = FactGraph()
        g.add_symbol(SymbolFact("a.py", "caller1", "a.caller1", "function", 1, 5, None))
        g.add_symbol(SymbolFact("a.py", "caller2", "a.caller2", "function", 7, 10, None))
        g.add_symbol(SymbolFact("b.py", "callee", "b.callee", "function", 1, 5, None))
        g.add_call(CallFact("a.caller1", "b.callee", "a.py", 2, 0))
        g.add_call(CallFact("a.caller2", "b.callee", "a.py", 8, 0))

        # Only delete caller1; caller2 still references callee
        cascade = g.cascade_dead({"a.caller1"})
        cascade_qns = {s.qualified_name for s in cascade}
        assert "b.callee" not in cascade_qns

    def test_cascade_empty_deletes(self):
        g = _make_graph()
        assert g.cascade_dead(set()) == []

    def test_cascade_excludes_initial_deletes(self):
        """Initial delete targets are not returned in cascade results."""
        g = FactGraph()
        g.add_symbol(SymbolFact("a.py", "f", "a.f", "function", 1, 5, None))
        g.add_symbol(SymbolFact("b.py", "g", "b.g", "function", 1, 5, None))
        g.add_call(CallFact("a.f", "b.g", "a.py", 2, 0))

        cascade = g.cascade_dead({"a.f"})
        cascade_qns = {s.qualified_name for s in cascade}
        assert "a.f" not in cascade_qns


class TestUnreferencedSymbols:
    """Test the unreferenced_symbols() Datalog query method."""

    def test_basic_unreferenced(self):
        """Without exclusions, behaves like dead_code()."""
        g = _make_graph()
        dead = g.unreferenced_symbols()
        dead_qns = {s.qualified_name for s in dead}
        # main, helper, method have no references
        assert "app.main" in dead_qns
        assert "app.helper" in dead_qns

    def test_unreferenced_with_exclusions(self):
        """Excluding a caller makes its callee unreferenced."""
        g = FactGraph()
        g.add_symbol(SymbolFact("a.py", "f", "a.f", "function", 1, 5, None))
        g.add_symbol(SymbolFact("b.py", "g", "b.g", "function", 1, 5, None))
        g.add_call(CallFact("a.f", "b.g", "a.py", 2, 0))
        g.add_reference(ReferenceFact("b.g", "a.py", 2, 0, "call"))

        # Without exclusions, b.g is referenced
        dead = g.unreferenced_symbols()
        dead_qns = {s.qualified_name for s in dead}
        assert "b.g" not in dead_qns

        # Excluding a.f's refs, b.g becomes unreferenced
        dead = g.unreferenced_symbols(exclude_qns={"a.f"})
        dead_qns = {s.qualified_name for s in dead}
        assert "b.g" in dead_qns

    def test_unreferenced_filter_by_kind(self):
        g = _make_graph()
        dead = g.unreferenced_symbols(kinds={"function"})
        for s in dead:
            assert s.kind == "function"


class TestPersistence:
    """Test that CozoDB SQLite backend persists data."""

    def test_persist_and_reload(self, tmp_path):
        db_file = str(tmp_path / "test.db")

        # Create and populate
        g1 = FactGraph(db_path=db_file)
        g1.add_symbol(SymbolFact("a.py", "foo", "a.foo", "function", 1, 5, None))
        g1.add_call(CallFact("a.foo", "a.bar", "a.py", 3, 0))
        g1.close()

        # Reopen and verify
        g2 = FactGraph(db_path=db_file)
        syms = g2.symbols()
        assert len(syms) == 1
        assert syms[0].name == "foo"
        calls = g2.calls_from("a.foo")
        assert len(calls) == 1
        g2.close()


# ---------------------------------------------------------------------------
# Phase 1+2: Schema, CFG population, block-tagged references
# ---------------------------------------------------------------------------


def _make_graph_with_cfg() -> FactGraph:
    """Build a graph with CFG blocks, decorators, and block-tagged refs."""
    g = FactGraph()

    # Symbols
    g.add_symbol(SymbolFact("app.py", "main", "app.main", "function", 1, 10, None))
    g.add_symbol(SymbolFact("app.py", "helper", "app.helper", "function", 12, 20, None))
    g.add_symbol(SymbolFact("lib.py", "compute", "lib.compute", "function", 1, 8, None))
    g.add_symbol(SymbolFact("lib.py", "MyClass", "lib.MyClass", "class", 10, 30, None))
    g.add_symbol(SymbolFact("lib.py", "__init__", "lib.MyClass.__init__", "method", 11, 15, "lib.MyClass"))

    # Decorators
    g.add_decorator_on(DecoratorOnFact("app.main", "app.route"))
    g.add_decorator_on(DecoratorOnFact("lib.MyClass.__init__", "property"))

    # CFG blocks for app.main
    g.add_cfg_block(CfgBlockFact("app.py", "app.main", 0, is_entry=True))
    g.add_cfg_block(CfgBlockFact("app.py", "app.main", 1))
    g.add_cfg_block(CfgBlockFact("app.py", "app.main", 2))
    g.add_cfg_block(CfgBlockFact("app.py", "app.main", 3, is_exit=True))

    # CFG edges for app.main
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.main", 0, 1, "true_branch", 0, 0))
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.main", 0, 2, "false_branch", 0, 0))
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.main", 1, 3, "fallthrough", 0, 0))
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.main", 2, 3, "fallthrough", 0, 0))

    # CFG blocks for app.helper (with unreachable block)
    g.add_cfg_block(CfgBlockFact("app.py", "app.helper", 0, is_entry=True))
    g.add_cfg_block(CfgBlockFact("app.py", "app.helper", 1))
    g.add_cfg_block(CfgBlockFact("app.py", "app.helper", 2))  # unreachable
    g.add_cfg_block(CfgBlockFact("app.py", "app.helper", 3, is_exit=True))
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.helper", 0, 1, "fallthrough", 0, 0))
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.helper", 1, 3, "jump", 0, 0))
    # Note: block 2 has no incoming edge -> unreachable

    # Block-tagged references
    g.add_reference(ReferenceFact("lib.compute", "app.py", 3, 4, "call", func_qn="app.main", block_id=0))
    g.add_reference(ReferenceFact("lib.compute", "app.py", 5, 4, "call", func_qn="app.main", block_id=1))
    g.add_reference(ReferenceFact("app.helper", "app.py", 7, 4, "call", func_qn="app.main", block_id=2))
    g.add_reference(ReferenceFact("lib.MyClass", "app.py", 1, 0, "import"))  # module-level

    # Block-tagged calls
    g.add_call(CallFact("app.main", "lib.compute", "app.py", 3, 4, func_qn="app.main", block_id=0))
    g.add_call(CallFact("app.main", "lib.compute", "app.py", 5, 4, func_qn="app.main", block_id=1))
    g.add_call(CallFact("app.main", "app.helper", "app.py", 7, 4, func_qn="app.main", block_id=2))
    g.add_call(CallFact("app.helper", "lib.compute", "app.py", 14, 4, func_qn="app.helper", block_id=0))

    # Def-use with block IDs
    g.add_def_use(DefUseFact("app.py", "app.main", "x", def_block=0, use_block=1))
    g.add_def_use(DefUseFact("app.py", "app.main", "y", def_block=1, use_block=2))

    # Source locations
    g.add_source_loc(SourceLocFact("app.py", "symbol", "app.main", line=1, end_line=10))

    # Imports
    g.add_import(ImportFact("app.py", "lib", "compute", None, 1))
    g.add_import(ImportFact("app.py", "lib", "MyClass", None, 2))

    return g


class TestCfgBlockFacts:
    """Test CFG block relation operations."""

    def test_add_and_query_blocks(self):
        g = _make_graph_with_cfg()
        blocks = g.cfg_blocks(func_qn="app.main")
        assert len(blocks) == 4
        entry_blocks = [b for b in blocks if b.is_entry]
        assert len(entry_blocks) == 1
        assert entry_blocks[0].block_id == 0

    def test_query_blocks_by_file(self):
        g = _make_graph_with_cfg()
        blocks = g.cfg_blocks(file_path="app.py")
        # 4 blocks for main + 4 for helper
        assert len(blocks) == 8

    def test_batch_blocks(self):
        g = FactGraph()
        facts = [
            CfgBlockFact("a.py", "a.foo", 0, is_entry=True),
            CfgBlockFact("a.py", "a.foo", 1),
            CfgBlockFact("a.py", "a.foo", 2, is_exit=True),
        ]
        g.add_cfg_blocks_batch(facts)
        blocks = g.cfg_blocks(func_qn="a.foo")
        assert len(blocks) == 3


class TestDecoratorOnFacts:
    """Test decorator_on relation operations."""

    def test_add_and_query_decorators(self):
        g = _make_graph_with_cfg()
        decs = g.decorators_on("app.main")
        assert len(decs) == 1
        assert decs[0].decorator == "app.route"

    def test_batch_decorators(self):
        g = FactGraph()
        g.add_symbol(SymbolFact("a.py", "foo", "a.foo", "function", 1, 5, None))
        g.add_decorator_on_batch([
            DecoratorOnFact("a.foo", "route"),
            DecoratorOnFact("a.foo", "login_required"),
        ])
        decs = g.decorators_on("a.foo")
        assert len(decs) == 2
        dec_names = {d.decorator for d in decs}
        assert dec_names == {"route", "login_required"}


class TestSourceLocFacts:
    """Test source_loc relation operations."""

    def test_add_and_query_source_locs(self):
        g = _make_graph_with_cfg()
        locs = g.source_locs(loc_id="app.main")
        assert len(locs) == 1
        assert locs[0].line == 1
        assert locs[0].end_line == 10

    def test_source_locs_by_kind(self):
        g = _make_graph_with_cfg()
        locs = g.source_locs(loc_kind="symbol")
        assert len(locs) >= 1

    def test_batch_source_locs(self):
        g = FactGraph()
        g.add_source_locs_batch([
            SourceLocFact("a.py", "symbol", "a.foo", line=1, end_line=5),
            SourceLocFact("a.py", "symbol", "a.bar", line=7, end_line=10),
        ])
        locs = g.source_locs(loc_kind="symbol")
        assert len(locs) == 2


class TestBlockTaggedRefs:
    """Test block-tagged references and calls."""

    def test_reference_has_block_info(self):
        g = _make_graph_with_cfg()
        refs = g.references_to("lib.compute")
        assert len(refs) >= 2
        # At least one should have block info
        tagged = [r for r in refs if r.block_id >= 0]
        assert len(tagged) >= 2
        assert all(r.func_qn == "app.main" for r in tagged)

    def test_module_level_ref_has_sentinel(self):
        g = _make_graph_with_cfg()
        refs = g.references_to("lib.MyClass")
        assert len(refs) == 1
        assert refs[0].func_qn == ""
        assert refs[0].block_id == -1

    def test_call_has_block_info(self):
        g = _make_graph_with_cfg()
        calls = g.calls_to("lib.compute")
        assert len(calls) >= 2
        tagged = [c for c in calls if c.block_id >= 0]
        assert len(tagged) >= 2

    def test_def_use_has_block_ids(self):
        g = _make_graph_with_cfg()
        du = g.def_uses(func_qn="app.main")
        assert len(du) >= 2
        x_du = [d for d in du if d.var_name == "x"]
        assert len(x_du) == 1
        assert x_du[0].def_block == 0
        assert x_du[0].use_block == 1


class TestFuncSummaryFacts:
    """Test func_summary relation operations."""

    def test_add_and_query_summary(self):
        g = FactGraph()
        g.add_func_summary(FuncSummaryFact("a.foo", "x", flows_to_return=True))
        g.add_func_summary(FuncSummaryFact("a.foo", "y", flows_to_sink=True, sink_label="sqli"))
        summaries = g.func_summaries(func_qn="a.foo")
        assert len(summaries) == 2
        ret_flow = [s for s in summaries if s.flows_to_return]
        assert len(ret_flow) == 1
        assert ret_flow[0].param_name == "x"

    def test_batch_summaries(self):
        g = FactGraph()
        g.add_func_summaries_batch([
            FuncSummaryFact("a.foo", "x", flows_to_return=True),
            FuncSummaryFact("a.bar", "y", flows_to_sink=True, sink_label="xss"),
        ])
        all_summaries = g.func_summaries()
        assert len(all_summaries) == 2


# ---------------------------------------------------------------------------
# Phase 3: Direct relation queries via Datalog
# ---------------------------------------------------------------------------


class TestRefsDatalog:
    """Test refs_datalog() Datalog query method."""

    def test_refs_finds_all(self):
        g = _make_graph_with_cfg()
        refs = g.refs_datalog("lib.compute")
        assert len(refs) >= 2

    def test_refs_calls_only(self):
        g = _make_graph_with_cfg()
        refs = g.refs_datalog("lib.compute", calls_only=True)
        assert all(r.ref_kind == "call" for r in refs)

    def test_refs_no_imports(self):
        g = _make_graph_with_cfg()
        refs = g.refs_datalog("lib.MyClass", include_imports=False)
        assert all(r.ref_kind != "import" for r in refs)

    def test_refs_nonexistent_symbol(self):
        g = _make_graph_with_cfg()
        refs = g.refs_datalog("nonexistent.symbol")
        assert refs == []


class TestCallersDatalog:
    """Test callers_datalog() Datalog query method."""

    def test_callers_finds_direct(self):
        g = _make_graph_with_cfg()
        callers = g.callers_datalog("lib.compute")
        caller_qns = {c.caller_qn for c in callers}
        assert "app.main" in caller_qns
        assert "app.helper" in caller_qns

    def test_callers_empty_for_unreferenced(self):
        g = _make_graph_with_cfg()
        callers = g.callers_datalog("lib.MyClass.__init__")
        assert callers == []


class TestCalleesDatalog:
    """Test callees_datalog() Datalog query method."""

    def test_callees_of_function(self):
        g = _make_graph_with_cfg()
        callees = g.callees_datalog("app.main")
        callee_qns = {c.callee_qn for c in callees}
        assert "lib.compute" in callee_qns
        assert "app.helper" in callee_qns

    def test_callees_uses_func_qn_not_caller_qn(self):
        """callees_datalog uses func_qn (block context), not caller_qn."""
        g = _make_graph_with_cfg()
        callees = g.callees_datalog("app.main")
        # All calls tagged with func_qn="app.main" should be found
        assert len(callees) >= 2


class TestGraphDatalog:
    """Test graph_datalog() Datalog query method."""

    def test_graph_all_edges(self):
        g = _make_graph_with_cfg()
        edges = g.graph_datalog()
        assert len(edges) >= 3
        edge_set = set(edges)
        assert ("app.main", "lib.compute") in edge_set
        assert ("app.main", "app.helper") in edge_set
        assert ("app.helper", "lib.compute") in edge_set

    def test_graph_filtered_by_file(self):
        g = _make_graph_with_cfg()
        edges = g.graph_datalog(file_path="app.py")
        assert len(edges) >= 3


# ---------------------------------------------------------------------------
# Phase 4: Unified dead code via Datalog
# ---------------------------------------------------------------------------


class TestDeadCodeUnified:
    """Test dead_code_unified() Datalog query method."""

    def test_unified_finds_unreferenced(self):
        g = _make_graph_with_cfg()
        dead, _ = g.dead_code_unified()
        dead_qns = {s.qualified_name for s in dead}
        # app.main has no callers; app.helper and lib.compute are only
        # referenced from within app.main, which is itself dead, so they
        # cascade to dead. lib.MyClass.__init__ is a dunder entry point and
        # lib.MyClass is referenced via import, so both are excluded.
        assert dead_qns == {"app.main", "app.helper", "lib.compute"}

    def test_unified_respects_entry_point_decorators(self):
        g = _make_graph_with_cfg()
        g.add_entry_point_decorator(EntryPointDecoratorFact("app.route"))
        dead, _ = g.dead_code_unified(entry_point_decorators=["app.route"])
        dead_qns = {s.qualified_name for s in dead}
        # app.main has @app.route, so it's an entry point
        assert "app.main" not in dead_qns

    def test_unified_dunders_are_entry_points(self):
        g = _make_graph_with_cfg()
        dead, _ = g.dead_code_unified()
        dead_qns = {s.qualified_name for s in dead}
        # __init__ is a dunder, so it's an entry point
        assert "lib.MyClass.__init__" not in dead_qns

    def test_unified_entry_point_names(self):
        g = FactGraph()
        g.add_symbol(SymbolFact("a.py", "main", "a.main", "function", 1, 5, None))
        g.add_symbol(SymbolFact("a.py", "unused", "a.unused", "function", 7, 10, None))
        dead, _ = g.dead_code_unified(entry_point_names=["main"])
        dead_qns = {s.qualified_name for s in dead}
        assert "a.main" not in dead_qns
        assert "a.unused" in dead_qns


class TestUnreachableBlocksDatalog:
    """Test unreachable_blocks_datalog() Datalog query method."""

    def test_finds_unreachable(self):
        g = _make_graph_with_cfg()
        unreachable = g.unreachable_blocks_datalog(func_qn="app.helper")
        unreachable_ids = {b.block_id for b in unreachable}
        # Block 2 in app.helper has no incoming edge
        assert 2 in unreachable_ids

    def test_no_unreachable_in_main(self):
        g = _make_graph_with_cfg()
        unreachable = g.unreachable_blocks_datalog(func_qn="app.main")
        # All blocks in main are reachable
        unreachable_ids = {b.block_id for b in unreachable}
        assert 0 not in unreachable_ids
        assert 1 not in unreachable_ids
        assert 2 not in unreachable_ids

    def test_all_unreachable(self):
        g = _make_graph_with_cfg()
        unreachable = g.unreachable_blocks_datalog()
        # Should find block 2 in app.helper
        assert len(unreachable) >= 1


# ---------------------------------------------------------------------------
# Phase 5: Taint analysis via Datalog
# ---------------------------------------------------------------------------


class TestTracePropagationDatalog:
    """Test trace_propagation_datalog() Datalog method."""

    def test_basic_propagation(self):
        g = FactGraph()
        # Setup: function with source -> def-use -> sink
        g.add_def_use(DefUseFact("app.py", "app.main", "x", def_block=0, use_block=1))
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.main", "x", 0, "sqli")],
            sinks=[("app.py", "app.main", "x", 1, "sqli")],
        )
        assert len(flows) >= 1

    def test_no_flow_with_sanitizer(self):
        g = FactGraph()
        g.add_def_use(DefUseFact("app.py", "app.main", "x", def_block=0, use_block=1))
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.main", "x", 0, "sqli")],
            sinks=[("app.py", "app.main", "x", 1, "sqli")],
            sanitizers=[("app.py", "app.main", "x", 0, "sqli")],
        )
        assert len(flows) == 0

    def test_empty_sources(self):
        g = FactGraph()
        flows = g.trace_propagation_datalog(sources=[], sinks=[("a.py", "f", "x", 0, "l")])
        assert flows == []


class TestInterproceduralTraceDatalog:
    """Test interprocedural_trace_datalog() Datalog method."""

    def test_basic_interprocedural(self):
        g = FactGraph()
        g.add_symbol(SymbolFact("a.py", "foo", "a.foo", "function", 1, 5, None))
        g.add_symbol(SymbolFact("b.py", "bar", "b.bar", "function", 1, 5, None))
        g.add_call(CallFact("a.foo", "b.bar", "a.py", 2, 0))
        g.add_func_summary(FuncSummaryFact("b.bar", "x", flows_to_sink=True, sink_label="sqli"))
        violations = g.interprocedural_trace_datalog()
        assert len(violations) >= 1

    def test_no_summary_no_violation(self):
        g = FactGraph()
        g.add_call(CallFact("a.foo", "b.bar", "a.py", 2, 0))
        violations = g.interprocedural_trace_datalog()
        assert violations == []

    def test_config_driven_sources_and_sinks_filter_results(self):
        g = FactGraph()
        g.add_call(CallFact("a.foo", "b.bar", "a.py", 2, 0))
        g.add_func_summary(FuncSummaryFact("b.bar", "x", flows_to_sink=True, sink_label="sqli"))

        violations = g.interprocedural_trace_datalog(
            sources=[("a.py", "a.foo", "data", 0, "xss")],
            sinks=[("b.py", "b.bar", "query", 1, "xss")],
        )

        assert violations == []


class TestFlowRuleCheckDatalog:
    """Test flow_rule_check_datalog() Datalog method."""

    def test_basic_flow_check(self):
        g = FactGraph()
        g.add_def_use(DefUseFact("app.py", "app.main", "x", def_block=0, use_block=1))
        violations = g.flow_rule_check_datalog(
            sources=[("app.py", "app.main", "x", 0)],
            sinks=[("app.py", "app.main", "x", 1)],
        )
        assert len(violations) >= 1

    def test_flow_blocked_by_not_through(self):
        g = FactGraph()
        g.add_def_use(DefUseFact("app.py", "app.main", "x", def_block=0, use_block=1))
        violations = g.flow_rule_check_datalog(
            sources=[("app.py", "app.main", "x", 0)],
            sinks=[("app.py", "app.main", "x", 1)],
            not_through=[("app.py", "app.main", "x", 0)],
        )
        assert len(violations) == 0

    @pytest.mark.parametrize("required_block", [0, 2])
    def test_through_at_flow_endpoint_satisfies_requirement(self, required_block):
        g = FactGraph()
        g.add_def_use(DefUseFact("app.py", "app.main", "x", def_block=0, use_block=2))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.main", 0, 1, "fallthrough", 0, 0))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.main", 1, 2, "fallthrough", 0, 0))

        assert g.flow_rule_check_datalog(
            sources=[("app.py", "app.main", "x", 0)],
            sinks=[("app.py", "app.main", "x", 2)],
            through=[("app.py", "app.main", "x", required_block)],
        ) == []

    def test_empty_sources(self):
        g = FactGraph()
        violations = g.flow_rule_check_datalog(sources=[], sinks=[("a.py", "f", "x", 0)])
        assert violations == []

    def test_blocker_on_sink_block(self):
        """Blocker on the use_block (sink block) should suppress violation."""
        g = FactGraph()
        g.add_def_use(DefUseFact("app.py", "f", "x", def_block=0, use_block=1))
        # Blocker is in sink block (1)
        violations = g.flow_rule_check_datalog(
            sources=[("app.py", "f", "x", 0)],
            sinks=[("app.py", "f", "x", 1)],
            not_through=[("app.py", "f", "x", 1)],
        )
        assert len(violations) == 0

    def test_same_block_ordering_source_before_sink(self):
        """When source and sink share a block, source must be before sink."""
        g = FactGraph()
        g.add_def_use(DefUseFact("app.py", "f", "x", def_block=0, use_block=0))
        # source at line 5, sink at line 10 — should fire
        violations = g.flow_rule_check_datalog(
            sources=[("app.py", "f", "x", 0)],
            sinks=[("app.py", "f", "x", 0)],
            source_lines={("app.py", "f", 0): 5},
            sink_lines={("app.py", "f", 0): 10},
        )
        assert len(violations) >= 1

    def test_same_block_ordering_sink_before_source(self):
        """When source and sink share a block and sink is first, suppress."""
        g = FactGraph()
        g.add_def_use(DefUseFact("app.py", "f", "x", def_block=0, use_block=0))
        # source at line 10, sink at line 5 — should NOT fire
        violations = g.flow_rule_check_datalog(
            sources=[("app.py", "f", "x", 0)],
            sinks=[("app.py", "f", "x", 0)],
            source_lines={("app.py", "f", 0): 10},
            sink_lines={("app.py", "f", 0): 5},
        )
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# Updated serialization test for new fact types
# ---------------------------------------------------------------------------


class TestSerializationWithNewFacts:
    """Test JSON serialization includes new fact types."""

    def test_roundtrip_with_cfg_blocks(self):
        g = FactGraph()
        g.add_cfg_block(CfgBlockFact("a.py", "a.foo", 0, is_entry=True))
        g.add_cfg_block(CfgBlockFact("a.py", "a.foo", 1, is_exit=True))
        g.add_decorator_on(DecoratorOnFact("a.foo", "route"))
        g.add_source_loc(SourceLocFact("a.py", "symbol", "a.foo", line=1, end_line=5))
        g.add_func_summary(FuncSummaryFact("a.foo", "x", flows_to_return=True))

        json_str = g.to_json()
        data = json.loads(json_str)
        type_names = {d["_type"] for d in data}
        assert "CfgBlockFact" in type_names
        assert "DecoratorOnFact" in type_names
        assert "SourceLocFact" in type_names
        assert "FuncSummaryFact" in type_names

        # Roundtrip
        g2 = FactGraph.from_json(json_str)
        assert len(g2.cfg_blocks(func_qn="a.foo")) == 2
        assert len(g2.decorators_on("a.foo")) == 1
        assert len(g2.source_locs(loc_id="a.foo")) == 1
        assert len(g2.func_summaries(func_qn="a.foo")) == 1


# ---------------------------------------------------------------------------
# Bug regression: build_from_project MethodCallFact line numbers and
# MODULE_LEVEL fallback.
# ---------------------------------------------------------------------------


class TestBuildFromProjectMethodCallFacts:
    """build_from_project() must produce MethodCallFacts with 0-based lines
    (matching DefUseFact convention) and must apply the MODULE_LEVEL sentinel
    for method calls that occur outside any function."""

    def test_method_call_lines_are_zero_based(self, tmp_path):
        """MethodCallFact.line from build_from_project should be 0-based.

        The scope resolver returns 1-based line numbers.  build_from_project
        must subtract 1 before storing the fact, exactly as update_files does.
        """
        src = tmp_path / "app.py"
        # Method call obj.method() is on line 2 (1-based) / line 1 (0-based)
        src.write_text(
            "def foo(obj):\n"
            "    obj.method()\n"
            "    return 1\n"
        )
        graph = FactGraph.build_from_project(str(tmp_path))
        mc_facts = graph.method_calls()
        method_facts = [m for m in mc_facts if m.method == "method"]
        assert method_facts, "Expected a MethodCallFact for obj.method()"
        for mf in method_facts:
            # Line 2 in the file (1-based) → should be stored as 1 (0-based)
            assert mf.line == 1, (
                f"MethodCallFact.line should be 0-based (1) but got {mf.line}. "
                "build_from_project is not subtracting 1 from the scope resolver line."
            )

    def test_method_call_lines_consistent_with_update_files(self, tmp_path):
        """build_from_project and update_files must produce identical MethodCallFact lines."""
        src = tmp_path / "app.py"
        src.write_text(
            "def foo(obj):\n"
            "    obj.method()\n"
            "    return 1\n"
        )
        graph_bfp = FactGraph.build_from_project(str(tmp_path))
        graph_ufl = FactGraph()
        graph_ufl.update_files([(str(src), src.read_text())])

        mc_bfp = {(m.receiver, m.method, m.line) for m in graph_bfp.method_calls()}
        mc_ufl = {(m.receiver, m.method, m.line) for m in graph_ufl.method_calls()}
        assert mc_bfp == mc_ufl, (
            f"build_from_project produced {mc_bfp} but update_files produced {mc_ufl}. "
            "MethodCallFact line numbers are inconsistent between the two builders."
        )

    def test_persisted_builder_matches_project_method_calls(self, tmp_path):
        """Persisted and project builders emit identical method-call facts."""
        from emend.transform.cache import _build_facts_db, _cache_db_dir

        src = tmp_path / "app.py"
        src.write_text(
            "class Client:\n"
            "    def fetch(self):\n"
            "        return 1\n\n"
            "client = Client()\n"
            "client.fetch()\n"
        )

        _build_facts_db(str(tmp_path))
        persisted = FactGraph(db_path=str(_cache_db_dir(tmp_path) / "facts.db"))
        project = FactGraph.build_from_project(str(tmp_path))

        def method_calls(graph):
            return {
                (f.file_path, f.func_qn, f.receiver, f.method, f.block_id, f.line)
                for f in graph.method_calls()
            }

        assert method_calls(persisted) == method_calls(project)

    def test_module_level_method_call_has_sentinel_func_qn(self, tmp_path):
        """Module-level method calls must use the MODULE_LEVEL_FUNC sentinel.

        When a method call occurs outside any function, build_from_project must
        fall back to MODULE_LEVEL_FUNC / MODULE_LEVEL_BLOCK rather than storing
        ("", -1).
        """
        from emend.location_resolver import MODULE_LEVEL_FUNC
        src = tmp_path / "app.py"
        # Module-level method call (not inside any function)
        src.write_text("import os\nos.getcwd()\n")
        graph = FactGraph.build_from_project(str(tmp_path))
        mc_facts = graph.method_calls()
        getcwd_facts = [m for m in mc_facts if m.method == "getcwd"]
        assert getcwd_facts, "Expected a MethodCallFact for os.getcwd()"
        for mf in getcwd_facts:
            assert mf.func_qn == MODULE_LEVEL_FUNC, (
                f"Module-level method call should have func_qn={MODULE_LEVEL_FUNC!r} "
                f"but got {mf.func_qn!r}. build_from_project is missing the MODULE_LEVEL fallback."
            )


# ---------------------------------------------------------------------------
# Bug regression: dead_code_unified excl_clauses string replacement mangles
# path strings that contain "fp".
# ---------------------------------------------------------------------------


class TestDeadCodeExcludePathsWithFp:
    """dead_code_unified must not mangle exclude_reference_paths that contain
    the substring 'fp', which was corrupted by a naive .replace('fp','ref_fp')
    on the Datalog query string."""

    def test_exclude_path_containing_fp_substring(self):
        """A symbol referenced only from a path containing 'fp' should be
        treated as dead when that path is excluded.

        The bug: excl_clauses.replace('fp', 'ref_fp') would turn the literal
        path 'tests_fp/' into 'tests_ref_fp/' in the module_level_ref rule,
        causing the exclusion to silently fail.
        """
        g = FactGraph()
        # Symbol in lib.py with no other callers
        g.add_symbol(SymbolFact("lib.py", "unused_fn", "lib.unused_fn", "function", 1, 3, None))
        # The only reference is a module-level import/call from a file whose
        # path contains "fp" — exactly the substring that the buggy replace mangled.
        g.add_reference(ReferenceFact(
            symbol_qn="lib.unused_fn",
            file_path="tests_fp/test_lib.py",
            line=1, col=0, ref_kind="read",
        ))

        dead, _ = g.dead_code_unified(
            exclude_reference_paths=["tests_fp/"],
        )
        dead_qns = {s.qualified_name for s in dead}
        assert "lib.unused_fn" in dead_qns, (
            "lib.unused_fn should be dead: its only reference is from tests_fp/, "
            "which is in the exclusion list. The bug caused the path 'tests_fp/' "
            "to be mangled to 'tests_ref_fp/' so the exclusion did not apply."
        )

    def test_exclude_segment_containing_fp_substring(self):
        """Exclusion by segment name containing 'fp' must not corrupt the path."""
        g = FactGraph()
        g.add_symbol(SymbolFact("lib.py", "helper", "lib.helper", "function", 1, 3, None))
        g.add_reference(ReferenceFact(
            symbol_qn="lib.helper",
            file_path="fp_tests/test_lib.py",
            line=1, col=0, ref_kind="read",
        ))

        dead, _ = g.dead_code_unified(
            exclude_reference_segments=["fp_tests"],
        )
        dead_qns = {s.qualified_name for s in dead}
        assert "lib.helper" in dead_qns, (
            "lib.helper should be dead: its only reference is from fp_tests/, "
            "which is in the exclusion segment list. The buggy .replace('fp','ref_fp') "
            "corrupted 'fp_tests/' to 'ref_fp_tests/'."
        )


class TestMultiLineImportExtraction:
    """Verify that multi-line parenthesised imports are correctly extracted.

    The old hand-rolled regex matched only single-line imports and missed
    multi-line statements such as ``from foo import (\\n    bar,\\n    baz\\n)``.
    statement. The tree-sitter based ``_extract_imports_python`` must handle
    all such cases.
    """

    def _imports(self, source: str) -> list[str]:
        """Return a sorted list of (module, imported_name) string pairs."""
        from emend.fact_graph import _extract_imports_python
        facts = _extract_imports_python("test_module.py", source)
        return sorted(
            f"{f.imported_module}:{f.imported_name or ''}"
            for f in facts
        )

    def test_single_line_from_import(self):
        src = "from foo import bar\n"
        pairs = self._imports(src)
        assert "foo:bar" in pairs

    def test_multi_line_parenthesised_import(self):
        src = "from foo import (\n    bar,\n    baz\n)\n"
        pairs = self._imports(src)
        assert "foo:bar" in pairs, f"Expected foo:bar in {pairs}"
        assert "foo:baz" in pairs, f"Expected foo:baz in {pairs}"

    def test_plain_import(self):
        src = "import os\n"
        pairs = self._imports(src)
        assert "os:" in pairs

    def test_mixed_imports(self):
        src = (
            "import sys\n"
            "from foo import (\n"
            "    bar,\n"
            "    baz,\n"
            ")\n"
            "from qux import quux\n"
        )
        pairs = self._imports(src)
        assert "sys:" in pairs
        assert "foo:bar" in pairs
        assert "foo:baz" in pairs
        assert "qux:quux" in pairs


# ---------------------------------------------------------------------------
# Phase 3: TypeScript import extraction via PyScopeResolver
# ---------------------------------------------------------------------------


class TestTypescriptImportExtraction:
    """Verify that TypeScript/JavaScript imports are extracted via the
    tree-sitter-backed PyScopeResolver (no hand-rolled regexes).
    """

    def _imports(self, filename: str, source: str):
        """Return ImportFact list from _extract_imports_typescript."""
        from emend.fact_graph import _extract_imports_typescript
        return _extract_imports_typescript(filename, source)

    def _modules(self, source: str) -> set[str]:
        """Return the set of imported module paths from a TypeScript snippet."""
        return {f.imported_module for f in self._imports("/tmp/app.ts", source)}

    def _names(self, source: str) -> set[str]:
        """Return the set of imported name values (imported_name field)."""
        return {f.imported_name for f in self._imports("/tmp/app.ts", source)
                if f.imported_name is not None}

    def test_named_imports_extract_module_and_names(self):
        """import { Foo, Bar } from './foo' should produce two facts."""
        source = 'import { Foo, Bar } from "./foo";'
        facts = self._imports("/tmp/app.ts", source)
        modules = {f.imported_module for f in facts}
        names = {f.imported_name for f in facts}
        assert "./foo" in modules
        assert "Foo" in names
        assert "Bar" in names

    def test_type_only_import_produces_fact(self):
        """import type { Foo } from './foo' should be handled like a regular import."""
        source = 'import type { Foo } from "./foo";'
        facts = self._imports("/tmp/app.ts", source)
        assert len(facts) == 1
        assert facts[0].imported_module == "./foo"
        assert facts[0].imported_name == "Foo"

    def test_multi_line_import_extracts_all_names(self):
        """Multi-line import { foo,\\n  bar } from 'baz' should produce two facts."""
        source = 'import {\n  foo,\n  bar\n} from "baz";'
        names = self._names(source)
        assert "foo" in names, f"'foo' not found in {names}"
        assert "bar" in names, f"'bar' not found in {names}"

    def test_module_path_quotes_are_stripped(self):
        """Module paths should not include surrounding quote characters."""
        source = 'import { X } from "./mymodule";'
        facts = self._imports("/tmp/app.ts", source)
        assert len(facts) >= 1
        for f in facts:
            assert not f.imported_module.startswith('"'), (
                f"Module path should not have leading quote: {f.imported_module!r}"
            )
            assert not f.imported_module.endswith('"'), (
                f"Module path should not have trailing quote: {f.imported_module!r}"
            )

    def test_line_numbers_are_zero(self):
        """PyScopeResolver.imports_in_file() does not return line numbers; line=0."""
        source = 'import { A } from "./a";'
        facts = self._imports("/tmp/app.ts", source)
        for f in facts:
            assert f.line == 0, f"Expected line=0, got line={f.line}"

    def test_tsx_extension_works(self):
        """TSX files (.tsx) should also be handled."""
        source = 'import React from "react";'
        facts = self._imports("/tmp/Component.tsx", source)
        assert any(f.imported_module == "react" for f in facts), (
            f"Expected 'react' in facts, got: {facts}"
        )

    def test_js_extension_works(self):
        """JS files (.js) should also be handled."""
        source = 'import { helper } from "./utils";'
        facts = self._imports("/tmp/app.js", source)
        modules = {f.imported_module for f in facts}
        assert "./utils" in modules, f"Expected './utils' in {modules}"

    @pytest.mark.parametrize("source", [
        'import "./side-effect";',
        'export { X } from "./bar";',
        'const { a, b } = require("./c");',
    ])
    def test_unsupported_import_forms_are_omitted(self, source):
        assert self._imports("/tmp/app.ts", source) == []


class TestFactsCliTaintFlowsAlias:
    """``emend analyze facts --type taint_flows`` is documented as an alias of
    ``trace_flows`` in the ``--type`` help text; it must not error out."""

    def _run(self, tmp_path, fact_type):
        from typer.testing import CliRunner
        from emend.cli import app

        (tmp_path / "a.py").write_text("def foo():\n    return 1\n")
        runner = CliRunner()
        return runner.invoke(
            app, ["analyze", "facts", str(tmp_path), "--type", fact_type]
        )

    @pytest.mark.parametrize("fact_type", ["taint_flows", "trace_flows"])
    def test_trace_flow_spellings_are_accepted(self, tmp_path, fact_type):
        result = self._run(tmp_path, fact_type)
        assert result.exit_code == 0, result.stdout

    def test_facts_json_empty_result_is_array(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from emend.cli import app
        from emend.fact_graph import FactGraph

        monkeypatch.setattr(
            FactGraph, "build_from_project", classmethod(lambda cls, _path: cls())
        )
        monkeypatch.setattr(FactGraph, "symbols", lambda self, **_kwargs: [])
        result = CliRunner().invoke(app, ["analyze", "facts", str(tmp_path), "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == []
