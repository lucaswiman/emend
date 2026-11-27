"""Lifetime of the fact database generations in ``.emend/cache``.

A published ``facts.db`` can be gigabytes.  Readers must not copy it to look at
it, no process may leave a private generation behind, and a generation that
other processes can see must never be written in place.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
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


def test_published_facts_in_the_doubled_key_layout_are_rebuilt(tmp_path):
    """Cloning an old database forward would keep its second copy of every key."""
    import sqlite3

    from emend.emend_core import PyCozoDb

    _project(tmp_path)
    store = AnalysisStore(tmp_path)
    store.ensure_cache_directory()
    with sqlite3.connect(store.facts_path) as db:
        db.execute("CREATE TABLE cozo (k BLOB PRIMARY KEY, v BLOB)")
    PyCozoDb("sqlite", str(store.facts_path)).run(":create facts_meta {key: String => value: String}")
    assert _storage_objects(store.facts_path) == ["cozo", "sqlite_autoindex_cozo_1"]

    try:
        assert _names(store.query_facts()) == ["original"]
        assert _storage_objects(store.facts_path) == ["cozo"]
    finally:
        store.close()
    assert _generations(tmp_path) == []


def test_reading_current_facts_scans_once_and_takes_no_publication_lock(tmp_path, monkeypatch):
    """A reader of an up-to-date index must not queue behind writers."""
    from contextlib import contextmanager

    _project(tmp_path)
    writer = AnalysisStore(tmp_path)
    writer.query_facts()
    writer.close()

    scans = 0
    scan = AnalysisStore._scan_disk_locked

    def counted_scan(self):
        nonlocal scans
        scans += 1
        return scan(self)

    @contextmanager
    def forbidden_lock(self):
        raise AssertionError("a reader took the publication lock")
        yield

    monkeypatch.setattr(AnalysisStore, "_scan_disk_locked", counted_scan)
    monkeypatch.setattr(AnalysisStore, "_facts_write_lock", forbidden_lock)
    reader = AnalysisStore(tmp_path)
    try:
        assert _names(reader.query_facts()) == ["original"]
        assert scans == 1
        assert _names(reader.query_facts()) == ["original"]
        assert scans == 2
    finally:
        reader.close()


def test_scanning_resolves_each_source_path_once(tmp_path, monkeypatch):
    """Canonicalising a path costs a system call per directory level."""
    from emend.project_config import module_name_for_file

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'sample'\n")
    package = tmp_path / "src" / "sample" / "nested"
    package.mkdir(parents=True)
    names = [f"module_{index}.py" for index in range(20)]
    for name in names:
        (package / name).write_text("value = 1\n")
    (package / "__init__.py").write_text("")
    (tmp_path / "lib.rs").write_text("pub fn run() {}\n")
    (tmp_path / "web.ts").write_text("export const value = 1;\n")
    store = AnalysisStore(tmp_path)
    try:
        store.disk_snapshot()
        resolves = 0
        resolve = Path.resolve

        def counted(self, *args, **kwargs):
            nonlocal resolves
            resolves += 1
            return resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", counted)
        snapshot = store.disk_snapshot()
        monkeypatch.undo()
    finally:
        store.close()

    files = len(snapshot.files)
    assert files == 23
    assert resolves <= files + 12, f"{resolves} path resolutions for {files} files"
    for revision in snapshot.files:
        assert revision.module_name == module_name_for_file(
            revision.file_path, tmp_path, language=revision.language,
        )
    names = {revision.module_name for revision in snapshot.files}
    assert {"lib", "web"} <= names
    assert any(name.endswith("sample.nested") for name in names)
    assert any(name.endswith("sample.nested.module_0") for name in names)


def test_bulk_import_puts_rows_and_replaces_by_key():
    from emend.emend_core import PyCozoDb

    db = PyCozoDb("mem", "")
    db.run(":create item {name: String, line: Int => kind: String, payload: Bytes}")
    db.run(":create other {name: String}")
    columns = ["name", "line", "kind", "payload"]
    db.import_relations({
        "item": (columns, [["a", 1, "def", b"\x00\xff"], ["b", 2, "use", b""]]),
        "other": (["name"], [["x"]]),
    })
    db.import_relations({"item": (columns, [["a", 1, "call", b"new"]])})

    rows = db.run("?[name, line, kind, payload] := *item[name, line, kind, payload]")["rows"]
    assert rows == [["a", 1, "call", b"new"], ["b", 2, "use", b""]]
    assert db.run("?[name] := *other[name]")["rows"] == [["x"]]
    with pytest.raises(RuntimeError):
        db.import_relations({"item": (["name", "line"], [["c", 3]])})
    with pytest.raises(RuntimeError):
        db.import_relations({"missing": (["name"], [["c"]])})


def test_bulk_import_into_an_owned_graph_never_changes_the_published_generation(tmp_path):
    _project(tmp_path)
    store = AnalysisStore(tmp_path)
    try:
        graph = store.query_facts()
        graph.client.import_relations({"symbol": (
            ["qualified_name", "file_path", "name", "kind", "line", "end_line", "parent"],
            [["app.bulk", "app.py", "bulk", "function", 9, 9, ""]],
        )}, {"symbol": (0, 1, 6)})
        assert _names(graph) == ["bulk", "original"]
        other = AnalysisStore(tmp_path)
        try:
            assert _names(other.query_facts()) == ["original"]
        finally:
            other.close()
    finally:
        store.close()
    assert _generations(tmp_path) == []


def _interned_texts(graph) -> set[str]:
    return {row[0] for row in graph.run_query("?[text] := *intern[text, _]")["rows"]}


def _stored_facts(root: Path) -> dict:
    store = AnalysisStore(root)
    try:
        return store.query_facts().to_json()
    finally:
        store.close()


def test_changed_module_inventory_builds_a_fresh_generation(tmp_path):
    """Adding or removing a module relinks every file, so nothing stored survives.

    Deleting and re-inserting every row of the copied generation costs several
    times more than building one, and leaves the removed files' names behind.
    """
    (tmp_path / "app.py").write_text(
        "from extra import only_in_extra\n\ndef original():\n    return only_in_extra()\n"
    )
    (tmp_path / "extra.py").write_text(
        "def only_in_extra():\n    return scratch_value()\n\n"
        "def scratch_value():\n    return 1\n"
    )
    first = AnalysisStore(tmp_path)
    try:
        assert "extra.scratch_value" in _interned_texts(first.query_facts())
    finally:
        first.close()

    (tmp_path / "extra.py").unlink()
    second = AnalysisStore(tmp_path)
    try:
        graph = second.query_facts()
        assert _names(graph) == ["original"]
        assert "extra.scratch_value" not in _interned_texts(graph)
        assert _generations(tmp_path) == [Path(graph._db_path).name]
    finally:
        second.close()
    assert _generations(tmp_path) == []

    # The generation is what a build from nothing stores.
    updated = _stored_facts(tmp_path)
    for name in ("facts.db", "analysis-artifacts.db", "parse.db"):
        (tmp_path / ".emend" / "cache" / name).unlink(missing_ok=True)
    assert _stored_facts(tmp_path) == updated


def test_an_edit_keeps_the_rest_of_the_generation(tmp_path):
    """A body edit replaces one file's rows in a copy of the generation."""
    (tmp_path / "app.py").write_text("def original():\n    return 1\n")
    (tmp_path / "other.py").write_text("def untouched():\n    return 2\n")
    first = AnalysisStore(tmp_path)
    try:
        before = dict(first.query_facts().run_query(
            "?[text, id] := *intern[text, id]", decode=False,
        )["rows"])
    finally:
        first.close()

    (tmp_path / "app.py").write_text("def original():\n    return 3\n\ndef added():\n    return 4\n")
    second = AnalysisStore(tmp_path)
    try:
        graph = second.query_facts()
        assert _names(graph) == ["added", "original", "untouched"]
        after = dict(graph.run_query("?[text, id] := *intern[text, id]", decode=False)["rows"])
    finally:
        second.close()
    # Texts the generation already held keep their ids.
    assert before.items() <= after.items()
    assert "app.added" in after


# ---------------------------------------------------------------------------
# Copying a generation
# ---------------------------------------------------------------------------


def _database(path, rows, *, journal_mode="delete"):
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA journal_mode={journal_mode}")
    conn.execute("CREATE TABLE t (v)")
    conn.executemany("INSERT INTO t VALUES (?)", [(row,) for row in rows])
    conn.commit()
    return conn


def _values(path):
    with closing(sqlite3.connect(path)) as conn:
        return [row[0] for row in conn.execute("SELECT v FROM t ORDER BY v")]


def test_generation_with_no_log_is_copied_as_a_file(tmp_path, monkeypatch):
    """A finished generation is one file; SQLite's page-by-page backup of it
    is several times slower than copying the file."""
    source, copy = tmp_path / "source.db", tmp_path / "copy.db"
    _database(source, range(500)).close()

    def backup(*args, **kwargs):
        raise AssertionError("a quiescent database was copied through SQLite")

    monkeypatch.setattr(AnalysisStore, "_backup_sqlite", staticmethod(backup))
    AnalysisStore._copy_sqlite(source, copy)

    assert copy.read_bytes() == source.read_bytes()
    assert _values(copy) == list(range(500))


def test_generation_with_a_write_ahead_log_is_copied_through_sqlite(tmp_path):
    """Rows still in the log are part of the database."""
    source, copy = tmp_path / "source.db", tmp_path / "copy.db"
    writer = _database(source, range(10), journal_mode="wal")
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.executemany("INSERT INTO t VALUES (?)", [(row,) for row in range(10, 20)])
        writer.commit()
        assert (tmp_path / "source.db-wal").stat().st_size > 0
        AnalysisStore._copy_sqlite(source, copy)
    finally:
        writer.close()

    assert _values(copy) == list(range(20))


def test_fresh_generation_is_published_compact(tmp_path):
    """A bulk save leaves a tenth of every page empty.  The published file is
    rewritten once, so rewriting it again finds nothing to give back."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'compact'\n")
    for module in range(30):
        (tmp_path / f"mod_{module:02}.py").write_text("".join(
            f"def function_{module}_{number}(value):\n"
            f"    if value > {number}:\n"
            f"        return function_{module}_{(number + 1) % 40}(value - 1)\n"
            f"    return value\n\n\n"
            for number in range(40)
        ))
    store = AnalysisStore.open(tmp_path)
    try:
        store.query_facts()
    finally:
        store.close()

    published = tmp_path / ".emend" / "cache" / "facts.db"
    rewritten = tmp_path / "rewritten.db"
    with closing(sqlite3.connect(published)) as conn:
        conn.execute("VACUUM INTO ?", (str(rewritten),))
    assert published.stat().st_size > 500_000
    assert published.stat().st_size == rewritten.stat().st_size
