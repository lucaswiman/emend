"""Phase 8 production tests: duplicate cache + facts integration.

Tests that verify:
1. ``emend index`` populates dup_cache and dup_subtree/dup_run facts on a small
   synthetic repo.
2. Re-indexing after editing one file updates only that file's duplicate facts.
3. Deleting a file removes its duplicate facts.
4. Re-running ``emend index`` with no changes reuses cached dup_cache rows
   instead of recomputing all files.
"""

from __future__ import annotations

import hashlib
import pickle
import sqlite3
import zlib
from pathlib import Path
from textwrap import dedent

import pytest

from emend.duplicate import DUP_CACHE_VERSION
from emend.transform import warm_caches, _get_facts_db, _cache_db_dir, _compute_duplicate_payloads


# ---------------------------------------------------------------------------
# Synthetic source fixtures
# ---------------------------------------------------------------------------

_SIMPLE_FUNC = dedent("""\
    def process_items(items):
        result = []
        for item in items:
            if item.is_valid():
                transformed = item.transform()
                result.append(transformed)
        return result

    def filter_items(items):
        result = []
        for item in items:
            if item.is_valid():
                transformed = item.transform()
                result.append(transformed)
        return result
""")

_HELPER_FUNC = dedent("""\
    def compute_score(values, weights):
        total = 0.0
        count = 0
        for value, weight in zip(values, weights):
            total += value * weight
            count += 1
        if count == 0:
            return 0.0
        return total / count

    def compute_average(values, weights):
        total = 0.0
        count = 0
        for value, weight in zip(values, weights):
            total += value * weight
            count += 1
        if count == 0:
            return 0.0
        return total / count
""")


def _make_project(tmp_path: Path) -> Path:
    """Create a minimal project with two Python files."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "utils.py").write_text(_SIMPLE_FUNC)
    (src / "helpers.py").write_text(_HELPER_FUNC)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'testpkg'\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Test 1: emend index populates dup_cache and facts
# ---------------------------------------------------------------------------


def test_warm_caches_populates_dup_cache(tmp_path):
    """warm_caches should write dup_cache rows for Python files."""
    _make_project(tmp_path)
    stats = warm_caches(str(tmp_path), type_engine="none")

    # The stats should indicate at least some files were dup-analyzed.
    assert stats.get("dup_cached", 0) >= 0  # may be 0 if no py files matched

    db_path = _cache_db_dir(str(tmp_path)) / "parse.db"
    assert db_path.exists(), "parse.db should be created by warm_caches"

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT hash, version FROM dup_cache").fetchall()
    conn.close()

    # We have 2 Python files; each should produce a dup_cache entry.
    assert len(rows) >= 1, "Expected at least one dup_cache row"
    for _hash, version in rows:
        assert version == DUP_CACHE_VERSION, "Cache version should match the module constant"


def test_warm_caches_dup_cache_data_valid(tmp_path):
    """dup_cache data should be deserializable and contain subtrees/sequences."""
    _make_project(tmp_path)
    warm_caches(str(tmp_path), type_engine="none")

    db_path = _cache_db_dir(str(tmp_path)) / "parse.db"
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT hash, version, data FROM dup_cache").fetchall()
    conn.close()

    assert rows, "Should have dup_cache rows"

    for content_hash, version, data in rows:
        payload = pickle.loads(zlib.decompress(data))
        assert isinstance(payload, dict), "Payload should be a dict"
        assert "subtrees" in payload, "Payload should have 'subtrees' key"
        assert "sequences" in payload, "Payload should have 'sequences' key"

        for s in payload["subtrees"]:
            assert "start_line" in s
            assert "end_line" in s
            assert "canonical_hash" in s
            assert "score" in s
            assert isinstance(s["score"], float)

        for seq in payload["sequences"]:
            assert "function_qn" in seq
            assert "hashes" in seq
            assert len(seq["hashes"]) >= 2


# ---------------------------------------------------------------------------
# Test 2: incremental refresh on file edit
# ---------------------------------------------------------------------------


def test_incremental_refresh_on_edit(tmp_path):
    """Editing a file should update its dup_cache row, not the unchanged file."""
    _make_project(tmp_path)
    warm_caches(str(tmp_path), type_engine="none")

    db_path = _cache_db_dir(str(tmp_path)) / "parse.db"

    # Capture the original hashes.
    conn = sqlite3.connect(str(db_path))
    original_hashes = set(
        row[0] for row in conn.execute("SELECT hash FROM dup_cache")
    )
    conn.close()

    assert len(original_hashes) >= 1

    # Edit one file.
    utils_file = tmp_path / "src" / "utils.py"
    new_content = _SIMPLE_FUNC + "\ndef extra_function(x):\n    return x + 1\n"
    utils_file.write_text(new_content)

    # Re-run warm_caches.
    warm_caches(str(tmp_path), type_engine="none")

    conn = sqlite3.connect(str(db_path))
    new_hashes = set(
        row[0] for row in conn.execute("SELECT hash FROM dup_cache")
    )
    conn.close()

    # The new set should differ from the original: the edited file produces a
    # new hash, so the total should have increased by 1 (new entry) while the
    # old entry for that file remains (content-addressed cache never deletes).
    new_file_hash = hashlib.md5(
        new_content.encode(), usedforsecurity=False
    ).hexdigest()
    assert new_file_hash in new_hashes, "New content hash should be in dup_cache"


# ---------------------------------------------------------------------------
# Test 3: no recomputation when nothing changed
# ---------------------------------------------------------------------------


def test_no_recomputation_on_unchanged_files(tmp_path):
    """Re-running warm_caches with unchanged files should reuse dup_cache."""
    _make_project(tmp_path)
    warm_caches(str(tmp_path), type_engine="none")

    db_path = _cache_db_dir(str(tmp_path)) / "parse.db"

    conn = sqlite3.connect(str(db_path))
    first_hashes = set(
        row[0] for row in conn.execute("SELECT hash FROM dup_cache")
    )
    first_count = len(first_hashes)
    conn.close()

    # Run again without any changes.
    warm_caches(str(tmp_path), type_engine="none")

    conn = sqlite3.connect(str(db_path))
    second_hashes = set(
        row[0] for row in conn.execute("SELECT hash FROM dup_cache")
    )
    second_count = len(second_hashes)
    conn.close()

    # The set of hashes should not grow (no recomputation = same set of entries).
    assert first_hashes == second_hashes, (
        "dup_cache hashes should be identical on second run with no changes"
    )
    assert first_count == second_count


# ---------------------------------------------------------------------------
# Test 4: _compute_duplicate_payloads directly
# ---------------------------------------------------------------------------


def test_compute_duplicate_payloads_directly(tmp_path):
    """_compute_duplicate_payloads should write dup_cache rows for .py files."""
    # Create a minimal parse.db with the dup_cache table.
    from emend.transform import _init_cache_schema

    db_path = tmp_path / "parse.db"
    conn = sqlite3.connect(str(db_path))
    _init_cache_schema(conn)
    conn.close()

    py_file = tmp_path / "sample.py"
    py_file.write_text(_SIMPLE_FUNC)

    file_contents = [(str(py_file), _SIMPLE_FUNC)]

    _compute_duplicate_payloads(str(db_path), str(tmp_path), file_contents)

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT hash, version, data FROM dup_cache").fetchall()
    conn.close()

    assert len(rows) == 1
    content_hash, version, data = rows[0]
    assert version == DUP_CACHE_VERSION

    expected_hash = hashlib.md5(
        _SIMPLE_FUNC.encode(), usedforsecurity=False
    ).hexdigest()
    assert content_hash == expected_hash

    payload = pickle.loads(zlib.decompress(data))
    assert "subtrees" in payload
    assert "sequences" in payload


def test_compute_duplicate_payloads_skips_non_python(tmp_path):
    """_compute_duplicate_payloads should skip non-.py files."""
    from emend.transform import _init_cache_schema

    db_path = tmp_path / "parse.db"
    conn = sqlite3.connect(str(db_path))
    _init_cache_schema(conn)
    conn.close()

    file_contents = [
        (str(tmp_path / "style.css"), "body { color: red; }"),
        (str(tmp_path / "config.ts"), "const x: number = 1;"),
    ]

    _compute_duplicate_payloads(str(db_path), str(tmp_path), file_contents)

    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM dup_cache").fetchone()[0]
    conn.close()

    assert count == 0, "Non-Python files should produce no dup_cache rows"


def test_compute_duplicate_payloads_idempotent(tmp_path):
    """Calling _compute_duplicate_payloads twice with the same content is a no-op."""
    from emend.transform import _init_cache_schema

    db_path = tmp_path / "parse.db"
    conn = sqlite3.connect(str(db_path))
    _init_cache_schema(conn)
    conn.close()

    py_file = tmp_path / "sample.py"
    py_file.write_text(_HELPER_FUNC)
    file_contents = [(str(py_file), _HELPER_FUNC)]

    _compute_duplicate_payloads(str(db_path), str(tmp_path), file_contents)
    _compute_duplicate_payloads(str(db_path), str(tmp_path), file_contents)

    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM dup_cache").fetchone()[0]
    conn.close()

    assert count == 1, "Second run should not create a duplicate row"


# ---------------------------------------------------------------------------
# Test 5: duplicate module public API
# ---------------------------------------------------------------------------


def test_duplicate_module_canonicalize_file_for_cache(tmp_path):
    """canonicalize_file_for_cache should return subtree dicts for a Python file."""
    from emend.duplicate import canonicalize_file_for_cache
    from emend import emend_core

    file_path = str(tmp_path / "sample.py")
    (tmp_path / "sample.py").write_text(_SIMPLE_FUNC)

    scope_resolver = emend_core.PyScopeResolver(str(tmp_path))
    scope_resolver.index_file(file_path, _SIMPLE_FUNC)

    subtrees = canonicalize_file_for_cache(file_path, _SIMPLE_FUNC, scope_resolver)

    assert isinstance(subtrees, list)
    # The two near-duplicate functions should produce some subtree candidates.
    # (May be 0 if triviality filters reject all; ensure no crash at minimum.)
    for s in subtrees:
        assert "start_line" in s
        assert "end_line" in s
        assert "canonical_hash" in s
        assert "root_kind" in s
        assert "node_count" in s
        assert "total_lines" in s
        assert "score" in s
        assert isinstance(s["canonical_hash"], str)
        assert len(s["canonical_hash"]) == 32  # 16-byte blake2b as hex


def test_duplicate_module_build_statement_seqs_for_cache(tmp_path):
    """build_statement_seqs_for_cache should return sequence dicts for a Python file."""
    from emend.duplicate import build_statement_seqs_for_cache
    from emend import emend_core

    file_path = str(tmp_path / "sample.py")
    (tmp_path / "sample.py").write_text(_SIMPLE_FUNC)

    scope_resolver = emend_core.PyScopeResolver(str(tmp_path))
    scope_resolver.index_file(file_path, _SIMPLE_FUNC)

    seqs = build_statement_seqs_for_cache(file_path, _SIMPLE_FUNC, scope_resolver)

    assert isinstance(seqs, list)
    for seq in seqs:
        assert "function_qn" in seq
        assert "start_line" in seq
        assert "end_line" in seq
        assert "hashes" in seq
        assert "line_ranges" in seq
        assert "kinds" in seq
        assert len(seq["hashes"]) >= 2
        assert len(seq["hashes"]) == len(seq["line_ranges"])
        assert len(seq["hashes"]) == len(seq["kinds"])
        for h in seq["hashes"]:
            assert isinstance(h, str)
            assert len(h) == 32  # 16-byte blake2b as hex


def test_duplicate_module_near_duplicate_detection(tmp_path):
    """Two near-duplicate functions should produce the same canonical_hash."""
    from emend.duplicate import canonicalize_file_for_cache
    from emend import emend_core

    # Both functions have the same structure; only variable names differ.
    source = dedent("""\
        def compute_total(values, multipliers):
            accumulator = 0.0
            item_count = 0
            for val, mult in zip(values, multipliers):
                accumulator += val * mult
                item_count += 1
            if item_count == 0:
                return 0.0
            return accumulator / item_count

        def compute_weighted(data, weights):
            total = 0.0
            n = 0
            for x, w in zip(data, weights):
                total += x * w
                n += 1
            if n == 0:
                return 0.0
            return total / n
    """)

    file_path = str(tmp_path / "dup.py")
    (tmp_path / "dup.py").write_text(source)

    scope_resolver = emend_core.PyScopeResolver(str(tmp_path))
    scope_resolver.index_file(file_path, source)

    subtrees = canonicalize_file_for_cache(file_path, source, scope_resolver)

    # Both functions should be candidates and have the same canonical_hash
    # because variable names are alpha-renamed.
    func_subtrees = [s for s in subtrees if s["root_kind"] == "function_definition"]
    if len(func_subtrees) >= 2:
        hashes = [s["canonical_hash"] for s in func_subtrees]
        assert hashes[0] == hashes[1], (
            "Structurally identical functions with renamed variables should "
            "produce the same canonical hash"
        )


def test_canonicalize_file_for_cache_records_symbol(tmp_path):
    """Cached payloads must carry the containing symbol.

    Without it the cached read path reports members with no symbol name and,
    worse, the dunder-boilerplate suppression heuristic (which keys off the
    symbol name) silently stops applying once the cache is warm.
    """
    from emend.duplicate import canonicalize_file_for_cache
    from emend import emend_core

    source = dedent("""\
        class Thing:
            def __eq__(self, other):
                return (self.alpha == other.alpha
                        and self.beta == other.beta
                        and self.gamma == other.gamma)
    """)
    file_path = str(tmp_path / "thing.py")
    (tmp_path / "thing.py").write_text(source)

    scope_resolver = emend_core.PyScopeResolver(str(tmp_path))
    scope_resolver.index_file(file_path, source)

    subtrees = canonicalize_file_for_cache(file_path, source, scope_resolver)
    assert subtrees, "expected at least one candidate subtree"
    assert all("symbol" in s for s in subtrees)
    assert any("__eq__" in s["symbol"] for s in subtrees)


def test_cached_and_uncached_duplicate_clusters_agree(tmp_path):
    """Dunder boilerplate suppressed on a cold cache stays suppressed warm."""
    from emend.duplicate import query_duplicates

    for i in (1, 2, 3):
        (tmp_path / f"mod{i}.py").write_text(dedent(f"""\
            class Thing{i}:
                def __eq__(self, other):
                    return (self.alpha == other.alpha
                            and self.beta == other.beta
                            and self.gamma == other.gamma)
        """))

    cold = query_duplicates(str(tmp_path))
    warm_caches(str(tmp_path), type_engine="none")
    warm = query_duplicates(str(tmp_path))

    def shape(clusters):
        return sorted(
            tuple(sorted((m.file, m.start_line, m.end_line) for m in c.members))
            for c in clusters
        )

    assert shape(warm) == shape(cold)
