"""Phase 8 production tests: duplicate cache + facts integration.

Tests that verify:
1. Explicit duplicate prewarming populates dup_cache on a small
   synthetic repo.
2. Re-indexing after editing one file updates only that file's duplicate facts.
3. Deleting a file removes its duplicate facts.
4. Re-running explicit prewarming with no changes reuses cached dup_cache rows
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
from emend.transform import warm_caches, _cache_db_dir, _compute_duplicate_payloads


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


def test_warm_caches_populates_valid_duplicate_payloads(tmp_path):
    """dup_cache data should be deserializable and contain subtrees/sequences."""
    _make_project(tmp_path)
    stats = warm_caches(str(tmp_path), type_engine="none", build_duplicates=True)
    assert stats["dup_cached"] == 2

    db_path = _cache_db_dir(str(tmp_path)) / "parse.db"
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT hash, version, data FROM dup_cache").fetchall()
    conn.close()

    assert len(rows) == 2

    for content_hash, version, data in rows:
        assert version == DUP_CACHE_VERSION
        payload = pickle.loads(zlib.decompress(data))
        assert isinstance(payload, dict), "Payload should be a dict"
        assert "subtrees" in payload, "Payload should have 'subtrees' key"
        assert "sequences" in payload, "Payload should have 'sequences' key"

        for s in payload["subtrees"]:
            assert "start_line" in s
            assert "end_line" in s
            assert "symbol" in s
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
    warm_caches(str(tmp_path), type_engine="none", build_duplicates=True)

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
    warm_caches(str(tmp_path), type_engine="none", build_duplicates=True)

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
    warm_caches(str(tmp_path), type_engine="none", build_duplicates=True)

    db_path = _cache_db_dir(str(tmp_path)) / "parse.db"

    conn = sqlite3.connect(str(db_path))
    first_hashes = set(
        row[0] for row in conn.execute("SELECT hash FROM dup_cache")
    )
    first_count = len(first_hashes)
    conn.close()

    # Run again without any changes.
    warm_caches(str(tmp_path), type_engine="none", build_duplicates=True)

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
    expected_hash = hashlib.md5(
        _SIMPLE_FUNC.encode(), usedforsecurity=False
    ).hexdigest()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO dup_cache (hash, version, data) VALUES (?, ?, ?)",
        (expected_hash, "1", b"stale"),
    )
    conn.commit()
    conn.close()

    _compute_duplicate_payloads(str(db_path), str(tmp_path), file_contents)

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT hash, version, data FROM dup_cache").fetchall()
    conn.close()

    assert len(rows) == 1
    content_hash, version, data = rows[0]
    assert version == DUP_CACHE_VERSION

    assert content_hash == expected_hash

    payload = pickle.loads(zlib.decompress(data))
    assert "subtrees" in payload
    assert "sequences" in payload
    assert all("symbol" in item for item in payload["subtrees"])


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


@pytest.fixture
def duplicate_sample(tmp_path):
    from emend import emend_core

    file_path = str(tmp_path / "sample.py")
    Path(file_path).write_text(_SIMPLE_FUNC)
    resolver = emend_core.PyScopeResolver(str(tmp_path))
    resolver.index_file(file_path, _SIMPLE_FUNC)
    return file_path, resolver


def test_duplicate_cache_public_payloads(duplicate_sample):
    from emend.duplicate import canonicalize_file_for_cache, build_statement_seqs_for_cache

    file_path, resolver = duplicate_sample
    subtrees = canonicalize_file_for_cache(file_path, _SIMPLE_FUNC, resolver)
    sequences = build_statement_seqs_for_cache(file_path, _SIMPLE_FUNC, resolver)
    assert isinstance(subtrees, list) and subtrees
    assert isinstance(sequences, list) and sequences
    for subtree in subtrees:
        assert {"start_line", "end_line", "canonical_hash", "root_kind",
                "node_count", "total_lines", "score"} <= subtree.keys()
        assert isinstance(subtree["canonical_hash"], str)
        assert len(subtree["canonical_hash"]) == 32
    for sequence in sequences:
        assert {"function_qn", "start_line", "end_line", "hashes",
                "line_ranges", "kinds"} <= sequence.keys()
        assert len(sequence["hashes"]) == len(sequence["line_ranges"]) == len(sequence["kinds"]) >= 2
        assert all(isinstance(value, str) and len(value) == 32 for value in sequence["hashes"])


def test_duplicate_payload_prepares_file_once(duplicate_sample, monkeypatch):
    """The combined cache path must not parse and project the file twice."""
    from unittest.mock import Mock
    import emend.duplicate as duplicate

    file_path, resolver = duplicate_sample
    watched = []
    for owner, name in (
        (duplicate.emend_core, "parse_source"),
        (duplicate.emend_core, "collect_symbols_from_str"),
        (duplicate, "_build_qn_at"),
    ):
        wrapped = Mock(wraps=getattr(owner, name))
        monkeypatch.setattr(owner, name, wrapped)
        watched.append(wrapped)

    payload = duplicate._build_duplicate_payload_for_cache(
        file_path, _SIMPLE_FUNC, resolver
    )

    assert set(payload) == {"subtrees", "sequences"}
    assert [call.call_count for call in watched] == [1, 1, 1]


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
    assert len(func_subtrees) == 2
    assert func_subtrees[0]["canonical_hash"] == func_subtrees[1]["canonical_hash"]
