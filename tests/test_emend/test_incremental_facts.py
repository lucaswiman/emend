"""Phase 14a: Tests for incremental fact updates and file removal.

Tests that FactGraph.update_files() and FactGraph.remove_files() correctly
add, replace, and delete per-file facts without a full rebuild.
"""

import textwrap

import pytest

import emend.fact_graph as fact_graph_module
from emend.fact_graph import (
    CallFact,
    CfgBlockFact,
    DefUseFact,
    DecoratorOnFact,
    ExportedSymbolFact,
    FactGraph,
    ImportFact,
    ReferenceFact,
    SourceLocFact,
    SymbolFact,
)
from emend.analysis_snapshot import ExtractedFile, FileRevision


@pytest.mark.parametrize("caller,statement", [
    ("pkg/sub/caller.py", "from ..helpers import live as alias"),
    ("pkg/sub/__init__.py", "from ..helpers import live as alias"),
    ("pkg/caller.py", "from .helpers import live as alias"),
    ("pkg/__init__.py", "from .helpers import live as alias"),
    ("pkg/caller.py", "from . import helpers as alias"),
])
def test_relative_import_facts_preserve_package(tmp_path, caller, statement):
    path = tmp_path / caller
    path.parent.mkdir(parents=True)
    module_import = " import helpers " in statement
    source = statement + "\ndef run():\n    " + ("alias.live()" if module_import else "alias()") + "\n"
    graph = FactGraph()
    graph.update_files([
        (str(tmp_path / "pkg/helpers.py"), "def live():\n    return 1\ndef dead():\n    return 2\n"),
        (str(path), source),
    ], project_root=str(tmp_path), resolver_root=str(tmp_path))
    assert any(ref.ref_kind == "call" for ref in graph.references_to("pkg.helpers.live"))
    dead, _ = graph.dead_code_unified()
    assert "live" not in {symbol.name for symbol in dead}
    assert "dead" in {symbol.name for symbol in dead}


# -- Helpers ----------------------------------------------------------------

def _simple_source_a():
    """A simple Python source for file 'a.py'."""
    return textwrap.dedent("""\
        def greet(name):
            return f"hello {name}"

        def farewell(name):
            return f"bye {name}"
    """)


def _simple_source_b():
    """A simple Python source for file 'b.py' that calls a.greet."""
    return textwrap.dedent("""\
        from a import greet

        def main():
            greet("world")
    """)


def _modified_source_a():
    """Modified version of a.py with greet removed and a new function added."""
    return textwrap.dedent("""\
        def farewell(name):
            return f"bye {name}"

        def welcome(name):
            return f"welcome {name}"
    """)


def test_warm_caches_rebuilds_old_export_relation(tmp_path):
    """A stale relation shape is replaced by a project-owned current snapshot."""
    from emend.analysis_store import AnalysisStore
    from emend.fact_graph import FACT_GRAPH_SCHEMA_VERSION, _create_cozo_client
    from emend.transform import _cache_db_dir

    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "mod.py").write_text("def live():\n    return 1\n")
    cache_dir = _cache_db_dir(tmp_path)
    cache_dir.mkdir(parents=True)
    db_path = cache_dir / "facts.db"
    store = AnalysisStore.open(tmp_path)
    snapshot = store.disk_snapshot()
    client = _create_cozo_client(str(db_path))
    client.run("{:create exported_symbol { qualified_name: String }}")
    client.run("{:create facts_meta { key: String => value: String }}")
    client.run(
        '?[key, value] <- $rows '
        ":put facts_meta {key => value}",
        {"rows": [
            ["schema_version", "6"],
            ["snapshot_id", snapshot.snapshot_id],
        ]}
    )
    client.run(
        "{:create file_revision {file_path: String => content_hash: String, "
        "language: String, module_name: String, origin: String, version: Int}}"
    )
    client.run(
        "?[file_path, content_hash, language, module_name, origin, version] <- $rows "
        ":put file_revision {file_path => content_hash, language, module_name, "
        "origin, version}",
        {"rows": [[
            revision.file_path, revision.content_hash, revision.language,
            revision.module_name, revision.origin, -1,
        ] for revision in snapshot.files]},
    )
    client.close()

    graph = store.query_facts()
    assert graph.published_snapshot_id() == graph.snapshot.snapshot_id
    assert graph.run_query(
        '?[value] := *facts_meta["schema_version", value]'
    )["rows"] == [[FACT_GRAPH_SCHEMA_VERSION]]


def test_facts_schema_rejects_corrupt_database(tmp_path):
    from emend.analysis_store import AnalysisStore

    db_path = tmp_path / "facts.db"
    db_path.write_bytes(b"not a sqlite database")
    (tmp_path / "live.py").write_text("def live():\n    return 1\n")
    store = AnalysisStore.open(tmp_path)
    store.facts_path = db_path
    assert [fact.name for fact in store.query_facts().symbols()] == ["live"]


# -- Test: update_files exists and populates facts --------------------------

class TestUpdateFiles:
    def test_update_files_populates_symbols(self, tmp_path):
        """update_files() should populate symbol facts from source content."""
        a_py = tmp_path / "a.py"
        a_py.write_text(_simple_source_a())

        graph = FactGraph()
        graph.update_files([(str(a_py), _simple_source_a())])

        syms = graph.symbols()
        sym_names = {s.name for s in syms}
        assert "greet" in sym_names
        assert "farewell" in sym_names

    def test_update_files_populates_imports(self, tmp_path):
        """update_files() should populate import facts."""
        b_py = tmp_path / "b.py"
        b_py.write_text(_simple_source_b())

        graph = FactGraph()
        graph.update_files([(str(b_py), _simple_source_b())])

        imports = graph._all_imports()
        assert len(imports) >= 1
        assert any(i.imported_name == "greet" for i in imports)

    def test_update_files_replaces_stale_facts(self, tmp_path):
        """Calling update_files() a second time for the same file should
        replace old facts, not accumulate them."""
        a_py = tmp_path / "a.py"
        a_py.write_text(_simple_source_a())

        graph = FactGraph()
        graph.update_files([(str(a_py), _simple_source_a())])

        # greet should be present initially
        sym_names = {s.name for s in graph.symbols()}
        assert "greet" in sym_names

        # Now update with modified source (greet removed, welcome added)
        graph.update_files([(str(a_py), _modified_source_a())])

        sym_names = {s.name for s in graph.symbols()}
        assert "greet" not in sym_names, "stale symbol 'greet' should have been removed"
        assert "welcome" in sym_names, "new symbol 'welcome' should be present"
        assert "farewell" in sym_names, "unchanged symbol 'farewell' should still be present"

    def test_update_files_does_not_affect_other_files(self, tmp_path):
        """Updating one file should not touch facts from another file."""
        a_py = tmp_path / "a.py"
        b_py = tmp_path / "b.py"
        a_py.write_text(_simple_source_a())
        b_py.write_text(_simple_source_b())

        graph = FactGraph()
        graph.update_files([
            (str(a_py), _simple_source_a()),
            (str(b_py), _simple_source_b()),
        ])

        # Now update only a.py
        graph.update_files([(str(a_py), _modified_source_a())])

        # b.py facts should be untouched
        b_path = str(b_py.resolve())
        b_imports = [i for i in graph._all_imports() if i.importing_file == b_path]
        assert len(b_imports) >= 1, "b.py imports should still be present"

    def test_entry_point_seeds_do_not_persist_between_queries(self):
        cases = [
            ({"entry_point_names": ["run"]}, "run"),
            ({"entry_point_prefixes": ["serve_"]}, "serve_app"),
            ({"entry_point_decorators": ["route"]}, "decorated"),
        ]
        for kwargs, name in cases:
            graph = FactGraph()
            graph.add_symbol(SymbolFact("app.py", name, f"app.{name}", "function", 1, 2))
            if "entry_point_decorators" in kwargs:
                graph.add_decorator_on(DecoratorOnFact(f"app.{name}", "route"))

            dead, _ = graph.dead_code_unified(**kwargs)
            assert name not in {symbol.name for symbol in dead}
            dead_again, _ = graph.dead_code_unified()
            assert name in {symbol.name for symbol in dead_again}

    def test_update_files_replaces_cfg_facts(self, tmp_path):
        """CFG block and edge facts should be replaced on update."""
        a_py = tmp_path / "a.py"
        a_py.write_text(_simple_source_a())

        graph = FactGraph()
        graph.update_files([(str(a_py), _simple_source_a())])

        a_path = str(a_py.resolve())
        blocks_before = [b for b in graph._all_cfg_blocks() if b.file_path == a_path]
        assert len(blocks_before) > 0, "should have CFG blocks for a.py"

        # Update with modified source
        graph.update_files([(str(a_py), _modified_source_a())])

        blocks_after = [b for b in graph._all_cfg_blocks() if b.file_path == a_path]
        # Should still have blocks, but they may differ
        assert len(blocks_after) > 0, "should still have CFG blocks after update"

        # No blocks referencing 'greet' function should remain
        greet_blocks = [
            b for b in blocks_after
            if "greet" in b.func_qn
        ]
        assert len(greet_blocks) == 0, "greet function blocks should be gone after update"

    def test_update_files_replaces_source_locs(self, tmp_path):
        """Source location facts should be replaced on update."""
        a_py = tmp_path / "a.py"
        a_py.write_text(_simple_source_a())

        graph = FactGraph()
        graph.update_files([(str(a_py), _simple_source_a())])

        a_path = str(a_py.resolve())
        locs_before = [l for l in graph._all_source_locs() if l.file_path == a_path]
        assert len(locs_before) > 0

        graph.update_files([(str(a_py), _modified_source_a())])

        locs_after = [l for l in graph._all_source_locs() if l.file_path == a_path]
        # No source locs for 'greet' should remain
        greet_locs = [l for l in locs_after if "greet" in l.loc_id]
        assert len(greet_locs) == 0, "greet source locs should be gone"

    def test_update_files_preserves_definition_reference_kind(self, tmp_path):
        source = "def greet():\n    return 1\n\ngreet()\n"
        path = tmp_path / "greet.py"
        graph = FactGraph()
        graph.update_files([(str(path), source)])

        refs = graph.references_to("greet.greet")
        assert {ref.ref_kind for ref in refs} == {"definition", "call"}


class TestRemoveFiles:
    def test_remove_files_deletes_file_owned_exports(self, tmp_path):
        a_path = str((tmp_path / "a.ts").resolve())
        b_path = str((tmp_path / "b.ts").resolve())
        graph = FactGraph()
        graph.add_exported_symbols_batch([
            ExportedSymbolFact(a_path, "pkg.shared"),
            ExportedSymbolFact(b_path, "pkg.shared"),
        ])

        graph.remove_files([a_path])

        assert graph.exported_symbols() == [ExportedSymbolFact(b_path, "pkg.shared")]

    def test_remove_files_deletes_all_facts(self, tmp_path):
        """remove_files() should delete all facts for the given files."""
        a_py = tmp_path / "a.py"
        b_py = tmp_path / "b.py"
        a_py.write_text(_simple_source_a())
        b_py.write_text(_simple_source_b())

        graph = FactGraph()
        graph.update_files([
            (str(a_py), _simple_source_a()),
            (str(b_py), _simple_source_b()),
        ])

        a_path = str(a_py.resolve())

        # Verify a.py has facts
        a_syms = [s for s in graph.symbols() if s.file_path == a_path]
        assert len(a_syms) > 0

        # Remove a.py
        graph.remove_files([a_path])

        # All a.py facts should be gone
        a_syms_after = [s for s in graph.symbols() if s.file_path == a_path]
        assert len(a_syms_after) == 0, "symbols for a.py should be removed"

        a_imports_after = [i for i in graph._all_imports() if i.importing_file == a_path]
        assert len(a_imports_after) == 0, "imports for a.py should be removed"

        a_blocks_after = [b for b in graph._all_cfg_blocks() if b.file_path == a_path]
        assert len(a_blocks_after) == 0, "cfg blocks for a.py should be removed"

        a_locs_after = [l for l in graph._all_source_locs() if l.file_path == a_path]
        assert len(a_locs_after) == 0, "source locs for a.py should be removed"

    def test_remove_files_preserves_other_files(self, tmp_path):
        """remove_files() should not affect facts from other files."""
        a_py = tmp_path / "a.py"
        b_py = tmp_path / "b.py"
        a_py.write_text(_simple_source_a())
        b_py.write_text(_simple_source_b())

        graph = FactGraph()
        graph.update_files([
            (str(a_py), _simple_source_a()),
            (str(b_py), _simple_source_b()),
        ])

        a_path = str(a_py.resolve())
        b_path = str(b_py.resolve())

        b_syms_before = [s for s in graph.symbols() if s.file_path == b_path]

        graph.remove_files([a_path])

        b_syms_after = [s for s in graph.symbols() if s.file_path == b_path]
        assert len(b_syms_after) == len(b_syms_before), "b.py symbols should be unchanged"

    def test_remove_files_deletes_multiple_files_together(self, tmp_path):
        paths = [str((tmp_path / name).resolve()) for name in ("a.py", "b.py", "c.py")]
        graph = FactGraph()
        graph.add_symbols_batch([
            SymbolFact(path, name, name, "function", 1, 1)
            for path, name in zip(paths, ("a", "b", "c"), strict=True)
        ])

        graph.remove_files(paths[:2])

        assert graph.symbols() == [SymbolFact(paths[2], "c", "c", "function", 1, 1)]

    def test_remove_nonexistent_file_is_noop(self, tmp_path):
        """Removing a file that has no facts should not error."""
        graph = FactGraph()
        # Should not raise
        graph.remove_files(["/nonexistent/file.py"])


def _empty_extracted(path, symbols):
    revision = FileRevision.create("/project", path, "hash", "py", path.removesuffix(".py"))
    return ExtractedFile(
        revision=revision,
        rows={"fg_sym": symbols, "cfg_blocks": [], "cfg_edges": []},
    )


def test_replace_extracted_coalesces_relation_mutations(monkeypatch):
    graph = FactGraph()
    captured = []
    monkeypatch.setattr(graph, "_run_mutations", captured.extend)
    extracted = [
        _empty_extracted("a.py", [["a.f", "a.py", "f", "function", 1, 1, ""]]),
        _empty_extracted("b.py", [["b.f", "b.py", "f", "function", 1, 1, ""]]),
    ]

    graph.replace_extracted(extracted, stored_paths=["a.py", "b.py"])

    removals = [(query, params) for query, params in captured if ":rm symbol" in query]
    inserts = [(query, params) for query, params in captured if ":put symbol" in query]
    assert len(removals) == 1
    assert removals[0][1] == {"fps": ["a.py", "b.py"]}
    assert len(inserts) == 1
    assert inserts[0][1]["rows"] == [
        ["a.f", "a.py", "f", "function", 1, 1, ""],
        ["b.f", "b.py", "f", "function", 1, 1, ""],
    ]


def test_replace_extracted_chunks_large_relations(monkeypatch):
    graph = FactGraph()
    captured = []
    monkeypatch.setattr(graph, "_run_mutations", captured.extend)
    monkeypatch.setattr(fact_graph_module, "_FACT_INSERT_BATCH_SIZE", 1)
    graph.replace_extracted(
        [
            _empty_extracted(
                "a.py",
                [
                    ["a.f", "a.py", "f", "function", 1, 1, ""],
                    ["a.g", "a.py", "g", "function", 2, 2, ""],
                ],
            ),
        ],
        stored_paths=["a.py"],
    )

    inserts = [params for query, params in captured if ":put symbol" in query]
    assert [params["rows"] for params in inserts] == [
        [["a.f", "a.py", "f", "function", 1, 1, ""]],
        [["a.g", "a.py", "g", "function", 2, 2, ""]],
    ]


def test_replace_extracted_preserves_last_symbol_for_duplicate_qn():
    graph = FactGraph()
    graph.replace_extracted(
        [
            _empty_extracted("a.py", [["shared.f", "a.py", "f", "function", 1, 1, ""]]),
            _empty_extracted("b.py", [["shared.f", "b.py", "f", "function", 2, 2, ""]]),
        ],
        stored_paths=["a.py", "b.py"],
    )

    assert graph.symbols() == [SymbolFact("b.py", "f", "shared.f", "function", 2, 2)]


class TestUpdateFilesMatchesBuildFromFiles:
    """update_files() on an empty graph should produce the same result as
    build_from_files()."""

    def test_symbols_match(self, tmp_path):
        a_py = tmp_path / "a.py"
        a_py.write_text(_simple_source_a())
        paths = [str(a_py)]

        graph_bff = FactGraph.build_from_files(paths)
        graph_uf = FactGraph()
        graph_uf.update_files([(str(a_py), _simple_source_a())])

        syms_bff = sorted(s.qualified_name for s in graph_bff.symbols())
        syms_uf = sorted(s.qualified_name for s in graph_uf.symbols())
        assert syms_bff == syms_uf

    def test_imports_match(self, tmp_path):
        b_py = tmp_path / "b.py"
        b_py.write_text(_simple_source_b())
        paths = [str(b_py)]

        graph_bff = FactGraph.build_from_files(paths)
        graph_uf = FactGraph()
        graph_uf.update_files([(str(b_py), _simple_source_b())])

        imports_bff = sorted(
            (i.importing_file, i.imported_module, i.imported_name)
            for i in graph_bff._all_imports()
        )
        imports_uf = sorted(
            (i.importing_file, i.imported_module, i.imported_name)
            for i in graph_uf._all_imports()
        )
        assert imports_bff == imports_uf


class TestDerivedQueriesAfterUpdate:
    """Verify that derived Datalog queries work correctly after incremental updates."""

    def test_callers_reflect_update(self, tmp_path):
        """After updating a file, callers_datalog should reflect the new call graph."""
        a_py = tmp_path / "a.py"
        a_py.write_text(textwrap.dedent("""\
            def foo():
                bar()

            def bar():
                pass
        """))

        graph = FactGraph()
        graph.update_files([(str(a_py), a_py.read_text())])

        a_path = str(a_py.resolve())
        # Verify foo calls bar
        calls = graph._all_calls()
        call_pairs = [(c.caller_qn, c.callee_qn) for c in calls]
        # bar should be called by foo
        assert any("foo" in c[0] and "bar" in c[1] for c in call_pairs), \
            f"Expected foo->bar call, got: {call_pairs}"

        # Now update: foo no longer calls bar, calls baz instead
        a_py.write_text(textwrap.dedent("""\
            def foo():
                baz()

            def bar():
                pass

            def baz():
                pass
        """))
        graph.update_files([(str(a_py), a_py.read_text())])

        calls_after = graph._all_calls()
        call_pairs_after = [(c.caller_qn, c.callee_qn) for c in calls_after]
        # foo->bar should be gone, foo->baz should exist
        assert not any("foo" in c[0] and "bar" in c[1] for c in call_pairs_after), \
            f"foo->bar should be removed, got: {call_pairs_after}"
        assert any("foo" in c[0] and "baz" in c[1] for c in call_pairs_after), \
            f"foo->baz should be present, got: {call_pairs_after}"
