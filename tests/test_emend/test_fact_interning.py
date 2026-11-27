"""File paths and qualified names are stored once and referred to by integer id.

A path or qualified name is repeated in the key of nearly every fact row, and
a string costs several times more than an integer there.  The ``intern`` and
``interned`` relations hold each text once; the fact relations hold its id.
Callers keep giving and receiving text: writes are encoded by column name,
query results are decoded before they reach Python, and query inputs are
converted with ``FactGraph._id``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from emend.analysis_snapshot import (
    CallFact,
    CfgBlockFact,
    CfgEdgeFact,
    DecoratorOnFact,
    DefUseFact,
    ExportedSymbolFact,
    FuncSummaryFact,
    ImportFact,
    MethodCallFact,
    ReferenceFact,
    SymbolFact,
    TraceFlowFact,
    TypeFact,
)
from emend.analysis_store import AnalysisStore
from emend.emend_core import PyCozoDb
from emend.fact_graph import (
    FACT_GRAPH_SCHEMA_VERSION,
    ID_BASE,
    FactGraph,
    prepare_cozo_storage,
)

SOURCE_APP = (
    "from lib import compute\n"
    "\n"
    "\n"
    "class Service:\n"
    "    def run(self):\n"
    "        return self._step(compute(1))\n"
    "\n"
    "    def _step(self, value):\n"
    "        return value\n"
    "\n"
    "\n"
    "def main():\n"
    "    return Service().run()\n"
)
SOURCE_LIB = (
    "def compute(x):\n"
    "    return x + 1\n"
)

# One query per relation: the columns holding a path or qualified name (by
# position in the query head) and the rows a caller sees.
STORED_ROWS = {
    "symbol": (
        "?[qualified_name, file_path, name, kind, parent] := "
        "*symbol[qualified_name, file_path, name, kind, _, _, parent]",
        (0, 1, 4),
        [
            ["app.Service", "app.py", "Service", "class", ""],
            ["app.Service._step", "app.py", "_step", "method", "app.Service"],
            ["app.Service.run", "app.py", "run", "method", "app.Service"],
            ["app.main", "app.py", "main", "function", ""],
            ["lib.compute", "lib.py", "compute", "function", ""],
        ],
    ),
    "call": (
        "?[caller_qn, callee_qn, file_path, line, func_qn] := "
        "*call[caller_qn, callee_qn, file_path, line, _, func_qn, _]",
        (0, 1, 2, 4),
        [
            ["app.Service.run", "app.Service.run.self._step", "app.py", 6, "app.Service.run"],
            ["app.Service.run", "lib.compute", "app.py", 6, "app.Service.run"],
            ["app.main", "app.Service", "app.py", 13, "app.main"],
        ],
    ),
    "call_by_callee": (
        "?[callee_qn, caller_qn, file_path, line, func_qn] := "
        "*call_by_callee[callee_qn, caller_qn, file_path, line, _, func_qn, _]",
        (0, 1, 2, 4),
        [
            ["app.Service", "app.main", "app.py", 13, "app.main"],
            ["app.Service.run.self._step", "app.Service.run", "app.py", 6, "app.Service.run"],
            ["lib.compute", "app.Service.run", "app.py", 6, "app.Service.run"],
        ],
    ),
    "reference": (
        "?[symbol_qn, file_path, line, ref_kind, func_qn] := "
        "*reference[symbol_qn, file_path, line, _, ref_kind, func_qn, _], "
        'ref_kind in ["call", "import"]',
        (0, 1, 4),
        [
            ["app.Service", "app.py", 13, "call", "app.main"],
            ["app.Service.run.self._step", "app.py", 6, "call", "app.Service.run"],
            ["lib", "app.py", 1, "import", ""],
            ["lib.compute", "app.py", 1, "import", ""],
            ["lib.compute", "app.py", 6, "call", "app.Service.run"],
        ],
    ),
    "search_symbol": (
        "?[file_path, module_qualified_name, name, qualified_name, parent] := "
        "*search_symbol[file_path, module_qualified_name, name, qualified_name, "
        "_, _, _, _, parent, _, _, _]",
        (0, 1, 4),
        [
            ["app.py", "app.Service", "Service", "Service", ""],
            ["app.py", "app.Service._step", "_step", "Service._step", "app.Service"],
            ["app.py", "app.Service.run", "run", "Service.run", "app.Service"],
            ["app.py", "app.main", "main", "main", ""],
            ["lib.py", "lib.compute", "compute", "compute", ""],
        ],
    ),
    "import": (
        "?[importing_file, imported_module, imported_name] := "
        "*import[importing_file, imported_module, imported_name, _, _]",
        (0,),
        [["app.py", "lib", "compute"]],
    ),
    "source_loc": (
        "?[file_path, loc_kind, loc_id, line] := "
        '*source_loc[file_path, loc_kind, loc_id, line, _, _, _], file_path == $lib',
        (0,),
        [
            ["lib.py", "block", "lib.compute:0", 2],
            ["lib.py", "symbol", "lib.compute", 1],
        ],
    ),
    "method_call": (
        "?[file_path, func_qn, receiver, method] := "
        "*method_call[file_path, func_qn, receiver, method, _, _]",
        (0, 1),
        [["app.py", "app.Service.run", "self", "_step"]],
    ),
    "file_namespace": (
        "?[file_path, namespace] := *file_namespace[file_path, namespace]",
        (0,),
        [["app.py", "python"], ["lib.py", "python"]],
    ),
    "ref_by_block": (
        "?[file_path, func_qn, block_id, symbol_qn] := "
        "*ref_by_block[file_path, func_qn, block_id, symbol_qn]",
        (0, 1, 3),
        [
            ["app.py", "app.Service._step", 0, "app.Service._step.value"],
            ["app.py", "app.Service.run", 0, "app.Service.run.self"],
            ["app.py", "app.Service.run", 0, "app.Service.run.self._step"],
            ["app.py", "app.Service.run", 0, "lib.compute"],
            ["app.py", "app.main", 0, "app.Service"],
            ["lib.py", "lib.compute", 0, "lib.compute.x"],
        ],
    ),
    "module_level_ref": (
        "?[symbol_qn, file_path, line] := *module_level_ref[symbol_qn, file_path, line], "
        "file_path == $lib",
        (0, 1),
        [["lib.compute", "lib.py", 1], ["lib.compute.x", "lib.py", 1]],
    ),
    "cfg_block": (
        "?[file_path, func_qn, block_id] := *cfg_block[file_path, func_qn, block_id, _, _], "
        "file_path == $lib",
        (0, 1),
        [["lib.py", "lib.compute", 0], ["lib.py", "lib.compute", 1], ["lib.py", "lib.compute", 2]],
    ),
    "cfg_edge": (
        "?[file_path, func_qn, from_block, to_block] := "
        "*cfg_edge[file_path, func_qn, from_block, to_block, _, _, _], file_path == $lib",
        (0, 1),
        [["lib.py", "lib.compute", 0, 2], ["lib.py", "lib.compute", 2, 1]],
    ),
    "reachable_block": (
        "?[file_path, func_qn, block_id] := *reachable_block[file_path, func_qn, block_id], "
        "file_path == $lib",
        (0, 1),
        [["lib.py", "lib.compute", 0], ["lib.py", "lib.compute", 1], ["lib.py", "lib.compute", 2]],
    ),
    "flow_blob": (
        "?[file_path] := *flow_blob[file_path, _]",
        (0,),
        [["app.py"], ["lib.py"]],
    ),
}


@pytest.fixture
def project_graph(tmp_path):
    (tmp_path / "app.py").write_text(SOURCE_APP)
    (tmp_path / "lib.py").write_text(SOURCE_LIB)
    store = AnalysisStore(tmp_path)
    try:
        yield store.query_facts()
    finally:
        store.close()


def _dictionary(graph: FactGraph) -> dict[int, str]:
    rows = graph.run_query("?[id, text] := *interned[id, text]", decode=False)["rows"]
    return {row[0]: row[1] for row in rows}


def _run(graph: FactGraph, query: str, *, decode: bool = True) -> list[list]:
    return graph.client.run(query, {"lib": graph._id("lib.py")}, decode=decode)["rows"]


@pytest.mark.parametrize("relation", sorted(STORED_ROWS))
def test_paths_and_names_are_stored_as_ids_and_read_as_text(project_graph, relation):
    query, id_columns, expected = STORED_ROWS[relation]

    assert _run(project_graph, query) == expected

    texts = _dictionary(project_graph)
    stored = _run(project_graph, query, decode=False)
    assert len(stored) == len(expected)
    for row in stored:
        for index, cell in enumerate(row):
            if index in id_columns:
                assert isinstance(cell, int) and cell >= ID_BASE, (relation, row)
            else:
                assert not (isinstance(cell, int) and cell >= ID_BASE), (relation, row)
    # The dictionary alone turns the stored rows into the ones a caller sees.
    assert sorted(
        [texts[cell] if index in id_columns else cell for index, cell in enumerate(row)]
        for row in stored
    ) == expected


def test_file_revisions_keep_absolute_paths_as_text(project_graph, tmp_path):
    """They never equal a fact path, so there is nothing for an id to share."""
    rows = project_graph.run_query(
        "?[file_path] := *file_revision[file_path, _, _, _, _, _]", decode=False,
    )["rows"]
    assert rows == [[str(tmp_path / "app.py")], [str(tmp_path / "lib.py")]]


def test_dictionary_relations_are_inverse_and_dense(project_graph):
    by_text = dict(project_graph.run_query(
        "?[text, id] := *intern[text, id]", decode=False,
    )["rows"])
    by_id = _dictionary(project_graph)

    assert by_text == {text: ident for ident, text in by_id.items()}
    assert sorted(by_id) == list(range(ID_BASE, ID_BASE + len(by_id)))
    assert by_text[""] == ID_BASE
    assert {"app.py", "lib.py", "app.main", "lib.compute", "app.Service"} <= by_text.keys()
    # Names that no rule joins stay out of the dictionary.
    assert not {"main", "Service.run", "python", "function", "lib.compute:0"} & by_text.keys()


def test_id_helpers_convert_query_inputs(project_graph):
    by_text = dict(project_graph.run_query(
        "?[text, id] := *intern[text, id]", decode=False,
    )["rows"])

    assert project_graph._id("lib.compute") == by_text["lib.compute"]
    assert project_graph._ids(["app.py", "never.seen", "", "lib.py"]) == [
        by_text["app.py"], -1, ID_BASE, by_text["lib.py"],
    ]
    # Asking about a text does not add it.
    assert project_graph._id("never.seen") == -1
    assert "never.seen" not in _dictionary(project_graph).values()


def test_never_seen_name_matches_nothing(project_graph):
    assert project_graph.refs_datalog("no.such.symbol") == []
    assert project_graph.calls_to("no.such.symbol") == []
    assert project_graph.calls_from("no.such.symbol") == []
    assert project_graph.transitive_callers("no.such.symbol") == set()
    assert project_graph.symbols(file_path="missing.py") == []
    assert project_graph.imports_in("missing.py") == []
    assert project_graph.cfg_blocks(func_qn="no.such.symbol") == []
    assert project_graph.graph_datalog("missing.py") == []
    assert project_graph.flow_events(file_path="missing.py") == []
    assert project_graph.impact_closure({"no.such.symbol"}) == {"impacted": set(), "edges": []}
    project_graph.remove_files(["missing.py"])
    assert len(project_graph.symbols()) == 5


def test_public_queries_take_and_return_text(project_graph):
    assert project_graph.calls_to("lib.compute") == [CallFact(
        caller_qn="app.Service.run", callee_qn="lib.compute", file_path="app.py",
        line=6, col=26, func_qn="app.Service.run", block_id=0,
    )]
    assert [(ref.file_path, ref.line, ref.ref_kind, ref.func_qn)
            for ref in project_graph.refs_datalog("lib.compute")] == [
        ("app.py", 1, "import", ""),
        ("app.py", 6, "call", "app.Service.run"),
        ("lib.py", 1, "definition", ""),
    ]
    assert project_graph.symbols(file_path="lib.py") == [SymbolFact(
        file_path="lib.py", name="compute", qualified_name="lib.compute",
        kind="function", line=1, end_line=2, parent=None,
    )]
    assert project_graph.transitive_callers("lib.compute") == {"app.Service.run"}
    assert project_graph.graph_datalog("app.py") == [
        ("app.Service.run", "app.Service.run.self._step"),
        ("app.Service.run", "lib.compute"),
        ("app.main", "app.Service"),
    ]
    assert project_graph.impact_closure({"lib.compute"}) == {
        "impacted": {"app.Service.run"},
        "edges": [("app.Service.run", "lib.compute")],
    }


def _filter_graph() -> FactGraph:
    """Two files with one fact of every kind each; ``b`` is the control."""
    graph = FactGraph()
    for path, qn in (("a.py", "a.f"), ("b.py", "b.g")):
        graph.add_symbol(SymbolFact(path, qn[-1], qn, "function", 1, 5))
        graph.add_cfg_block(CfgBlockFact(path, qn, 0, is_entry=True))
        graph.add_cfg_block(CfgBlockFact(path, qn, 7))
        graph.add_cfg_edge(CfgEdgeFact(path, qn, 0, 1, "fallthrough", 1, 2))
        graph.add_def_use(DefUseFact(path, qn, "x", "write", 0, 0, 1))
        graph.add_method_call(MethodCallFact(path, qn, "self", "m", 0, 1))
        graph.add_decorator_on(DecoratorOnFact(qn, "route", path))
        graph.add_func_summary(FuncSummaryFact(qn, "p", True, False, "", path))
        graph.add_exported_symbol(ExportedSymbolFact(path, qn))
        graph.add_type(TypeFact(qn, "int", path, 1, "return"))
        graph.add_trace_flow(TraceFlowFact("src", "sink", "label", path, qn, 1, 2))
        graph.add_import(ImportFact(path, "os", None, None, 1))
        graph.add_call(CallFact(qn, "lib.helper", path, 2, 4, qn, 0))
    return graph


_A_CALL = CallFact("a.f", "lib.helper", "a.py", 2, 4, "a.f", 0)
FILTERS = {
    "symbols(file_path)": (
        lambda graph: graph.symbols(file_path="a.py"),
        [SymbolFact("a.py", "f", "a.f", "function", 1, 5)],
    ),
    "cfg_edges(func_qn)": (
        lambda graph: graph.cfg_edges(func_qn="a.f"),
        [CfgEdgeFact("a.py", "a.f", 0, 1, "fallthrough", 1, 2)],
    ),
    "cfg_edges(file_path)": (
        lambda graph: graph.cfg_edges(file_path="a.py"),
        [CfgEdgeFact("a.py", "a.f", 0, 1, "fallthrough", 1, 2)],
    ),
    "def_uses(func_qn)": (
        lambda graph: graph.def_uses(func_qn="a.f"),
        [DefUseFact("a.py", "a.f", "x", "write", 0, 0, 1)],
    ),
    "def_uses(file_path)": (
        lambda graph: graph.def_uses(file_path="a.py"),
        [DefUseFact("a.py", "a.f", "x", "write", 0, 0, 1)],
    ),
    "method_calls(func_qn)": (
        lambda graph: graph.method_calls(func_qn="a.f"),
        [MethodCallFact("a.py", "a.f", "self", "m", 0, 1)],
    ),
    "method_calls(file_path)": (
        lambda graph: graph.method_calls(file_path="a.py"),
        [MethodCallFact("a.py", "a.f", "self", "m", 0, 1)],
    ),
    "cfg_blocks(func_qn)": (
        lambda graph: graph.cfg_blocks(func_qn="a.f"),
        [CfgBlockFact("a.py", "a.f", 0, is_entry=True), CfgBlockFact("a.py", "a.f", 7)],
    ),
    "cfg_blocks(file_path)": (
        lambda graph: graph.cfg_blocks(file_path="a.py"),
        [CfgBlockFact("a.py", "a.f", 0, is_entry=True), CfgBlockFact("a.py", "a.f", 7)],
    ),
    "unreachable_blocks_datalog": (
        lambda graph: graph.unreachable_blocks_datalog("a.f"),
        [CfgBlockFact("a.py", "a.f", 7)],
    ),
    "decorators_on": (
        lambda graph: graph.decorators_on("a.f"),
        [DecoratorOnFact("a.f", "route", "a.py")],
    ),
    "func_summaries": (
        lambda graph: graph.func_summaries("a.f"),
        [FuncSummaryFact("a.f", "p", True, False, "", "a.py")],
    ),
    "exported_symbols(file_path)": (
        lambda graph: graph.exported_symbols(file_path="a.py"),
        [ExportedSymbolFact("a.py", "a.f")],
    ),
    "exported_symbols(qualified_name)": (
        lambda graph: graph.exported_symbols(qualified_name="a.f"),
        [ExportedSymbolFact("a.py", "a.f")],
    ),
    "types_for": (
        lambda graph: graph.types_for("a.f"),
        [TypeFact("a.f", "int", "a.py", 1, "return")],
    ),
    "trace_flows(file_path)": (
        lambda graph: graph.trace_flows(file_path="a.py"),
        [TraceFlowFact("src", "sink", "label", "a.py", "a.f", 1, 2)],
    ),
    "imports_in": (
        lambda graph: graph.imports_in("a.py"),
        [ImportFact("a.py", "os", None, None, 1)],
    ),
    "calls_from": (lambda graph: graph.calls_from("a.f"), [_A_CALL]),
    "callees_datalog": (lambda graph: graph.callees_datalog("a.f"), [_A_CALL]),
    "calls_to": (
        lambda graph: graph.calls_to("lib.helper"),
        [_A_CALL, CallFact("b.g", "lib.helper", "b.py", 2, 4, "b.g", 0)],
    ),
    "graph_datalog(file_path)": (
        lambda graph: graph.graph_datalog("a.py"), [("a.f", "lib.helper")],
    ),
    "transitive_callees": (lambda graph: graph.transitive_callees("a.f"), {"lib.helper"}),
    "transitive_callers": (
        lambda graph: graph.transitive_callers("lib.helper"), {"a.f", "b.g"},
    ),
    "namespace_for_file": (lambda graph: graph.namespace_for_file("a.py"), "python"),
}


@pytest.mark.parametrize("name", sorted(FILTERS))
def test_every_filter_takes_text(name):
    query, expected = FILTERS[name]
    graph = _filter_graph()
    try:
        assert query(graph) == expected
    finally:
        graph.close()


def test_dead_code_seeds_take_text():
    graph = FactGraph()
    try:
        graph.add_symbols_batch([
            SymbolFact("a.py", "f", "a.f", "function", 1, 5),
            SymbolFact("b.py", "g", "b.g", "function", 1, 5),
            SymbolFact("t.py", "h", "t.h", "function", 1, 2),
        ])
        graph.add_reference(ReferenceFact("b.g", "t.py", 3, 0, "read"))
        graph.add_call(CallFact("t.h", "a.f", "t.py", 2, 4, "t.h", 0))

        def dead(**options) -> list[str]:
            return sorted(s.qualified_name for s in graph.dead_code_unified(**options)[0])

        assert dead() == ["a.f", "t.h"]
        assert dead(entry_point_qualified_names=["a.f", "unknown.name"]) == ["t.h"]
        assert dead(exclude_reference_files=["t.py", "unknown.py"]) == ["a.f", "b.g", "t.h"]
        # A requested entry point is one even if the graph does not hold it.
        assert graph.entry_point_qualified_names(
            entry_point_qualified_names=["a.f", "unknown.name"],
        ) == {"a.f", "unknown.name"}
        assert [s.qualified_name for s in graph.cascade_dead({"t.h"})] == ["a.f"]
        assert sorted(
            s.qualified_name for s in graph.unreferenced_symbols(exclude_qns={"t.h"})
        ) == ["a.f", "b.g", "t.h"]
        assert graph.impact_closure({"a.f", "unknown.name"}) == {
            "impacted": {"t.h"}, "edges": [("t.h", "a.f")],
        }
    finally:
        graph.close()


def test_raw_query_results_are_text_in_text_order():
    """Rows come back as if the columns held text, however the ids were handed out."""
    graph = FactGraph()
    try:
        for line, name in enumerate(["m.zeta", "m.alpha", "m.mid"], start=1):
            graph.add_reference(ReferenceFact(name, "z.py" if line == 2 else "a.py", line, 0, "read"))
        query = "?[symbol_qn, file_path, line] := *reference[symbol_qn, file_path, line, _, _, _, _]"

        assert graph.run_query(query)["rows"] == [
            ["m.alpha", "z.py", 2], ["m.mid", "a.py", 3], ["m.zeta", "a.py", 1],
        ]
        # The stored order is the order the names were first written in.
        stored = graph.run_query(query, decode=False)["rows"]
        assert [row[2] for row in stored] == [1, 2, 3]
        assert [row[0] for row in stored] == graph._ids(["m.zeta", "m.alpha", "m.mid"])
    finally:
        graph.close()


def test_aggregated_names_are_decoded_inside_lists(project_graph):
    rows = project_graph.run_query(
        "?[file_path, collect(qualified_name)] := *symbol[qualified_name, file_path, _, kind, _, _, _], "
        'kind == "method"'
    )["rows"]
    assert [(row[0], sorted(row[1])) for row in rows] == [
        ("app.py", ["app.Service._step", "app.Service.run"]),
    ]


def test_raw_query_matches_text_through_the_dictionary(project_graph):
    """A stored path or name is an id.  A literal written in its column
    stands for that id; a comparison with the column's value does not."""
    assert project_graph.run_query(
        '?[callee_qn] := *call["app.main", callee_qn, _, _, _, _, _]'
    )["rows"] == [["app.Service"]]
    assert project_graph.run_query(
        '?[callee_qn] := *call[caller, callee_qn, _, _, _, _, _], caller == "app.main"'
    )["rows"] == []
    assert project_graph.run_query(
        '?[callee_qn] := *intern["app.main", caller], *call[caller, callee_qn, _, _, _, _, _]'
    )["rows"] == [["app.Service"]]
    # String functions need the text, bound from the id.
    assert project_graph.run_query(
        "?[text] := *symbol[qualified_name, _, _, _, _, _, _], "
        '*interned[qualified_name, text], starts_with(text, "app.Service.")'
    )["rows"] == [["app.Service._step"], ["app.Service.run"]]


def test_explicit_order_applies_to_the_stored_ids(project_graph):
    """``:order`` on an id column sorts ids; order by the text to sort names."""
    by_id = project_graph.run_query(
        "?[qualified_name] := *symbol[qualified_name, _, _, _, _, _, _] :order qualified_name"
    )["rows"]
    by_text = project_graph.run_query(
        "?[text] := *symbol[qualified_name, _, _, _, _, _, _], "
        "*interned[qualified_name, text] :order -text"
    )["rows"]
    raw = project_graph.run_query(
        "?[qualified_name] := *symbol[qualified_name, _, _, _, _, _, _] :order qualified_name",
        decode=False,
    )["rows"]
    texts = _dictionary(project_graph)

    assert raw == sorted(raw)
    assert by_id == [[texts[row[0]]] for row in raw]
    assert by_text == [
        ["lib.compute"], ["app.main"], ["app.Service.run"], ["app.Service._step"],
        ["app.Service"],
    ]


def test_ids_survive_an_update_of_another_file(tmp_path):
    (tmp_path / "app.py").write_text(SOURCE_APP)
    (tmp_path / "lib.py").write_text(SOURCE_LIB)
    store = AnalysisStore(tmp_path)
    try:
        before = dict(store.query_facts().run_query(
            "?[text, id] := *intern[text, id]", decode=False,
        )["rows"])
        (tmp_path / "lib.py").write_text(
            SOURCE_LIB + "\n\ndef extra(y):\n    return compute(y)\n"
        )
        graph = store.query_facts()
        after = dict(graph.run_query(
            "?[text, id] := *intern[text, id]", decode=False,
        )["rows"])

        assert {text: after[text] for text in before} == before
        added = {text: ident for text, ident in after.items() if text not in before}
        assert {"lib.extra", "lib.extra.y"} <= added.keys()
        assert sorted(added.values()) == list(
            range(ID_BASE + len(before), ID_BASE + len(after))
        )
        assert graph.calls_to("lib.compute") == [
            CallFact("app.Service.run", "lib.compute", "app.py", 6, 26, "app.Service.run", 0),
            CallFact("lib.extra", "lib.compute", "lib.py", 6, 11, "lib.extra", 0),
        ]
        assert graph.run_query(
            "?[file_path] := *symbol[qualified_name, file_path, _, _, _, _, _], "
            "qualified_name == $name".replace("$name", str(after["app.main"])),
        )["rows"] == [["app.py"]]
    finally:
        store.close()


def test_remove_files_removes_a_files_rows(project_graph):
    project_graph.remove_files(["app.py"])

    assert [symbol.qualified_name for symbol in project_graph.symbols()] == ["lib.compute"]
    assert project_graph.calls_to("lib.compute") == []
    assert [ref.file_path for ref in project_graph.refs_datalog("lib.compute")] == ["lib.py"]
    assert project_graph.imports_in("app.py") == []
    for relation, (query, _columns, expected) in STORED_ROWS.items():
        assert _run(project_graph, query) == [
            row for row in expected if "app.py" not in row
        ], relation


def test_json_round_trip_keeps_text(project_graph):
    exported = project_graph.to_json()
    facts = json.loads(exported)

    assert {"_type": "SymbolFact", "file_path": "lib.py", "name": "compute",
            "qualified_name": "lib.compute", "kind": "function", "line": 1,
            "end_line": 2, "parent": None} in facts
    assert not any(
        isinstance(value, int) and value >= ID_BASE
        for fact in facts for value in fact.values()
    )

    clone = FactGraph.from_json(exported)
    try:
        assert clone.to_json() == exported
        assert clone.calls_to("lib.compute") == project_graph.calls_to("lib.compute")
        # The rebuilt graph stores ids as well.
        stored = clone.run_query(
            "?[qualified_name, file_path] := *symbol[qualified_name, file_path, _, _, _, _, _]",
            decode=False,
        )["rows"]
        assert len(stored) == 5
        assert all(cell >= ID_BASE for row in stored for cell in row)
    finally:
        clone.close()


def test_added_facts_take_and_return_text():
    graph = FactGraph()
    try:
        graph.add_symbol(SymbolFact("b.py", "late", "b.late", "function", 1, 2))
        graph.add_symbol(SymbolFact("a.py", "early", "a.early", "method", 3, 4, parent="a.Owner"))
        graph.add_call(CallFact("b.late", "a.early", "b.py", 2, 4))
        graph.add_reference(ReferenceFact("a.early", "b.py", 2, 4, "call"))
        graph.add_cfg_block(CfgBlockFact("b.py", "b.late", 0, is_entry=True))

        assert graph.symbols() == [
            SymbolFact("a.py", "early", "a.early", "method", 3, 4, parent="a.Owner"),
            SymbolFact("b.py", "late", "b.late", "function", 1, 2, parent=None),
        ]
        assert graph.calls_from("b.late") == [CallFact("b.late", "a.early", "b.py", 2, 4)]
        assert graph.references_to("a.early") == [ReferenceFact("a.early", "b.py", 2, 4, "call")]
        assert graph.namespace_for_file("b.py") == "python"
        # Defaults of id columns are the empty string's id.
        assert graph.run_query(
            "?[func_qn, parent] := *call[_, _, _, _, _, func_qn, _], "
            "*symbol[_, _, _, _, _, _, parent]", decode=False,
        )["rows"] == [[ID_BASE, ID_BASE], [ID_BASE, graph._id("a.Owner")]]
        assert _dictionary(graph)[ID_BASE] == ""
    finally:
        graph.close()


def test_failed_transaction_stores_no_dictionary_rows():
    graph = FactGraph()
    try:
        graph.add_symbol(SymbolFact("a.py", "before", "a.before", "function", 1, 1))
        dictionary = _dictionary(graph)
        put = (
            "?[qualified_name, file_path, name, kind, line, end_line, parent] <- $rows "
            ":put symbol {qualified_name, file_path => name, kind, line, end_line, parent}"
        )
        with pytest.raises(RuntimeError, match="CozoDB query error"):
            graph._run_mutations([
                (put, {"rows": [["a.after", "new.py", "after", "function", 2, 2, ""]]},
                 {"rows": (0, 1, 6)}),
                ("?[x] := *missing_relation[x]", {}),
            ])

        assert _dictionary(graph) == dictionary
        assert graph._ids(["a.after", "new.py"]) == [-1, -1]
        # The ids the failed write would have used go to the next write.
        graph.add_symbol(SymbolFact("c.py", "later", "c.later", "function", 1, 1))
        added = {ident: text for ident, text in _dictionary(graph).items()
                 if ident not in dictionary}
        assert sorted(added) == [ID_BASE + len(dictionary), ID_BASE + len(dictionary) + 1]
        assert sorted(added.values()) == ["c.later", "c.py"]
    finally:
        graph.close()


def test_persisted_copy_carries_its_dictionary(project_graph, tmp_path):
    """A generation copied with SQLite alone still decodes."""
    copy = tmp_path / "copy.db"
    with (
        closing(sqlite3.connect(project_graph._db_path)) as source,
        closing(sqlite3.connect(copy)) as target,
    ):
        source.backup(target)

    reopened = FactGraph(db_path=str(copy))
    try:
        assert reopened.calls_to("lib.compute") == project_graph.calls_to("lib.compute")
        assert _dictionary(reopened) == _dictionary(project_graph)
        # A reopened dictionary continues where the stored one ends.
        size = len(_dictionary(reopened))
        reopened.add_symbol(SymbolFact("new.py", "fresh", "new.fresh", "function", 1, 1))
        assert sorted(reopened._ids(["new.fresh", "new.py"])) == [
            ID_BASE + size, ID_BASE + size + 1,
        ]
        assert reopened._id("lib.compute") == project_graph._id("lib.compute")
    finally:
        reopened.close()


def test_shared_generation_reads_text_and_copies_before_interning(tmp_path):
    (tmp_path / "app.py").write_text(SOURCE_APP)
    (tmp_path / "lib.py").write_text(SOURCE_LIB)
    store = AnalysisStore(tmp_path)
    try:
        graph = store.query_facts()
        shared_path = graph._db_path
        known = graph._id("lib.compute")
        assert graph.calls_to("lib.compute")[0].caller_qn == "app.Service.run"

        graph.add_symbol(SymbolFact("new.py", "fresh", "new.fresh", "function", 1, 1))

        assert graph._db_path != shared_path
        assert graph._id("lib.compute") == known
        assert [symbol.qualified_name for symbol in graph.symbols(file_path="new.py")] == [
            "new.fresh"
        ]
        other = AnalysisStore(tmp_path)
        try:
            published = other.query_facts()
            assert published._id("new.fresh") == -1
            assert len(published.symbols()) == 5
        finally:
            other.close()
    finally:
        store.close()


def test_schema_version_marks_the_storage_change(project_graph):
    assert int(FACT_GRAPH_SCHEMA_VERSION) >= 13
    assert project_graph.run_query(
        '?[value] := *facts_meta["schema_version", value]'
    )["rows"] == [[FACT_GRAPH_SCHEMA_VERSION]]


# -- The bridge ---------------------------------------------------------------

def _dictionary_db(path=None) -> PyCozoDb:
    if path is None:
        db = PyCozoDb("mem", "")
    else:
        prepare_cozo_storage(path)
        db = PyCozoDb("sqlite", str(path))
    db.run(":create intern {text: String => id: Int}")
    db.run(":create interned {id: Int => text: String}")
    db.run(":create edge {source: Int, target: Int => label: String}")
    return db


def test_bridge_shares_the_id_base():
    assert PyCozoDb.ID_BASE == ID_BASE == 2**40


@pytest.mark.parametrize("bulk", [True, False], ids=["import", "transaction"])
def test_bridge_interns_named_positions_on_write(bulk):
    db = _dictionary_db()
    rows = [["b", "a", "first"], ["a", "c", "second"], ["b", "", "third"]]
    if bulk:
        db.import_relations(
            {"edge": (["source", "target", "label"], rows)}, {"edge": (0, 1)},
        )
    else:
        db.run_transaction([(
            "?[source, target, label] <- $rows :put edge {source, target => label}",
            {"rows": rows}, {"rows": (0, 1)},
        )])

    assert rows == [["b", "a", "first"], ["a", "c", "second"], ["b", "", "third"]]
    assert db.run("?[id, text] := *interned[id, text]", decode=False)["rows"] == [
        [ID_BASE, ""], [ID_BASE + 1, "b"], [ID_BASE + 2, "a"], [ID_BASE + 3, "c"],
    ]
    assert db.run("?[text, id] := *intern[text, id]", decode=False)["rows"] == [
        ["", ID_BASE], ["a", ID_BASE + 2], ["b", ID_BASE + 1], ["c", ID_BASE + 3],
    ]
    query = "?[source, target, label] := *edge[source, target, label]"
    assert db.run(query, decode=False)["rows"] == [
        [ID_BASE + 1, ID_BASE, "third"],
        [ID_BASE + 1, ID_BASE + 2, "first"],
        [ID_BASE + 2, ID_BASE + 3, "second"],
    ]
    assert db.run(query)["rows"] == [
        ["a", "c", "second"], ["b", "", "third"], ["b", "a", "first"],
    ]
    assert db.intern_ids(["c", "missing", "", "b"]) == [ID_BASE + 3, -1, ID_BASE, ID_BASE + 1]


def test_bridge_rejects_a_non_text_cell_in_an_interned_position():
    db = _dictionary_db()
    with pytest.raises(TypeError, match="interned"):
        db.import_relations(
            {"edge": (["source", "target", "label"], [[7, "a", "x"]])}, {"edge": (0, 1)},
        )
    assert db.run("?[id] := *interned[id, _]", decode=False)["rows"] == []


def test_bridge_leaves_unknown_large_integers_alone():
    db = _dictionary_db()
    db.import_relations(
        {"edge": (["source", "target", "label"], [["a", "b", "x"]])}, {"edge": (0, 1)},
    )
    assert db.run("?[value, nested] <- [[$known, [$unknown, $known, 3]], [$unknown, []]]", {
        "known": ID_BASE + 1, "unknown": ID_BASE + 99,
    })["rows"] == [[ID_BASE + 99, []], ["a", [ID_BASE + 99, "a", 3]]]
    # A database with no dictionary is read as it is.
    plain = PyCozoDb("mem", "")
    assert plain.run("?[value] <- [[$value]]", {"value": ID_BASE + 1})["rows"] == [[ID_BASE + 1]]


def test_bridge_reopened_database_finds_and_extends_its_dictionary(tmp_path):
    path = tmp_path / "facts.db"
    first = _dictionary_db(path)
    columns = ["source", "target", "label"]
    first.import_relations({"edge": (columns, [["a", "b", "x"]])}, {"edge": (0, 1)})
    first.backup(str(tmp_path / "copy.db"))
    del first

    second = PyCozoDb("sqlite", str(tmp_path / "copy.db"))
    assert second.intern_ids(["b", "a"]) == [ID_BASE + 2, ID_BASE + 1]
    second.run_transaction([(
        "?[source, target, label] <- $rows :put edge {source, target => label}",
        {"rows": [["c", "a", "y"], ["a", "d", "z"]]}, {"rows": (0, 1)},
    )])

    assert second.run("?[id, text] := *interned[id, text]", decode=False)["rows"] == [
        [ID_BASE, ""], [ID_BASE + 1, "a"], [ID_BASE + 2, "b"],
        [ID_BASE + 3, "c"], [ID_BASE + 4, "d"],
    ]
    assert second.run("?[source, target, label] := *edge[source, target, label]")["rows"] == [
        ["a", "b", "x"], ["a", "d", "z"], ["c", "a", "y"],
    ]


def test_bridge_remembers_a_missing_text_until_it_is_written():
    db = _dictionary_db()
    columns = ["source", "target", "label"]
    db.import_relations({"edge": (columns, [["a", "b", "x"]])}, {"edge": (0, 1)})

    assert db.intern_ids(["late"]) == [-1]
    assert db.intern_ids(["late"]) == [-1]
    db.import_relations({"edge": (columns, [["late", "a", "y"]])}, {"edge": (0, 1)})

    assert db.intern_ids(["late", "a"]) == [ID_BASE + 3, ID_BASE + 1]
    assert db.run("?[source] := *edge[source, _, _]")["rows"] == [["a"], ["late"]]


def test_bridge_transaction_shares_new_texts_between_its_scripts():
    db = _dictionary_db()
    put = "?[source, target, label] <- $rows :put edge {source, target => label}"
    db.run_transaction([
        (put, {"rows": [["a", "b", "first"]]}, {"rows": (0, 1)}),
        ("?[source, target] <- [[1, 2]] :rm edge {source, target}", {}),
        (put, {"rows": [["b", "c", "second"]]}, {"rows": (0, 1)}),
    ])

    assert db.run("?[id, text] := *interned[id, text]", decode=False)["rows"] == [
        [ID_BASE, ""], [ID_BASE + 1, "a"], [ID_BASE + 2, "b"], [ID_BASE + 3, "c"],
    ]
    assert db.run("?[source, target, label] := *edge[source, target, label]")["rows"] == [
        ["a", "b", "first"], ["b", "c", "second"],
    ]


def test_bridge_handles_a_dictionary_too_large_to_look_up_piecewise(tmp_path):
    """Many unknown texts or ids are resolved by reading the dictionary once."""
    path = tmp_path / "facts.db"
    names = [f"pkg.module_{index % 97}.name_{index:05d}" for index in range(6_500)]
    rows = [[name, names[(index * 7) % len(names)], "x"] for index, name in enumerate(names)]
    columns = ["source", "target", "label"]
    first = _dictionary_db(path)
    first.import_relations({"edge": (columns, rows[:6_000])}, {"edge": (0, 1)})
    stored = dict(first.run("?[text, id] := *intern[text, id]", decode=False)["rows"])
    del first

    # A new handle knows nothing yet: decoding needs every stored id at once.
    reader = PyCozoDb("sqlite", str(path))
    query = "?[source, target] := *edge[source, target, _]"
    assert reader.run(query)["rows"] == sorted([row[0], row[1]] for row in rows[:6_000])
    del reader

    # So does a write that names every stored text and some new ones.
    writer = PyCozoDb("sqlite", str(path))
    writer.import_relations({"edge": (columns, rows)}, {"edge": (0, 1)})
    after = dict(writer.run("?[text, id] := *intern[text, id]", decode=False)["rows"])
    assert {text: after[text] for text in stored} == stored
    assert sorted(after.values()) == list(range(ID_BASE, ID_BASE + len(names) + 1))
    assert writer.run(query)["rows"] == sorted([row[0], row[1]] for row in rows)
    assert writer.intern_ids(names + ["absent"]) == [after[name] for name in names] + [-1]

    # Having read everything, the handle still learns what it writes later.
    writer.import_relations({"edge": (columns, [["zz.added", names[0], "y"]])}, {"edge": (0, 1)})
    assert writer.run("?[source, target] := *edge[source, target, 'y']")["rows"] == [
        ["zz.added", names[0]],
    ]
    assert writer.run("?[value] <- [[$value]]", {"value": ID_BASE + 10**9})["rows"] == [
        [ID_BASE + 10**9],
    ]


def test_bridge_reads_ahead_after_many_small_lookups(tmp_path):
    """Lookups add up: a handle that keeps asking reads the rest at once."""
    path = tmp_path / "facts.db"
    names = [f"pkg.name_{index:05d}" for index in range(9_000)]
    first = _dictionary_db(path)
    first.import_relations(
        {"edge": (["source", "target", "label"], [[name, "", str(index // 1000)]
                                                  for index, name in enumerate(names)])},
        {"edge": (0, 1)},
    )
    del first

    reader = PyCozoDb("sqlite", str(path))
    for label in range(9):
        rows = reader.run("?[source] := *edge[source, _, $label]", {"label": str(label)})["rows"]
        assert rows == [[name] for name in names[label * 1000:(label + 1) * 1000]]
    for start in range(0, 9_000, 1_000):
        chunk = names[start:start + 1_000]
        assert reader.intern_ids(chunk) == [ID_BASE + 1 + start + offset for offset in range(1_000)]
    assert reader.intern_ids(["pkg.name_99999"]) == [-1]


def test_bridge_orders_a_long_result_by_text():
    db = _dictionary_db()
    # Later rows get larger ids but sort first as text.
    names = [f"n{index:05d}" for index in reversed(range(60_000))]
    rows = [[name, names[0], "x"] for name in names]
    db.import_relations({"edge": (["source", "target", "label"], rows)}, {"edge": (0, 1)})

    result = db.run("?[source, label] := *edge[source, _, label]")["rows"]

    assert len(result) == 60_000
    assert result[:2] == [["n00000", "x"], ["n00001", "x"]]
    assert result == sorted(result)


@pytest.mark.parametrize("persistent", [False, True], ids=["memory", "sqlite"])
def test_bridge_concurrent_writes_keep_the_dictionary_dense(tmp_path, persistent):
    """Writers on one handle take turns; none waits for another forever."""
    import threading

    db = _dictionary_db(tmp_path / "facts.db" if persistent else None)
    columns = ["source", "target", "label"]
    put = "?[source, target, label] <- $rows :put edge {source, target => label}"
    workers, batches, size = 4, 12, 25

    def write(worker: int) -> None:
        for batch in range(batches):
            rows = [[f"w{worker}.b{batch}.{index}", f"shared.{index}", "x"] for index in range(size)]
            if (batch + worker) % 2:
                db.import_relations({"edge": (columns, rows)}, {"edge": (0, 1)})
            else:
                # The transaction is already open when it first needs an id.
                db.run_transaction([
                    ("?[source, target] <- [[1, 2]] :rm edge {source, target}", {}),
                    (put, {"rows": rows}, {"rows": (0, 1)}),
                ])

    threads = [threading.Thread(target=write, args=(worker,), daemon=True)
               for worker in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "writers are deadlocked"

    by_id = dict(db.run("?[id, text] := *interned[id, text]", decode=False)["rows"])
    by_text = dict(db.run("?[text, id] := *intern[text, id]", decode=False)["rows"])
    assert len(by_id) == 1 + workers * batches * size + size
    assert sorted(by_id) == list(range(ID_BASE, ID_BASE + len(by_id)))
    assert by_text == {text: ident for ident, text in by_id.items()}
    assert len(db.run("?[source, target] := *edge[source, target, _]")["rows"]) == (
        workers * batches * size
    )


# ---------------------------------------------------------------------------
# Searching the dictionary by fragment
# ---------------------------------------------------------------------------


def _store_texts(db: PyCozoDb, texts: list[str]) -> None:
    db.run_transaction([(
        "?[source, target, label] <- $rows :put edge {source, target => label}",
        {"rows": [[text, "", "x"] for text in texts]},
        {"rows": (0, 1)},
    )])


def _searchable_db() -> PyCozoDb:
    db = _dictionary_db()
    _store_texts(db, ["pkg.Zeta", "pkg.alpha", "pkg.Alpha_one", "other.beta", "src/pkg/a.py"])
    return db


def test_bridge_finds_stored_texts_by_fragment_in_text_order():
    db = _searchable_db()
    found = db.texts_containing(["pkg."], 10)
    assert [text for text, _ in found] == ["pkg.Alpha_one", "pkg.Zeta", "pkg.alpha"]
    assert [ident for _, ident in found] == db.intern_ids([text for text, _ in found])
    assert all(ident >= ID_BASE for _, ident in found)


def test_bridge_fragment_search_keeps_the_first_texts_under_a_limit():
    db = _searchable_db()
    assert [text for text, _ in db.texts_containing(["pkg."], 2)] == [
        "pkg.Alpha_one", "pkg.Zeta",
    ]
    assert db.texts_containing(["pkg."], 0) == []


def test_bridge_fragment_search_matches_any_fragment_and_can_ignore_case():
    db = _searchable_db()
    assert [text for text, _ in db.texts_containing(["beta", "Zeta"], 10)] == [
        "other.beta", "pkg.Zeta",
    ]
    assert db.texts_containing(["ALPHA"], 10) == []
    assert [text for text, _ in db.texts_containing(["ALPHA"], 10, ignore_case=True)] == [
        "pkg.Alpha_one", "pkg.alpha",
    ]
    assert db.texts_containing(["absent"], 10) == []
    assert db.texts_containing([], 10) == []


def test_bridge_fragment_search_sees_texts_added_after_it_first_ran():
    db = _searchable_db()
    assert db.texts_containing(["gamma"], 10) == []
    # A text looked up and not found is remembered as absent, not as a text.
    assert db.intern_ids(["pkg.gamma"]) == [-1]
    assert db.texts_containing(["gamma"], 10) == []
    _store_texts(db, ["pkg.gamma"])
    assert [text for text, _ in db.texts_containing(["gamma"], 10)] == ["pkg.gamma"]


def test_bridge_fragment_search_reads_a_reopened_database(tmp_path):
    path = tmp_path / "facts.db"
    first = _dictionary_db(str(path))
    _store_texts(first, ["pkg.stored"])
    first.close()
    reopened = PyCozoDb("sqlite", str(path))
    try:
        assert [text for text, _ in reopened.texts_containing(["stored"], 10)] == ["pkg.stored"]
    finally:
        reopened.close()


def test_bridge_fragment_search_reads_a_dictionary_larger_than_one_page():
    """The dictionary is read in pages; none may be skipped or read twice."""
    db = _dictionary_db()
    texts = [f"name.{number:06}" for number in range(45_000)]
    db.import_relations(
        {"edge": (["source", "target", "label"], [[text, "", "x"] for text in texts])},
        {"edge": (0, 1)},
    )
    found = db.texts_containing(["name."], 100_000)
    assert [text for text, _ in found] == texts
    assert [ident for _, ident in found] == db.intern_ids(texts)
    assert [text for text, _ in db.texts_containing(["0444"], 3)] == [
        "name.000444", "name.004440", "name.004441",
    ]


# ---------------------------------------------------------------------------
# String literals in a raw query's path and name columns
# ---------------------------------------------------------------------------


def _literal_graph() -> FactGraph:
    graph = FactGraph()
    graph.add_calls_batch([
        CallFact("app.main", "os.system", "app.py", 3, 4),
        CallFact("app.main", "app.helper", "app.py", 4, 4),
        CallFact("lib.run", "os.system", "lib.py", 9, 0),
    ])
    graph.add_references_batch([
        ReferenceFact("os.system", "app.py", 3, 4, "call"),
        ReferenceFact("os.system", "lib.py", 9, 0, "call"),
        ReferenceFact("app.helper", "app.py", 4, 4, "call"),
    ])
    return graph


@pytest.mark.parametrize("query, expected", [
    # A name or path written where its column is: the natural way to ask.
    ('?[caller] := *call[caller, "os.system", _, _, _, _, _]', [["app.main"], ["lib.run"]]),
    ("?[caller] := *call[caller, 'os.system', _, _, _, _, _]", [["app.main"], ["lib.run"]]),
    ('?[callee] := *call["app.main", callee, "app.py", _, _, _, _]',
     [["app.helper"], ["os.system"]]),
    ('?[file] := *reference{symbol_qn: "os.system", file_path: file}', [["app.py"], ["lib.py"]]),
    ('?[line] := *reference{symbol_qn: "os.system", file_path: "lib.py", line}', [[9]]),
    ('?[caller] := *call_by_callee[ "os.system" , caller, _, _, _, _, _]',
     [["app.main"], ["lib.run"]]),
    # A name the graph has never seen matches nothing, without an error.
    ('?[caller] := *call[caller, "os.absent", _, _, _, _, _]', []),
])
def test_string_literal_in_an_id_column_matches_its_text(query, expected):
    assert _literal_graph().run_query(query)["rows"] == expected


@pytest.mark.parametrize("query, expected", [
    # A text column keeps its string.
    ('?[name] := *reference{symbol_qn: name, ref_kind: "call", file_path: "lib.py"}',
     [["os.system"]]),
    # Text that only looks like an atom is left alone.
    ("?[text] := text = \"*call[caller, 'os.system', _, _, _, _, _]\"",
     [["*call[caller, 'os.system', _, _, _, _, _]"]]),
    # So is a comment, and an expression inside a position.
    ('# *call[caller, "os.system", _, _, _, _, _]\n'
     '?[callee] := *call[caller, callee, _, line, _, _, _], line == max(4, 0)',
     [["app.helper"]]),
    # The dictionary is still there for comparisons and string functions.
    ('?[caller] := *intern["os.system", id], *call[caller, id, _, _, _, _, _]',
     [["app.main"], ["lib.run"]]),
])
def test_other_strings_in_a_raw_query_are_untouched(query, expected):
    assert _literal_graph().run_query(query)["rows"] == expected
