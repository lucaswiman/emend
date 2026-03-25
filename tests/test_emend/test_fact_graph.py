"""Tests for the relational fact graph (Phase 4)."""

import json

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
