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
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import build_indexed_project


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


@contextmanager
def _engine(project_root):
    """Yield an ``EditorSearchEngine`` and close it on exit."""
    from emend.editor_search import EditorSearchEngine

    engine = EditorSearchEngine(str(project_root))
    try:
        yield engine
    finally:
        engine.close()


@pytest.fixture
def indexed_project(tmp_path):
    """Fixture providing a project with indexed symbols."""
    return build_indexed_project(tmp_path, {"sample.py": SAMPLE_SOURCE})


# ---------------------------------------------------------------------------
# FTS5 tests
# ---------------------------------------------------------------------------


class TestFTS5:
    def test_failed_rebuild_raises_clear_error(self, indexed_project, monkeypatch):
        """A rebuild failure must surface as an actionable RuntimeError naming
        the cache DB, not a raw sqlite3 traceback (and not silent degradation).
        """
        import emend.editor_search as es
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine(str(indexed_project))

        def boom(conn):
            raise sqlite3.OperationalError("database disk image is malformed")

        monkeypatch.setattr(es, "rebuild_fts", boom)
        with pytest.raises(RuntimeError, match=r"FTS index rebuild failed.*parse\.db"):
            engine._ensure_fts()

    def test_rebuild_fts_creates_table(self, indexed_project):
        from emend.editor_search import rebuild_fts

        db_path = indexed_project / ".emend" / "cache" / "parse.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")

        count = rebuild_fts(conn)
        assert count > 0

        # FTS tables should exist now
        fts_count = conn.execute(
            "SELECT COUNT(*) FROM symbol_fts"
        ).fetchone()[0]
        file_fts_count = conn.execute(
            "SELECT COUNT(*) FROM file_fts"
        ).fetchone()[0]
        assert fts_count + file_fts_count == count
        assert fts_count > 0
        assert file_fts_count > 0
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
        with _engine(indexed_project) as engine:
            result = engine.search_symbols("greet")
            names = [item["name"] for item in result.items]
            assert "greet" in names
            # Exact match should be first (highest score)
            assert result.items[0]["name"] == "greet"
            assert result.items[0]["score"] == 1000.0

    def test_prefix_match(self, indexed_project):
        with _engine(indexed_project) as engine:
            result = engine.search_symbols("parse")
            names = [item["name"] for item in result.items]
            assert "parse_pattern" in names
            assert "parse_extended_selector" in names

    def test_substring_match(self, indexed_project):
        with _engine(indexed_project) as engine:
            result = engine.search_symbols("loudly")
            names = [item["name"] for item in result.items]
            assert "greet_loudly" in names

    def test_case_insensitive(self, indexed_project):
        with _engine(indexed_project) as engine:
            result = engine.search_symbols("GREET")
            names = [item["name"] for item in result.items]
            assert "greet" in names or "Greeter" in names

    def test_kind_filter(self, indexed_project):
        with _engine(indexed_project) as engine:
            result = engine.search_symbols("greet", kind="class")
            names = [item["name"] for item in result.items]
            # 'greet' is a method, not a class — should not appear
            assert "greet" not in names

    def test_limit(self, indexed_project):
        with _engine(indexed_project) as engine:
            # "greet" matches greet, greet_loudly and Greeter (3 symbols).
            full = engine.search_symbols("greet", limit=100)
            assert len(full.items) > 2
            limited = engine.search_symbols("greet", limit=2)
            assert len(limited.items) == 2
            assert limited.truncated is True

    def test_dotted_query(self, indexed_project):
        with _engine(indexed_project) as engine:
            result = engine.search_symbols("Greeter.greet")
            # Should match via qualified_name search
            assert len(result.items) >= 1

    def test_returns_score_field(self, indexed_project):
        with _engine(indexed_project) as engine:
            result = engine.search_symbols("parse_pattern")
            assert len(result.items) >= 1
            assert "score" in result.items[0]
            assert result.items[0]["score"] > 0

    def test_elapsed_ms(self, indexed_project):
        with _engine(indexed_project) as engine:
            result = engine.search_symbols("greet")
            assert result.elapsed_ms >= 0


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
        with _engine(indexed_project) as engine:
            result = engine.resolve_selector("sample.py::Greeter")
            names = [item["name"] for item in result.items]
            assert "Greeter" in names
            assert result.mode == "selector"

    def test_partial_symbol_prefix(self, indexed_project):
        with _engine(indexed_project) as engine:
            result = engine.resolve_selector("sample.py::pars")
            names = [item["name"] for item in result.items]
            assert "parse_pattern" in names
            assert "parse_extended_selector" in names

    def test_glob_file_pattern(self, indexed_project):
        with _engine(indexed_project) as engine:
            result = engine.resolve_selector("*.py::Greeter")
            names = [item["name"] for item in result.items]
            assert "Greeter" in names

    def test_dotted_path(self, indexed_project):
        with _engine(indexed_project) as engine:
            result = engine.resolve_selector("sample.py::Greeter.greet")
            assert len(result.items) >= 1

    def test_bare_name_falls_back_to_symbol_search(self, indexed_project):
        with _engine(indexed_project) as engine:
            result = engine.resolve_selector("Greeter")
            assert result.mode == "symbol"
            names = [item["name"] for item in result.items]
            assert "Greeter" in names


# ---------------------------------------------------------------------------
# Unified search (auto-detect mode)
# ---------------------------------------------------------------------------


class TestUnifiedSearch:
    def test_symbol_mode(self, indexed_project):
        with _engine(indexed_project) as engine:
            result = engine.search("greet")
            assert result.mode == "symbol"
            assert len(result.items) >= 1

    def test_selector_mode(self, indexed_project):
        with _engine(indexed_project) as engine:
            result = engine.search("sample.py::Greeter")
            assert result.mode == "selector"

    def test_query_field_populated(self, indexed_project):
        with _engine(indexed_project) as engine:
            result = engine.search("greet")
            assert result.query == "greet"


# ---------------------------------------------------------------------------
# Reference search
# ---------------------------------------------------------------------------


class TestReferenceSearch:
    def test_find_references(self, indexed_project):
        with _engine(indexed_project) as engine:
            conn = engine._get_conn()
            conn.execute(
                "INSERT INTO reference_index "
                "(content_hash, target_qn, file_path, line, col, ref_kind) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (b"h", "sample.greet", "sample.py", 6, 15, "call"),
            )
            conn.commit()

            result = engine.search_references("sample.greet")
            assert result.mode == "reference"
            assert result.items == [{
                "target_qn": "sample.greet",
                "file_path": "sample.py",
                "line": 6,
                "col": 15,
                "ref_kind": "call",
            }]

    def test_ref_kind_filter(self, indexed_project):
        with _engine(indexed_project) as engine:
            # Seed known references so the filter has something to act on:
            # two "call" refs and one "read" ref for the same target.
            conn = engine._get_conn()
            for ref_kind, line in [("call", 10), ("read", 11), ("call", 12)]:
                conn.execute(
                    "INSERT INTO reference_index "
                    "(content_hash, target_qn, file_path, line, col, ref_kind) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (b"h", "sample.greet", "f.py", line, 0, ref_kind),
                )
            conn.commit()

            result = engine.search_references(
                "sample.greet", ref_kind="call"
            )
            # The read ref is filtered out; only the two call refs remain.
            assert len(result.items) == 2
            for item in result.items:
                assert item["ref_kind"] == "call"


# ---------------------------------------------------------------------------
# File symbols (outline)
# ---------------------------------------------------------------------------


class TestFileSymbols:
    def test_file_outline(self, indexed_project):

        py_file = str((indexed_project / "sample.py").resolve())
        with _engine(indexed_project) as engine:
            result = engine.file_symbols(py_file)
            assert result.mode == "file_symbols"
            names = [item["name"] for item in result.items]
            assert "Greeter" in names
            assert "parse_pattern" in names
            assert "fetch_data" in names

    def test_file_outline_ordered_by_line(self, indexed_project):

        py_file = str((indexed_project / "sample.py").resolve())
        with _engine(indexed_project) as engine:
            result = engine.file_symbols(py_file)
            lines = [item["line"] for item in result.items]
            assert lines == sorted(lines)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status(self, indexed_project):
        with _engine(indexed_project) as engine:
            result = engine.status()
            assert result.mode == "status"
            info = result.items[0]
            assert info["available"] is True
            assert info["symbol_count"] > 0


# ---------------------------------------------------------------------------
# JSON-RPC server protocol
# ---------------------------------------------------------------------------


class TestServerProtocol:
    def test_dispatch_search(self, indexed_project):
        from emend.editor_search import EditorSearchEngine, _dispatch

        with _engine(indexed_project) as engine:
            result = _dispatch(engine, "search", {"query": "greet"})
            assert "items" in result
            assert "elapsed_ms" in result
            assert result["mode"] == "symbol"

    def test_dispatch_symbols(self, indexed_project):
        from emend.editor_search import EditorSearchEngine, _dispatch

        with _engine(indexed_project) as engine:
            result = _dispatch(engine, "symbols", {"query": "parse", "limit": 5})
            assert len(result["items"]) <= 5

    def test_dispatch_file_symbols(self, indexed_project):
        from emend.editor_search import EditorSearchEngine, _dispatch

        py_file = str((indexed_project / "sample.py").resolve())
        with _engine(indexed_project) as engine:
            result = _dispatch(engine, "file_symbols", {"file": py_file})
            assert len(result["items"]) > 0

    def test_dispatch_status(self, indexed_project):
        from emend.editor_search import EditorSearchEngine, _dispatch

        with _engine(indexed_project) as engine:
            result = _dispatch(engine, "status", {})
            assert result["items"][0]["available"] is True

    # "shutdown" is handled by run_editor_server before _dispatch, so both it
    # and a bogus method reach _dispatch only as unknown methods.
    @pytest.mark.parametrize("method", ["shutdown", "nonexistent"])
    def test_dispatch_unknown_method(self, indexed_project, method):
        from emend.editor_search import EditorSearchEngine, _dispatch

        with _engine(indexed_project) as engine:
            with pytest.raises(ValueError, match="Unknown method"):
                _dispatch(engine, method, {})

    def test_result_serializable(self, indexed_project):
        """Verify that dispatch results can be JSON-serialized."""
        from emend.editor_search import EditorSearchEngine, _dispatch

        with _engine(indexed_project) as engine:
            result = _dispatch(engine, "search", {"query": "greet"})
            serialized = json.dumps(result, default=str)
            parsed = json.loads(serialized)
            assert parsed["mode"] == "symbol"


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


# ---------------------------------------------------------------------------
# Pattern search with index prefilter
# ---------------------------------------------------------------------------


MULTI_FILE_A = '''\
def hello():
    print("hello world")

def goodbye():
    print("goodbye world")
'''

MULTI_FILE_B = '''\
import math

def compute(x):
    return math.sqrt(x)

class Calculator:
    def add(self, a, b):
        return a + b
'''

MULTI_FILE_C = '''\
def unrelated():
    return 42

class Empty:
    pass
'''


@pytest.fixture
def multi_file_project(tmp_path):
    return build_indexed_project(tmp_path, {
        "a.py": MULTI_FILE_A,
        "b.py": MULTI_FILE_B,
        "c.py": MULTI_FILE_C,
    })


class TestPatternPrefilter:
    def test_index_prefilter_narrows_scope(self, multi_file_project):
        """Index prefilter should return only files containing the literal."""
        from emend.transform import _index_prefilter

        db_path = multi_file_project / ".emend" / "cache" / "parse.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            # "sqrt" only appears in b.py
            candidates = _index_prefilter(["sqrt"], conn)
            assert candidates is not None
            assert any("b.py" in f for f in candidates)
        finally:
            conn.close()

    def test_index_prefilter_intersection(self, multi_file_project):
        """Multiple literals should intersect — only files with ALL literals."""
        from emend.transform import _index_prefilter

        db_path = multi_file_project / ".emend" / "cache" / "parse.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            # "math" and "sqrt" both in b.py only
            candidates = _index_prefilter(["math", "sqrt"], conn)
            assert candidates is not None
            assert {Path(f).name for f in candidates} == {"b.py"}
        finally:
            conn.close()

    def test_index_prefilter_unknown_literal_returns_none(self, multi_file_project):
        """Unknown literal not in the index should return None (no useful data)."""
        from emend.transform import _index_prefilter

        db_path = multi_file_project / ".emend" / "cache" / "parse.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            candidates = _index_prefilter(["xyzzy_nonexistent"], conn)
            # Should return None — index had nothing useful
            assert candidates is None
        finally:
            conn.close()

    def test_pattern_search_single_file(self, multi_file_project):
        """Pattern search on a single file should work without prefilter."""
        with _engine(multi_file_project) as engine:
            result = engine.search_pattern(
                "print($X)",
                file_scope=str(multi_file_project / "a.py"),
            )
            assert result.mode == "pattern"
            assert len(result.items) >= 2  # two print() calls in a.py
            for item in result.items:
                assert "a.py" in item["file_path"]

    def test_pattern_search_multi_file(self, multi_file_project):
        """Pattern search across directory should use prefilter and find matches."""
        with _engine(multi_file_project) as engine:
            result = engine.search_pattern(
                "print($X)",
                file_scope=str(multi_file_project),
            )
            assert result.mode == "pattern"
            assert len(result.items) >= 2

    def test_pattern_search_respects_limit(self, multi_file_project):
        """Pattern search should stop after limit matches."""
        with _engine(multi_file_project) as engine:
            result = engine.search_pattern(
                "print($X)",
                file_scope=str(multi_file_project),
                limit=1,
            )
            assert len(result.items) == 1
            assert result.truncated is True

    def test_pattern_search_no_matches(self, multi_file_project):
        """Pattern for something not in any file should return empty."""
        with _engine(multi_file_project) as engine:
            result = engine.search_pattern(
                "nonexistent_func($X)",
                file_scope=str(multi_file_project),
            )
            assert result.items == []


class TestGrepSearch:
    """Regex (``/pattern/``) grep search via rg / grep."""

    def test_grep_search_single_file_scope(self, multi_file_project):
        """Regex search scoped to a single file must return matches.

        Regression: when rg searches exactly one file it omits the
        filename prefix, so the parser (which expected
        ``file:line:text``) silently dropped every match.
        """
        with _engine(multi_file_project) as engine:
            result = engine._search_grep(
                "print",
                file_scope=str(multi_file_project / "a.py"),
            )
            assert result.mode == "grep"
            assert len(result.items) >= 2  # two print() calls in a.py
            for item in result.items:
                assert "a.py" in item["file_path"]

    def test_grep_search_multi_file_scope(self, multi_file_project):
        """Regex search across the project should still find matches."""
        with _engine(multi_file_project) as engine:
            result = engine._search_grep(
                "print",
                file_scope=str(multi_file_project),
            )
            assert result.mode == "grep"
            assert len(result.items) >= 2

    def test_grep_search_single_file_scope_grep_fallback(
        self, multi_file_project, monkeypatch
    ):
        """The plain-grep fallback (no rg installed) omits the filename
        prefix for a single file operand just like rg did, and must also
        return matches (this is the code path CI takes)."""
        import shutil as _shutil

        real_which = _shutil.which

        def _no_rg(cmd, *args, **kwargs):
            if cmd == "rg":
                return None
            return real_which(cmd, *args, **kwargs)

        monkeypatch.setattr("emend.editor_search.shutil.which", _no_rg)

        with _engine(multi_file_project) as engine:
            result = engine._search_grep(
                "print",
                file_scope=str(multi_file_project / "a.py"),
            )
            assert result.mode == "grep"
            assert len(result.items) >= 2  # two print() calls in a.py
            for item in result.items:
                assert "a.py" in item["file_path"]

    def test_grep_search_single_file_via_search(self, multi_file_project):
        """The ``/regex/`` dispatch path must work with a single-file scope."""
        with _engine(multi_file_project) as engine:
            result = engine.search(
                "/print/",
                file_scope=str(multi_file_project / "a.py"),
            )
            assert result.mode == "grep"
            assert len(result.items) >= 2
            for item in result.items:
                assert "a.py" in item["file_path"]


# ---------------------------------------------------------------------------
# Bug fixes: wrong arguments and method names
# ---------------------------------------------------------------------------


class TestEditorSearchBugFixes:
    def test_complete_via_mapping_passes_filepath_not_content(self, tmp_path):
        """_complete_via_mapping must pass a file path (not content)
        to find_nested_definitions."""
        from unittest.mock import patch, MagicMock

        src = tmp_path / "mod.py"
        src.write_text("class Foo:\n    def bar(self): pass\n")

        with _engine(tmp_path) as engine:
            with patch("emend.ast_utils.find_nested_definitions") as mock_fnd:
                mock_fnd.return_value = []
                mock_store = MagicMock()
                mock_store.resolve_selector.return_value = f"{src}::Foo"
                with patch("emend.knowledge.MappingStore", return_value=mock_store):
                    engine._complete_via_mapping(
                        "Foo", member_prefix="", limit=10, seen=set(),
                    )
                assert mock_fnd.called, (
                    "find_nested_definitions was never called — the mapping "
                    "resolution path was not exercised"
                )
                arg = mock_fnd.call_args[0][0]
                assert not arg.startswith("class "), (
                    "find_nested_definitions was called with file content "
                    "instead of a file path"
                )

    def test_types_at_cursor_calls_infer_file_not_get_file_types(self, tmp_path):
        """types_at_cursor must call oracle.infer_file(), not the
        non-existent oracle.get_file_types()."""
        from unittest.mock import patch, MagicMock

        src = tmp_path / "app.py"
        src.write_text("x: int = 1\n")

        with _engine(tmp_path) as engine:
            mock_oracle = MagicMock()
            mock_oracle.infer_file.return_value = None
            with patch(
                "emend.type_oracle.create_type_oracle", return_value=mock_oracle
            ):
                result = engine.types_at_cursor(str(src), line=1, col=0)
            mock_oracle.infer_file.assert_called_once()

    def test_complete_via_mapping_handles_nested_symbol_dataclass(self, tmp_path):
        """_complete_via_mapping must use attribute access on NestedSymbol, not .get()."""
        from unittest.mock import patch, MagicMock
        from emend.component_selector import NestedSymbol

        src = tmp_path / "mod.py"
        src.write_text("class Foo:\n    def bar(self): pass\n")

        with _engine(tmp_path) as engine:
            symbols = [
                NestedSymbol(
                    name="bar", kind="method",
                    line_start=2, line_end=2, col_offset=4,
                    path=["Foo", "bar"],
                ),
            ]
            with patch("emend.ast_utils.find_nested_definitions", return_value=symbols):
                mock_store = MagicMock()
                mock_store.resolve_selector.return_value = f"{src}::Foo"
                with patch("emend.knowledge.MappingStore", return_value=mock_store):
                    items = engine._complete_via_mapping(
                        "Foo", member_prefix="", limit=10, seen=set(),
                    )
            assert len(items) >= 1
            assert items[0]["word"] == "bar"

    def test_class_member_name_uses_tree_sitter(self):
        """_class_member_name extracts member names via tree-sitter PyNode."""
        import emend.emend_core as _ec
        from emend.editor_search import EditorSearchEngine

        engine = EditorSearchEngine.__new__(EditorSearchEngine)

        # Sync function definition
        ts_tree = _ec.parse_source("class C:\n    def foo(self): pass\n", "py")
        class_def = ts_tree.root.named_children()[0]
        body = class_def.child_by_field_name("body")
        func_node = body.named_children()[0]
        assert engine._class_member_name(func_node) == "foo"

        # Async function definition
        ts_tree2 = _ec.parse_source("class C:\n    async def bar(self): pass\n", "py")
        class_def2 = ts_tree2.root.named_children()[0]
        body2 = class_def2.child_by_field_name("body")
        async_node = body2.named_children()[0]
        assert engine._class_member_name(async_node) == "bar"


class TestEditorSearchGetKbBug:
    def test_search_include_map_does_not_raise_name_error(self, tmp_path):
        """search(include_map=True) must not raise NameError for _get_kb.

        Bug: editor_search.py line 656 calls _get_kb(self) but the function
        is named _get_store. This causes a NameError at runtime.
        """

        src = tmp_path / "mod.py"
        src.write_text("def greet():\n    pass\n")

        with _engine(tmp_path) as engine:
            result = engine.search("greet", include_map=True)
            assert result is not None


class TestEditorSearchCLIMode:
    def test_mode_symbol_singular_accepted(self, tmp_path):
        """--mode symbol (singular) should be accepted without error."""
        from typer.testing import CliRunner
        from emend.cli import app

        src = tmp_path / "mod.py"
        src.write_text("def greet():\n    pass\n")

        runner = CliRunner()
        result = runner.invoke(app, [
            "editor-search", "greet", str(tmp_path),
            "--mode", "symbol",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["mode"] == "symbol"


# ---------------------------------------------------------------------------
# Bug: _extract_import_names mis-binds plain dotted imports (import a.b.c)
# ---------------------------------------------------------------------------


class TestExtractImportNames:
    """``import a.b.c`` binds the top-level package name ``a`` locally — not
    the last component.  Mapping the last component (``c``) is wrong and lets
    a submodule leaf shadow a real local name."""

    def test_plain_dotted_import_binds_top_level(self, tmp_path):
        from emend.editor_search import EditorSearchEngine

        proj = build_indexed_project(tmp_path, {"sample.py": SAMPLE_SOURCE})
        f = proj / "uses.py"
        f.write_text("import os.path\n")

        engine = EditorSearchEngine(str(proj))
        names = engine._extract_import_names(str(f))

        assert "os" in names
        assert "path" not in names

    def test_aliased_import_still_binds_alias(self, tmp_path):
        from emend.editor_search import EditorSearchEngine

        proj = build_indexed_project(tmp_path, {"sample.py": SAMPLE_SOURCE})
        f = proj / "uses.py"
        f.write_text("import a.b as c\n")

        engine = EditorSearchEngine(str(proj))
        names = engine._extract_import_names(str(f))

        assert "c" in names
        assert names["c"] == "a.b"


# ---------------------------------------------------------------------------
# Bug: _search_literals leaks SQL LIKE wildcards for identifiers with '_'
# ---------------------------------------------------------------------------


class TestSearchLiteralsWildcards:
    """The literal is interpolated into a ``LIKE`` pattern; ``_`` must be
    treated as a literal underscore, not a single-character wildcard."""

    def test_underscore_not_treated_as_wildcard(self, tmp_path):
        from emend.editor_search import EditorSearchEngine

        proj = build_indexed_project(tmp_path, {"sample.py": SAMPLE_SOURCE})
        engine = EditorSearchEngine(str(proj))
        conn = engine._get_conn()
        conn.execute(
            "INSERT INTO reference_index "
            "(content_hash, target_qn, file_path, line, col, ref_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (b"h", "abc", "f.py", 1, 0, "read"),
        )
        conn.execute(
            "INSERT INTO reference_index "
            "(content_hash, target_qn, file_path, line, col, ref_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (b"h", "a_c", "f.py", 2, 0, "read"),
        )
        conn.commit()

        result = engine._search_literals(["a_c"])
        qns = {item["target_qn"] for item in result.items}
        assert "a_c" in qns
        assert "abc" not in qns
