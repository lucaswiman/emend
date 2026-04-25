"""Symbol index, QN cache, and venv index management."""
from __future__ import annotations
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
import hashlib
import logging
import re

from ..language_plugins import NOQA_PATTERN as _NOQA_PATTERN
from emend import emend_core as _rust

logger = logging.getLogger(__name__)

def _get_cached_qnames(content_hash: bytes) -> set[str] | None:
    """Look up cached qualified-name set for a file by content hash."""
    from .cache import _get_disk_cache
    conn = _get_disk_cache()
    if conn is None:
        return None
    try:
        import pickle
        import zlib
        row = conn.execute(
            "SELECT qnames FROM qn_index WHERE hash = ?", (content_hash,)
        ).fetchone()
        if row is not None:
            return pickle.loads(zlib.decompress(row[0]))
    except Exception:
        pass
    return None


def _extract_all_exports_text(source: str, file_path: str = "__temp__.py") -> set[str]:
    """Extract names from ``__all__`` using tree-sitter pattern matching.

    Uses ``find_pattern`` so that the match is tree-sitter-based and respects
    syntactic boundaries (won't match ``__all__`` inside string literals or
    comments).  The small inner regex that pulls quoted names out of the
    already-parsed ``$NAMES`` captured text is acceptable because it operates
    on a structurally extracted sub-tree, not raw source.
    """
    from .patterns import find_pattern
    names: set[str] = set()
    try:
        matches = find_pattern(
            "__all__ = $NAMES",
            file_path,
            source_override=source,
            language="python",
        )
    except Exception:
        return names
    for m in matches:
        raw = m.captures.get("NAMES", "")
        for n in re.findall(r"""['"](\w+)['"]""", raw):
            names.add(n)
    return names


# Build from the canonical pattern so the noqa fragment is not duplicated.
# Matches both Python (#) and C-style (//) comment prefixes.
_NOQA_RE = re.compile(r'(?:#|//)\s*' + _NOQA_PATTERN, re.IGNORECASE)


def _extract_noqa_lines(source: str) -> set[int]:
    """Return line numbers that have ``# noqa: emend:deadcode`` (index-time helper)."""
    result: set[int] = set()
    for lineno, line in enumerate(source.splitlines(), 1):
        m = _NOQA_RE.search(line)
        if m is None:
            continue
        codes = m.group(1)
        if codes is None:
            # Bare noqa — suppresses everything
            result.add(lineno)
        elif 'deadcode' in codes:
            result.add(lineno)
    return result


def _index_batch(args: tuple[str, str, str, list[tuple[str, str]]]) -> tuple[int, int, int, int, int, int, int]:
    """Worker function for process-pool indexing.

    Runs in a subprocess.  Parses a batch of files, resolves qualified names,
    collects symbol definitions, import relationships, reference entries,
    and DSL symbols, then writes directly to the SQLite disk cache.

    Files whose content hash is already present in all cache tables are
    skipped (cache-hit fast path).

    Args:
        args: (db_path, source_root, project_root, [(file_path, content), ...])

    Returns:
        (parse_count, qn_count, skipped_count, sym_count, import_count, ref_count, dsl_count).
    """
    import pickle
    import sqlite3
    import zlib
    from emend.query import _collect_symbols as _collect_symbols_ts
    from emend import emend_core as _rust

    from .cache import _init_cache_schema, _SCHEMA_VERSION
    from .deadcode import _is_likely_entry_point
    db_path, source_root, project_root, file_batch = args
    qn_rows: list[tuple[bytes, bytes]] = []
    sym_rows: list[tuple] = []
    import_rows: list[tuple[bytes, str, str]] = []
    ref_rows: list[tuple] = []
    dsl_rows: list[tuple] = []

    if not file_batch:
        return (0, 0, 0, 0, 0, 0, 0)

    # Scope resolver for QN and reference collection (replaces MetadataWrapper).
    scope_resolver = _rust.PyScopeResolver(project_root)

    # Compute content hashes up-front so we can bulk-check the cache.
    file_hashes: list[tuple[bytes, str, str]] = [
        (hashlib.md5(content.encode(), usedforsecurity=False).digest(), py_file, content)
        for py_file, content in file_batch
    ]
    all_hashes = [h for h, _, _ in file_hashes]

    # Pre-check which hashes are already present in cache tables.
    cached_qn: set[bytes] = set()
    cached_sym: set[bytes] = set()
    cached_import: set[bytes] = set()
    cached_ref: set[bytes] = set()
    try:
        conn_check = sqlite3.connect(db_path, timeout=30)
        conn_check.execute("PRAGMA journal_mode=WAL")
        conn_check.execute("PRAGMA synchronous=NORMAL")
        placeholders = ",".join("?" * len(all_hashes))
        for table, target_set in [
            ("qn_index", cached_qn),
        ]:
            try:
                target_set.update(
                    row[0]
                    for row in conn_check.execute(
                        f"SELECT hash FROM {table} WHERE hash IN ({placeholders})",
                        all_hashes,
                    ).fetchall()
                )
            except Exception:
                pass
        # For the new tables, check by content_hash column
        for table, target_set in [
            ("symbol_index", cached_sym),
            ("import_graph", cached_import),
            ("reference_index", cached_ref),
        ]:
            try:
                target_set.update(
                    row[0]
                    for row in conn_check.execute(
                        f"SELECT DISTINCT content_hash FROM {table} "
                        f"WHERE content_hash IN ({placeholders})",
                        all_hashes,
                    ).fetchall()
                )
            except Exception:
                pass
        conn_check.close()
    except Exception:
        pass  # If pre-check fails, process everything

    skipped = 0
    processed = 0
    for content_hash, py_file, content in file_hashes:
        need_qn = content_hash not in cached_qn
        need_sym = content_hash not in cached_sym
        need_import = content_hash not in cached_import
        need_ref = content_hash not in cached_ref
        # Skip if the QN cache is populated (the core index).
        # The derived tables (symbol_index, import_graph,
        # reference_index) may legitimately have zero rows for a given file
        # (e.g., a file with only assignments has no symbols, a file with
        # no imports has no import_graph rows).  We re-derive them only
        # when the QN cache needs updating.
        if not need_qn:
            skipped += 1
            continue

        processed += 1

        # Use Rust scope resolver for QN and reference collection
        # (replaces expensive MetadataWrapper + _QNCollector + _RefIndexCollector).
        scope_indexed = False
        if need_qn or need_ref:
            try:
                scope_resolver.index_file(py_file, content)
                scope_indexed = True
            except Exception:
                pass

        if need_qn and scope_indexed:
            try:
                all_qnames = set(scope_resolver.all_qnames_in_file(py_file))
                qn_blob = zlib.compress(
                    pickle.dumps(all_qnames, protocol=pickle.HIGHEST_PROTOCOL),
                    level=1,
                )
                qn_rows.append((content_hash, qn_blob))
            except Exception:
                pass

        if need_sym:
            try:
                syms_for_file = _collect_symbols_ts(Path(py_file), content)

                # Compute module_qn prefix for this file.
                _src = Path(source_root)
                _proj = Path(project_root)
                _abs = Path(py_file).resolve()
                try:
                    _rel = _abs.relative_to(_src)
                except ValueError:
                    _rel = _abs.relative_to(_proj)
                _module_prefix = ".".join(
                    list(_rel.parts[:-1]) + [_rel.stem]
                )

                # __all__ membership and noqa for dead-code pre-filtering.
                exported_names = _extract_all_exports_text(content, py_file)
                noqa_lines = _extract_noqa_lines(content)

                for sym in syms_for_file:
                    # Build qualified_name from file module path + symbol path
                    # For index batch, use the dotted symbol path from the selector
                    parts = sym.path.split("::", 1)
                    dotted = parts[1] if len(parts) > 1 else sym.name
                    m_qn = f"{_module_prefix}.{dotted}"
                    sig = None
                    if sym.parameters:
                        ret_str = f" -> {sym.returns}" if sym.returns else ""
                        sig = f"def {sym.name}({', '.join(sym.parameters)}){ret_str}"
                    sym_rows.append((
                        content_hash,
                        py_file,
                        sym.name,
                        dotted,
                        m_qn,
                        sym.kind,
                        sym.line,
                        sym.end_line,
                        sym.depth,
                        sym.parent,
                        ",".join(sym.bases) if getattr(sym, "bases", None) else None,
                        sig,
                        sym.returns,
                        ",".join(sym.decorators) if sym.decorators else None,
                        int(_is_likely_entry_point(
                            sym.name, sym.kind, sym.decorators, sym.depth,
                        )),
                        int(sym.name in exported_names),
                        int(sym.line in noqa_lines),
                    ))
            except Exception:
                pass

        if need_import and scope_indexed:
            try:
                for _local, _mod, _imp_name, _is_star in scope_resolver.imports_in_file(py_file):
                    if _mod:
                        import_rows.append((content_hash, py_file, _mod))
            except Exception:
                pass

        if need_ref and scope_indexed:
            try:
                for qn_str, line, col, offset, end_offset, kind, _ann in scope_resolver.references_in_file(py_file):
                    ref_rows.append((content_hash, qn_str, py_file, line, col, kind))
            except Exception:
                pass

        # DSL symbol extraction (SQL, Jinja2, GraphQL, etc.)
        try:
            from emend.dsl import (
                detect_dsl_regions, extract_sql_symbols,
                extract_jinja_symbols, extract_graphql_symbols, DslKind,
            )
            regions = detect_dsl_regions(py_file, source=content)
            for region in regions:
                syms = []
                if region.dsl == DslKind.SQL:
                    syms = extract_sql_symbols(region)
                elif region.dsl == DslKind.JINJA:
                    syms = extract_jinja_symbols(region)
                elif region.dsl == DslKind.GRAPHQL:
                    syms = extract_graphql_symbols(region)
                for sym in syms:
                    dsl_rows.append((
                        sym.name,
                        sym.kind.value,
                        sym.dsl.value,
                        py_file,
                        region.host_start_line,
                        region.host_start_col,
                        region.host_end_line,
                        region.host_end_col,
                        content_hash,
                    ))
        except Exception:
            pass

    # Bulk-write to SQLite from this worker process.
    # WAL mode allows concurrent readers/writers across processes.
    has_data = qn_rows or sym_rows or import_rows or ref_rows or dsl_rows
    if has_data:
        try:
            conn = sqlite3.connect(db_path, timeout=30)
            # Ensure schema exists (idempotent; normally pre-created by
            # warm_caches, but needed when _index_batch is called directly).
            _init_cache_schema(conn)
            if qn_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO qn_index VALUES (?, ?)", qn_rows
                )
            if sym_rows:
                # Bulk-delete old entries before inserting
                hashes_with_syms = list({r[0] for r in sym_rows})
                placeholders = ",".join("?" * len(hashes_with_syms))
                conn.execute(
                    f"DELETE FROM symbol_index WHERE content_hash IN ({placeholders})",
                    hashes_with_syms,
                )
                conn.executemany(
                    "INSERT INTO symbol_index "
                    "(content_hash, file_path, name, qualified_name, module_qn, kind, "
                    "line, end_line, depth, parent, bases, signature, returns, decorators, "
                    "is_entry_point, is_exported, has_noqa) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    sym_rows,
                )
            if import_rows:
                hashes_with_imports = list({r[0] for r in import_rows})
                placeholders = ",".join("?" * len(hashes_with_imports))
                conn.execute(
                    f"DELETE FROM import_graph WHERE content_hash IN ({placeholders})",
                    hashes_with_imports,
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO import_graph "
                    "(content_hash, file_path, imported_module) "
                    "VALUES (?, ?, ?)",
                    import_rows,
                )
            if ref_rows:
                hashes_with_refs = list({r[0] for r in ref_rows})
                placeholders = ",".join("?" * len(hashes_with_refs))
                conn.execute(
                    f"DELETE FROM reference_index WHERE content_hash IN ({placeholders})",
                    hashes_with_refs,
                )
                conn.executemany(
                    "INSERT INTO reference_index "
                    "(content_hash, target_qn, file_path, line, col, ref_kind) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ref_rows,
                )
            if dsl_rows:
                hashes_with_dsl = list({r[-1] for r in dsl_rows})
                placeholders = ",".join("?" * len(hashes_with_dsl))
                conn.execute(
                    f"DELETE FROM dsl_symbols WHERE content_hash IN ({placeholders})",
                    hashes_with_dsl,
                )
                conn.executemany(
                    "INSERT INTO dsl_symbols "
                    "(name, kind, dsl, host_file, host_start_line, host_start_col, "
                    "host_end_line, host_end_col, content_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    dsl_rows,
                )
            conn.commit()
            conn.close()
        except Exception:
            pass

    # NOTE: CozoDB facts.db is NOT written here — it's built by the caller
    # (_build_facts_db) after all workers complete, extracting directly
    # from source files to avoid dual-write through parse.db.

    return (processed, len(qn_rows), skipped,
            len(sym_rows), len(import_rows), len(ref_rows), len(dsl_rows))


# ---------------------------------------------------------------------------
# Staleness detection and incremental index helpers
# ---------------------------------------------------------------------------


@dataclass
class ManifestScanResult:
    """Result of scanning the file manifest for staleness."""
    unchanged: list[str]             # files with matching mtime+size
    changed: list[tuple[str, bytes, bytes]]  # (path, old_hash, new_hash)
    new_files: list[str]             # files not in manifest
    deleted: list[str]               # manifest entries with no file on disk
    git_head_changed: bool           # True if HEAD differs from stored HEAD


def _scan_manifest(
    project_path: str,
    conn: sqlite3.Connection | None = None,
) -> ManifestScanResult:
    """Three-tier staleness check against the file manifest.

    Tier 1: Git HEAD check (~1ms).
    Tier 2: File stat scan (mtime_ns + size, no I/O).
    Tier 3: Content hash verification (only for stat-mismatched files).

    Returns a ManifestScanResult with categorized files.
    """
    import os as _os
    import sqlite3 as _sql3
    from .cache import _get_worktree_id, _cache_db_dir
    from .project_iter import _find_project_root, _collect_source_files_scandir

    result = ManifestScanResult(
        unchanged=[], changed=[], new_files=[], deleted=[],
        git_head_changed=False,
    )

    project_root = _find_project_root(project_path)
    worktree_id = _get_worktree_id(project_root)
    scan_root = str(Path(project_path).resolve())
    source_files = _collect_source_files_scandir(scan_root)
    source_files_resolved = {str(Path(f).resolve()): f for f in source_files}

    # Open DB (use provided conn or open fresh)
    close_conn = False
    if conn is None:
        cache_dir = _cache_db_dir(project_root)
        db_path = cache_dir / "parse.db"
        if not db_path.exists():
            # No index at all — everything is new
            result.new_files = source_files
            return result
        try:
            conn = _sql3.connect(str(db_path), timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            close_conn = True
        except Exception:
            result.new_files = source_files
            return result

    try:
        # Tier 1: Git HEAD check (scoped to this worktree)
        git_head_key = f"git_head:{worktree_id}"
        try:
            import subprocess as _sp
            git_result = _sp.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, timeout=5,
                cwd=project_root,
            )
            if git_result.returncode == 0:
                current_head = git_result.stdout.decode().strip()
                stored = conn.execute(
                    "SELECT value FROM index_meta WHERE key = ?",
                    (git_head_key,),
                ).fetchone()
                if stored and stored[0] != current_head:
                    result.git_head_changed = True
        except Exception:
            pass

        # Tier 2 + 3: Stat scan + hash verification
        # Load manifest into memory for fast lookup (filtered by worktree)
        manifest: dict[str, tuple[int, int, bytes]] = {}
        try:
            for row in conn.execute(
                "SELECT path, mtime_ns, size, content_hash FROM file_manifest "
                "WHERE worktree_id = ?",
                (worktree_id,),
            ).fetchall():
                manifest[row[0]] = (row[1], row[2], row[3])
        except Exception:
            # Table might not exist yet
            result.new_files = source_files
            return result

        manifest_paths = set(manifest.keys())
        current_paths = set(source_files_resolved.keys())

        # Deleted files
        result.deleted = list(manifest_paths - current_paths)

        mtime_updates: list[tuple] = []
        for resolved_path, original_path in source_files_resolved.items():
            if resolved_path not in manifest:
                result.new_files.append(original_path)
                continue

            stored_mtime, stored_size, stored_hash = manifest[resolved_path]

            # Tier 2: stat check
            try:
                st = _os.stat(resolved_path)
            except OSError:
                result.deleted.append(resolved_path)
                continue

            if st.st_mtime_ns == stored_mtime and st.st_size == stored_size:
                result.unchanged.append(original_path)
                continue

            # Tier 3: content hash verification
            try:
                content = Path(resolved_path).read_text()
                actual_hash = hashlib.md5(
                    content.encode(), usedforsecurity=False
                ).digest()
                if actual_hash == stored_hash:
                    # Content identical — just mtime changed (e.g. git checkout)
                    mtime_updates.append(
                        (st.st_mtime_ns, st.st_size, worktree_id, resolved_path)
                    )
                    result.unchanged.append(original_path)
                else:
                    result.changed.append((original_path, stored_hash, actual_hash))
            except Exception:
                result.new_files.append(original_path)

        # Batch-commit all mtime updates (avoids per-file fsync)
        if mtime_updates:
            try:
                conn.executemany(
                    "UPDATE file_manifest SET mtime_ns = ?, size = ? "
                    "WHERE worktree_id = ? AND path = ?",
                    mtime_updates,
                )
                conn.commit()
            except Exception:
                pass
    finally:
        if close_conn and conn:
            conn.close()

    return result


def _ensure_index_fresh(
    project_path: str,
    *,
    max_inline_reindex: int = 50,
    language: str = "python",
) -> bool:
    """Lightweight freshness check for the index.

    If the index is fresh, returns True immediately.
    If a small number of files changed, re-indexes them inline and returns True.
    If many files changed or no index exists, returns False (caller should
    fall back to cold path or suggest ``emend index``).
    """
    import sqlite3 as _sql3
    import time
    from .cache import _get_worktree_id, _cache_db_dir, _get_facts_db, _build_facts_db, _SCHEMA_VERSION, _delete_facts_for_file
    from .project_iter import _find_project_root, _find_source_root

    project_root = _find_project_root(project_path)
    worktree_id = _get_worktree_id(project_root)
    cache_dir = _cache_db_dir(project_root)
    db_path = cache_dir / "parse.db"
    if not db_path.exists():
        return False

    try:
        conn = _sql3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        return False

    try:
        # Check schema version — force re-index on mismatch.
        try:
            ver = conn.execute(
                "SELECT value FROM index_meta WHERE key = 'schema_version'"
            ).fetchone()
            if ver is None or ver[0] != _SCHEMA_VERSION:
                conn.close()
                return False
        except Exception:
            conn.close()
            return False

        # Check if index tables exist and have data
        try:
            count = conn.execute("SELECT COUNT(*) FROM symbol_index").fetchone()[0]
        except Exception:
            conn.close()
            return False
        if count == 0:
            conn.close()
            return False

        scan = _scan_manifest(project_path, conn=conn)
        n_stale = len(scan.changed) + len(scan.new_files)
        if n_stale == 0 and not scan.deleted:
            conn.close()
            return True

        if n_stale > max_inline_reindex:
            conn.close()
            return False

        # Inline re-index the small number of changed/new files
        files_to_index: list[tuple[str, str]] = []
        for path in scan.new_files:
            try:
                content = Path(path).read_text()
                files_to_index.append((path, content))
            except Exception:
                pass
        for path, old_hash, _new_hash in scan.changed:
            try:
                content = Path(path).read_text()
                files_to_index.append((path, content))
            except Exception:
                continue
            # Remove stale derived-table entries for the old content hash
            # so they don't linger after re-indexing with the new hash.
            for table in ("symbol_index", "import_graph", "reference_index"):
                try:
                    conn.execute(
                        f"DELETE FROM {table} WHERE content_hash = ?",
                        (old_hash,),
                    )
                except Exception:
                    pass
        if scan.changed:
            conn.commit()

        if files_to_index:
            _src_root = _find_source_root(project_root, language=language)
            _index_batch((str(db_path), _src_root, project_root, files_to_index))
            # Incrementally update CozoDB facts for changed files only.
            try:
                fdb = _get_facts_db(project_root)
                if fdb is not None:
                    from emend.fact_graph import FactGraph
                    fg = FactGraph(db_path=str(cache_dir / "facts.db"))
                    fg.update_files(files_to_index)
                    fg.close()
                else:
                    # No existing facts.db — fall back to full build.
                    _build_facts_db(project_root)
            except BaseException:
                logger.debug("incremental facts update failed, falling back to full rebuild", exc_info=True)
                try:
                    _build_facts_db(project_root)
                except BaseException:
                    pass
            # Update manifest for re-indexed files
            import os as _os
            now = time.time()
            for py_file, content in files_to_index:
                content_hash = hashlib.md5(
                    content.encode(), usedforsecurity=False
                ).digest()
                resolved = str(Path(py_file).resolve())
                try:
                    st = _os.stat(resolved)
                    conn.execute(
                        "INSERT OR REPLACE INTO file_manifest "
                        "(worktree_id, path, mtime_ns, size, content_hash, indexed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (worktree_id, resolved, st.st_mtime_ns, st.st_size, content_hash, now),
                    )
                except Exception:
                    pass
            conn.commit()

        # Clean up deleted files
        for deleted_path in scan.deleted:
            try:
                # Get the content_hash for this path to clean derived tables
                row = conn.execute(
                    "SELECT content_hash FROM file_manifest "
                    "WHERE worktree_id = ? AND path = ?",
                    (worktree_id, deleted_path),
                ).fetchone()
                if row:
                    old_hash = row[0]
                    conn.execute(
                        "DELETE FROM symbol_index WHERE content_hash = ?", (old_hash,)
                    )
                    conn.execute(
                        "DELETE FROM import_graph WHERE content_hash = ?", (old_hash,)
                    )
                    conn.execute(
                        "DELETE FROM reference_index WHERE content_hash = ?", (old_hash,)
                    )
                conn.execute(
                    "DELETE FROM file_manifest WHERE worktree_id = ? AND path = ?",
                    (worktree_id, deleted_path),
                )
            except Exception:
                pass
        if scan.deleted:
            conn.commit()
            # Also clean CozoDB facts db for deleted files
            try:
                fdb = _get_facts_db(project_root)
                if fdb is not None:
                    for dp in scan.deleted:
                        _delete_facts_for_file(fdb, dp)
                # Also clean FactGraph-style relations
                from emend.fact_graph import FactGraph
                fg = FactGraph(db_path=str(cache_dir / "facts.db"))
                # _delete_facts_for_file uses short column names from
                # _FACTS_SCHEMA; FactGraph.remove_files handles the
                # full-column-name schema.
                rel_deleted = []
                for dp in scan.deleted:
                    try:
                        rel_deleted.append(
                            str(Path(dp).relative_to(Path(project_root).resolve()))
                        )
                    except ValueError:
                        rel_deleted.append(dp)
                fg.remove_files(rel_deleted)
                fg.close()
            except BaseException:
                pass

        conn.close()
        return True
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return False


def query_symbol_index(
    project_path: str,
    *,
    name_pattern: str | None = None,
    kind: str | None = None,
    file_path: str | None = None,
    qualified_name: str | None = None,
    limit: int = 0,
    language: str = "python",
) -> list[dict] | None:
    """Query the fact_symbol relation for fast symbol lookup.

    Uses CozoDB facts.db when available, with SQLite parse.db fallback.
    Returns a list of dicts with symbol info, or None if the index
    is not available or not fresh.
    """
    if not _ensure_index_fresh(project_path, language=language):
        return None

    from .project_iter import _find_project_root
    project_root = _find_project_root(project_path)

    results = _query_symbol_index_cozo(
        project_root,
        name_pattern=name_pattern,
        kind=kind,
        file_path=file_path,
        qualified_name=qualified_name,
        limit=limit,
    )
    if results is None:
        return None

    # Fallback: if no results and not constrained to a specific file,
    # try looking up the symbol in venv site-packages.
    if not results and not file_path:
        venv_results = lookup_venv_symbol(
            project_path,
            name_pattern=name_pattern,
            qualified_name=qualified_name,
            kind=kind,
            limit=limit,
        )
        if venv_results:
            return venv_results

    # Fallback: if still no results and a qualified_name was given,
    # try resolving through module mappings (modmap).
    if not results and not file_path and qualified_name:
        modmap_results = _lookup_via_modmap(
            project_root, qualified_name,
            name_pattern=name_pattern, kind=kind, limit=limit,
        )
        if modmap_results:
            return modmap_results

    return results


def _query_symbol_index_cozo(
    project_root: str,
    *,
    name_pattern: str | None = None,
    kind: str | None = None,
    file_path: str | None = None,
    qualified_name: str | None = None,
    limit: int = 0,
) -> list[dict] | None:
    """Query fact_symbol via CozoDB Datalog."""
    from .cache import _get_facts_db
    fdb = _get_facts_db(project_root)
    if fdb is None:
        return None

    try:
        clauses = [
            "*fact_symbol[fp, mqn, name, qn, kind, line, end_line, depth, "
            "parent, bases, sig, returns, decs, is_entry, is_exported, has_noqa]"
        ]
        params: dict = {}

        if name_pattern:
            if "*" in name_pattern or "?" in name_pattern:
                # CozoDB doesn't have GLOB; use starts_with/ends_with/contains
                # Convert simple patterns; for complex globs fall back to SQLite.
                if name_pattern.endswith("*") and "*" not in name_pattern[:-1]:
                    clauses.append("starts_with(name, $name_prefix)")
                    params["name_prefix"] = name_pattern[:-1]
                elif name_pattern.startswith("*") and "*" not in name_pattern[1:]:
                    clauses.append("ends_with(name, $name_suffix)")
                    params["name_suffix"] = name_pattern[1:]
                else:
                    return None  # Complex glob — fall back to SQLite
            else:
                clauses.append("name == $name")
                params["name"] = name_pattern

        if kind:
            clauses.append("kind == $kind")
            params["kind"] = kind

        if file_path:
            # facts.db stores relative paths; convert absolute to relative.
            resolved = str(Path(file_path).resolve())
            try:
                rel_fp = str(Path(resolved).relative_to(Path(project_root).resolve()))
            except ValueError:
                rel_fp = resolved
            clauses.append("fp == $file_path")
            params["file_path"] = rel_fp

        if qualified_name:
            # Match qn, mqn, or mqn prefix
            clauses.append(
                "(qn == $qname or mqn == $qname or starts_with(mqn, $qname_prefix))"
            )
            params["qname"] = qualified_name
            params["qname_prefix"] = qualified_name + "."

        query = (
            "?[name, qn, kind, fp, line, end_line, depth, parent, sig, returns, decs] := "
            + ", ".join(clauses)
            + "\n:order name, fp, line"
        )
        if limit > 0:
            query += f"\n:limit {limit}"

        result = fdb.run(query, params)
        abs_root = str(Path(project_root).resolve())
        return [
            {
                "name": r[0],
                "qualified_name": r[1],
                "kind": r[2],
                "file_path": str(Path(abs_root) / r[3]) if not Path(r[3]).is_absolute() else r[3],
                "line": r[4],
                "end_line": r[5],
                "depth": r[6],
                "parent": r[7],
                "signature": r[8],
                "returns": r[9],
                "decorators": r[10].split(",") if r[10] else [],
            }
            for r in result["rows"]
        ]
    except Exception:
        logger.debug("CozoDB query_symbol_index failed", exc_info=True)
        return None


def _lookup_via_modmap(
    project_root: str,
    qualified_name: str,
    *,
    name_pattern: str | None = None,
    kind: str | None = None,
    limit: int = 0,
) -> list[dict]:
    """Try to resolve a qualified name via module mappings.

    If a modmap entry maps the module prefix to a local path or cloned
    repo, resolve it and search that directory's symbol index for the
    target symbol.
    """
    try:
        from emend.knowledge import MappingStore
    except Exception:
        return []

    try:
        store = MappingStore(project_root)
    except Exception:
        return []

    try:
        resolved = store.resolve_module_to_path(qualified_name)
        if resolved is None:
            return []

        resolved_path = Path(resolved)

        # Determine the symbol name to search for: the part of the
        # qualified name after the module mapping prefix.
        mm = store.resolve_module(qualified_name)
        if mm is None:
            return []
        prefix = mm.module_prefix
        suffix = qualified_name
        if qualified_name.startswith(prefix + "."):
            suffix = qualified_name[len(prefix) + 1:]
        # The last component is the symbol name.
        parts = suffix.rsplit(".", 1)
        sym_name = parts[-1] if parts else suffix

        # resolved_path may be a file or directory; find symbols there.
        if resolved_path.is_file():
            search_files = [resolved_path]
        elif resolved_path.is_dir():
            search_files = list(resolved_path.rglob("*.py"))
        else:
            return []

        from emend import emend_core

        results: list[dict] = []
        for fpath in search_files:
            try:
                source = fpath.read_text()
                ext = fpath.suffix.lstrip(".") or "py"
                rust_syms = emend_core.collect_symbols_from_str(source, ext=ext)
                for sym in rust_syms:
                    if sym.get("name") == sym_name or (name_pattern and sym.get("name") == name_pattern):
                        if kind and sym.get("kind") != kind:
                            continue
                        decs = sym.get("decorators", [])
                        results.append({
                            "name": sym.get("name", ""),
                            "qualified_name": sym.get("qualified_name", ""),
                            "kind": sym.get("kind", ""),
                            "file_path": str(fpath),
                            "line": sym.get("line", 0),
                            "end_line": sym.get("end_line", 0),
                            "depth": sym.get("depth", 0),
                            "parent": sym.get("parent", ""),
                            "signature": sym.get("signature", ""),
                            "returns": sym.get("returns", ""),
                            "decorators": decs if isinstance(decs, list) else decs.split(",") if decs else [],
                        })
                        if limit > 0 and len(results) >= limit:
                            return results
            except Exception:
                continue
        return results
    except Exception:
        return []
    finally:
        try:
            store.close()
        except Exception:
            pass


def _venv_db_path(project_root: str) -> Path:
    """Return the path to the venv-specific parse cache DB."""
    from .cache import _cache_db_dir
    return _cache_db_dir(project_root) / "parse_venv.db"


def _ensure_venv_index(project_root: str, language: str = "python") -> Path | None:
    """Build or refresh the venv symbol index.

    Creates ``parse_venv.db`` in ``.emend/cache/`` with the same
    ``symbol_index`` schema as the project cache.  The index is rebuilt
    when the site-packages directory's mtime changes.

    Returns the DB path, or ``None`` if venv lookup is disabled / no venv.
    """
    import sqlite3 as _sql3
    from .cache import _init_cache_schema

    from emend.project_config import resolve_environment_path

    site_packages = resolve_environment_path(project_root, language)
    if site_packages is None:
        return None

    db_path = _venv_db_path(project_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Check freshness: compare site-packages mtime with stored value
    import os
    try:
        sp_mtime = os.stat(str(site_packages)).st_mtime_ns
    except OSError:
        return None

    try:
        conn = _sql3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        return None

    try:
        # Create schema if needed
        _init_cache_schema(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS venv_meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )

        # Check stored mtime
        row = conn.execute(
            "SELECT value FROM venv_meta WHERE key = 'site_packages_mtime'"
        ).fetchone()
        if row and row[0] == str(sp_mtime):
            # Index is fresh
            count = conn.execute("SELECT COUNT(*) FROM symbol_index").fetchone()[0]
            if count > 0:
                conn.close()
                return db_path

        conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass

    # (Re)build the venv index
    logger.info("Building venv symbol index for %s", site_packages)
    _build_venv_index(str(db_path), str(site_packages), project_root, str(sp_mtime))
    return db_path


def _build_venv_index(
    db_path: str, site_packages: str, project_root: str, sp_mtime: str
) -> None:
    """Scan site-packages and populate the venv symbol index."""
    import sqlite3 as _sql3
    from emend.query import _collect_symbols
    from .cache import _init_cache_schema

    sp = Path(site_packages)
    # Collect .py and .pyi files, skipping common non-package dirs
    skip_names = {"__pycache__", ".git", "bin", "include", "share", "Scripts"}
    py_files: list[Path] = []
    stack = [sp]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in skip_names and not entry.name.startswith("."):
                    # Only descend into directories that look like Python packages
                    # (have __init__.py or are dist-info) or are top-level
                    if (entry / "__init__.py").exists() or (entry / "__init__.pyi").exists():
                        stack.append(entry)
                    elif entry.suffix in (".dist-info", ".egg-info"):
                        pass  # skip metadata dirs
                    elif entry.parent == sp:
                        # Top-level dir without __init__.py — could be namespace package
                        stack.append(entry)
            elif entry.suffix in (".py", ".pyi"):
                py_files.append(entry)

    logger.info("Venv index: found %d Python files in %s", len(py_files), site_packages)

    conn = _sql3.connect(db_path, timeout=30)
    _init_cache_schema(conn)

    # Clear old data
    conn.execute("DELETE FROM symbol_index")
    conn.commit()

    sym_rows: list[tuple] = []
    for fpath in py_files:
        try:
            content = fpath.read_text(errors="replace")
        except Exception:
            continue

        content_hash = hashlib.md5(content.encode(), usedforsecurity=False).digest()

        try:
            symbols = _collect_symbols(fpath, content)
        except Exception:
            continue

        # Compute module_qn from path relative to site-packages
        rel = fpath.relative_to(sp)
        module_parts = list(rel.parts[:-1])
        stem = rel.stem
        if stem != "__init__":
            module_parts.append(stem)
        module_qn = ".".join(module_parts)

        for sym in symbols:
            parts = sym.path.split("::", 1)
            dotted = parts[1] if len(parts) > 1 else sym.name
            m_qn = f"{module_qn}.{dotted}" if module_qn else dotted
            sig = None
            if sym.parameters:
                ret_str = f" -> {sym.returns}" if sym.returns else ""
                sig = f"def {sym.name}({', '.join(sym.parameters)}){ret_str}"
            sym_rows.append((
                content_hash,
                str(fpath),
                sym.name,
                dotted,
                m_qn,
                sym.kind,
                sym.line,
                sym.end_line,
                sym.depth,
                sym.parent,
                sig,
                sym.returns,
                ",".join(sym.decorators) if sym.decorators else None,
                0,  # is_entry_point
                0,  # is_exported
                0,  # has_noqa
            ))

    if sym_rows:
        conn.executemany(
            "INSERT INTO symbol_index "
            "(content_hash, file_path, name, qualified_name, module_qn, kind, "
            "line, end_line, depth, parent, signature, returns, decorators, "
            "is_entry_point, is_exported, has_noqa) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            sym_rows,
        )
        conn.commit()

    # Store mtime
    conn.execute(
        "INSERT OR REPLACE INTO venv_meta (key, value) VALUES (?, ?)",
        ("site_packages_mtime", sp_mtime),
    )
    conn.commit()
    conn.close()
    logger.info("Venv index: indexed %d symbols from %d files", len(sym_rows), len(py_files))


def lookup_venv_symbol(
    project_path: str,
    *,
    name_pattern: str | None = None,
    qualified_name: str | None = None,
    kind: str | None = None,
    limit: int = 0,
    language: str = "python",
) -> list[dict]:
    """Search the venv symbol index for symbol definitions.

    Uses a separate ``parse_venv.db`` cache that is built lazily on first
    lookup and refreshed when the venv's site-packages directory changes.

    Returns a list of symbol dicts (same shape as ``query_symbol_index``),
    or an empty list if no venv is found or lookup is disabled.
    """
    import sqlite3 as _sql3
    from .project_iter import _find_project_root

    project_root = _find_project_root(project_path)
    db_path = _ensure_venv_index(project_root, language)
    if db_path is None:
        return []

    try:
        conn = _sql3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        return []

    try:
        conditions: list[str] = []
        params: list = []

        if name_pattern:
            if "*" in name_pattern or "?" in name_pattern:
                conditions.append("name GLOB ?")
                params.append(name_pattern)
            else:
                conditions.append("name = ?")
                params.append(name_pattern)

        if kind:
            conditions.append("kind = ?")
            params.append(kind)

        if qualified_name:
            # Match exact or prefix (e.g. "requests.get" matches
            # module_qn "requests.api.get" via qualified_name column)
            conditions.append(
                "(qualified_name = ? OR module_qn = ? OR module_qn LIKE ?)"
            )
            params.extend([qualified_name, qualified_name, qualified_name + ".%"])

        where = " AND ".join(conditions) if conditions else "1=1"
        query = (
            f"SELECT name, qualified_name, kind, file_path, line, end_line, "
            f"depth, parent, signature, returns, decorators "
            f"FROM symbol_index WHERE {where} ORDER BY name, file_path, line"
        )
        if limit > 0:
            query += f" LIMIT {limit}"

        rows = conn.execute(query, params).fetchall()
        results = []
        for row in rows:
            results.append({
                "name": row[0],
                "qualified_name": row[1],
                "kind": row[2],
                "file_path": row[3],
                "line": row[4],
                "end_line": row[5],
                "depth": row[6],
                "parent": row[7],
                "signature": row[8],
                "returns": row[9],
                "decorators": row[10].split(",") if row[10] else [],
            })
        conn.close()
        return results
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return []


def query_reference_index(
    project_path: str,
    target_qn: str,
    *,
    ref_kind: str | None = None,
    language: str = "python",
) -> list[dict] | None:
    """Query references via CozoDB Datalog.

    Returns a list of dicts with reference info, or None if the index
    is not available or not fresh.
    """
    if not _ensure_index_fresh(project_path, language=language):
        return None

    from .project_iter import _find_project_root
    from .cache import _get_facts_db
    project_root = _find_project_root(project_path)

    fdb = _get_facts_db(project_root)
    if fdb is None:
        return None

    try:
        clauses = ["*fact_reference[tqn, fp, line, col, kind]", "tqn == $qn"]
        params: dict = {"qn": target_qn}
        if ref_kind:
            clauses.append("kind == $ref_kind")
            params["ref_kind"] = ref_kind
        query = (
            "?[fp, line, col, kind] := " + ", ".join(clauses)
            + "\n:order fp, line"
        )
        result = fdb.run(query, params)
        abs_root = str(Path(project_root).resolve())
        return [
            {
                "file_path": str(Path(abs_root) / r[0]) if not Path(r[0]).is_absolute() else r[0],
                "line": r[1],
                "col": r[2],
                "ref_kind": r[3],
            }
            for r in result["rows"]
        ]
    except Exception:
        logger.debug("CozoDB query_reference_index failed", exc_info=True)
        return None


def query_import_graph(
    project_path: str,
    imported_module: str,
) -> list[str] | None:
    """Query for files importing a module via CozoDB Datalog.

    Returns file paths, or None if index not available.
    """
    from .project_iter import _find_project_root
    from .cache import _get_facts_db
    project_root = _find_project_root(project_path)

    fdb = _get_facts_db(project_root)
    if fdb is None:
        return None

    try:
        result = fdb.run(
            "?[fp] := *fact_import[fp, mod], mod == $mod",
            {"mod": imported_module},
        )
        abs_root = str(Path(project_root).resolve())
        return [
            str(Path(abs_root) / r[0]) if not Path(r[0]).is_absolute() else r[0]
            for r in result["rows"]
        ]
    except Exception:
        logger.debug("CozoDB query_import_graph failed", exc_info=True)
        return None


def get_index_status(project_path: str) -> dict | None:
    """Return index freshness stats, or None if no index exists."""
    import sqlite3 as _sql3
    from .project_iter import _find_project_root
    from .cache import _cache_db_dir, _get_worktree_id

    project_root = _find_project_root(project_path)
    cache_dir = _cache_db_dir(project_root)
    db_path = cache_dir / "parse.db"
    if not db_path.exists():
        return None

    try:
        conn = _sql3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        return None

    try:
        info: dict = {}
        worktree_id = _get_worktree_id(project_root)

        # Index metadata.  Some keys (``git_head``, ``indexed_at``) are
        # scoped per-worktree and stored as ``"<key>:<worktree_id>"``.
        # Surface the current worktree's value under the plain key name
        # so consumers (including ``emend index --status``) can look it
        # up without knowing the worktree id.
        for row in conn.execute("SELECT key, value FROM index_meta").fetchall():
            info[row[0]] = row[1]
        for scoped in ("git_head", "indexed_at"):
            scoped_key = f"{scoped}:{worktree_id}"
            if scoped_key in info:
                info[scoped] = info[scoped_key]

        # Counts
        for table in ("file_manifest", "symbol_index", "import_graph", "reference_index"):
            try:
                info[f"{table}_count"] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            except Exception:
                info[f"{table}_count"] = 0

        # Staleness scan
        scan = _scan_manifest(project_path, conn=conn)
        info["unchanged_files"] = len(scan.unchanged)
        info["changed_files"] = len(scan.changed)
        info["new_files"] = len(scan.new_files)
        info["deleted_files"] = len(scan.deleted)
        info["git_head_changed"] = scan.git_head_changed

        conn.close()
        return info
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return None


def warm_caches(
    project_path: str = ".",
    *,
    jobs: int | None = None,
    callback: Callable[[str, str], None] | None = None,
    type_engine: str | None = "pyrefly",
    language: str = "python",
) -> dict[str, int | str]:
    """Pre-populate the parse, QN-index, and type caches for all project files.

    Designed to be called from the ``emend index`` CLI command or at MCP
    server start-up.  Each file is parsed, then QualifiedNameProvider is
    resolved to build the QN index, and finally type inference results are
    stored in the ``type_cache`` table.

    Uses a ``ProcessPoolExecutor`` so that file parsing (CPU-bound)
    runs across multiple cores without GIL contention.  Files are split
    into batches; each worker process parses its batch and writes results
    directly to the SQLite disk cache (WAL mode allows concurrent writers),
    avoiding the overhead of serialising parse results back to the main
    process.

    Args:
        project_path: Root directory of the project.
        jobs: Max parallelism (defaults to CPU count).
        callback: Called with ``(phase, file_path)`` for progress reporting.
        type_engine: Type inference engine for the type-cache phase.
            ``"auto"`` (default) auto-detects from project config and PATH.
            ``"none"`` or ``None`` skips type indexing entirely.
            Explicit values: ``"pyrefly"``, ``"pyright"``, ``"ty"``.

    Returns:
        Dict with stats: ``{"files", "indexed", "qn_cached",
        "type_cached", "type_engine"}``.
    """
    import multiprocessing
    import time
    from concurrent.futures import ProcessPoolExecutor
    from emend import emend_core as _rust
    from .cache import _cache_db_dir, _init_cache_schema, _build_facts_db, _get_worktree_id, _SCHEMA_VERSION
    from .project_iter import _find_project_root, _find_source_root, _collect_source_files_scandir

    project_root = _find_project_root(project_path)
    # Collect files from the user-specified path (not the project root)
    # so that `emend index src/` only indexes src/, not the entire repo.
    scan_root = str(Path(project_path).resolve())
    source_files = _collect_source_files_scandir(scan_root)
    logger.info("warm_caches: %d source files in %s", len(source_files), scan_root)

    max_workers = jobs or multiprocessing.cpu_count() or 4

    # Phase 1: read all files (Rust parallel I/O)
    t0 = time.monotonic()
    file_contents = _rust.read_and_filter_files(source_files, [])
    logger.info("warm_caches: read %d files in %.3fs", len(file_contents), time.monotonic() - t0)

    stats: dict[str, int | str] = {
        "files": len(file_contents), "indexed": 0, "qn_cached": 0,
        "skipped": 0, "sym_cached": 0, "import_cached": 0, "ref_cached": 0,
        "dsl_cached": 0, "type_cached": 0, "type_engine": "",
    }

    # Phase 2: parse + QN index in subprocesses.
    # Resolve the DB path and ensure the directory exists before spawning workers.
    cache_dir = _cache_db_dir(project_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    _ensure_cache_ignore_files(project_root)
    db_path = str(cache_dir / "parse.db")
    # Pre-create all tables in the main process so workers don't race on schema setup.
    try:
        import sqlite3 as _sqlite3
        _init_conn = _sqlite3.connect(db_path)
        _init_cache_schema(_init_conn)
        _init_conn.close()
    except Exception:
        pass

    # Resolve source root once so _index_batch workers can compute module_qn.
    source_root = _find_source_root(project_root, language=language)

    # Split files into batches — one batch per worker.
    batch_size = max(1, len(file_contents) // max_workers)
    batches: list[tuple[str, str, str, list[tuple[str, str]]]] = []
    for i in range(0, len(file_contents), batch_size):
        chunk = file_contents[i : i + batch_size]
        batches.append((db_path, source_root, project_root, chunk))

    t0 = time.monotonic()
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        # TODO: Conditionally use ProcessPoolExecutor or ThreadPoolExecutor for GIL-python vs free-threaded.
        for batch_idx, (parse_n, qn_n, skip_n, sym_n, import_n, ref_n, dsl_n) in enumerate(
            executor.map(_index_batch, batches)
        ):
            stats["indexed"] += parse_n
            stats["qn_cached"] += qn_n
            stats["skipped"] += skip_n
            stats["sym_cached"] += sym_n
            stats["import_cached"] += import_n
            stats["ref_cached"] += ref_n
            stats["dsl_cached"] += dsl_n
            # Report progress for all files in this batch
            if callback:
                _db_path, _src, _proj, chunk = batches[batch_idx]
                for py_file, _content in chunk:
                    callback("index", py_file)

    logger.info(
        "warm_caches: indexed %d files in %.3fs (parse=%d, qn=%d, sym=%d, import=%d, ref=%d, dsl=%d)",
        stats["files"], time.monotonic() - t0,
        stats["indexed"], stats["qn_cached"],
        stats["sym_cached"], stats["import_cached"], stats["ref_cached"],
        stats["dsl_cached"],
    )

    # Phase 2.5: Update file_manifest and index_meta with freshness data.
    worktree_id = _get_worktree_id(project_root)
    try:
        import os as _os
        import sqlite3 as _sqlite3
        _mf_conn = _sqlite3.connect(db_path, timeout=30)
        _mf_conn.execute("PRAGMA journal_mode=WAL")
        _mf_conn.execute("PRAGMA synchronous=NORMAL")
        now = time.time()
        manifest_rows = []
        for py_file, content in file_contents:
            content_hash = hashlib.md5(
                content.encode(), usedforsecurity=False
            ).digest()
            try:
                st = _os.stat(py_file)
                manifest_rows.append((
                    worktree_id,
                    str(Path(py_file).resolve()),
                    st.st_mtime_ns,
                    st.st_size,
                    content_hash,
                    now,
                ))
            except OSError:
                pass
        if manifest_rows:
            _mf_conn.executemany(
                "INSERT OR REPLACE INTO file_manifest "
                "(worktree_id, path, mtime_ns, size, content_hash, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                manifest_rows,
            )
        # Update git HEAD (scoped to this worktree)
        git_head_key = f"git_head:{worktree_id}"
        try:
            import subprocess as _sp
            result = _sp.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, timeout=5,
                cwd=project_root,
            )
            if result.returncode == 0:
                head_sha = result.stdout.decode().strip()
                _mf_conn.execute(
                    "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                    (git_head_key, head_sha),
                )
        except Exception:
            pass
        _mf_conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            (f"indexed_at:{worktree_id}", str(now)),
        )
        _mf_conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("schema_version", _SCHEMA_VERSION),
        )
        _mf_conn.commit()
        _mf_conn.close()
    except Exception:
        pass

    # Phase 3: type indexing — populate the type_cache table.
    # Runs in the main process.  Pyrefly handles its own parallelism
    # internally; LSP adapters (pyright, ty) are inherently sequential.
    if type_engine and type_engine.lower() != "none":
        from emend.type_oracle import (
            create_type_oracle,
            TypeEngineUnavailableError,
        )

        oracle = create_type_oracle(
            engine=type_engine, project_root=Path(project_root)
        )
        engine_name = type(oracle).__name__.replace("Adapter", "").lower()

        if not oracle.is_available():
            raise TypeEngineUnavailableError(
                f"Type inference engine '{engine_name}' is not installed or not on PATH. "
                f"Install it (pyrefly, ty, or pyright) and re-run, or pass "
                f"--type-engine=none to skip type indexing."
            )

        stats["type_engine"] = engine_name
        all_paths = [Path(f) for f, _ in file_contents]
        project_root_path = Path(project_root)

        t_type = time.monotonic()
        results = oracle.infer_batch(all_paths, project_root=project_root_path)
        stats["type_cached"] = len(results)
        if callback:
            for p in all_paths:
                callback("types", str(p))

        logger.info(
            "warm_caches: type-indexed %d files via %s in %.3fs",
            stats["type_cached"], engine_name, time.monotonic() - t_type,
        )

    # Phase 4: rebuild FTS5 trigram index for fast symbol search.
    try:
        import sqlite3 as _sqlite3
        from emend.editor_search import rebuild_fts as _rebuild_fts

        _fts_conn = _sqlite3.connect(db_path, timeout=30)
        _fts_conn.execute("PRAGMA journal_mode=WAL")
        _fts_conn.execute("PRAGMA synchronous=NORMAL")
        t_fts = time.monotonic()
        fts_count = _rebuild_fts(_fts_conn)
        _fts_conn.close()
        stats["fts_indexed"] = fts_count
        logger.info(
            "warm_caches: FTS index rebuilt (%d rows) in %.3fs",
            fts_count, time.monotonic() - t_fts,
        )
    except Exception as exc:
        logger.debug("warm_caches: FTS rebuild skipped: %s", exc)
        stats["fts_indexed"] = 0

    # Phase 5: build CozoDB facts.db directly from source files.
    # Read pre-computed references from parse.db (written by _index_batch
    # in Phase 2) so _build_facts_db can skip the expensive scope resolver
    # indexing entirely (~34s saved on Django-sized projects).
    try:
        t_facts = time.monotonic()
        precomputed_refs: dict[str, list[tuple]] = {}
        try:
            import sqlite3 as _sqlite3
            _ref_conn = _sqlite3.connect(db_path, timeout=30)
            _ref_conn.execute("PRAGMA journal_mode=WAL")
            for row in _ref_conn.execute(
                "SELECT file_path, target_qn, line, col, ref_kind "
                "FROM reference_index"
            ):
                fp, tqn, line, col, kind = row
                precomputed_refs.setdefault(fp, []).append((tqn, line, col, kind))
            _ref_conn.close()
            logger.info(
                "warm_caches: loaded %d pre-computed refs for %d files",
                sum(len(v) for v in precomputed_refs.values()),
                len(precomputed_refs),
            )
        except Exception:
            precomputed_refs = {}

        if precomputed_refs:
            _build_facts_db(
                project_root, precomputed_refs=precomputed_refs,
            )
        else:
            # Fallback: build scope resolver from scratch
            scope_resolver = _rust.PyScopeResolver(
                str(Path(project_root).resolve()),
            )
            for py_file, content in file_contents:
                try:
                    scope_resolver.index_file(py_file, content)
                except Exception:
                    pass
            _build_facts_db(project_root, scope_resolver=scope_resolver)
        logger.info(
            "warm_caches: facts db built in %.3fs",
            time.monotonic() - t_facts,
        )
    except BaseException:
        logger.debug("warm_caches: facts db build failed", exc_info=True)

    # Phase 6: duplicate analysis — compute and cache per-file duplicate payloads,
    # then materialize queryable facts into facts.db.
    try:
        t_dup = time.monotonic()
        _compute_duplicate_payloads(db_path, project_root, file_contents)
        stats["dup_cached"] = len(
            [fc for fc in file_contents if fc[0].endswith(".py")]
        )
        logger.info(
            "warm_caches: duplicate analysis done in %.3fs",
            time.monotonic() - t_dup,
        )
    except BaseException:
        logger.debug("warm_caches: duplicate analysis failed", exc_info=True)
        stats["dup_cached"] = 0

    return stats


def _ensure_cache_ignore_files(project_root: str) -> None:
    """Create .gitignore and .dockerignore in the cache directory."""
    from .cache import _cache_db_dir
    cache_dir = _cache_db_dir(project_root)
    if not cache_dir.is_dir():
        return
    gitignore = cache_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("# Auto-generated by emend index\n*\n")
    dockerignore = cache_dir / ".dockerignore"
    if not dockerignore.exists():
        dockerignore.write_text("# Auto-generated by emend index\n*\n")


def _compute_duplicate_payloads(
    db_path: str,
    project_root: str,
    file_contents: list[tuple[str, str]],
) -> None:
    """Compute and cache per-file duplicate analysis payloads.

    For each Python file whose content hash is not already in ``dup_cache``,
    builds canonical subtree + sibling-sequence payloads via
    :mod:`emend.duplicate` and stores the compressed payload in ``parse.db``
    (``dup_cache`` table).

    Only Python (``.py``) files are processed in production v1.
    """
    import pickle
    import sqlite3 as _sqlite3
    import zlib

    from emend.duplicate import DUP_CACHE_VERSION as DUP_VERSION

    conn = _sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # Collect hashes that already have up-to-date dup_cache entries so we
    # can skip recomputation for unchanged files.
    cached_hashes: set[str] = set()
    try:
        for row in conn.execute(
            "SELECT hash FROM dup_cache WHERE version = ?", (DUP_VERSION,)
        ):
            cached_hashes.add(row[0])
    except Exception:
        pass

    # Filter to Python files only and compute content hashes.
    py_files: list[tuple[str, str, str]] = []  # (path, content, content_hash)
    for file_path, content in file_contents:
        if not file_path.endswith(".py"):
            continue
        content_hash = hashlib.md5(
            content.encode(), usedforsecurity=False
        ).hexdigest()
        py_files.append((file_path, content, content_hash))

    # Build a scope resolver and index all Python files up front so that
    # canonicalize_file_for_cache / build_statement_seqs_for_cache get
    # accurate qualified-name information even for files that reference each
    # other.  Files that are already cached are still indexed (cheap) so that
    # cross-file qualified names resolve correctly.
    scope_resolver = _rust.PyScopeResolver(str(Path(project_root).resolve()))
    for file_path, content, _hash in py_files:
        try:
            scope_resolver.index_file(file_path, content)
        except Exception:
            pass

    from emend.duplicate import canonicalize_file_for_cache, build_statement_seqs_for_cache

    for file_path, content, content_hash in py_files:
        if content_hash in cached_hashes:
            continue
        try:
            subtrees = canonicalize_file_for_cache(file_path, content, scope_resolver)
            sequences = build_statement_seqs_for_cache(file_path, content, scope_resolver)
            payload = {"subtrees": subtrees, "sequences": sequences}
            data = zlib.compress(pickle.dumps(payload))
            conn.execute(
                "INSERT OR REPLACE INTO dup_cache (hash, version, data) VALUES (?, ?, ?)",
                (content_hash, DUP_VERSION, data),
            )
        except Exception:
            continue

    conn.commit()
    conn.close()


# _METAVAR_RE is defined here for backward compatibility (used in patterns.py)
_METAVAR_RE = re.compile(r'\$(?:\.\.\.)?[A-Z_][A-Z_0-9]*')
