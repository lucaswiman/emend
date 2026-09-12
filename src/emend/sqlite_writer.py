"""Transaction-sized write jobs on a database-owned connection."""

from concurrent.futures import Future
from queue import Queue
import sqlite3
import threading


class SQLiteWriter:
    def __init__(self, path):
        self.path = path
        self._jobs = Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="emend-sqlite-writer", daemon=True)
        self._thread.start()

    def submit(self, transaction):
        if threading.current_thread() is self._thread:
            raise RuntimeError("A database write job cannot enqueue another write")
        future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("Database writer is closed")
            self._jobs.put((transaction, future))
        return future

    def close(self):
        if threading.current_thread() is self._thread:
            raise RuntimeError("A database write job cannot close its writer")
        with self._lock:
            if not self._closed:
                self._closed = True
                self._jobs.put(None)
        self._thread.join()

    def _run(self):
        conn = None
        try:
            while (job := self._jobs.get()) is not None:
                transaction, future = job
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    if conn is None:
                        conn = sqlite3.connect(str(self.path), timeout=30)
                        conn.execute("PRAGMA journal_mode=WAL")
                        conn.execute("PRAGMA synchronous=NORMAL")
                    with conn:
                        conn.execute("BEGIN IMMEDIATE")
                        result = transaction(conn)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
        finally:
            if conn is not None:
                conn.close()
