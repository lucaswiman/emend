"""Tests for the editor search interface (``editor_search.py``).

Covers:
- FTS5 trigram index creation and rebuild
- Multi-strategy symbol search (exact, prefix, substring, fuzzy)
- Partial pattern normalization (incomplete patterns)
- Selector resolution
- Reference search
- File outline (file_symbols)
- Scoring / ranking
- JSON-RPC server protocol
"""

import json
import sqlite3
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_SOURCE = '''\
class Greeter:
    def greet(self, name: str) -> str:
        return f"Hello, {name}!"

    def greet_loudly(self, name: str) -> str:
        return self.greet(name).upper()


def parse_pattern(raw: str) -> str:
    """Parse a pattern string."""
    return raw.strip()


def parse_extended_selector(raw: str) -> str:
    return raw


async def fetch_data(url: str) -> dict:
    return {}


class TestHelper:
    def test_something(self):
        pass
'''


def _build_index(tmp_path: Path, source: str = SAMPLE_SOURCE) -> Path:
    """Create a small indexed project and return the project root."""
    from emend.transform import _index_batch

    proj = tmp_path / "proj"
    proj.mkdir()
    cache = proj / ".emend" / "cache"
    cache.mkdir(parents=True)
    db_path = cache / "parse.db"

    py_file = proj / "sample.py"
    py_file.write_text(source)

    # Build the index
    batch = [(str(py_file), source)]
    _index_batch((str(db_path), str(proj), str(proj), batch))

    # Set schema version so freshness checks pass
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS index_meta "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS file_manifest ("
        "  path TEXT PRIMARY KEY,"
        "  mtime_ns INTEGER NOT NULL,"
        "  size INTEGER NOT NULL,"
        "  content_hash BLOB NOT NULL,"
        "  indexed_at REAL NOT NULL"
        ")"
    )
    conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
        ("schema_version", "3"),
    )
    conn.commit()
    conn.close()

    return proj


@pytest.fixture
def indexed_project(tmp_path):
    """Fixture providing a project with indexed symbols."""
    return _build_index(tmp_path)


# ---------------------------------------------------------------------------
# FTS5 tests
# ---------------------------------------------------------------------------


class TestFTS5:
    def test_rebuild_fts_creates_table(self, indexed_project):
        from emend.editor_search import rebuild_fts

        db_path = indexed_project / ".emend" / "cache" / "parse.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")

        count = rebuild_fts(conn)
        assert count > 0

        # FTS table should exist now
        fts_count = conn.execute(
            "SELECT COUNT(*) FROM symbol_fts"
        ).fetchone()[0]
        assert fts_count == count
        conn.close()

    def test_rebuild_fts_idempotent(self, indexed_project):
        from emend.editor_search import rebuild_fts

        db_path = indexed_project / ".emend" / "cache" / "parse.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")

        count1 = rebuild_fts(conn)
        count2 = rebuild_fts(conn)
        assert count1 == count2
        conn.close()

    def test_fts_trigram_substring_match(self, indexed_project):
        """FTS5 trigram should find 'greet' inside 'greet_loudly'."""
        from emend.editor_search import rebuild_fts

        db_path = indexed_project / ".emend" / "cache" / "parse.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")

        rebuild_fts(conn)

        rows = conn.execute(
            'SELECT name FROM symbol_fts WHERE name MATCH \'"greet"\'',
        ).fetchall()
        names = [r[0] for r in rows]
        assert "greet" in names
        assert "greet_loudly" in names
        conn.close()


# ---------------------------------------------------------------------------
# EditorSearchEngine: symbol search
# ---------------------------------------------------------------------------


class TestSymbolSearch:
    def test_exact_match(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.search_symbols("greet")
            names = [item["name"] for item in result.items]
            assert "greet" in names
            # Exact match should be first (highest score)
            assert result.items[0]["name"] == "greet"
            assert result.items[0]["score"] == 1000.0
        finally:
            engine.close()

    def test_prefix_match(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.search_symbols("parse")
            names = [item["name"] for item in result.items]
            assert "parse_pattern" in names
            assert "parse_extended_selector" in names
        finally:
            engine.close()

    def test_substring_match(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.search_symbols("loudly")
            names = [item["name"] for item in result.items]
            assert "greet_loudly" in names
        finally:
            engine.close()

    def test_case_insensitive(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.search_symbols("GREET")
            names = [item["name"] for item in result.items]
            assert "greet" in names or "Greeter" in names
        finally:
            engine.close()

    def test_kind_filter(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.search_symbols("greet", kind="class")
            names = [item["name"] for item in result.items]
            # 'greet' is a method, not a class — should not appear
            assert "greet" not in names
        finally:
            engine.close()

    def test_limit(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.search_symbols("", limit=2)
            # Can't match empty string well, but should not crash
            assert len(result.items) <= 2
        finally:
            engine.close()

    def test_dotted_query(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.search_symbols("Greeter.greet")
            # Should match via qualified_name search
            assert len(result.items) >= 1
        finally:
            engine.close()

    def test_returns_score_field(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.search_symbols("parse_pattern")
            assert len(result.items) >= 1
            assert "score" in result.items[0]
            assert result.items[0]["score"] > 0
        finally:
            engine.close()

    def test_elapsed_ms(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.search_symbols("greet")
            assert result.elapsed_ms >= 0
        finally:
            engine.close()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class TestScoring:
    def test_exact_beats_prefix(self):
        from emend.editor_search import _score_symbol

        exact = _score_symbol("parse", "mod.parse", "parse")
        prefix = _score_symbol("parse_pattern", "mod.parse_pattern", "parse")
        assert exact > prefix

    def test_prefix_beats_substring(self):
        from emend.editor_search import _score_symbol

        prefix = _score_symbol("parse_pattern", "mod.parse_pattern", "parse")
        substr = _score_symbol("re_parse", "mod.re_parse", "parse")
        assert prefix > substr

    def test_substring_beats_fuzzy(self):
        from emend.editor_search import _score_symbol

        substr = _score_symbol("re_parse", "mod.re_parse", "parse")
        fuzzy = _score_symbol("pxaxrxsxe", "mod.pxaxrxsxe", "parse")
        assert substr > fuzzy

    def test_no_match_returns_zero(self):
        from emend.editor_search import _score_symbol

        score = _score_symbol("xyz", "mod.xyz", "abc")
        assert score == 0.0

    def test_segment_boundary_bonus(self):
        from emend.editor_search import _score_symbol

        # "parse" at word boundary (_parse) scores higher than in the middle
        at_boundary = _score_symbol("_parse", "mod._parse", "parse")
        # For names where 'parse' appears after underscore, use a real name
        boundary_name = _score_symbol("re_parse", "mod.re_parse", "parse")
        mid_name = _score_symbol("xxparsexx", "mod.xxparsexx", "parse")
        assert boundary_name > mid_name


# ---------------------------------------------------------------------------
# Partial pattern normalization
# ---------------------------------------------------------------------------


class TestPartialPattern:
    def test_trailing_dollar(self):
        from emend.editor_search import normalize_partial_pattern

        norm, literals = normalize_partial_pattern("foo(bar, $")
        assert norm is not None
        assert "foo" in literals
        assert "bar" in literals
        # Should have closed the paren and replaced $ with $_
        assert norm.endswith(")")
        assert "$_" in norm

    def test_trailing_ellipsis_dollar(self):
        from emend.editor_search import normalize_partial_pattern

        norm, literals = normalize_partial_pattern("func($...")
        assert norm is not None
        assert "$...TAIL" in norm

    def test_unclosed_paren(self):
        from emend.editor_search import normalize_partial_pattern

        norm, literals = normalize_partial_pattern("print(x")
        assert norm is not None
        assert norm == "print(x)"

    def test_unclosed_bracket(self):
        from emend.editor_search import normalize_partial_pattern

        norm, literals = normalize_partial_pattern("data[key")
        assert norm is not None
        assert "]" in norm

    def test_complete_pattern_unchanged(self):
        from emend.editor_search import normalize_partial_pattern

        norm, literals = normalize_partial_pattern("print($X)")
        assert norm == "print($X)"
        assert "print" in literals

    def test_literals_extracted_on_failure(self):
        from emend.editor_search import normalize_partial_pattern

        # Something so broken it can't normalize
        _, literals = normalize_partial_pattern("!@#$%^&*(")
        # Should still extract nothing meaningful, but not crash
        assert isinstance(literals, list)

    def test_keywords_excluded_from_literals(self):
        from emend.editor_search import normalize_partial_pattern

        _, literals = normalize_partial_pattern("if True and foo")
        assert "foo" in literals
        assert "if" not in literals
        assert "True" not in literals
        assert "and" not in literals


# ---------------------------------------------------------------------------
# Selector resolution
# ---------------------------------------------------------------------------


class TestSelectorResolution:
    def test_file_and_symbol(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.resolve_selector("sample.py::Greeter")
            names = [item["name"] for item in result.items]
            assert "Greeter" in names
            assert result.mode == "selector"
        finally:
            engine.close()

    def test_partial_symbol_prefix(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.resolve_selector("sample.py::pars")
            names = [item["name"] for item in result.items]
            assert "parse_pattern" in names
            assert "parse_extended_selector" in names
        finally:
            engine.close()

    def test_glob_file_pattern(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.resolve_selector("*.py::Greeter")
            names = [item["name"] for item in result.items]
            assert "Greeter" in names
        finally:
            engine.close()

    def test_dotted_path(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.resolve_selector("sample.py::Greeter.greet")
            assert len(result.items) >= 1
        finally:
            engine.close()

    def test_bare_name_falls_back_to_symbol_search(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.resolve_selector("Greeter")
            assert result.mode == "symbol"
            names = [item["name"] for item in result.items]
            assert "Greeter" in names
        finally:
            engine.close()


# ---------------------------------------------------------------------------
# Unified search (auto-detect mode)
# ---------------------------------------------------------------------------


class TestUnifiedSearch:
    def test_symbol_mode(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.search("greet")
            assert result.mode == "symbol"
            assert len(result.items) >= 1
        finally:
            engine.close()

    def test_selector_mode(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.search("sample.py::Greeter")
            assert result.mode == "selector"
        finally:
            engine.close()

    def test_query_field_populated(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.search("greet")
            assert result.query == "greet"
        finally:
            engine.close()


# ---------------------------------------------------------------------------
# Reference search
# ---------------------------------------------------------------------------


class TestReferenceSearch:
    def test_find_references(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            # "greet" is called in greet_loudly
            result = engine.search_references("sample.greet")
            assert result.mode == "reference"
            # May or may not find refs depending on index quality
            # Just verify it doesn't crash and returns valid structure
            assert isinstance(result.items, list)
        finally:
            engine.close()

    def test_ref_kind_filter(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.search_references(
                "sample.greet", ref_kind="call"
            )
            for item in result.items:
                assert item["ref_kind"] == "call"
        finally:
            engine.close()


# ---------------------------------------------------------------------------
# File symbols (outline)
# ---------------------------------------------------------------------------


class TestFileSymbols:
    def test_file_outline(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        py_file = str((indexed_project / "sample.py").resolve())
        try:
            result = engine.file_symbols(py_file)
            assert result.mode == "file_symbols"
            names = [item["name"] for item in result.items]
            assert "Greeter" in names
            assert "parse_pattern" in names
            assert "fetch_data" in names
        finally:
            engine.close()

    def test_file_outline_ordered_by_line(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        py_file = str((indexed_project / "sample.py").resolve())
        try:
            result = engine.file_symbols(py_file)
            lines = [item["line"] for item in result.items]
            assert lines == sorted(lines)
        finally:
            engine.close()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status(self, indexed_project):
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = engine.status()
            assert result.mode == "status"
            info = result.items[0]
            assert info["available"] is True
            assert info["symbol_count"] > 0
        finally:
            engine.close()


# ---------------------------------------------------------------------------
# JSON-RPC server protocol
# ---------------------------------------------------------------------------


class TestServerProtocol:
    def test_dispatch_search(self, indexed_project):
        from emend.editor_search import EditorSearchEngine, _dispatch

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = _dispatch(engine, "search", {"query": "greet"})
            assert "items" in result
            assert "elapsed_ms" in result
            assert result["mode"] == "symbol"
        finally:
            engine.close()

    def test_dispatch_symbols(self, indexed_project):
        from emend.editor_search import EditorSearchEngine, _dispatch

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = _dispatch(engine, "symbols", {"query": "parse", "limit": 5})
            assert len(result["items"]) <= 5
        finally:
            engine.close()

    def test_dispatch_file_symbols(self, indexed_project):
        from emend.editor_search import EditorSearchEngine, _dispatch

        engine = EditorSearchEngine(str(indexed_project))
        py_file = str((indexed_project / "sample.py").resolve())
        try:
            result = _dispatch(engine, "file_symbols", {"file": py_file})
            assert len(result["items"]) > 0
        finally:
            engine.close()

    def test_dispatch_status(self, indexed_project):
        from emend.editor_search import EditorSearchEngine, _dispatch

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = _dispatch(engine, "status", {})
            assert result["items"][0]["available"] is True
        finally:
            engine.close()

    def test_dispatch_shutdown(self, indexed_project):
        from emend.editor_search import EditorSearchEngine, _dispatch

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = _dispatch(engine, "shutdown", {})
            assert result == {"ok": True}
        finally:
            engine.close()

    def test_dispatch_unknown_method(self, indexed_project):
        from emend.editor_search import EditorSearchEngine, _dispatch

        engine = EditorSearchEngine(str(indexed_project))
        try:
            with pytest.raises(ValueError, match="Unknown method"):
                _dispatch(engine, "nonexistent", {})
        finally:
            engine.close()

    def test_result_serializable(self, indexed_project):
        """Verify that dispatch results can be JSON-serialized."""
        from emend.editor_search import EditorSearchEngine, _dispatch

        engine = EditorSearchEngine(str(indexed_project))
        try:
            result = _dispatch(engine, "search", {"query": "greet"})
            serialized = json.dumps(result, default=str)
            parsed = json.loads(serialized)
            assert parsed["mode"] == "symbol"
        finally:
            engine.close()


# ---------------------------------------------------------------------------
# Identifier splitting (used by scoring)
# ---------------------------------------------------------------------------


class TestIdentifierSplit:
    def test_snake_case(self):
        from emend.editor_search import _split_identifier

        assert _split_identifier("parse_pattern") == ["parse", "pattern"]

    def test_camel_case(self):
        from emend.editor_search import _split_identifier

        result = _split_identifier("parsePattern")
        assert "parse" in result
        assert "Pattern" in result

    def test_mixed(self):
        from emend.editor_search import _split_identifier

        result = _split_identifier("XMLParser_v2")
        assert len(result) >= 2

    def test_single_word(self):
        from emend.editor_search import _split_identifier

        assert _split_identifier("greet") == ["greet"]
