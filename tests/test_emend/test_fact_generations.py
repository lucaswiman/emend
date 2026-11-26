"""Lifetime of the fact database generations in ``.emend/cache``.

A published ``facts.db`` can be gigabytes.  Readers must not copy it to look at
it, no process may leave a private generation behind, and a generation that
other processes can see must never be written in place.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from emend.analysis_snapshot import SymbolFact
from emend.analysis_store import AnalysisStore

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="an open file cannot be replaced on Windows; it is copied",
)


def _names(graph) -> list[str]:
    return sorted(fact.name for fact in graph.symbols())


def _generations(root: Path) -> list[str]:
    cache = root / ".emend" / "cache"
    return sorted(
        path.name for path in cache.glob("facts-*")
        if path.name not in {"facts.db", "facts.lock"}
    )


def _project(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("def original():\n    return 1\n")
    return tmp_path / "app.py"


@posix_only
def test_reading_published_facts_does_not_copy_the_database(tmp_path):
    _project(tmp_path)
    writer = AnalysisStore(tmp_path)
    writer.query_facts()
    writer.close()

    reader = AnalysisStore(tmp_path)
    try:
        graph = reader.query_facts()
        assert _names(graph) == ["original"]
        assert os.path.samefile(graph._db_path, reader.facts_path)
    finally:
        reader.close()


@posix_only
def test_publishing_a_generation_does_not_copy_the_database(tmp_path):
    source = _project(tmp_path)
    store = AnalysisStore(tmp_path)
    try:
        cold = store.query_facts()
        assert os.path.samefile(cold._db_path, store.facts_path)

        source.write_text("def changed():\n    return 2\n")
        warm = store.query_facts()
        assert _names(warm) == ["changed"]
        assert os.path.samefile(warm._db_path, store.facts_path)
    finally:
        store.close()
    assert _names(AnalysisStore(tmp_path).query_facts()) == ["changed"]


def test_reader_keeps_its_generation_when_another_store_publishes(tmp_path):
    source = _project(tmp_path)
    reader, writer = AnalysisStore(tmp_path), AnalysisStore(tmp_path)
    try:
        held = reader.query_facts()
        source.write_text("def published_later():\n    return 2\n")
        assert _names(writer.query_facts()) == ["published_later"]

        assert _names(held) == ["original"]
        assert held.snapshot.snapshot_id == held.published_snapshot_id()
        assert _names(reader.query_facts()) == ["published_later"]
    finally:
        reader.close()
        writer.close()


def test_writing_to_an_owned_graph_never_changes_the_published_generation(tmp_path):
    _project(tmp_path)
    store = AnalysisStore(tmp_path)
    try:
        graph = store.query_facts()
        graph.add_symbol(SymbolFact("app.py", "private", "app.private", "function", 9, 9))
        assert _names(graph) == ["original", "private"]

        other = AnalysisStore(tmp_path)
        try:
            assert _names(other.query_facts()) == ["original"]
        finally:
            other.close()
        assert _names(graph) == ["original", "private"]
    finally:
        store.close()
    assert _generations(tmp_path) == []


def test_closed_stores_leave_only_the_published_generation(tmp_path):
    source = _project(tmp_path)
    store = AnalysisStore(tmp_path)
    store.query_facts()
    source.write_text("def changed():\n    return 2\n")
    store.query_facts()
    store.update_overlay(source, "def unsaved():\n    return 3\n", 1)
    assert _names(store.query_facts()) == ["unsaved"]
    store.close()

    assert _generations(tmp_path) == []
    assert store.facts_path.is_file()


def test_process_exit_without_close_leaves_no_private_generations(tmp_path):
    source = _project(tmp_path)
    script = (
        "import sys\n"
        "from emend.analysis_store import AnalysisStore\n"
        "store = AnalysisStore.open(sys.argv[1])\n"
        "print(sorted(fact.name for fact in store.query_facts().symbols()))\n"
    )

    def run() -> str:
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    assert run() == "['original']"
    assert _generations(tmp_path) == []
    assert run() == "['original']"
    assert _generations(tmp_path) == []
    source.write_text("def changed():\n    return 2\n")
    assert run() == "['changed']"
    assert _generations(tmp_path) == []


def test_generations_abandoned_by_dead_processes_are_swept(tmp_path):
    from emend.analysis_store import generation_owner_tag

    _project(tmp_path)
    cache = tmp_path / ".emend" / "cache"
    cache.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    host = generation_owner_tag().split("p")[0]

    abandoned = cache / f"facts-open-{host}p{dead.pid}-aaaaaaaa.db"
    abandoned_journal = cache / f"facts-open-{host}p{dead.pid}-aaaaaaaa.db-journal"
    legacy_old = cache / "facts-next-j91vxlt3.db"
    legacy_new = cache / "facts-overlay-dpffyjrc.db"
    live = cache / f"facts-open-{generation_owner_tag()}-bbbbbbbb.db"
    foreign = cache / f"facts-open-h00000000p{dead.pid}-cccccccc.db"
    unrelated = cache / "facts-notes.txt"
    for path in (abandoned, abandoned_journal, legacy_old, legacy_new, live, foreign, unrelated):
        path.write_bytes(b"")
    two_hours_ago = time.time() - 2 * 3600
    os.utime(legacy_old, (two_hours_ago, two_hours_ago))

    store = AnalysisStore(tmp_path)
    try:
        assert _names(store.query_facts()) == ["original"]
    finally:
        store.close()

    remaining = set(_generations(tmp_path))
    if os.name != "nt":
        assert abandoned.name not in remaining
        assert abandoned_journal.name not in remaining
    assert legacy_old.name not in remaining
    assert {legacy_new.name, live.name, foreign.name, unrelated.name} <= remaining


def test_store_recovers_when_its_generation_file_is_removed(tmp_path):
    _project(tmp_path)
    store = AnalysisStore(tmp_path)
    try:
        first = store.query_facts()
        Path(first._db_path).unlink()

        second = store.query_facts()
        assert Path(second._db_path).exists()
        assert _names(second) == ["original"]
    finally:
        store.close()
    assert _generations(tmp_path) == []


def _storage_objects(db_path: Path) -> list[str]:
    import sqlite3

    with sqlite3.connect(f"{Path(db_path).as_uri()}?mode=ro", uri=True) as db:
        return sorted(name for (name,) in db.execute("SELECT name FROM sqlite_master"))


def test_fact_storage_keeps_one_copy_of_each_key(tmp_path):
    """Cozo's default rowid table adds a full second copy of every key."""
    from emend.fact_graph import FactGraph

    source = _project(tmp_path)
    store = AnalysisStore(tmp_path)
    try:
        store.query_facts()
        assert _storage_objects(store.facts_path) == ["cozo"]

        source.write_text("def changed():\n    return 2\n")
        assert _names(store.query_facts()) == ["changed"]
        assert _storage_objects(store.facts_path) == ["cozo"]
    finally:
        store.close()

    direct = tmp_path / "direct.db"
    graph = FactGraph(db_path=str(direct))
    graph.add_symbol(SymbolFact("app.py", "direct", "app.direct", "function", 1, 1))
    assert _names(graph) == ["direct"]
    graph.close()
    assert _storage_objects(direct) == ["cozo"]
