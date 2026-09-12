"""Symbol index, QN cache, and venv index management."""
from __future__ import annotations
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
import hashlib
import logging
import os
import re

from ..language_plugins import NOQA_PATTERN as _NOQA_PATTERN
from emend import emend_core as _rust
from emend.errors import BUG_EXCEPTIONS

if TYPE_CHECKING:
    import sqlite3
    from emend.type_oracle import TypeBatchInputs, TypeOracle

logger = logging.getLogger(__name__)


def _scope_cache_hash(content_hash: bytes, file_path: str, project_root: str) -> bytes:
    """Key QN-derived data without changing content-addressed syntax keys."""
    from emend.language_registry import detect_language
    from emend.project_config import module_resolution_context

    language = detect_language(file_path) or "python"
    module_root, root_module = module_resolution_context(
        project_root, language, file_path=file_path,
    )
    root = Path(project_root).resolve()
    identity = (
        os.path.relpath(module_root, root),
        os.path.relpath(root_module, root) if root_module else "",
    )
    return hashlib.md5(
        repr(identity).encode() + content_hash, usedforsecurity=False,
    ).digest()


def _get_cached_qnames(
    content_hash: bytes,
    *,
    file_path: str,
    project_root: str | Path = ".",
) -> set[str] | None:
    """Look up names for one file revision, never content in another module."""
    from .cache import _get_disk_cache
    conn = _get_disk_cache(project_root)
    if conn is None:
        return None
    import pickle
    import sqlite3
    import zlib
    try:
        row = conn.execute(
            "SELECT qnames FROM qn_index WHERE file_path = ? AND hash = ?",
            (str(Path(file_path).resolve()),
             _scope_cache_hash(content_hash, file_path, str(project_root))),
        ).fetchone()
    except sqlite3.Error:
        logger.debug("qn_index cache lookup failed", exc_info=True)
        return None
    if row is None:
        return None
    try:
        return pickle.loads(zlib.decompress(row[0]))
    except Exception:
        logger.debug("corrupt qn_index cache row; ignoring", exc_info=True)
        return None


def _extract_all_exports_text(source: str) -> set[str]:
    """Backward-compatible wrapper for canonical Python export detection."""
    from emend.language_registry import detect_exported_names

    return detect_exported_names(source, "python")


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


def _check_cache_hits(
    conn: sqlite3.Connection, file_revisions: list[tuple[str, bytes]]
) -> set[tuple[str, bytes]]:
    """QN markers and derived rows are committed together; probe only markers."""
    cached_qn: set[tuple[str, bytes]] = set()
    try:
        for file_path, content_hash in file_revisions:
            resolved = str(Path(file_path).resolve())
            if conn.execute(
                "SELECT 1 FROM qn_index WHERE file_path = ? AND hash = ?",
                (resolved, content_hash),
            ).fetchone() is not None:
                cached_qn.add((resolved, content_hash))
    except sqlite3.Error:
        logger.debug("qn_index cache pre-check query failed", exc_info=True)
    return cached_qn


def _write_index_rows(
    conn: sqlite3.Connection,
    qn_rows: list[tuple[str, bytes, bytes]],
    sym_rows: list[tuple],
    import_rows: list[tuple[bytes, str, str]],
    ref_rows: list[tuple],
    dsl_rows: list[tuple],
) -> None:
    """Bulk-write collected index rows to the SQLite cache.

    Performs a delete-then-insert for the per-file-derived tables so that
    a second indexing pass replaces stale rows.  SQLite errors are swallowed
    (and logged) — environmental failures must not crash the worker process.
    """
    import sqlite3

    has_data = qn_rows or sym_rows or import_rows or ref_rows or dsl_rows
    if not has_data:
        return
    try:
        if qn_rows:
            revised_paths = list({row[0] for row in qn_rows})
            for table, column in (
                ("symbol_index", "file_path"),
                ("import_graph", "file_path"),
                ("reference_index", "file_path"),
                ("dsl_symbols", "host_file"),
            ):
                conn.executemany(
                    f"DELETE FROM {table} WHERE {column} = ?",
                    ((path,) for path in revised_paths),
                )
            conn.executemany(
                "INSERT OR REPLACE INTO qn_index(file_path, hash, qnames) "
                "VALUES (?, ?, ?)",
                qn_rows,
            )
        if sym_rows:
            conn.executemany(
                "INSERT INTO symbol_index "
                "(content_hash, file_path, name, qualified_name, module_qn, kind, "
                "line, end_line, depth, parent, bases, signature, returns, decorators, "
                "is_entry_point, is_exported, has_noqa) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                sym_rows,
            )
        if import_rows:
            conn.executemany(
                "INSERT OR IGNORE INTO import_graph "
                "(content_hash, file_path, imported_module) "
                "VALUES (?, ?, ?)",
                import_rows,
            )
        if ref_rows:
            conn.executemany(
                "INSERT INTO reference_index "
                "(content_hash, target_qn, file_path, line, col, ref_kind) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ref_rows,
            )
        if dsl_rows:
            conn.executemany(
                "INSERT INTO dsl_symbols "
                "(name, kind, dsl, host_file, host_start_line, host_start_col, "
                "host_end_line, host_end_col, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                dsl_rows,
            )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        logger.debug("bulk index write failed", exc_info=True)


def _index_batch(args: tuple[str, str, str, list[tuple[str, str]]]) -> tuple[int, int, int, int, int, int, int]:
    """Own one connection per worker batch, with one transaction per file."""
    import sqlite3
    from .cache import _init_cache_schema

    if not args[3]:
        return (0, 0, 0, 0, 0, 0, 0)
    with closing(sqlite3.connect(args[0], timeout=30)) as conn:
        _init_cache_schema(conn)
        return _index_batch_rows(args, conn)


def _index_batch_rows(args, conn: sqlite3.Connection) -> tuple[int, int, int, int, int, int, int]:
    """Worker function for per-file indexing.

    Parses a batch of files, resolves qualified names,
    collects symbol definitions, import relationships, reference entries,
    and DSL symbols. Each completed file is written atomically to SQLite
    before deriving the next, overlapping writes with other workers' analysis.

    Files whose content hash is already present in all cache tables are
    skipped (cache-hit fast path).

    Args:
        args: (db_path, source_root, project_root, [(file_path, content), ...])

    Returns:
        (parse_count, qn_count, skipped_count, sym_count, import_count, ref_count, dsl_count).
    """
    import pickle
    import zlib
    from emend.analysis_store import collect_symbol_info as _collect_symbols_ts
    from emend import emend_core as _rust
    from emend.dsl import (
        detect_dsl_regions, extract_sql_symbols,
        extract_jinja_symbols, extract_graphql_symbols, DslKind,
    )

    from .deadcode import _is_likely_entry_point
    db_path, source_root, project_root, file_batch = args
    # Scope resolvers share the configured module identity used by live
    # project traversal; one resolver is retained per language extension.
    scope_resolvers = {}

    # Compute content hashes up-front so we can bulk-check the cache.
    file_hashes: list[tuple[bytes, str, str]] = [
        (hashlib.md5(content.encode(), usedforsecurity=False).digest(), py_file, content)
        for py_file, content in file_batch
    ]
    cached_qn = _check_cache_hits(
        conn, [(path, _scope_cache_hash(digest, path, project_root))
               for digest, path, _ in file_hashes]
    )

    skipped = 0
    processed = 0
    row_counts = [0] * 5
    for content_hash, py_file, content in file_hashes:
        # The QN cache is the core index. The derived tables (symbol_index,
        # import_graph, reference_index) may legitimately have zero rows for a
        # given file (e.g. a file with only assignments has no symbols) and are
        # written in lockstep with the QN cache, so we re-derive all of them
        # exactly when the QN cache entry is missing.
        scope_hash = _scope_cache_hash(content_hash, py_file, project_root)
        if (str(Path(py_file).resolve()), scope_hash) in cached_qn:
            skipped += 1
            continue

        processed += 1
        qn_rows: list[tuple[str, bytes, bytes]] = []
        sym_rows: list[tuple] = []
        import_rows: list[tuple[bytes, str, str]] = []
        ref_rows: list[tuple] = []
        dsl_rows: list[tuple] = []

        # Use Rust scope resolver for QN and reference collection
        # (replaces expensive MetadataWrapper + _QNCollector + _RefIndexCollector).
        scope_indexed = False
        try:
            from emend.language_registry import detect_language
            from emend.project_config import module_resolution_context
            extension = Path(py_file).suffix.lstrip(".")
            language = detect_language(py_file) or "python"
            module_root, root_module = module_resolution_context(
                project_root, language, file_path=py_file,
            )
            resolver_key = (extension, str(root_module) if root_module else None)
            if resolver_key not in scope_resolvers:
                scope_resolvers[resolver_key] = _rust.PyScopeResolver(
                    project_root, extension, str(module_root),
                    str(root_module) if root_module else None,
                )
            scope_resolver = scope_resolvers[resolver_key]
            scope_resolver.index_file(py_file, content)
            scope_indexed = True
        except Exception:
            logger.debug("scope indexing failed for %s", py_file, exc_info=True)

        if scope_indexed:
            try:
                all_qnames = set(scope_resolver.all_qnames_in_file(py_file))
            except Exception:
                logger.debug("qname collection failed for %s", py_file, exc_info=True)
            else:
                qn_blob = zlib.compress(
                    pickle.dumps(all_qnames, protocol=pickle.HIGHEST_PROTOCOL),
                    level=1,
                )
                qn_rows.append((str(Path(py_file).resolve()), scope_hash, qn_blob))

        try:
            syms_for_file = _collect_symbols_ts(Path(py_file), content)
        except BUG_EXCEPTIONS:
            raise
        except Exception:
            logger.debug("symbol collection failed for %s", py_file, exc_info=True)
            syms_for_file = []

        # Compute module_qn prefix for this file.
        _src = Path(source_root)
        _proj = Path(project_root)
        _abs = Path(py_file).resolve()
        try:
            _rel = _abs.relative_to(_src)
        except ValueError:
            try:
                _rel = _abs.relative_to(_proj)
            except ValueError:
                _rel = None

        if _rel is not None:
            _module_prefix = ".".join(
                list(_rel.parts[:-1]) + [_rel.stem]
            )

            # __all__ membership and noqa for dead-code pre-filtering.
            exported_names = _extract_all_exports_text(content)
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

        if scope_indexed:
            try:
                file_imports = scope_resolver.imports_in_file(py_file)
            except Exception:
                logger.debug("import collection failed for %s", py_file, exc_info=True)
                file_imports = []
            for _local, _mod, _imp_name, _is_star in file_imports:
                if _mod:
                    import_rows.append((content_hash, py_file, _mod))

        if scope_indexed:
            try:
                file_refs = scope_resolver.references_in_file(py_file)
            except Exception:
                logger.debug("reference collection failed for %s", py_file, exc_info=True)
                file_refs = []
            for qn_str, line, col, offset, end_offset, kind, _ann in file_refs:
                ref_rows.append((content_hash, qn_str, py_file, line, col, kind))

        # DSL symbol extraction (SQL, Jinja2, GraphQL, etc.)
        try:
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
        except BUG_EXCEPTIONS:
            raise
        except Exception:
            logger.debug("DSL extraction failed for %s", py_file, exc_info=True)

        # Keep a file's freshness marker and derived rows in one transaction.
        # Do not retain a worker's entire batch before starting disk writes.
        rows = (qn_rows, sym_rows, import_rows, ref_rows, dsl_rows)
        _write_index_rows(conn, *rows)
        row_counts = [count + len(batch) for count, batch in zip(row_counts, rows)]

    return (processed, row_counts[0], skipped, *row_counts[1:])


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
    """Compare the derived index generation with the owner's disk snapshot."""
    import sqlite3 as _sql3
    from .cache import _get_worktree_id, _cache_db_dir
    from .project_iter import _find_project_root
    from emend.analysis_store import AnalysisStore

    result = ManifestScanResult(
        unchanged=[], changed=[], new_files=[], deleted=[],
        git_head_changed=False,
    )

    project_root = _find_project_root(project_path)
    worktree_id = _get_worktree_id(project_root)
    scan_root = Path(project_path).resolve()
    revisions = AnalysisStore.open(project_root).disk_snapshot().files

    def in_scope(file_path: str) -> bool:
        path = Path(file_path)
        return path == scan_root if scan_root.is_file() else path.is_relative_to(scan_root)

    current = {
        revision.file_path: bytes.fromhex(revision.content_hash)
        for revision in revisions
        if in_scope(revision.file_path)
    }

    # Open DB (use provided conn or open fresh)
    close_conn = False
    if conn is None:
        cache_dir = _cache_db_dir(project_root)
        db_path = cache_dir / "parse.db"
        if not db_path.exists():
            # No index at all — everything is new
            result.new_files = list(current)
            return result
        try:
            conn = _sql3.connect(str(db_path), timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            close_conn = True
        except _sql3.Error:
            logger.debug("could not open parse.db for manifest scan", exc_info=True)
            result.new_files = list(current)
            return result

    try:
        # Tier 1: Git HEAD check (scoped to this worktree)
        git_head_key = f"git_head:{worktree_id}"
        import subprocess as _sp
        try:
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
        except (OSError, _sp.SubprocessError, _sql3.Error):
            logger.debug("git HEAD staleness check failed", exc_info=True)

        # Tier 2 + 3: Stat scan + hash verification
        # Load manifest into memory for fast lookup (filtered by worktree)
        manifest = {}
        try:
            for row in conn.execute(
                "SELECT path, content_hash, scope_hash FROM file_manifest "
                "WHERE worktree_id = ?",
                (worktree_id,),
            ).fetchall():
                if in_scope(row[0]):
                    manifest[row[0]] = row[1:]
        except _sql3.Error:
            # Table might not exist yet
            logger.debug("file_manifest read failed; treating all files as new", exc_info=True)
            result.new_files = list(current)
            return result

        result.deleted = list(set(manifest) - set(current))
        for path, content_hash in current.items():
            stored = manifest.get(path)
            if stored is None:
                result.new_files.append(path)
            elif stored == (content_hash, _scope_cache_hash(b"", path, project_root)):
                result.unchanged.append(path)
            else:
                result.changed.append((path, stored[0], content_hash))
    finally:
        if close_conn and conn:
            conn.close()

    return result


def _ensure_index_fresh_impl(
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
    from .cache import _get_worktree_id, _cache_db_dir, _SCHEMA_VERSION
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
    except _sql3.Error:
        logger.debug("could not open parse.db for freshness check", exc_info=True)
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
        except _sql3.Error:
            conn.close()
            return False

        # Check if index tables exist and have data
        try:
            count = conn.execute("SELECT COUNT(*) FROM symbol_index").fetchone()[0]
        except _sql3.Error:
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
            except (OSError, UnicodeDecodeError):
                pass
        for path, old_hash, _new_hash in scan.changed:
            try:
                content = Path(path).read_text()
                files_to_index.append((path, content))
            except (OSError, UnicodeDecodeError):
                continue
            # File path is the owning identity.  Content hashes are revisions,
            # and may intentionally be shared by multiple modules.
            for table, column in (
                ("qn_index", "file_path"),
                ("symbol_index", "file_path"),
                ("import_graph", "file_path"),
                ("reference_index", "file_path"),
                ("dsl_symbols", "host_file"),
            ):
                try:
                    conn.execute(
                        f"DELETE FROM {table} WHERE {column} = ?",
                        (str(Path(path).resolve()),),
                    )
                except _sql3.Error:
                    logger.debug("stale %s cleanup failed", table, exc_info=True)
        if scan.changed:
            conn.commit()

        if files_to_index:
            _src_root = _find_source_root(project_root, language=language)
            _index_batch((str(db_path), _src_root, project_root, files_to_index))
            # Update manifest for re-indexed files
            import os as _os
            now = time.time()
            for py_file, content in files_to_index:
                content_hash = hashlib.sha256(content.encode()).digest()
                resolved = str(Path(py_file).resolve())
                try:
                    st = _os.stat(resolved)
                    conn.execute(
                        "INSERT OR REPLACE INTO file_manifest "
                        "(worktree_id, path, mtime_ns, size, content_hash, indexed_at, scope_hash) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (worktree_id, resolved, st.st_mtime_ns, st.st_size, content_hash, now,
                         _scope_cache_hash(b"", resolved, project_root)),
                    )
                except (OSError, _sql3.Error):
                    logger.debug("manifest update failed for %s", py_file, exc_info=True)
            conn.commit()

        # Clean up deleted files
        for deleted_path in scan.deleted:
            try:
                for table, column in (
                    ("qn_index", "file_path"),
                    ("symbol_index", "file_path"),
                    ("import_graph", "file_path"),
                    ("reference_index", "file_path"),
                    ("dsl_symbols", "host_file"),
                ):
                    conn.execute(
                        f"DELETE FROM {table} WHERE {column} = ?",
                        (str(Path(deleted_path).resolve()),),
                    )
                conn.execute(
                    "DELETE FROM file_manifest WHERE worktree_id = ? AND path = ?",
                    (worktree_id, deleted_path),
                )
            except _sql3.Error:
                logger.debug("deleted-file cleanup failed for %s", deleted_path, exc_info=True)
        if scan.deleted:
            conn.commit()

        conn.close()
        return True
    except BUG_EXCEPTIONS:
        raise
    except Exception:
        logger.debug("inline re-index failed; treating index as stale", exc_info=True)
        try:
            conn.close()
        except _sql3.Error:
            pass
        return False


def _ensure_index_fresh(
    project_path: str = ".",
    *,
    language: str = "python",
    max_inline_reindex: int = 10,
) -> bool:
    """Refresh the legacy parse/search index when needed."""
    return _ensure_index_fresh_impl(
        project_path, language=language,
        max_inline_reindex=max_inline_reindex,
    )


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
        from .venv_index import lookup_venv_symbol
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
    """Query owner-managed completion metadata via CozoDB Datalog."""
    from emend.analysis_store import AnalysisStore
    fdb = AnalysisStore.open(project_root).query_facts().client

    try:
        clauses = [
            "*search_symbol[fp, mqn, name, qn, kind, line, end_line, depth, "
            "parent, sig, returns, decs]"
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
                "(qn == $qname or mqn == $qname or "
                "starts_with(mqn, $qname_prefix))"
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
    except BUG_EXCEPTIONS:
        raise
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
    except ImportError:
        logger.debug("emend.knowledge unavailable; skipping modmap lookup", exc_info=True)
        return []

    try:
        store = MappingStore(project_root)
    except BUG_EXCEPTIONS:
        raise
    except Exception:
        logger.debug("MappingStore init failed; skipping modmap lookup", exc_info=True)
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
            ext = fpath.suffix.lstrip(".") or "py"
            try:
                source = fpath.read_text()
                rust_syms = emend_core.collect_symbols_from_str(source, ext=ext)
            except Exception:
                logger.debug("modmap symbol scan failed for %s", fpath, exc_info=True)
                continue
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
        return results
    except BUG_EXCEPTIONS:
        raise
    except Exception:
        logger.debug("modmap lookup failed for %s", qualified_name, exc_info=True)
        return []
    finally:
        # No BUG_EXCEPTIONS re-raise here: raising from a finally clause
        # would clobber any in-flight exception from the main body.
        try:
            store.close()
        except Exception:
            logger.debug("MappingStore close failed", exc_info=True)


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
    from .project_iter import _find_project_root
    project_root = _find_project_root(project_path)
    from emend.analysis_store import AnalysisStore
    graph = AnalysisStore.open(project_root).query_facts()

    try:
        refs = graph.refs_datalog(target_qn)
        if ref_kind:
            refs = [ref for ref in refs if ref.ref_kind == ref_kind]
        abs_root = str(Path(project_root).resolve())
        return [
            {
                "file_path": str(Path(abs_root) / ref.file_path) if not Path(ref.file_path).is_absolute() else ref.file_path,
                "line": ref.line,
                "col": ref.col,
                "ref_kind": ref.ref_kind,
            }
            for ref in sorted(refs, key=lambda item: (item.file_path, item.line))
        ]
    except BUG_EXCEPTIONS:
        raise
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
    project_root = _find_project_root(project_path)
    from emend.analysis_store import AnalysisStore
    graph = AnalysisStore.open(project_root).query_facts()

    try:
        result = graph.client.run(
            "?[fp] := *import[fp, mod, _, _, _], mod == $mod",
            {"mod": imported_module},
        )
        abs_root = str(Path(project_root).resolve())
        return [
            str(Path(abs_root) / r[0]) if not Path(r[0]).is_absolute() else r[0]
            for r in result["rows"]
        ]
    except BUG_EXCEPTIONS:
        raise
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
    except _sql3.Error:
        logger.debug("could not open parse.db for status check", exc_info=True)
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
            except _sql3.Error:
                # Table might not exist yet
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
    except BUG_EXCEPTIONS:
        raise
    except Exception:
        logger.debug("index status collection failed", exc_info=True)
        try:
            conn.close()
        except _sql3.Error:
            pass
        return None


def warm_caches(
    project_path: str = ".",
    *,
    jobs: int | None = None,
    callback: Callable[[str, str], None] | None = None,
    type_engine: str | None = "pyrefly",
    language: str = "python",
    build_fts: bool = True,
    build_duplicates: bool = False,
) -> dict[str, int | str]:
    """Pre-populate the parse, QN-index, and type caches for all project files.

    Designed to be called from the ``emend index`` CLI command or lazily by
    an analysis operation. Workers populate each file's search index and
    native facts together. The owner caches native results once and streams
    those same objects into a private graph while preparing type inputs.
    Native parsing releases Python; threads avoid serializing fact batches
    across process boundaries. The graph is published only after success.

    Args:
        project_path: Root directory of the project.
        jobs: Max parallelism (defaults to CPU count).
        callback: Called with ``(phase, file_path)`` for progress reporting.
        type_engine: Type inference engine for the type-cache phase.
            Defaults to ``"pyrefly"``. ``"auto"`` detects from project config
            and PATH.
            ``"none"`` or ``None`` skips type indexing entirely.
            Explicit values: ``"pyrefly"``, ``"pyright"``, ``"ty"``.
        build_fts: Rebuild the editor full-text index. Fact-only consumers can
            disable this to return as soon as their analysis data is ready.
        build_duplicates: Explicitly prewarm duplicate-code caches. Disabled
            by default; duplicate queries compute their payloads on demand.
            Use this after detecting that the persisted fact graph is empty or
            invalid while parse/reference caches are still current.

    Returns:
        Dict with stats: ``{"files", "indexed", "qn_cached",
        "type_cached", "type_engine"}``.
    """
    import multiprocessing
    import time
    from concurrent.futures import ThreadPoolExecutor
    from emend import emend_core as _rust
    from .cache import (
        _SCHEMA_VERSION,
        _cache_db_dir,
        _get_worktree_id,
        _init_cache_schema,
    )
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
        "fts_indexed": 0, "dup_cached": 0,
    }

    def announce_phase(label: str) -> None:
        if callback:
            callback("phase", label)

    # Initialize the search cache before starting file workers.
    cache_dir = _cache_db_dir(project_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    _ensure_cache_ignore_files(project_root)
    db_path = str(cache_dir / "parse.db")
    # Pre-create all tables in the main process so workers don't race on schema setup.
    import sqlite3 as _sqlite3
    try:
        _init_conn = _sqlite3.connect(db_path)
        _init_cache_schema(_init_conn)
        _init_conn.close()
    except _sqlite3.Error:
        logger.debug("cache schema pre-creation failed", exc_info=True)

    # Resolve source root once so _index_batch workers can compute module_qn.
    source_root = _find_source_root(project_root, language=language)

    indexed_paths = {str(Path(path).resolve()) for path, _ in file_contents}

    def prepare_file(revision, content):
        if revision.file_path in indexed_paths:
            return _index_batch((db_path, source_root, project_root,
                                 [(revision.file_path, content)]))
        return None

    def prepared_file(revision, result):
        if result is None:
            return
        for key, count in zip(
            ("indexed", "qn_cached", "skipped", "sym_cached",
             "import_cached", "ref_cached", "dsl_cached"), result,
        ):
            stats[key] += count
        if callback:
            callback("index", revision.file_path)

    def finish_search_index():
        # Phase 2.5: Update file_manifest and index_meta with freshness data.
        worktree_id = _get_worktree_id(project_root)
        import os as _os
        try:
            _mf_conn = _sqlite3.connect(db_path, timeout=30)
            _mf_conn.execute("PRAGMA journal_mode=WAL")
            _mf_conn.execute("PRAGMA synchronous=NORMAL")
            now = time.time()
            manifest_rows = []
            for py_file, content in file_contents:
                content_hash = hashlib.sha256(content.encode()).digest()
                try:
                    st = _os.stat(py_file)
                    manifest_rows.append((
                        worktree_id,
                        str(Path(py_file).resolve()),
                        st.st_mtime_ns,
                        st.st_size,
                        content_hash,
                        now,
                        _scope_cache_hash(b"", py_file, project_root),
                    ))
                except OSError:
                    pass
            if manifest_rows:
                _mf_conn.executemany(
                    "INSERT OR REPLACE INTO file_manifest "
                    "(worktree_id, path, mtime_ns, size, content_hash, indexed_at, scope_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    manifest_rows,
                )
            # Update git HEAD (scoped to this worktree)
            git_head_key = f"git_head:{worktree_id}"
            import subprocess as _sp
            try:
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
            except (OSError, _sp.SubprocessError, _sqlite3.Error):
                logger.debug("git HEAD update failed", exc_info=True)
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
        except BUG_EXCEPTIONS:
            raise
        except Exception:
            logger.debug("warm_caches: file_manifest update failed", exc_info=True)

        # Rebuild FTS5 after all search-index rows have been written.
        if build_fts:
            announce_phase("Full-text search index")
            try:
                from emend.editor_search import rebuild_fts as _rebuild_fts

                t_fts = time.monotonic()
                with closing(_sqlite3.connect(db_path, timeout=30)) as _fts_conn:
                    _fts_conn.execute("PRAGMA journal_mode=WAL")
                    _fts_conn.execute("PRAGMA synchronous=NORMAL")
                    fts_count = _rebuild_fts(_fts_conn)
                stats["fts_indexed"] = fts_count
                logger.info(
                    "warm_caches: FTS index rebuilt (%d rows) in %.3fs",
                    fts_count, time.monotonic() - t_fts,
                )
            except BUG_EXCEPTIONS:
                raise
            except Exception as exc:
                logger.debug("warm_caches: FTS rebuild skipped: %s", exc, exc_info=True)
                stats["fts_indexed"] = 0

    # Stream native extraction into type inputs and the unpublished fact graph.
    # Progress callbacks stay on the calling thread.
    # The executor joins even on failure, so no cache writer outlives this call.
    from emend.analysis_store import AnalysisStore
    store = AnalysisStore.open(project_root)
    types_enabled = type_engine and type_engine.lower() != "none"
    if types_enabled:
        from emend.type_oracle import create_type_oracle, TypeEngineUnavailableError

        oracle = create_type_oracle(engine=type_engine, project_root=Path(project_root))
        engine_name = type(oracle).__name__.replace("Adapter", "").lower()
        if not oracle.is_available():
            raise TypeEngineUnavailableError(
                f"Type inference engine '{engine_name}' is not installed or not on PATH. "
                f"Install it (pyrefly, ty, or pyright) and re-run, or pass "
                f"--type-engine=none to skip type indexing."
            )
        paths = [Path(f) for f, _ in file_contents]
        announce_phase("Analysis inputs")
    t_facts = time.monotonic()
    with store.prepare_index_facts(
        paths if types_enabled else None,
        include_overlays=bool(types_enabled and oracle.supports_source_overrides),
        prepare=prepare_file, prepared=prepared_file, jobs=max_workers,
    ) as inputs, ThreadPoolExecutor(max_workers=1) as pool:
        if types_enabled:
            announce_phase(f"Type analysis ({engine_name}) + facts database")
            types = pool.submit(_warm_type_cache, oracle, project_root, paths, inputs)
        else:
            announce_phase("Facts database")
        stats["snapshot_id"] = store.query_facts().snapshot.snapshot_id
        logger.info(
            "warm_caches: source indexes and facts database populated in %.3fs",
            time.monotonic() - t_facts,
        )
        finish_search_index()
        if types_enabled:
            stats.update(types.result())
            if callback:
                for file_path, _ in file_contents:
                    callback("types", file_path)

    # Optional duplicate prewarming. Ordinary indexing leaves this analysis
    # to its consumers, which can compute payloads in memory on demand.
    if build_duplicates:
        from emend.language_registry import detect_language
        announce_phase("Duplicate analysis")
        try:
            t_dup = time.monotonic()
            _compute_duplicate_payloads(db_path, project_root, file_contents)
            stats["dup_cached"] = len(
                [fc for fc in file_contents if detect_language(fc[0]) in {"python", "rust", "typescript"}]
            )
            logger.info(
                "warm_caches: duplicate analysis done in %.3fs",
                time.monotonic() - t_dup,
            )
        except BUG_EXCEPTIONS:
            raise
        except BaseException:
            logger.debug("warm_caches: duplicate analysis failed", exc_info=True)
            stats["dup_cached"] = 0

    return stats


def _warm_type_cache(
    oracle: TypeOracle, project_root: str, paths: list[Path], inputs: TypeBatchInputs,
) -> dict[str, int | str]:
    """Run one oracle and persist its results, without touching CLI state."""
    import time
    engine_name = type(oracle).__name__.replace("Adapter", "").lower()
    t_type = time.monotonic()
    results = oracle.infer_batch(paths, project_root=Path(project_root), inputs=inputs)
    logger.info(
        "warm_caches: type-indexed %d files via %s in %.3fs",
        len(results), engine_name, time.monotonic() - t_type,
    )
    return {"type_cached": len(results), "type_engine": engine_name}


def _ensure_cache_ignore_files(project_root: str) -> None:
    """Compatibility delegate for cache-directory ownership."""
    from emend.analysis_store import AnalysisStore

    AnalysisStore.open(project_root).ensure_cache_directory()


def _compute_duplicate_payloads(
    db_path: str,
    project_root: str,
    file_contents: list[tuple[str, str]],
) -> None:
    """Compute and cache per-file duplicate analysis payloads.

    For each supported file whose grammar/content hash is not already in ``dup_cache``,
    builds canonical subtree + sibling-sequence payloads via
    :mod:`emend.duplicate` and stores the compressed payload in ``parse.db``
    (``dup_cache`` table).

    """
    import pickle
    import sqlite3 as _sqlite3
    import zlib

    from emend.duplicate import DUP_CACHE_VERSION as DUP_VERSION, _duplicate_cache_key
    from emend.language_registry import detect_language

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
    except _sqlite3.Error:
        # Table might not exist yet
        logger.debug("dup_cache read failed; recomputing all payloads", exc_info=True)

    # Filter supported grammars and compute content-addressed cache keys.
    source_files: list[tuple[str, str, str]] = []  # (path, content, content_hash)
    for file_path, content in file_contents:
        if detect_language(file_path) not in {"python", "rust", "typescript"}:
            continue
        content_hash = _duplicate_cache_key(file_path, content)
        source_files.append((file_path, content, content_hash))

    # The payload cache is content-addressed, so if every current source file
    # is present there is no need to rebuild a project-wide scope resolver.
    # On large repositories constructing that resolver dominates warm runs.
    if all(content_hash in cached_hashes for _, _, content_hash in source_files):
        conn.close()
        return

    resolvers = {}
    for file_path, content, _hash in source_files:
        extension = Path(file_path).suffix.lstrip(".")
        if extension not in resolvers:
            resolvers[extension] = _rust.PyScopeResolver(str(Path(project_root).resolve()), extension)
        resolvers[extension].index_file(file_path, content)

    from emend.duplicate import _build_duplicate_payload_for_cache

    for file_path, content, content_hash in source_files:
        if content_hash in cached_hashes:
            continue
        try:
            payload = _build_duplicate_payload_for_cache(
                file_path, content, resolvers[Path(file_path).suffix.lstrip(".")]
            )
            data = zlib.compress(pickle.dumps(payload))
            conn.execute(
                "INSERT OR REPLACE INTO dup_cache (hash, version, data) VALUES (?, ?, ?)",
                (content_hash, DUP_VERSION, data),
            )
        except BUG_EXCEPTIONS:
            raise
        except Exception:
            logger.debug("duplicate payload computation failed for %s", file_path, exc_info=True)
            continue

    conn.commit()
    conn.close()
