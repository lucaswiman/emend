"""Tests for the CozoDB-backed relational fact graph."""

import json

import pytest

from emend.fact_graph import (
    CallFact,
    FactGraph,
    ImportFact,
    ReferenceFact,
    SymbolFact,
    TaintFlowFact,
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

    g.add_taint_flow(TaintFlowFact("user_input", "query", "sqli", "app.py", "app.main", 3, 5))
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


class TestTaintFlowQueries:
    def test_taint_flows_all(self):
        g = _make_graph()
        flows = g.taint_flows()
        assert len(flows) == 1

    def test_taint_flows_by_label(self):
        g = _make_graph()
        flows = g.taint_flows(label="sqli")
        assert len(flows) == 1
        assert flows[0].source_var == "user_input"

    def test_taint_flows_by_file(self):
        g = _make_graph()
        flows = g.taint_flows(file_path="app.py")
        assert len(flows) == 1

    def test_taint_flows_no_match(self):
        g = _make_graph()
        assert g.taint_flows(label="nonexistent") == []


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


class TestSerialization:
    def test_roundtrip(self):
        g = _make_graph()
        json_str = g.to_json()
        g2 = FactGraph.from_json(json_str)

        assert len(g2.symbols()) == len(g.symbols())
        assert len(g2.calls_from("app.main")) == len(g.calls_from("app.main"))
        assert len(g2.references_to("lib.compute")) == len(g.references_to("lib.compute"))
        assert len(g2.taint_flows()) == len(g.taint_flows())
        assert len(g2.types_for("app.main")) == len(g.types_for("app.main"))
        assert len(g2.imports_in("app.py")) == len(g.imports_in("app.py"))

    def test_json_parseable(self):
        g = _make_graph()
        data = json.loads(g.to_json())
        assert isinstance(data, list)
        assert any(d["_type"] == "SymbolFact" for d in data)


class TestGenericQuery:
    def test_query_predicate(self):
        g = _make_graph()
        results = g.query(lambda f: isinstance(f, SymbolFact) and f.kind == "class")
        assert len(results) == 1
        assert results[0].name == "MyClass"


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
            'has_ref[qn] := *reference[qn, _, _, _, _]\n'
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
            'reaches[b] := *call["app.main", b, _, _, _]\n'
            'reaches[b] := *call[mid, b, _, _, _], reaches[mid]\n'
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
