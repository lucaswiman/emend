"""Tests for ``emend index`` cache-hit behavior.

Verifies that a second call to ``warm_caches`` on an unchanged project
skips all files (cache hits) instead of re-parsing them.
"""
import sqlite3
from pathlib import Path

import pytest


SOURCE = "def hello():\n    return 42\n"


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
        """First run writes parse and qn entries."""
        from emend.transform import _index_batch

        db_path = tmp_path / "parse.db"
        batch = [(str(tmp_path / "a.py"), SOURCE)]
        parse_n, qn_n, skipped = _index_batch((str(db_path), batch))

        assert parse_n == 1
        assert qn_n == 1
        assert skipped == 0

    def test_warm_cache_skips_file(self, tmp_path):
        """Second run with same content skips the file entirely."""
        from emend.transform import _index_batch

        db_path = tmp_path / "parse.db"
        batch = [(str(tmp_path / "a.py"), SOURCE)]

        # Cold run
        _index_batch((str(db_path), batch))

        # Warm run — must skip
        parse_n, qn_n, skipped = _index_batch((str(db_path), batch))
        assert parse_n == 0
        assert qn_n == 0
        assert skipped == 1

    def test_warm_cache_no_extra_db_rows(self, tmp_path):
        """Warm run must not increase the row count in the DB."""
        from emend.transform import _index_batch

        db_path = tmp_path / "parse.db"
        batch = [(str(tmp_path / "a.py"), SOURCE)]

        _index_batch((str(db_path), batch))
        rows_after_cold = _db_row_count(db_path, "parse_cache")

        _index_batch((str(db_path), batch))
        rows_after_warm = _db_row_count(db_path, "parse_cache")

        assert rows_after_cold == rows_after_warm == 1

    def test_partial_cache_only_missing_part_indexed(self, tmp_path):
        """If only parse is cached (not qn), only qn is added on second run."""
        import hashlib
        import pickle
        import zlib
        import libcst as cst

        from emend.transform import _index_batch

        db_path = tmp_path / "parse.db"
        content_hash = hashlib.md5(SOURCE.encode(), usedforsecurity=False).digest()

        # Pre-populate parse_cache but NOT qn_index
        module = cst.parse_module(SOURCE)
        blob = zlib.compress(pickle.dumps(module, protocol=pickle.HIGHEST_PROTOCOL), level=1)
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE parse_cache (hash BLOB PRIMARY KEY, data BLOB)")
        conn.execute("INSERT INTO parse_cache VALUES (?, ?)", (content_hash, blob))
        conn.commit()
        conn.close()

        batch = [(str(tmp_path / "a.py"), SOURCE)]
        parse_n, qn_n, skipped = _index_batch((str(db_path), batch))

        assert parse_n == 0  # already cached
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
        stats = warm_caches(str(proj))

        assert stats["skipped"] == 0
        assert stats["parse_cached"] == 2
        assert stats["qn_cached"] == 2

    def test_warm_run_all_skipped(self, tmp_path):
        """Second run on unchanged project skips every file."""
        from emend.transform import warm_caches

        proj = self._make_project(tmp_path)
        warm_caches(str(proj))  # cold

        stats = warm_caches(str(proj))  # warm
        assert stats["skipped"] == 2
        assert stats["parse_cached"] == 0
        assert stats["qn_cached"] == 0

    def test_warm_run_is_fast(self, tmp_path):
        """Warm run completes much faster than cold run (basic sanity check)."""
        import time
        from emend.transform import warm_caches

        proj = self._make_project(tmp_path)
        warm_caches(str(proj))  # cold

        t0 = time.monotonic()
        warm_caches(str(proj))  # warm
        warm_elapsed = time.monotonic() - t0

        # Even for 2 files, warm run should be well under 5 seconds.
        assert warm_elapsed < 5.0
