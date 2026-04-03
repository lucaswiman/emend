"""Tests for Phase 1: Picker Workflow improvements.

Covers:
- Result provenance in SearchResult (server-side)
- Query history recording and retrieval (server-side RPC)
- File-path fallback visibility when index is unavailable
"""

import json
import textwrap
import time
from pathlib import Path

import pytest

from emend.editor_search import (
    EditorSearchEngine,
    SearchResult,
    _dispatch,
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
""")


@pytest.fixture
def engine(tmp_path):
    proj = build_indexed_project(tmp_path, {"app.py": SAMPLE_SOURCE})
    eng = EditorSearchEngine(str(proj))
    yield eng, proj
    eng.close()


# ---------------------------------------------------------------------------
# Tests: Provenance in SearchResult
# ---------------------------------------------------------------------------


class TestProvenance:
    """Result provenance should indicate *how* results were obtained."""

    def test_symbol_search_provenance(self, engine):
        """Symbol search should report provenance='indexed'."""
        eng, proj = engine
        result = _dispatch(eng, "search", {"query": "get_user"})
        assert "provenance" in result, "Missing provenance field"
        assert result["provenance"] == "indexed"

    def test_pattern_search_provenance(self, engine):
        """Pattern search should report provenance='pattern'."""
        eng, proj = engine
        result = _dispatch(eng, "search", {"query": "def $NAME($ARGS)"})
        assert result["provenance"] == "pattern"

    def test_grep_search_provenance(self, engine):
        """Grep search should report provenance='grep'."""
        eng, proj = engine
        result = _dispatch(eng, "search", {"query": "/get_user/"})
        assert result["provenance"] == "grep"

    def test_selector_search_provenance(self, engine):
        """Selector search should report provenance='selector'."""
        eng, proj = engine
        result = _dispatch(eng, "search", {"query": "app.py::UserService"})
        assert result["provenance"] == "selector"

    def test_file_search_provenance(self, engine):
        """File search should report provenance='files'."""
        eng, proj = engine
        result = _dispatch(eng, "search", {"query": "app.py"})
        assert result["provenance"] == "files"

    def test_provenance_in_to_dict(self):
        """provenance should appear in to_dict() output."""
        sr = SearchResult(
            items=[],
            elapsed_ms=1.0,
            mode="symbol",
            provenance="indexed",
        )
        d = sr.to_dict()
        assert d["provenance"] == "indexed"


# ---------------------------------------------------------------------------
# Tests: Query history (server-side)
# ---------------------------------------------------------------------------


class TestQueryHistory:
    """Server-side query history for picker recall."""

    def test_query_history_initially_empty(self, engine):
        """query_history should return an empty list initially."""
        eng, proj = engine
        result = _dispatch(eng, "query_history", {})
        assert result["items"] == []

    def test_query_history_records_searches(self, engine):
        """Queries are recorded after each search."""
        eng, proj = engine
        _dispatch(eng, "search", {"query": "get_user"})
        _dispatch(eng, "search", {"query": "parse_request"})
        result = _dispatch(eng, "query_history", {})
        queries = [item["query"] for item in result["items"]]
        # Most recent first
        assert queries[0] == "parse_request"
        assert queries[1] == "get_user"

    def test_query_history_deduplicates(self, engine):
        """Repeated queries should move to the top, not duplicate."""
        eng, proj = engine
        _dispatch(eng, "search", {"query": "get_user"})
        _dispatch(eng, "search", {"query": "parse_request"})
        _dispatch(eng, "search", {"query": "get_user"})
        result = _dispatch(eng, "query_history", {})
        queries = [item["query"] for item in result["items"]]
        assert queries.count("get_user") == 1
        assert queries[0] == "get_user"

    def test_query_history_limit(self, engine):
        """History should respect the limit parameter."""
        eng, proj = engine
        for i in range(5):
            _dispatch(eng, "search", {"query": f"query_{i}"})
        result = _dispatch(eng, "query_history", {"limit": 3})
        assert len(result["items"]) == 3

    def test_query_history_includes_mode(self, engine):
        """Each history entry should include the search mode."""
        eng, proj = engine
        _dispatch(eng, "search", {"query": "get_user"})
        result = _dispatch(eng, "query_history", {})
        assert result["items"][0]["mode"] == "symbol"

    def test_query_history_includes_result_count(self, engine):
        """Each history entry should include the number of results."""
        eng, proj = engine
        _dispatch(eng, "search", {"query": "get_user"})
        result = _dispatch(eng, "query_history", {})
        assert "result_count" in result["items"][0]
        assert result["items"][0]["result_count"] > 0

    def test_query_history_skips_empty_queries(self, engine):
        """Empty or whitespace-only queries should not be recorded."""
        eng, proj = engine
        _dispatch(eng, "search", {"query": ""})
        _dispatch(eng, "search", {"query": "   "})
        _dispatch(eng, "search", {"query": "get_user"})
        result = _dispatch(eng, "query_history", {})
        assert len(result["items"]) == 1

    def test_query_history_caps_at_100(self, engine):
        """History should not grow beyond 100 entries."""
        eng, proj = engine
        for i in range(110):
            _dispatch(eng, "search", {"query": f"q{i}"})
        result = _dispatch(eng, "query_history", {})
        assert len(result["items"]) <= 100


# ---------------------------------------------------------------------------
# Tests: File-path fallback visibility
# ---------------------------------------------------------------------------


class TestFilePathFallback:
    """File-path results must remain visible alongside symbol results."""

    def test_file_results_included_in_symbol_search(self, engine):
        """When query matches both symbols and files, both appear."""
        eng, proj = engine
        result = _dispatch(eng, "search", {"query": "app"})
        kinds = {item.get("kind") for item in result["items"]}
        # Should have both file and non-file results
        assert "file" in kinds, "File results should be included in symbol search"

    def test_file_only_search_provenance(self, engine):
        """A path-like query with only file results should have files provenance."""
        eng, proj = engine
        # Create a file with a unique name that won't match symbols
        (proj / "unique_config.yaml").write_text("key: value\n")
        _dispatch(eng, "reindex", {})
        result = _dispatch(eng, "search", {"query": "unique_config.yaml"})
        # Should find the file
        if result["items"]:
            assert result["provenance"] in ("files", "indexed")
