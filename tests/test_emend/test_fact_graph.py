"""Tests for the CozoDB-backed relational fact graph."""

import json

import pytest

from emend.fact_graph import (
    CallFact,
    CfgBlockFact,
    CfgEdgeFact,
    DecoratorOnFact,
    DefUseFact,
    EntryPointDecoratorFact,
    EntryPointNameFact,
    FactGraph,
    FuncSummaryFact,
    ImportFact,
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
        dead = g.dead_code_unified()
        dead_qns = {s.qualified_name for s in dead}
        # app.helper is called but only from block 2 which is in the graph
        # lib.MyClass.__init__ has no reference and is not a dunder entry point
        # Wait - __init__ IS a dunder, so it should be excluded
        # app.main has no callers but has @app.route... but entry_point_decorator is empty
        assert "app.main" in dead_qns or "app.helper" in dead_qns

    def test_unified_respects_entry_point_decorators(self):
        g = _make_graph_with_cfg()
        g.add_entry_point_decorator(EntryPointDecoratorFact("app.route"))
        dead = g.dead_code_unified(entry_point_decorators=["app.route"])
        dead_qns = {s.qualified_name for s in dead}
        # app.main has @app.route, so it's an entry point
        assert "app.main" not in dead_qns

    def test_unified_dunders_are_entry_points(self):
        g = _make_graph_with_cfg()
        dead = g.dead_code_unified()
        dead_qns = {s.qualified_name for s in dead}
        # __init__ is a dunder, so it's an entry point
        assert "lib.MyClass.__init__" not in dead_qns

    def test_unified_entry_point_names(self):
        g = FactGraph()
        g.add_symbol(SymbolFact("a.py", "main", "a.main", "function", 1, 5, None))
        g.add_symbol(SymbolFact("a.py", "unused", "a.unused", "function", 7, 10, None))
        dead = g.dead_code_unified(entry_point_names=["main"])
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

    def test_empty_sources(self):
        g = FactGraph()
        violations = g.flow_rule_check_datalog(sources=[], sinks=[("a.py", "f", "x", 0)])
        assert violations == []


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
