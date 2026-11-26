"""Flow occurrences and edges are stored as one compact blob per file.

A project has millions of these facts and nothing joins on them: they are
only ever read back a file at a time.  Storing a database row for each one
made them more than half of ``facts.db``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from emend import emend_core
from emend.analysis_snapshot import FlowEdgeFact, FlowEventFact
from emend.analysis_store import AnalysisStore
from emend.fact_graph import FactGraph

SOURCE_A = (
    "def handler(request):\n"
    "    value = request.args.get('name')\n"
    "    items = []\n"
    "    items.append(value)\n"
    "    return render(items)\n"
)
SOURCE_B = (
    "def helper(x):\n"
    "    y = x + 1\n"
    "    return y\n"
)


def _event(file_path="a.py", event_id=0, **overrides) -> FlowEventFact:
    values = dict(
        file_path=file_path, event_id=event_id, func_id="f@0", func_name="f",
        func_start=0, role="use", var="x", access_path="x.y", block=1,
        start_byte=10, end_byte=13, start_line=2, start_col=4, end_line=2,
        end_col=7, ordinal=event_id, call_id=None, arg_index=None,
        arg_name=None, text="x.y",
    )
    values.update(overrides)
    return FlowEventFact(**values)


def _relations(graph: FactGraph) -> set[str]:
    return {row[0] for row in graph.run_query("::relations")["rows"]}


@pytest.fixture
def project_graph(tmp_path):
    (tmp_path / "a.py").write_text(SOURCE_A)
    (tmp_path / "b.py").write_text(SOURCE_B)
    (tmp_path / "empty.py").write_text("")
    store = AnalysisStore(tmp_path)
    try:
        yield store.query_facts()
    finally:
        store.close()


def test_flow_facts_take_one_stored_row_per_file(project_graph):
    relations = _relations(project_graph)
    assert "flow_blob" in relations
    assert not {"flow_event", "flow_edge"} & relations

    rows = project_graph.run_query("?[file_path] := *flow_blob[file_path, _]")["rows"]
    # Even an empty module has its entry and exit occurrences.
    assert [row[0] for row in rows] == ["a.py", "b.py", "empty.py"]

    events = project_graph.flow_events()
    assert [path for path in dict.fromkeys(event.file_path for event in events)] == [
        "a.py", "b.py", "empty.py",
    ]
    assert len(events) > 20
    assert len(project_graph.flow_edges()) > 20


def test_flow_events_keep_their_exact_source_positions(project_graph):
    by_file = {
        "a.py": SOURCE_A.encode(),
        "b.py": SOURCE_B.encode(),
    }
    events = project_graph.flow_events()
    spans = [event for event in events if event.end_byte > event.start_byte]
    assert spans
    for event in spans:
        text = by_file[event.file_path][event.start_byte:event.end_byte].decode()
        assert event.text == text
        assert text.splitlines()[0] in by_file[event.file_path].decode().splitlines()[event.start_line]
    assert any(event.var is None for event in events)
    assert any(event.call_id is not None for event in events)
    assert any(event.arg_index == 0 for event in events)
    ids = [event.event_id for event in events if event.file_path == "a.py"]
    assert len(ids) == len(set(ids))


def test_flow_accessors_filter_like_a_scan(project_graph):
    events, edges = project_graph.flow_events(), project_graph.flow_edges()
    a_events = [event for event in events if event.file_path == "a.py"]
    assert project_graph.flow_events(file_path="a.py") == a_events
    function = next(event.func_id for event in a_events if event.func_name == "handler")
    assert project_graph.flow_events(func_id=function) == [
        event for event in events if event.func_id == function
    ]
    assert project_graph.flow_events(file_path="a.py", role="call") == [
        event for event in a_events if event.role == "call"
    ]
    assert project_graph.flow_events(file_path="missing.py") == []
    assert project_graph.flow_edges(file_path="b.py") == [
        edge for edge in edges if edge.file_path == "b.py"
    ]
    kind = edges[0].edge_kind
    assert project_graph.flow_edges(edge_kind=kind) == [
        edge for edge in edges if edge.edge_kind == kind
    ]


def test_removing_a_file_removes_its_flow_facts(project_graph):
    project_graph.remove_files(["a.py", "empty.py"])
    assert {event.file_path for event in project_graph.flow_events()} == {"b.py"}
    assert {edge.file_path for edge in project_graph.flow_edges()} == {"b.py"}


def test_added_flow_facts_round_trip_and_upsert_by_key():
    graph = FactGraph()
    events = [
        _event(event_id=0, role="function_entry", var=None, access_path=None,
               start_byte=0, end_byte=0, text=""),
        _event(event_id=1, call_id=4, arg_index=0, arg_name="value",
               text="héllo ☃ «x»", access_path=None),
        _event(event_id=7, func_id="g@120", func_name="g", func_start=120,
               start_byte=4_000_000_000, end_byte=4_000_000_050, start_line=90_000,
               end_line=90_002, ordinal=3),
        _event(file_path="b.py", event_id=0, role="def"),
    ]
    edges = [
        FlowEdgeFact("a.py", 0, 1, "control"),
        FlowEdgeFact("a.py", 1, 7, "transfer"),
        FlowEdgeFact("a.py", 1, 7, "reaching"),
        FlowEdgeFact("a.py", 7, 1, "control;enter,4,1"),
        FlowEdgeFact("b.py", 0, 0, "transfer"),
    ]
    graph.add_flow_events_batch(events[:2])
    graph.add_flow_events_batch(events[2:])
    graph.add_flow_edges_batch(edges[:1])
    graph.add_flow_edges_batch(edges[1:])

    assert graph.flow_events() == events
    assert sorted(graph.flow_edges(), key=dataclasses.astuple) == sorted(
        edges, key=dataclasses.astuple
    )

    replaced = dataclasses.replace(events[1], role="mutation", text="changed")
    graph.add_flow_event(replaced)
    graph.add_flow_edge(edges[0])
    assert graph.flow_events(file_path="a.py") == [events[0], replaced, events[2]]
    assert len(graph.flow_edges()) == len(edges)

    restored = FactGraph.from_json(graph.to_json())
    assert restored.flow_events() == graph.flow_events()
    assert sorted(restored.flow_edges(), key=dataclasses.astuple) == sorted(
        graph.flow_edges(), key=dataclasses.astuple
    )


def test_flow_blob_codec_round_trips_and_rejects_foreign_bytes():
    events = [
        (0, "f@0", "f", 0, "function_entry", None, None, 0, 0, 0, 0, 0, 0, 0, 0, None, None, None, ""),
        (1, "f@0", "f", 0, "call_arg", "x", "x.y", 2, 10, 13, 3, 4, 3, 7, 1, 5, 0, "name", "x.y"),
        (9, "g@40", "g", 40, "use", "z", None, 0, 5, 6, 1, 0, 1, 1, 0, None, 2, None, "z"),
    ]
    edges = [(0, 1, "control"), (1, 9, "transfer"), (9, 0, "reaching")]
    payload = emend_core.encode_flow_facts(events, edges)
    assert isinstance(payload, bytes)
    assert emend_core.decode_flow_events(payload) == events
    assert emend_core.decode_flow_edges(payload) == edges

    empty = emend_core.encode_flow_facts([], [])
    assert emend_core.decode_flow_events(empty) == []
    assert emend_core.decode_flow_edges(empty) == []

    for garbage in (b"", b"\x00", b"\x01not zlib", payload[:-3], b"\x09" + payload[1:]):
        with pytest.raises(ValueError):
            emend_core.decode_flow_events(garbage)
        with pytest.raises(ValueError):
            emend_core.decode_flow_edges(garbage)


def test_flow_blob_is_much_smaller_than_its_rows():
    source = "def run(items):\n" + "".join(
        f"    value_{index} = transform(items[{index}], scale={index})\n"
        f"    sink(value_{index})\n"
        for index in range(200)
    )
    facts = emend_core.build_flow_facts(source, ext="py")
    events = [
        (e["id"], e["func_id"], e["func_name"], e["func_start"], e["role"],
         e.get("var"), e.get("access_path"), e["block"], e["start_byte"], e["end_byte"],
         e["start_line"], e["start_col"], e["end_line"], e["end_col"], e["ordinal"],
         e.get("call_id"), e.get("arg_index"), e.get("arg_name"), e.get("text", ""))
        for e in facts["events"]
    ]
    edges = [(e["from"], e["to"], e["kind"]) for e in facts["edges"]]
    payload = emend_core.encode_flow_facts(events, edges)
    assert emend_core.decode_flow_events(payload) == events
    # Edges come back as an ordered set, whatever order they were given in.
    assert emend_core.decode_flow_edges(payload) == sorted(set(edges))
    assert emend_core.encode_flow_facts(events[::-1], edges[::-1] + edges) == payload
    assert len(events) > 1000
    # One byte-oriented database row per event was over 300 bytes.
    assert len(payload) < 12 * (len(events) + len(edges))
