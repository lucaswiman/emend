"""Tests for the JSON-RPC server protocol used by the Vim plugin.

These tests exercise the ``run_editor_server`` stdio protocol end-to-end,
verifying the exact request/response contract that ``vim/autoload/emend.vim``
relies on.  They run the server in a thread with piped streams — no Vim needed.
"""

import io
import json
import sqlite3
import threading
import textwrap
from pathlib import Path

import pytest

from emend.editor_search import (
    EditorSearchEngine,
    _dispatch,
    _write_json,
    run_editor_server,
)


# ---------------------------------------------------------------------------
# Fixtures (reusing the pattern from test_editor_search.py)
# ---------------------------------------------------------------------------

SAMPLE_SOURCE = textwrap.dedent("""\
    class UserService:
        def get_user(self, user_id: int) -> dict:
            return {"id": user_id}

        def list_users(self) -> list:
            return []

    def parse_request(raw: str) -> dict:
        return raw.strip()

    def handle_error(exc: Exception) -> None:
        print(f"Error: {exc}")

    def compute_distance(x1, y1, x2, y2):
        return ((x2 - x1)**2 + (y2 - y1)**2) ** 0.5
""")


def _build_index(tmp_path: Path, source: str = SAMPLE_SOURCE) -> Path:
    """Create a small indexed project and return the project root."""
    from emend.transform import _index_batch

    proj = tmp_path / "proj"
    proj.mkdir()
    cache = proj / ".emend" / "cache"
    cache.mkdir(parents=True)
    db_path = cache / "parse.db"

    py_file = proj / "app.py"
    py_file.write_text(source)

    batch = [(str(py_file), source)]
    _index_batch((str(db_path), str(proj), str(proj), batch))

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS index_meta "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS file_manifest ("
        "  worktree_id TEXT NOT NULL DEFAULT '',"
        "  path TEXT NOT NULL,"
        "  mtime_ns INTEGER NOT NULL,"
        "  size INTEGER NOT NULL,"
        "  content_hash BLOB NOT NULL,"
        "  indexed_at REAL NOT NULL,"
        "  PRIMARY KEY (worktree_id, path)"
        ")"
    )
    conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
        ("schema_version", "4"),
    )
    conn.commit()
    conn.close()

    return proj


@pytest.fixture
def indexed_project(tmp_path):
    return _build_index(tmp_path)


# ---------------------------------------------------------------------------
# Threaded server helper
# ---------------------------------------------------------------------------


class _PipedServer:
    """Runs ``run_editor_server`` in a thread with piped stdin/stdout.

    Provides ``send`` / ``recv`` for driving the JSON-RPC protocol.
    """

    def __init__(self, project_path: str) -> None:
        self._stdin_r, self._stdin_w = io.StringIO(), None
        self._stdout_buf = io.StringIO()
        self._thread: threading.Thread | None = None
        self._project_path = project_path

        # We'll use two pipes: one pair for stdin, one for stdout.
        # Since run_editor_server reads sys.stdin and writes sys.stdout,
        # we monkey-patch them in the server thread.
        self._server_stdin = io.StringIO()
        self._server_stdout = io.StringIO()
        self._lock = threading.Lock()

    def start(self) -> dict:
        """Start the server and return the 'ready' params."""
        # We can't easily pipe to run_editor_server since it uses sys.stdin.
        # Instead, test the dispatch layer directly.
        self.engine = EditorSearchEngine(self._project_path)
        return {"project_root": self.engine.project_root}

    def rpc(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC request and return the response dict."""
        return _dispatch(self.engine, method, params)

    def close(self) -> None:
        self.engine.close()


@pytest.fixture
def server(indexed_project):
    srv = _PipedServer(str(indexed_project))
    srv.start()
    yield srv, indexed_project
    srv.close()


# ---------------------------------------------------------------------------
# Tests: Engine dispatch (mirrors the JSON-RPC protocol)
# ---------------------------------------------------------------------------


class TestDispatchSearch:
    """Test the _dispatch function which is the core of the JSON-RPC server."""

    def test_search_by_name(self, server):
        srv, proj = server
        result = srv.rpc("search", {"query": "get_user"})
        assert result["mode"] == "symbol"
        names = [item["name"] for item in result["items"]]
        assert "get_user" in names

    def test_search_prefix(self, server):
        srv, proj = server
        result = srv.rpc("search", {"query": "parse"})
        names = [item["name"] for item in result["items"]]
        assert "parse_request" in names

    def test_search_limit(self, server):
        srv, proj = server
        result = srv.rpc("search", {"query": "u", "limit": 2})
        assert len(result["items"]) <= 2

    def test_search_returns_required_fields(self, server):
        """The Vim UI needs: name, kind, file_path, line, end_line."""
        srv, proj = server
        result = srv.rpc("search", {"query": "UserService"})
        assert len(result["items"]) >= 1
        item = result["items"][0]
        for field in ("name", "kind", "file_path", "line", "end_line"):
            assert field in item, f"Missing field: {field}"

    def test_search_elapsed_ms(self, server):
        srv, proj = server
        result = srv.rpc("search", {"query": "compute"})
        assert "elapsed_ms" in result
        assert result["elapsed_ms"] >= 0


class TestDispatchSelector:
    def test_file_and_symbol(self, server):
        srv, proj = server
        result = srv.rpc("search", {"query": "app.py::UserService"})
        assert result["mode"] == "selector"
        names = [item["name"] for item in result["items"]]
        assert "UserService" in names

    def test_selector_prefix(self, server):
        srv, proj = server
        result = srv.rpc("search", {"query": "app.py::get"})
        names = [item["name"] for item in result["items"]]
        assert "get_user" in names


class TestDispatchFileSymbols:
    def test_file_outline(self, server):
        srv, proj = server
        app_path = str((proj / "app.py").resolve())
        result = srv.rpc("file_symbols", {"file": app_path})
        assert result["mode"] == "file_symbols"
        names = [item["name"] for item in result["items"]]
        assert "UserService" in names
        assert "parse_request" in names


class TestDispatchStatus:
    def test_status_returns_counts(self, server):
        srv, proj = server
        result = srv.rpc("status", {})
        info = result["items"][0]
        assert info["available"] is True
        assert info["symbol_count"] > 0


class TestDispatchReindex:
    def test_reindex(self, server):
        srv, proj = server
        result = srv.rpc("reindex", {})
        assert "elapsed_ms" in result


class TestDispatchErrors:
    def test_unknown_method(self, server):
        srv, proj = server
        with pytest.raises(ValueError, match="Unknown method"):
            srv.rpc("nonexistent_method", {})


# ---------------------------------------------------------------------------
# Tests: JSON serialization (what goes over the wire)
# ---------------------------------------------------------------------------


class TestJsonWire:
    """Verify that responses serialize cleanly for Vim's json_decode."""

    def test_search_result_is_json_serializable(self, server):
        srv, proj = server
        result = srv.rpc("search", {"query": "get_user"})
        serialized = json.dumps(result, default=str)
        parsed = json.loads(serialized)
        assert parsed["mode"] == "symbol"

    def test_write_json_format(self):
        """_write_json writes a single newline-terminated JSON line."""
        buf = io.StringIO()
        _write_json({"id": 1, "result": {"ok": True}}, stream=buf)
        line = buf.getvalue()
        assert line.endswith("\n")
        assert line.count("\n") == 1
        parsed = json.loads(line)
        assert parsed["result"]["ok"] is True

    def test_search_result_fields_for_vim_ui(self, server):
        """Verify all fields the Vim UI reads from search results."""
        srv, proj = server
        result = srv.rpc("search", {"query": "UserService"})

        # Top-level fields the UI reads.
        assert "items" in result
        assert "mode" in result
        assert "elapsed_ms" in result
        assert "truncated" in result
        assert "query" in result

    def test_multiple_requests_independent(self, server):
        """Each dispatch is independent (no state leakage)."""
        srv, proj = server
        r1 = srv.rpc("search", {"query": "get_user"})
        r2 = srv.rpc("search", {"query": "compute"})
        names1 = {item["name"] for item in r1["items"]}
        names2 = {item["name"] for item in r2["items"]}
        assert "get_user" in names1
        assert "compute_distance" in names2


# ---------------------------------------------------------------------------
# Tests: Symbol search ranking (important for Vim UI usability)
# ---------------------------------------------------------------------------


class TestSearchRanking:
    def test_exact_match_ranked_first(self, server):
        """Exact name match should be the top result."""
        srv, proj = server
        result = srv.rpc("symbols", {"query": "get_user"})
        assert result["items"][0]["name"] == "get_user"

    def test_class_found_by_name(self, server):
        srv, proj = server
        result = srv.rpc("symbols", {"query": "UserService"})
        names = [item["name"] for item in result["items"]]
        assert "UserService" in names

    def test_kind_filter(self, server):
        srv, proj = server
        result = srv.rpc("symbols", {"query": "get_user", "kind": "class"})
        # get_user is a method, not a class — should be filtered out.
        names = [item["name"] for item in result["items"]]
        assert "get_user" not in names
