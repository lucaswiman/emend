"""Tests for ``emend index`` cache-hit behavior.

Verifies that a second call to ``warm_caches`` on an unchanged project
skips all files (cache hits) instead of re-parsing them.

Also tests the new index tables: symbol_index, import_graph,
reference_index, file_manifest, and the staleness detection logic.
"""
import shutil
import sqlite3
from pathlib import Path

import pytest


SOURCE = "def hello():\n    return 42\n"


def test_index_cli_reports_long_running_phases(monkeypatch, tmp_path):
    """The progress display must not go silent during post-parse phases."""
    from typer.testing import CliRunner
    from emend.cli import app

    (tmp_path / "a.py").write_text(SOURCE)

    def fake_warm_caches(path, *, jobs, callback, type_engine):
        callback("index", str(tmp_path / "a.py"))
        callback("phase", "Type analysis (pyrefly)")
        callback("phase", "Full-text search index")
        callback("phase", "Facts database")
        callback("phase", "Duplicate analysis")
        return {
            "files": 1, "indexed": 1, "qn_cached": 1, "skipped": 0,
            "sym_cached": 1, "ref_cached": 0, "type_cached": 1,
            "type_engine": "pyrefly",
        }

    monkeypatch.setattr("emend.cli_tooling.warm_caches", fake_warm_caches)
    result = CliRunner().invoke(
        app, ["tool", "index", str(tmp_path), "--type-engine", "pyrefly"]
    )

    assert result.exit_code == 0, result.output
    for label in (
        "Type analysis (pyrefly)",
        "Full-text search index",
        "Facts database",
        "Duplicate analysis",
    ):
        assert label in result.output


def test_index_cli_defaults_to_pyrefly(monkeypatch, tmp_path):
    """Repository config must not override the index command's default engine."""
    from typer.testing import CliRunner
    from emend.cli import app

    (tmp_path / "a.py").write_text(SOURCE)
    (tmp_path / "pyrightconfig.json").write_text("{}")
    selected = []

    def fake_warm_caches(path, *, jobs, callback, type_engine):
        selected.append(type_engine)
        return {
            "files": 1, "indexed": 1, "qn_cached": 1, "skipped": 0,
            "sym_cached": 1, "ref_cached": 0, "type_cached": 1,
            "type_engine": "pyrefly",
        }

    monkeypatch.setattr("emend.cli_tooling.warm_caches", fake_warm_caches)
    result = CliRunner().invoke(app, ["tool", "index", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert selected == ["pyrefly"]


def _db_row_count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


class TestIndexBatchCacheHit:
    """Unit tests for _index_batch cache-hit fast path."""

    def test_cold_cache_indexes_file(self, tmp_path):
        """First run writes parse, qn, symbol, import, and ref entries."""
        from emend.transform import _index_batch

        db_path = tmp_path / "parse.db"
        batch = [(str(tmp_path / "a.py"), SOURCE)]
        processed_n, qn_n, skipped, sym_n, import_n, ref_n, dsl_n = _index_batch(
            (str(db_path), str(tmp_path), str(tmp_path), batch)
        )

        assert processed_n == 1
        assert qn_n == 1
        assert skipped == 0
        # SOURCE has one function "hello" — should have at least 1 symbol
        assert sym_n >= 1

    def test_warm_cache_skips_file(self, tmp_path):
        """Second run with same content skips the file entirely."""
        from emend.transform import _index_batch

        db_path = tmp_path / "parse.db"
        batch = [(str(tmp_path / "a.py"), SOURCE)]

        # Cold run
        _index_batch((str(db_path), str(tmp_path), str(tmp_path), batch))

        # Warm run — must skip
        processed_n, qn_n, skipped, sym_n, import_n, ref_n, dsl_n = _index_batch(
            (str(db_path), str(tmp_path), str(tmp_path), batch)
        )
        assert processed_n == 0
        assert qn_n == 0
        assert sym_n == 0
        assert skipped == 1

    def test_warm_cache_no_extra_db_rows(self, tmp_path):
        """Warm run must not increase the row count in the DB."""
        from emend.transform import _index_batch

        db_path = tmp_path / "parse.db"
        batch = [(str(tmp_path / "a.py"), SOURCE)]

        _index_batch((str(db_path), str(tmp_path), str(tmp_path), batch))
        rows_after_cold = _db_row_count(db_path, "qn_index")

        _index_batch((str(db_path), str(tmp_path), str(tmp_path), batch))
        rows_after_warm = _db_row_count(db_path, "qn_index")

        assert rows_after_cold == rows_after_warm == 1

    def test_partial_cache_only_missing_part_indexed(self, tmp_path):
        """If qn_index is missing for a file, it is indexed on next run."""
        from emend.transform import _index_batch

        db_path = tmp_path / "parse.db"

        # Create DB with schema but no qn_index entries for this file
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE IF NOT EXISTS qn_index (hash BLOB PRIMARY KEY, qnames BLOB)")
        conn.commit()
        conn.close()

        batch = [(str(tmp_path / "a.py"), SOURCE)]
        processed_n, qn_n, skipped, sym_n, import_n, ref_n, dsl_n = _index_batch(
            (str(db_path), str(tmp_path), str(tmp_path), batch)
        )

        assert processed_n == 1  # file processed
        assert qn_n == 1    # was missing, now added
        assert skipped == 0  # not fully cached, so not counted as skipped


class TestWarmCachesSkipped:
    """Integration tests for warm_caches skipped-file tracking."""

    def _make_project(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "a.py").write_text(SOURCE)
        (proj / "b.py").write_text("x = 1\n")
        return proj

    def test_cold_run_no_skips(self, tmp_path):
        """Cold run reports zero skipped files."""
        from emend.transform import warm_caches

        proj = self._make_project(tmp_path)
        stats = warm_caches(str(proj), type_engine=None)

        assert stats["skipped"] == 0
        assert stats["indexed"] == 2
        assert stats["qn_cached"] == 2

    def test_warm_run_all_skipped(self, tmp_path):
        """Second run on unchanged project skips every file."""
        from emend.transform import warm_caches

        proj = self._make_project(tmp_path)
        warm_caches(str(proj), type_engine=None)  # cold

        stats = warm_caches(str(proj), type_engine=None)  # warm
        assert stats["skipped"] == 2
        assert stats["indexed"] == 0
        assert stats["qn_cached"] == 0

    def test_warm_run_is_fast(self, tmp_path):
        """Warm run completes much faster than cold run (basic sanity check)."""
        import time
        from emend.transform import warm_caches

        proj = self._make_project(tmp_path)
        warm_caches(str(proj), type_engine=None)  # cold

        t0 = time.monotonic()
        warm_caches(str(proj), type_engine=None)  # warm
        warm_elapsed = time.monotonic() - t0

        # Even for 2 files, warm run should be well under 5 seconds.
        assert warm_elapsed < 5.0

    def test_warm_run_does_not_rebuild_facts(self, tmp_path, monkeypatch):
        """An unchanged parse index implies the persisted facts are current."""
        from emend.transform import warm_caches
        from emend.transform import cache as cache_module

        proj = self._make_project(tmp_path)
        calls = 0
        real_build = cache_module._build_facts_db

        def counting_build(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_build(*args, **kwargs)

        monkeypatch.setattr(cache_module, "_build_facts_db", counting_build)
        warm_caches(str(proj), type_engine=None)
        assert calls == 1

        warm_caches(str(proj), type_engine=None)
        assert calls == 1


def test_duplicate_cache_hit_does_not_build_scope_resolver(tmp_path, monkeypatch):
    """Fully cached duplicate payloads should return before project indexing."""
    from emend.transform import _compute_duplicate_payloads
    from emend.transform import index as index_module
    from emend.transform.cache import _init_cache_schema

    source = str(tmp_path / "a.py")
    file_contents = [(source, SOURCE)]
    db_path = tmp_path / "parse.db"
    conn = sqlite3.connect(db_path)
    _init_cache_schema(conn)
    conn.close()

    _compute_duplicate_payloads(str(db_path), str(tmp_path), file_contents)

    class UnexpectedResolver:
        def __init__(self, *args, **kwargs):
            raise AssertionError("scope resolver constructed for a full cache hit")

    monkeypatch.setattr(index_module._rust, "PyScopeResolver", UnexpectedResolver)
    _compute_duplicate_payloads(str(db_path), str(tmp_path), file_contents)


# ---------------------------------------------------------------------------
# Type cache warming
# ---------------------------------------------------------------------------

_HAS_TYPE_ENGINE = bool(
    shutil.which("pyright-langserver") or shutil.which("pyright")
    or shutil.which("pyrefly") or shutil.which("ty")
)


class TestTypeCacheWarming:
    """Verify that warm_caches populates the type_cache table."""

    def _make_project(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "a.py").write_text("def hello(x: int) -> str:\n    return str(x)\n")
        (proj / "b.py").write_text("y: float = 3.14\n")
        return proj

    @pytest.mark.skipif(not _HAS_TYPE_ENGINE, reason="no type engine on PATH")
    def test_type_cache_populated(self, tmp_path):
        """warm_caches with auto engine writes rows to the type_cache table."""
        from emend.transform import warm_caches

        proj = self._make_project(tmp_path)
        stats = warm_caches(str(proj), type_engine="auto")

        assert stats["type_cached"] >= 2
        assert stats["type_engine"] != ""

        db_path = proj / ".emend" / "cache" / "parse.db"
        assert db_path.exists()
        assert _db_row_count(db_path, "type_cache") >= 2

    @pytest.mark.skipif(not _HAS_TYPE_ENGINE, reason="no type engine on PATH")
    def test_type_cache_warm_run_uses_disk_cache(self, tmp_path):
        """Second warm_caches call reads types from disk, not the engine."""
        from emend.transform import warm_caches

        proj = self._make_project(tmp_path)
        warm_caches(str(proj), type_engine="auto")

        db_path = proj / ".emend" / "cache" / "parse.db"
        rows_after_cold = _db_row_count(db_path, "type_cache")
        assert rows_after_cold >= 2

        # Second run — type_cache rows should not grow.
        warm_caches(str(proj), type_engine="auto")
        rows_after_warm = _db_row_count(db_path, "type_cache")
        assert rows_after_warm == rows_after_cold

    def test_type_engine_none_skips_type_cache(self, tmp_path):
        """type_engine=None skips type indexing entirely."""
        from emend.transform import warm_caches

        proj = self._make_project(tmp_path)
        stats = warm_caches(str(proj), type_engine=None)

        assert stats["type_cached"] == 0
        assert stats["type_engine"] == ""


# ---------------------------------------------------------------------------
# Error caching — files that fail type inference should be cached so they
# are not re-computed on every run.
# ---------------------------------------------------------------------------


class TestErrorFileCaching:
    """Verify that files causing errors during infer_file are cached."""

    def test_base_infer_batch_caches_on_error(self, tmp_path):
        """Base class infer_batch returns empty FileTypes for files that raise."""
        from unittest.mock import patch
        from emend.type_oracle import FileTypes, _FileTypeCache, TypeOracle

        # Create a concrete adapter with a real cache to test the base class
        # infer_batch fallback.
        class _StubAdapter(TypeOracle):
            def __init__(self):
                self._cache = _FileTypeCache(max_entries=16)

            def infer_file(self, path, project_root=None):
                raise RuntimeError("simulated parse failure")

            def type_at(self, path, line, col, project_root=None):
                return None

            def clear_cache(self):
                self._cache.clear()

            def is_available(self):
                return True

        adapter = _StubAdapter()
        py = tmp_path / "bad.py"
        py.write_text("this is not valid: python [\n")

        results = adapter.infer_batch([py])
        key = str(py.resolve())
        assert key in results
        assert isinstance(results[key], FileTypes)
        assert results[key].bindings == []

    def test_pyright_infer_file_caches_on_error(self, tmp_path):
        """PyrightAdapter.infer_file caches an empty result when a file errors."""
        import hashlib
        from emend.type_oracle import PyrightAdapter, _content_hash

        db_path = str(tmp_path / "parse.db")
        adapter = PyrightAdapter(db_path=db_path)

        py = tmp_path / "bad.py"
        # Write non-UTF-8 bytes to trigger UnicodeDecodeError in read_text()
        py.write_bytes(b"x = 1\n\xff\xfe invalid utf8\n")

        content_hash = _content_hash(py)
        # First call — should catch the error and cache an empty result
        ft = adapter.infer_file(py)
        assert ft.bindings == []

        # Verify the empty result was cached
        cached = adapter._cache.get(content_hash)
        assert cached is not None
        assert cached.bindings == []

    def test_ty_infer_file_caches_on_error(self, tmp_path):
        """TyAdapter.infer_file caches an empty result when a file errors."""
        from emend.type_oracle import TyAdapter, _content_hash

        db_path = str(tmp_path / "parse.db")
        adapter = TyAdapter(db_path=db_path)

        py = tmp_path / "bad.py"
        py.write_bytes(b"x = 1\n\xff\xfe invalid utf8\n")

        content_hash = _content_hash(py)
        ft = adapter.infer_file(py)
        assert ft.bindings == []

        cached = adapter._cache.get(content_hash)
        assert cached is not None


# ---------------------------------------------------------------------------
# New index tables: symbol_index, import_graph, reference_index
# ---------------------------------------------------------------------------


class TestSymbolIndex:
    """Tests for the symbol_index table population and querying."""

    def test_symbol_index_populated(self, tmp_path):
        """Indexing populates the symbol_index table."""
        from emend.transform import _index_batch

        db_path = tmp_path / "parse.db"
        source = "def process_request(x: int) -> str:\n    return str(x)\n\nclass MyClass:\n    pass\n"
        batch = [(str(tmp_path / "mod.py"), source)]
        _index_batch((str(db_path), str(tmp_path), str(tmp_path), batch))

        count = _db_row_count(db_path, "symbol_index")
        assert count >= 2  # at least process_request + MyClass

        # Check symbol details
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT name, kind, line FROM symbol_index ORDER BY line"
        ).fetchall()
        conn.close()

        names = [r[0] for r in rows]
        assert "process_request" in names
        assert "MyClass" in names

    def test_symbol_index_has_signature(self, tmp_path):
        """Symbol index stores function signatures."""
        from emend.transform import _index_batch

        db_path = tmp_path / "parse.db"
        source = "def greet(name: str, loud: bool = False) -> str:\n    pass\n"
        batch = [(str(tmp_path / "mod.py"), source)]
        _index_batch((str(db_path), str(tmp_path), str(tmp_path), batch))

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT signature, returns FROM symbol_index WHERE name = 'greet'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert "name" in row[0]
        assert row[1] == "str"

    def test_symbol_index_kind_query(self, tmp_path):
        """Can query symbol_index by kind."""
        from emend.transform import _index_batch

        db_path = tmp_path / "parse.db"
        source = "def foo(): pass\nclass Bar: pass\ndef baz(): pass\n"
        batch = [(str(tmp_path / "mod.py"), source)]
        _index_batch((str(db_path), str(tmp_path), str(tmp_path), batch))

        conn = sqlite3.connect(str(db_path))
        funcs = conn.execute(
            "SELECT name FROM symbol_index WHERE kind = 'function'"
        ).fetchall()
        classes = conn.execute(
            "SELECT name FROM symbol_index WHERE kind = 'class'"
        ).fetchall()
        conn.close()

        assert len(funcs) == 2
        assert len(classes) == 1

    def test_symbol_index_prefix_query(self, tmp_path):
        """Can query symbols by name prefix (typeahead)."""
        from emend.transform import _index_batch

        db_path = tmp_path / "parse.db"
        source = (
            "def process_data(): pass\n"
            "def process_request(): pass\n"
            "def handle_error(): pass\n"
        )
        batch = [(str(tmp_path / "mod.py"), source)]
        _index_batch((str(db_path), str(tmp_path), str(tmp_path), batch))

        conn = sqlite3.connect(str(db_path))
        results = conn.execute(
            "SELECT name FROM symbol_index WHERE name LIKE 'process%'"
        ).fetchall()
        conn.close()

        assert len(results) == 2
        names = {r[0] for r in results}
        assert names == {"process_data", "process_request"}


class TestImportGraph:
    """Tests for the import_graph table."""

    def test_import_graph_populated(self, tmp_path):
        """Indexing populates the import_graph table."""
        from emend.transform import _index_batch

        db_path = tmp_path / "parse.db"
        source = "import os\nfrom pathlib import Path\nimport json\n"
        batch = [(str(tmp_path / "mod.py"), source)]
        _index_batch((str(db_path), str(tmp_path), str(tmp_path), batch))

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT imported_module FROM import_graph"
        ).fetchall()
        conn.close()

        modules = {r[0] for r in rows}
        assert "os" in modules
        assert "pathlib" in modules
        assert "json" in modules


class TestReferenceIndex:
    """Tests for the reference_index table."""

    def test_reference_index_populated(self, tmp_path):
        """Indexing populates the reference_index with QN references."""
        from emend.transform import _index_batch

        db_path = tmp_path / "parse.db"
        source = (
            "def helper(): return 1\n"
            "\n"
            "def main():\n"
            "    x = helper()\n"
            "    return x\n"
        )
        batch = [(str(tmp_path / "mod.py"), source)]
        _index_batch((str(db_path), str(tmp_path), str(tmp_path), batch))

        count = _db_row_count(db_path, "reference_index")
        # Should have references for helper, main, x, etc.
        assert count > 0

    def test_reference_index_has_call_kind(self, tmp_path):
        """Reference index records call sites with ref_kind='call'."""
        from emend.transform import _index_batch

        db_path = tmp_path / "parse.db"
        source = (
            "def process(): return 1\n"
            "\n"
            "result = process()\n"
        )
        batch = [(str(tmp_path / "mod.py"), source)]
        _index_batch((str(db_path), str(tmp_path), str(tmp_path), batch))

        conn = sqlite3.connect(str(db_path))
        calls = conn.execute(
            "SELECT target_qn, line, ref_kind FROM reference_index "
            "WHERE ref_kind = 'call'"
        ).fetchall()
        conn.close()

        # The call to process() should be recorded
        assert len(calls) >= 1


class TestFileManifest:
    """Tests for the file_manifest table via warm_caches."""

    def test_manifest_populated_after_index(self, tmp_path):
        """warm_caches populates the file_manifest table."""
        from emend.transform import warm_caches

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "a.py").write_text(SOURCE)

        warm_caches(str(proj), type_engine=None)

        db_path = proj / ".emend" / "cache" / "parse.db"
        count = _db_row_count(db_path, "file_manifest")
        assert count == 1

    def test_manifest_has_correct_hash(self, tmp_path):
        """File manifest content_hash matches actual file hash."""
        import hashlib
        from emend.transform import warm_caches

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "a.py").write_text(SOURCE)

        warm_caches(str(proj), type_engine=None)

        expected_hash = hashlib.md5(
            SOURCE.encode(), usedforsecurity=False
        ).digest()

        db_path = proj / ".emend" / "cache" / "parse.db"
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT content_hash FROM file_manifest"
        ).fetchone()
        conn.close()

        assert row[0] == expected_hash


class TestScanManifest:
    """Tests for the _scan_manifest staleness detection."""

    def test_unchanged_files_detected(self, tmp_path):
        """Unchanged files are correctly identified."""
        from emend.transform import warm_caches, _scan_manifest

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "a.py").write_text(SOURCE)

        warm_caches(str(proj), type_engine=None)

        result = _scan_manifest(str(proj))
        assert len(result.unchanged) == 1
        assert len(result.changed) == 0
        assert len(result.new_files) == 0
        assert len(result.deleted) == 0

    def test_new_file_detected(self, tmp_path):
        """New files not in manifest are detected."""
        from emend.transform import warm_caches, _scan_manifest

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "a.py").write_text(SOURCE)

        warm_caches(str(proj), type_engine=None)

        # Add a new file
        (proj / "b.py").write_text("x = 1\n")

        result = _scan_manifest(str(proj))
        assert len(result.new_files) == 1

    def test_changed_file_detected(self, tmp_path):
        """Modified files are detected via content hash."""
        import time
        from emend.transform import warm_caches, _scan_manifest

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "a.py").write_text(SOURCE)

        warm_caches(str(proj), type_engine=None)

        # Modify file content
        time.sleep(0.01)  # ensure different mtime
        (proj / "a.py").write_text("def goodbye():\n    return 0\n")

        result = _scan_manifest(str(proj))
        assert len(result.changed) == 1


class TestIndexStatus:
    """Tests for get_index_status."""

    def test_status_returns_info(self, tmp_path):
        """get_index_status returns useful information after indexing."""
        from emend.transform import warm_caches, get_index_status

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "a.py").write_text(SOURCE)
        (proj / "b.py").write_text("class Foo:\n    pass\n")

        warm_caches(str(proj), type_engine=None)

        info = get_index_status(str(proj))
        assert info is not None
        assert info["file_manifest_count"] == 2
        assert info["symbol_index_count"] >= 2  # hello + Foo
        assert info["schema_version"] == "4"

    def test_status_returns_none_without_index(self, tmp_path):
        """get_index_status returns None when no index exists."""
        from emend.transform import get_index_status

        info = get_index_status(str(tmp_path))
        assert info is None

    def test_status_exposes_git_head_and_indexed_at(self, tmp_path):
        """``info['git_head']`` and ``info['indexed_at']`` are populated
        after indexing — they must not remain under their worktree-scoped
        keys, otherwise ``emend index --status`` prints "unknown"."""
        import subprocess
        from emend.transform import warm_caches, get_index_status

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "a.py").write_text(SOURCE)

        # Initialise a git repo so that indexing records a real HEAD sha.
        git_env = [
            "-c", "user.email=t@t", "-c", "user.name=t",
            "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
        ]
        subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
        subprocess.run(
            ["git", *git_env, "add", "a.py"], cwd=proj, check=True,
        )
        subprocess.run(
            ["git", *git_env, "commit", "-q", "-m", "init"],
            cwd=proj, check=True,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=proj,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        warm_caches(str(proj), type_engine=None)

        info = get_index_status(str(proj))
        assert info is not None
        # Each worktree stores its metadata under "<key>:<worktree_id>",
        # but the returned dict should also expose the current worktree's
        # values under the plain key names — the CLI status formatter
        # in ``cli_tooling.py`` calls ``info.get('git_head')`` / ``get('indexed_at')``
        # and silently prints "unknown" otherwise.
        assert info.get("git_head") == head
        assert info.get("indexed_at")  # non-empty timestamp string
