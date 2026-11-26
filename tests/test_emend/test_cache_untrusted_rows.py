"""Cache rows are untrusted input and must never be unpickled.

``.emend/cache`` lives inside the analysed checkout, so a repository can ship
prepared cache databases.  Unpickling a row runs whatever code its author
chose.  Each test plants a payload that creates a marker file when unpickled,
then exercises the reader of that cache.
"""

from __future__ import annotations

import hashlib
import pickle
import sqlite3
import zlib
from pathlib import Path

import pytest

from emend.analysis_store import AnalysisStore


class _CreatesMarker:
    def __init__(self, marker: Path) -> None:
        self.marker = marker

    def __reduce__(self):
        return (Path.touch, (self.marker,))


@pytest.fixture
def planted(tmp_path_factory):
    """Return ``(payload, marker)``; the marker exists only if the payload ran."""
    marker = tmp_path_factory.mktemp("marker") / "unpickled"
    payload = pickle.dumps(_CreatesMarker(marker))
    return payload, marker


def _overwrite(db_path: Path, table: str, column: str, value: bytes) -> int:
    """Replace every cached payload in *table*; a missing table holds nothing."""
    if not Path(db_path).exists():
        return 0
    with sqlite3.connect(db_path) as db:
        try:
            return db.execute(f"UPDATE {table} SET {column} = ?", (value,)).rowcount
        except sqlite3.OperationalError:
            return 0


def test_no_analysis_module_can_unpickle():
    source_root = Path(__file__).parents[2] / "src" / "emend"
    offenders = sorted(
        str(path.relative_to(source_root))
        for path in source_root.rglob("*.py")
        if "pickle" in path.read_text(encoding="utf-8")
    )
    assert offenders == []


def test_extracted_facts_are_not_read_from_a_planted_artifact(tmp_path, planted):
    payload, marker = planted
    source = tmp_path / "app.py"
    source.write_text("def first():\n    return 1\n")
    store = AnalysisStore(tmp_path)
    store.query_facts()
    artifacts = store.artifact_path
    store.close()

    with sqlite3.connect(artifacts) as db:
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master")}
    assert "extracted_file_artifact" not in tables
    _overwrite(artifacts, "extracted_file_artifact", "payload", zlib.compress(payload))

    store.facts_path.unlink()
    reopened = AnalysisStore(tmp_path)
    try:
        assert [fact.name for fact in reopened.query_facts().symbols()] == ["first"]
    finally:
        reopened.close()
    assert not marker.exists()


def test_symbol_projection_is_not_unpickled(tmp_path, planted):
    payload, marker = planted
    source = "def first():\n    return 1\n"
    (tmp_path / "app.py").write_text(source)
    store = AnalysisStore(tmp_path)
    assert [symbol.name for symbol in store.symbols(source)] == ["first"]
    artifacts = store.artifact_path
    store.close()
    assert _overwrite(artifacts, "symbol_projection", "payload", zlib.compress(payload)) == 1

    reopened = AnalysisStore(tmp_path)
    try:
        assert [symbol.name for symbol in reopened.symbols(source)] == ["first"]
    finally:
        reopened.close()
    assert not marker.exists()


def test_symbol_projection_round_trips_through_the_artifact_cache(tmp_path, monkeypatch):
    from emend import emend_core

    source = (
        "import os\n\n"
        "@decorate\n"
        "class Outer(Base):\n"
        "    value: int = 1\n\n"
        "    async def method(self, item: str, *rest, flag=False) -> list[str]:\n"
        "        def inner():\n"
        "            return item\n"
        "        return [inner()]\n"
    )
    (tmp_path / "app.py").write_text(source)
    first = AnalysisStore(tmp_path)
    computed = first.symbols(source)
    first.close()

    def unavailable(*args, **kwargs):
        raise AssertionError("symbols were recomputed instead of read from the cache")

    monkeypatch.setattr(emend_core, "collect_symbols_from_str", unavailable)
    second = AnalysisStore(tmp_path)
    try:
        assert second.symbols(source) == computed
    finally:
        second.close()


def test_qualified_name_cache_is_not_unpickled(tmp_path, planted):
    from emend.transform import _get_cached_qnames, warm_caches

    payload, marker = planted
    source = tmp_path / "hello.py"
    content = "def hello():\n    return 1\n"
    source.write_text(content)
    warm_caches(str(tmp_path), type_engine=None)
    digest = hashlib.md5(content.encode(), usedforsecurity=False).digest()
    assert _get_cached_qnames(
        digest, file_path=str(source), project_root=tmp_path,
    ) == {"hello.hello"}

    store = AnalysisStore.open(tmp_path)
    with sqlite3.connect(store.db_path) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(qn_index)")]
        (blob_column,) = [name for name in columns if name not in {"file_path", "hash"}]
        assert db.execute(
            f"UPDATE qn_index SET {blob_column} = ?", (zlib.compress(payload),)
        ).rowcount == 1

    assert _get_cached_qnames(
        digest, file_path=str(source), project_root=tmp_path,
    ) is None
    assert not marker.exists()


def test_duplicate_payload_cache_is_not_unpickled(tmp_path, planted):
    from emend.duplicate import DUP_CACHE_VERSION, _duplicate_cache_key, _load_cached_payloads
    from emend.transform.cache import _init_cache_schema

    payload, marker = planted
    source = tmp_path / "app.py"
    content = "def repeated():\n    return 1\n"
    source.write_text(content)
    store = AnalysisStore.open(tmp_path)
    store.ensure_cache_directory()
    with sqlite3.connect(store.db_path) as db:
        _init_cache_schema(db)
        db.execute(
            "INSERT OR REPLACE INTO dup_cache (hash, version, data) VALUES (?, ?, ?)",
            (_duplicate_cache_key(str(source), content), DUP_CACHE_VERSION,
             zlib.compress(payload)),
        )

    assert _load_cached_payloads(str(tmp_path), [str(source)]) is None
    assert not marker.exists()


def test_duplicate_payload_cache_round_trips(tmp_path):
    from emend.duplicate import _load_cached_payloads, query_duplicates
    from emend.transform import warm_caches

    body = "".join(f"    total += compute({index})\n" for index in range(8))
    for name in ("first", "second"):
        (tmp_path / f"{name}.py").write_text(
            f"def {name}(total):\n{body}    return total\n"
        )
    fresh = query_duplicates(str(tmp_path), min_lines=3)
    assert fresh

    warm_caches(str(tmp_path), type_engine=None, build_duplicates=True)
    cached = _load_cached_payloads(
        str(tmp_path), [str(tmp_path / "first.py"), str(tmp_path / "second.py")],
    )
    assert cached is not None and len(cached) == 2
    assert query_duplicates(str(tmp_path), min_lines=3) == fresh


def test_type_result_cache_is_not_unpickled(tmp_path, planted):
    from emend.type_oracle import _TypeOracleDiskCache

    payload, marker = planted
    db_path = tmp_path / "types.db"
    cache = _TypeOracleDiskCache(str(db_path))
    try:
        with sqlite3.connect(db_path) as db:
            db.execute(
                "INSERT OR REPLACE INTO type_cache VALUES (?, ?)",
                ("planted", zlib.compress(payload)),
            )
        assert cache.get("planted") is None
    finally:
        cache.close()
    assert not marker.exists()


def test_type_result_cache_round_trips(tmp_path):
    from emend.type_oracle import (
        FileTypes,
        TypeBinding,
        TypeDescriptor,
        _TypeOracleDiskCache,
        parse_type_string,
    )

    callable_type = TypeDescriptor(
        "callable", params=(TypeDescriptor("named", "int"),),
        return_type=parse_type_string("dict[str, list[int] | None]"),
    )
    original = FileTypes(
        path=str(tmp_path / "app.py"),
        bindings=[
            TypeBinding("value", 3, 4, 9, parse_type_string("list[int]"), "list[int]", "definition"),
            TypeBinding("handler", 7, 0, None, callable_type, "(int) -> dict", "reference"),
        ],
        complete=False,
        error="analysis stopped early",
    )
    original.build_index()
    cache = _TypeOracleDiskCache(str(tmp_path / "types.db"))
    try:
        cache.put("key", original)
        restored = cache.get("key")
    finally:
        cache.close()

    assert restored is not None
    assert restored.path == original.path
    assert restored.bindings == original.bindings
    assert (restored.complete, restored.error) == (False, "analysis stopped early")
    assert restored._by_name.keys() == original._by_name.keys()
