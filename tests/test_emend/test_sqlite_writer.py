"""Writer jobs own transactions; readers never borrow the writer connection."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from emend.analysis_store import AnalysisStore
from emend.sqlite_writer import SQLiteWriter
from emend.type_oracle import FileTypes, _TypeOracleDiskCache


@pytest.mark.parametrize("name", ["parse.db", "analysis-artifacts.db"])
def test_concurrent_type_cache_clients_persist_every_write(tmp_path, name):
    path = tmp_path / ".emend" / "cache" / name
    path.parent.mkdir(parents=True)
    caches = [_TypeOracleDiskCache(str(path)) for _ in range(8)]
    def write(index):
        for item in range(25):
            key = f"context|manual|{index}-{item}"
            caches[index].put(key, FileTypes(path=key))
            assert caches[index].get(key).path == key
    with ThreadPoolExecutor(max_workers=len(caches)) as pool:
        list(pool.map(write, range(len(caches))))
    AnalysisStore.open(tmp_path).close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM type_cache").fetchone() == (200,)


@pytest.mark.parametrize("artifacts", [False, True])
def test_queued_transaction_rollback_read_isolation_and_drain(tmp_path, artifacts):
    store = AnalysisStore(tmp_path)
    reader = store.artifact_connection() if artifacts else store.connection()
    path = store.artifact_path if artifacts else store.db_path
    store.write(lambda conn: conn.execute("CREATE TABLE items (value)").close(), artifacts=artifacts)
    entered, release = Event(), Event()

    def failing_transaction(conn):
        conn.execute("INSERT INTO items VALUES (1)")
        entered.set()
        assert release.wait(5)
        raise ValueError("rollback")

    first = store.submit_write(failing_transaction, artifacts=artifacts)
    try:
        assert entered.wait(5)
        second = store.submit_write(
            lambda conn: conn.execute("INSERT INTO items VALUES (2)").close(), artifacts=artifacts,
        )
        assert not second.done()
        assert reader.execute("SELECT * FROM items").fetchall() == []
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("INSERT INTO items VALUES (99)")
    finally:
        release.set()
    with pytest.raises(ValueError, match="rollback"):
        first.result(timeout=5)
    second.result(timeout=5)
    assert reader.execute("SELECT * FROM items").fetchall() == [(2,)]
    last = store.submit_write(
        lambda conn: conn.execute("INSERT INTO items VALUES (3)").close(), artifacts=artifacts,
    )
    store.close()
    assert last.done()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM items ORDER BY value").fetchall() == [(2,), (3,)]


def test_writer_accepts_queued_transactions_and_rejects_after_close(tmp_path):
    writer = SQLiteWriter(tmp_path / "queue.db")
    entered, release = Event(), Event()

    def block(conn):
        entered.set()
        assert release.wait(5)

    active = writer.submit(block)
    assert entered.wait(5)
    queued = writer.submit(lambda conn: 2)
    try:
        last = writer.submit(lambda conn: 3)
        assert not queued.done() and not last.done()
    finally:
        release.set()
        writer.close()
    assert active.result() is None and queued.result() == 2 and last.result() == 3
    with pytest.raises(RuntimeError, match="closed"):
        writer.submit(lambda conn: None)
