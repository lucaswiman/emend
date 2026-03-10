"""Tests for the JSON-RPC dispatch protocol used by the Vim plugin.

These tests verify the request/response contract that ``vim/autoload/emend.vim``
relies on, by calling ``_dispatch`` directly.  They complement the protocol
tests in ``test_editor_search.py::TestServerProtocol`` with Vim-specific
contract assertions (required fields, wire format, statelessness).
"""

import io
import json
import textwrap
from pathlib import Path

import pytest

from emend.editor_search import (
    EditorSearchEngine,
    _dispatch,
    _write_json,
)

from conftest import build_indexed_project


# ---------------------------------------------------------------------------
# Fixtures
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


@pytest.fixture
def engine(tmp_path):
    """Fixture: indexed project + EditorSearchEngine."""
    proj = build_indexed_project(tmp_path, {"app.py": SAMPLE_SOURCE})
    eng = EditorSearchEngine(str(proj))
    yield eng, proj
    eng.close()


# ---------------------------------------------------------------------------
# Tests: Vim UI field contract
# ---------------------------------------------------------------------------


class TestVimFieldContract:
    """Verify that dispatch results contain all fields the Vim UI reads."""

    def test_search_top_level_fields(self, engine):
        """The UI reads: items, mode, elapsed_ms, truncated, query."""
        eng, proj = engine
        result = _dispatch(eng, "search", {"query": "get_user"})
        for field in ("items", "mode", "elapsed_ms", "truncated", "query"):
            assert field in result, f"Missing top-level field: {field}"

    def test_search_item_fields(self, engine):
        """Each item must have: name, kind, file_path, line, end_line."""
        eng, proj = engine
        result = _dispatch(eng, "search", {"query": "UserService"})
        assert len(result["items"]) >= 1
        item = result["items"][0]
        for field in ("name", "kind", "file_path", "line", "end_line"):
            assert field in item, f"Missing item field: {field}"

    def test_selector_mode(self, engine):
        """Queries with :: should return mode='selector'."""
        eng, proj = engine
        result = _dispatch(eng, "search", {"query": "app.py::UserService"})
        assert result["mode"] == "selector"

    def test_file_symbols_mode(self, engine):
        eng, proj = engine
        app_path = str((proj / "app.py").resolve())
        result = _dispatch(eng, "file_symbols", {"file": app_path})
        assert result["mode"] == "file_symbols"
        names = [item["name"] for item in result["items"]]
        assert "UserService" in names
        assert "parse_request" in names


# ---------------------------------------------------------------------------
# Tests: Wire format (JSON serialization)
# ---------------------------------------------------------------------------


class TestJsonWire:
    """Verify JSON serialization for Vim's json_decode."""

    def test_result_json_serializable(self, engine):
        eng, proj = engine
        result = _dispatch(eng, "search", {"query": "get_user"})
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


# ---------------------------------------------------------------------------
# Tests: Statelessness and sequential requests
# ---------------------------------------------------------------------------


class TestStatelessness:
    def test_multiple_requests_independent(self, engine):
        """Each dispatch is independent (no state leakage between searches)."""
        eng, proj = engine
        r1 = _dispatch(eng, "search", {"query": "get_user"})
        r2 = _dispatch(eng, "search", {"query": "compute"})
        names1 = {item["name"] for item in r1["items"]}
        names2 = {item["name"] for item in r2["items"]}
        assert "get_user" in names1
        assert "compute_distance" in names2


# ---------------------------------------------------------------------------
# Tests: Search ranking (important for Vim UI usability)
# ---------------------------------------------------------------------------


class TestSearchRanking:
    def test_exact_match_ranked_first(self, engine):
        eng, proj = engine
        result = _dispatch(eng, "symbols", {"query": "get_user"})
        assert result["items"][0]["name"] == "get_user"

    def test_kind_filter(self, engine):
        eng, proj = engine
        result = _dispatch(eng, "symbols", {"query": "get_user", "kind": "class"})
        names = [item["name"] for item in result["items"]]
        assert "get_user" not in names


# ---------------------------------------------------------------------------
# Tests: Reindex (not covered by test_editor_search.py)
# ---------------------------------------------------------------------------


class TestReindex:
    def test_reindex(self, engine):
        eng, proj = engine
        result = _dispatch(eng, "reindex", {})
        assert "elapsed_ms" in result


# ---------------------------------------------------------------------------
# Tests: Status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_returns_counts(self, engine):
        eng, proj = engine
        result = _dispatch(eng, "status", {})
        info = result["items"][0]
        assert info["available"] is True
        assert info["symbol_count"] > 0


# ---------------------------------------------------------------------------
# Tests: Replace preview
# ---------------------------------------------------------------------------


class TestReplacePreview:
    def test_replace_preview_returns_diffs(self, engine):
        eng, proj = engine
        target = proj / "target.py"
        target.write_text("print('hello')\nprint('world')\n")
        _dispatch(eng, "reindex", {})
        result = _dispatch(eng, "replace_preview", {
            "pattern": "print($X)",
            "replacement": "log($X)",
            "file": str(target),
        })
        assert result["mode"] == "replace_preview"
        assert len(result["items"]) > 0
        assert "diff" in result["items"][0]
        assert "count" in result["items"][0]

    def test_replace_preview_no_matches(self, engine):
        eng, proj = engine
        result = _dispatch(eng, "replace_preview", {
            "pattern": "nonexistent_func($X)",
            "replacement": "other($X)",
            "file": str(proj / "app.py"),
        })
        assert result["items"] == []


# ---------------------------------------------------------------------------
# Tests: Move preview
# ---------------------------------------------------------------------------


class TestMovePreview:
    def test_move_preview_returns_diffs(self, engine):
        eng, proj = engine
        dest = proj / "dest.py"
        dest.write_text("")
        result = _dispatch(eng, "move_preview", {
            "qualified_name": "parse_request",
            "dest_file": str(dest),
            "file": str(proj / "app.py"),
        })
        assert result["mode"] == "move_preview"
        assert len(result["items"]) > 0
        assert "diff" in result["items"][0]


# ---------------------------------------------------------------------------
# Tests: Callers
# ---------------------------------------------------------------------------


class TestCallers:
    def test_callers_mode(self, engine):
        eng, proj = engine
        caller = proj / "caller.py"
        caller.write_text("from app import parse_request\n\ndef main():\n    parse_request('test')\n")
        _dispatch(eng, "reindex", {})
        result = _dispatch(eng, "callers", {
            "qualified_name": "parse_request",
            "file": str(proj / "app.py"),
        })
        assert result["mode"] == "callers"
        assert isinstance(result["items"], list)


# ---------------------------------------------------------------------------
# Tests: Callees
# ---------------------------------------------------------------------------


class TestCallees:
    def test_callees_mode(self, engine):
        eng, proj = engine
        result = _dispatch(eng, "callees", {
            "qualified_name": "parse_request",
            "file": str(proj / "app.py"),
        })
        assert result["mode"] == "callees"
        assert isinstance(result["items"], list)


# ---------------------------------------------------------------------------
# Tests: Types at cursor
# ---------------------------------------------------------------------------


class TestTypesAtCursor:
    def test_types_at_cursor_returns_result(self, engine):
        eng, proj = engine
        result = _dispatch(eng, "types_at_cursor", {
            "file": str(proj / "app.py"),
            "line": 2,
            "col": 0,
        })
        assert result["mode"] == "types"
        # May or may not have items depending on whether type engine is available
        assert isinstance(result["items"], list)
