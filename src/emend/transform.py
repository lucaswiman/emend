"""Transform engine for extended selectors."""
from __future__ import annotations
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING
import ast
import difflib
import hashlib
import logging
from dataclasses import dataclass
import re
import sys
import io
import json
import time
from .component_selector import ExtendedSelector, parse_extended_selector
from .pattern import (
    parse_pattern,
    compile_pattern_to_rust_ir,
    compile_constraint_to_rust_ir,
    Pattern,
    is_oracle_type_constraint,
    parse_oracle_type_constraint,
)

if TYPE_CHECKING:
    import sqlite3
    from .type_oracle import TypeOracle

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "4"


# ---------------------------------------------------------------------------
# Git worktree support: resolve cache path to main repo
# ---------------------------------------------------------------------------

@lru_cache(maxsize=4)
def _resolve_cache_root(project_root: str) -> Path:
    """Return the main repo root for cache storage.

    In a git worktree, the cache lives in the main repo so all
    worktrees share a single parse.db. Searches upwards for a project root
    marker (.git or .emend). For non-git projects without .emend, returns
    *project_root* unchanged.
    """
    root = Path(project_root).resolve()
    current = root
    while True:
        git_path = current / ".git"
        if git_path.exists():
            if git_path.is_file():
                # Worktree: .git is a file like "gitdir: /main/.git/worktrees/foo"
                try:
                    text = git_path.read_text().strip()
                except OSError:
                    return current
                if text.startswith("gitdir:"):
                    gitdir = Path(text.split(":", 1)[1].strip())
                    if not gitdir.is_absolute():
                        gitdir = (current / gitdir).resolve()
                    # gitdir is e.g. /main/repo/.git/worktrees/my-wt
                    # The commondir file points to the main .git
                    commondir_file = gitdir / "commondir"
                    if commondir_file.is_file():
                        try:
                            commondir = commondir_file.read_text().strip()
                            main_git_dir = (gitdir / commondir).resolve()
                            # main_git_dir is /main/repo/.git → parent is /main/repo
                            return main_git_dir.parent
                        except OSError:
                            pass
            else:
                # Regular git repo
                return current

        if (current / ".emend").is_dir():
            return current

        if current == current.parent:
            break
        current = current.parent

    # Not found — use project_root as-is
    return root


def _cache_db_dir(project_root: str | Path) -> Path:
    """Return the directory for the shared cache DB."""
    main_root = _resolve_cache_root(str(project_root))
    return main_root / ".emend" / "cache"


def _knowledge_db_dir(project_root: str | Path) -> Path:
    """Return the directory for the knowledge DB (non-cache user data).

    Unlike cache data, the knowledge DB contains user-created content
    (notes, identifier mappings, module mappings) that cannot be
    recomputed, so it lives directly in ``.emend/`` rather than
    ``.emend/cache/``.
    """
    main_root = _resolve_cache_root(str(project_root))
    return main_root / ".emend"


@lru_cache(maxsize=4)
def _get_worktree_id(project_root: str) -> str:
    """Return a stable identifier for the current working tree.

    This is the resolved absolute path of *project_root*.  Each worktree
    gets its own manifest rows keyed by this ID, while sharing all
    content-hashed cache data.
    """
    return str(Path(project_root).resolve())


def _init_cache_schema(conn: sqlite3.Connection) -> None:
    """Create all cache tables and indexes if they don't exist (idempotent).

    Called from ``_get_disk_cache()`` (lazy init) and ``warm_caches()``
    (pre-create before spawning workers).  Keeping the DDL in one place
    prevents the two call-sites from drifting out of sync.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS qn_index "
        "(hash BLOB PRIMARY KEY, qnames BLOB)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS type_cache "
        "(hash TEXT PRIMARY KEY, data BLOB)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS index_meta "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS file_manifest ("
        "  worktree_id TEXT NOT NULL DEFAULT '',"
        "  path TEXT NOT NULL,"
        "  mtime_ns INTEGER NOT NULL,"
        "  size INTEGER NOT NULL,"
        "  content_hash BLOB NOT NULL,"
        "  indexed_at REAL NOT NULL,"
        "  PRIMARY KEY (worktree_id, path)"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_manifest_hash "
        "ON file_manifest(content_hash)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS symbol_index ("
        "  content_hash BLOB NOT NULL,"
        "  file_path TEXT NOT NULL,"
        "  name TEXT NOT NULL,"
        "  qualified_name TEXT NOT NULL,"
        "  module_qn TEXT,"
        "  kind TEXT NOT NULL,"
        "  line INTEGER NOT NULL,"
        "  end_line INTEGER NOT NULL,"
        "  depth INTEGER NOT NULL DEFAULT 1,"
        "  parent TEXT,"
        "  signature TEXT,"
        "  returns TEXT,"
        "  decorators TEXT,"
        "  is_entry_point INTEGER NOT NULL DEFAULT 0,"
        "  is_exported INTEGER NOT NULL DEFAULT 0,"
        "  has_noqa INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sym_name ON symbol_index(name)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sym_qn ON symbol_index(qualified_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sym_file ON symbol_index(file_path)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sym_hash ON symbol_index(content_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sym_kind ON symbol_index(kind)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS import_graph ("
        "  content_hash BLOB NOT NULL,"
        "  file_path TEXT NOT NULL,"
        "  imported_module TEXT NOT NULL,"
        "  PRIMARY KEY (content_hash, imported_module)"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_import_module "
        "ON import_graph(imported_module)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_import_hash "
        "ON import_graph(content_hash)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reference_index ("
        "  content_hash BLOB NOT NULL,"
        "  target_qn TEXT NOT NULL,"
        "  file_path TEXT NOT NULL,"
        "  line INTEGER NOT NULL,"
        "  col INTEGER NOT NULL,"
        "  ref_kind TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ref_qn "
        "ON reference_index(target_qn)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ref_file "
        "ON reference_index(file_path)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ref_hash "
        "ON reference_index(content_hash)"
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Disk cache connection (lazy singleton)
# ---------------------------------------------------------------------------
import threading as _threading

_disk_cache_conn: sqlite3.Connection | None = None
_disk_cache_lock = _threading.Lock()
_disk_cache_checked = False


def _get_disk_cache() -> sqlite3.Connection | None:
    """Return a thread-safe SQLite connection for the cache DB, or None."""
    global _disk_cache_conn, _disk_cache_checked
    if _disk_cache_checked:
        return _disk_cache_conn
    with _disk_cache_lock:
        if _disk_cache_checked:
            return _disk_cache_conn
        _disk_cache_checked = True
        try:
            import sqlite3
            root = _find_project_root(".")
            cache_dir = _cache_db_dir(root)
            cache_dir.mkdir(parents=True, exist_ok=True)
            _ensure_cache_ignore_files(root)
            db_path = cache_dir / "parse.db"
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            _init_cache_schema(conn)
            _disk_cache_conn = conn
            logger.debug("disk cache opened at %s", db_path)
        except Exception as exc:
            logger.debug("disk cache unavailable: %s", exc)
            _disk_cache_conn = None
    return _disk_cache_conn


# ---------------------------------------------------------------------------
# Qualified-name index cache: per-file set of QN strings for pre-filtering
# ---------------------------------------------------------------------------
# After the first cross-project operation populates this cache, subsequent
# operations can skip MetadataWrapper for files whose QN set doesn't overlap
# with the target.  Content-hash keyed, persisted in the same SQLite DB.

def _get_cached_qnames(content_hash: bytes) -> set[str] | None:
    """Look up cached qualified-name set for a file by content hash."""
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


def _store_qnames(content_hash: bytes, qnames: set[str]) -> None:
    """Cache a file's qualified-name set (best-effort)."""
    conn = _get_disk_cache()
    if conn is None:
        return
    try:
        import pickle
        import zlib
        data = zlib.compress(
            pickle.dumps(qnames, protocol=pickle.HIGHEST_PROTOCOL), level=1
        )
        with _disk_cache_lock:
            conn.execute(
                "INSERT OR REPLACE INTO qn_index VALUES (?, ?)",
                (content_hash, data),
            )
            conn.commit()
    except Exception:
        pass


_ALL_RE = re.compile(
    r'^__all__\s*=\s*[\[\(](.*?)[\]\)]',
    re.MULTILINE | re.DOTALL,
)
_ALL_NAME_RE = re.compile(r"""['"](\w+)['"]""")


def _extract_all_exports_text(source: str) -> set[str]:
    """Extract names from ``__all__`` using regex (no AST dependency)."""
    m = _ALL_RE.search(source)
    if m is None:
        return set()
    return set(_ALL_NAME_RE.findall(m.group(1)))


_NOQA_RE = re.compile(r'#\s*noqa\b(?:\s*:\s*(.*))?', re.IGNORECASE)


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


def _index_batch(args: tuple[str, str, str, list[tuple[str, str]]]) -> tuple[int, int, int, int, int, int]:
    """Worker function for process-pool indexing.

    Runs in a subprocess.  Parses a batch of files, resolves qualified names,
    collects symbol definitions, import relationships, and reference entries,
    then writes directly to the SQLite disk cache.

    Files whose content hash is already present in all cache tables are
    skipped (cache-hit fast path).

    Args:
        args: (db_path, source_root, project_root, [(file_path, content), ...])

    Returns:
        (parse_count, qn_count, skipped_count, sym_count, import_count, ref_count).
    """
    import pickle
    import sqlite3
    import zlib
    from .query import _collect_symbols as _collect_symbols_ts
    from emend import emend_core as _rust

    db_path, source_root, project_root, file_batch = args
    qn_rows: list[tuple[bytes, bytes]] = []
    sym_rows: list[tuple] = []
    import_rows: list[tuple[bytes, str, str]] = []
    ref_rows: list[tuple] = []

    if not file_batch:
        return (0, 0, 0, 0, 0, 0)

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

        if need_import:
            try:
                # Use a lightweight regex-based import extraction
                # (avoids importing the Rust module in subprocesses)
                import_re = re.compile(
                    r'^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))',
                    re.MULTILINE,
                )
                for m_match in import_re.finditer(content):
                    mod = m_match.group(1) or m_match.group(2)
                    if mod:
                        import_rows.append((content_hash, py_file, mod))
            except Exception:
                pass

        if need_ref and scope_indexed:
            try:
                for qn_str, line, col, offset, end_offset, kind in scope_resolver.references_in_file(py_file):
                    ref_rows.append((content_hash, qn_str, py_file, line, col, kind))
            except Exception:
                pass

    # Bulk-write to SQLite from this worker process.
    # WAL mode allows concurrent readers/writers across processes.
    has_data = qn_rows or sym_rows or import_rows or ref_rows
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
                    "line, end_line, depth, parent, signature, returns, decorators, "
                    "is_entry_point, is_exported, has_noqa) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            conn.commit()
            conn.close()
        except Exception:
            pass

    return (processed, len(qn_rows), skipped,
            len(sym_rows), len(import_rows), len(ref_rows))


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
) -> bool:
    """Lightweight freshness check for the index.

    If the index is fresh, returns True immediately.
    If a small number of files changed, re-indexes them inline and returns True.
    If many files changed or no index exists, returns False (caller should
    fall back to cold path or suggest ``emend index``).
    """
    import sqlite3 as _sql3

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
            _src_root = _find_python_source_root(project_root)
            _index_batch((str(db_path), _src_root, project_root, files_to_index))
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
) -> list[dict] | None:
    """Query the symbol_index table directly for fast symbol lookup.

    Returns a list of dicts with symbol info, or None if the index
    is not available or not fresh.
    """
    import sqlite3 as _sql3

    if not _ensure_index_fresh(project_path):
        return None

    project_root = _find_project_root(project_path)
    cache_dir = _cache_db_dir(project_root)
    db_path = cache_dir / "parse.db"

    try:
        conn = _sql3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        return None

    try:
        conditions = []
        params: list = []

        if name_pattern:
            if "*" in name_pattern or "?" in name_pattern:
                # Convert glob to SQL GLOB
                conditions.append("name GLOB ?")
                params.append(name_pattern)
            else:
                conditions.append("name = ?")
                params.append(name_pattern)

        if kind:
            conditions.append("kind = ?")
            params.append(kind)

        if file_path:
            resolved = str(Path(file_path).resolve())
            conditions.append("file_path = ?")
            params.append(resolved)

        if qualified_name:
            # Match against both qualified_name and module_qn (the
            # fully-qualified module path).  This mirrors the logic in
            # lookup_venv_symbol and allows lookups like
            # "common.db.models.Foo" to find a symbol whose module_qn
            # is "common.db.models" and name is "Foo".
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
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
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
        from emend.knowledge import KnowledgeBase
    except Exception:
        return []

    try:
        kb = KnowledgeBase(project_root)
    except Exception:
        return []

    try:
        resolved = kb.resolve_module_to_path(qualified_name)
        if resolved is None:
            return []

        resolved_path = Path(resolved)

        # Determine the symbol name to search for: the part of the
        # qualified name after the module mapping prefix.
        mm = kb.resolve_module(qualified_name)
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
            kb.close()
        except Exception:
            pass


def _venv_db_path(project_root: str) -> Path:
    """Return the path to the venv-specific parse cache DB."""
    return _cache_db_dir(project_root) / "parse_venv.db"


def _ensure_venv_index(project_root: str, language: str = "python") -> Path | None:
    """Build or refresh the venv symbol index.

    Creates ``parse_venv.db`` in ``.emend/cache/`` with the same
    ``symbol_index`` schema as the project cache.  The index is rebuilt
    when the site-packages directory's mtime changes.

    Returns the DB path, or ``None`` if venv lookup is disabled / no venv.
    """
    import sqlite3 as _sql3

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
) -> list[dict] | None:
    """Query the reference_index table for fast find-references.

    Returns a list of dicts with reference info, or None if the index
    is not available or not fresh.
    """
    import sqlite3 as _sql3

    if not _ensure_index_fresh(project_path):
        return None

    project_root = _find_project_root(project_path)
    cache_dir = _cache_db_dir(project_root)
    db_path = cache_dir / "parse.db"

    try:
        conn = _sql3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        return None

    try:
        if ref_kind:
            rows = conn.execute(
                "SELECT file_path, line, col, ref_kind FROM reference_index "
                "WHERE target_qn = ? AND ref_kind = ? "
                "ORDER BY file_path, line",
                (target_qn, ref_kind),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT file_path, line, col, ref_kind FROM reference_index "
                "WHERE target_qn = ? ORDER BY file_path, line",
                (target_qn,),
            ).fetchall()
        results = []
        for row in rows:
            results.append({
                "file_path": row[0],
                "line": row[1],
                "col": row[2],
                "ref_kind": row[3],
            })
        conn.close()
        return results
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return None


def query_import_graph(
    project_path: str,
    imported_module: str,
) -> list[str] | None:
    """Query the import_graph for files importing a module.

    Returns file paths, or None if index not available.
    """
    import sqlite3 as _sql3

    project_root = _find_project_root(project_path)
    cache_dir = _cache_db_dir(project_root)
    db_path = cache_dir / "parse.db"
    if not db_path.exists():
        return None

    try:
        conn = _sql3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT DISTINCT file_path FROM import_graph "
            "WHERE imported_module = ?",
            (imported_module,),
        ).fetchall()
        conn.close()
        return [row[0] for row in rows]
    except Exception:
        return None


def get_index_status(project_path: str) -> dict | None:
    """Return index freshness stats, or None if no index exists."""
    import sqlite3 as _sql3

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

        # Index metadata
        for row in conn.execute("SELECT key, value FROM index_meta").fetchall():
            info[row[0]] = row[1]

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
    type_engine: str | None = "auto",
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
    from concurrent.futures import ProcessPoolExecutor

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
        "type_cached": 0, "type_engine": "",
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
    source_root = _find_source_root(project_root)

    # Split files into batches — one batch per worker.
    batch_size = max(1, len(file_contents) // max_workers)
    batches: list[tuple[str, str, str, list[tuple[str, str]]]] = []
    for i in range(0, len(file_contents), batch_size):
        chunk = file_contents[i : i + batch_size]
        batches.append((db_path, source_root, project_root, chunk))

    t0 = time.monotonic()
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        # TODO: Conditionally use ProcessPoolExecutor or ThreadPoolExecutor for GIL-python vs free-threaded.
        for batch_idx, (parse_n, qn_n, skip_n, sym_n, import_n, ref_n) in enumerate(
            executor.map(_index_batch, batches)
        ):
            stats["indexed"] += parse_n
            stats["qn_cached"] += qn_n
            stats["skipped"] += skip_n
            stats["sym_cached"] += sym_n
            stats["import_cached"] += import_n
            stats["ref_cached"] += ref_n
            # Report progress for all files in this batch
            if callback:
                _db_path, _src, _proj, chunk = batches[batch_idx]
                for py_file, _content in chunk:
                    callback("index", py_file)

    logger.info(
        "warm_caches: indexed %d files in %.3fs (parse=%d, qn=%d, sym=%d, import=%d, ref=%d)",
        stats["files"], time.monotonic() - t0,
        stats["indexed"], stats["qn_cached"],
        stats["sym_cached"], stats["import_cached"], stats["ref_cached"],
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

    return stats


def _ensure_cache_ignore_files(project_root: str) -> None:
    """Create .gitignore and .dockerignore in the cache directory."""
    cache_dir = _cache_db_dir(project_root)
    if not cache_dir.is_dir():
        return
    gitignore = cache_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("# Auto-generated by emend index\n*\n")
    dockerignore = cache_dir / ".dockerignore"
    if not dockerignore.exists():
        dockerignore.write_text("# Auto-generated by emend index\n*\n")


# ---------------------------------------------------------------------------
# Rust accelerator (bundled with the emend wheel via maturin)
# ---------------------------------------------------------------------------
from emend import emend_core as _rust

_METAVAR_RE = re.compile(r'\$(?:\.\.\.)?[A-Z_][A-Z_0-9]*')


def _ext_from_path(file_path: str | Path) -> str:
    """Return the file extension (without dot) for passing to emend_core functions."""
    return Path(file_path).suffix.lstrip('.') or 'py'


def extract_pattern_literals(pattern_str: str) -> list[str]:
    """Extract literal identifier tokens from a pattern string for pre-filtering.

    For a pattern like "$X.objects.filter($...ARGS)", returns ["objects", "filter"].
    These can be used with Rust filter_files_by_content to quickly eliminate files
    that cannot possibly match the pattern.
    """
    # Remove metavariables
    cleaned = _METAVAR_RE.sub('', pattern_str)
    # Extract identifier-like tokens (Python identifiers)
    tokens = re.findall(r'[a-zA-Z_][a-zA-Z_0-9]*', cleaned)
    # Filter out Python keywords and very short tokens that would match too broadly
    _PY_KEYWORDS = {'if', 'else', 'elif', 'for', 'while', 'try', 'except',
                    'finally', 'with', 'as', 'import', 'from', 'class', 'def',
                    'return', 'yield', 'raise', 'pass', 'break', 'continue',
                    'and', 'or', 'not', 'in', 'is', 'True', 'False', 'None',
                    'lambda', 'global', 'nonlocal', 'del', 'assert', 'async',
                    'await'}
    return [t for t in tokens if t not in _PY_KEYWORDS and len(t) > 1]


@dataclass
class ProjectPatternMatch:
    """A pattern match paired with its originating file path."""
    file_path: str
    match: PatternMatch


def find_pattern_in_project(
    pattern_str: str,
    file_paths: list[str],
    *,
    scope: list[str] | None = None,
    inside: str | None = None,
    not_inside: str | None = None,
    imported_from: str | None = None,
    scope_local: bool = False,
    type_oracle: TypeOracle | None = None,
    index_conn: sqlite3.Connection | None = None,
    limit: int | None = None,
    language: str = "python",
) -> list[ProjectPatternMatch]:
    """Search for a pattern across multiple files.

    Four-stage pipeline, each stage reducing the file set:

    1. **Index prefilter** (optional) — if *index_conn* is provided,
       query ``reference_index`` / ``symbol_index`` for files that
       mention the pattern's literal identifiers.
    2. **Rust string-contains filter** — ``read_and_filter_files``
       drops files whose text doesn't contain every required literal.
    3. **Rust tree-sitter batch** — if the pattern compiles to Rust IR
       and no advanced constraints are active, match all files at once
       in Rust.
    4. **Pattern matching fallback** — parse and match remaining files
       in parallel via ``ThreadPoolExecutor``.

    Returns a list of ``ProjectPatternMatch`` (file_path + match).
    """
    # Validate constraints eagerly so callers see errors immediately.
    if inside and not_inside:
        raise ValueError("Cannot specify both 'inside' and 'not_inside' parameters")

    is_single_file = len(file_paths) == 1

    literals = extract_pattern_literals(pattern_str)

    # --- Stage 1: index prefilter ---
    if literals and index_conn is not None and not is_single_file:
        candidate_set = _index_prefilter(literals, index_conn)
        if candidate_set is not None:
            before = len(file_paths)
            file_paths = [f for f in file_paths if f in candidate_set]
            logger.debug(
                "index prefilter: %d → %d files", before, len(file_paths),
            )
            if not file_paths:
                return []

    # --- Stage 2: Rust string-contains filter ---
    if literals and len(file_paths) > 1:
        try:
            file_contents: list[tuple[str, str]] = _rust.read_and_filter_files(
                file_paths, literals,
            )
        except Exception:
            file_contents = _read_and_filter_py(file_paths, literals)
    else:
        file_contents = []
        for fp in file_paths:
            try:
                file_contents.append((fp, Path(fp).read_text()))
            except OSError:
                # For single-file requests, propagate not-found so callers
                # can report a meaningful error.
                if is_single_file:
                    raise FileNotFoundError(f"File not found: {fp}")
                pass

    logger.debug(
        "string-contains filter: %d files surviving", len(file_contents),
    )

    if not file_contents:
        return []

    # --- Stage 3: Rust batch fast-path ---
    has_constraints = (
        scope is not None
        or imported_from is not None
        or scope_local
        or type_oracle is not None
    )

    if not has_constraints:
        from emend.pattern import (
            compile_pattern_to_rust_ir,
            compile_constraint_to_rust_ir,
        )

        pattern_ir = compile_pattern_to_rust_ir(pattern_str, language=language)
        if pattern_ir is not None:
            inside_ir = (
                compile_constraint_to_rust_ir(inside, language=language) if inside else None
            )
            not_inside_ir = (
                compile_constraint_to_rust_ir(not_inside, language=language)
                if not_inside
                else None
            )
            if (inside is None or inside_ir is not None) and (
                not_inside is None or not_inside_ir is not None
            ):
                try:
                    raw = _rust.find_pattern_in_files(
                        list(file_contents), pattern_ir,
                        inside_ir, not_inside_ir,
                    )
                    results = [
                        ProjectPatternMatch(
                            file_path=fp,
                            match=PatternMatch(
                                node=None, captures={},
                                line=line, end_line=end_line,
                                col=col, end_col=end_col,
                                matched_text=text,
                            ),
                        )
                        for fp, line, col, end_line, end_col, text in raw
                    ]
                    if limit is not None:
                        results = results[:limit]
                    return results
                except Exception:
                    logger.debug("Rust batch path failed, falling back")

    # --- Stage 4: Pattern matching fallback (parallel) ---
    results: list[ProjectPatternMatch] = []

    if is_single_file:
        # Single file: call directly so errors propagate to caller.
        fp, content = file_contents[0]
        matches = find_pattern(
            pattern_str, fp,
            scope=scope, inside=inside, not_inside=not_inside,
            imported_from=imported_from, scope_local=scope_local,
            source_override=content, type_oracle=type_oracle,
            language=language,
        )
        results = [ProjectPatternMatch(file_path=fp, match=m) for m in matches]
        if limit is not None:
            results = results[:limit]
    else:
        from concurrent.futures import ThreadPoolExecutor

        def _find_one(args: tuple[str, str]) -> list[ProjectPatternMatch]:
            fp, content = args
            try:
                matches = find_pattern(
                    pattern_str, fp,
                    scope=scope, inside=inside, not_inside=not_inside,
                    imported_from=imported_from, scope_local=scope_local,
                    source_override=content, type_oracle=type_oracle,
                    language=language,
                )
                return [ProjectPatternMatch(file_path=fp, match=m) for m in matches]
            except Exception:
                return []

        with ThreadPoolExecutor() as executor:
            for batch in executor.map(_find_one, file_contents):
                results.extend(batch)
                if limit is not None and len(results) >= limit:
                    results = results[:limit]
                    break

    return results


def _index_prefilter(
    literals: list[str],
    conn: sqlite3.Connection,
) -> set[str] | None:
    """Query the index for files likely to contain *literals*.

    Returns a set of file paths, or ``None`` if the index has no useful
    data (caller should skip this stage).
    """
    per_literal: list[set[str]] = []
    for lit in literals:
        files_for_lit: set[str] = set()
        try:
            for (fp,) in conn.execute(
                "SELECT DISTINCT file_path FROM reference_index "
                "WHERE target_qn LIKE ?",
                ("%" + lit + "%",),
            ):
                files_for_lit.add(fp)
        except Exception:
            pass
        try:
            for (fp,) in conn.execute(
                "SELECT DISTINCT file_path FROM symbol_index "
                "WHERE name = ? OR qualified_name LIKE ?",
                (lit, "%" + lit + "%"),
            ):
                files_for_lit.add(fp)
        except Exception:
            pass
        if files_for_lit:
            per_literal.append(files_for_lit)

    if not per_literal:
        return None

    candidates = per_literal[0]
    for s in per_literal[1:]:
        candidates &= s
    return candidates


def _read_and_filter_py(
    file_paths: list[str], literals: list[str],
) -> list[tuple[str, str]]:
    """Pure-Python fallback for Rust ``read_and_filter_files``."""
    results: list[tuple[str, str]] = []
    for fp in file_paths:
        try:
            content = Path(fp).read_text()
            if all(lit in content for lit in literals):
                results.append((fp, content))
        except Exception:
            pass
    return results


# Helper functions for cross-project operations

def _find_project_root(start_path: str) -> str:
    """Find project root by looking for markers."""
    path = Path(start_path).resolve()
    if path.is_file():
        path = path.parent

    markers = ['.git', 'pyproject.toml', 'setup.py', 'setup.cfg']

    current = path
    while current != current.parent:
        for marker in markers:
            if (current / marker).exists():
                return str(current)
        current = current.parent

    return str(path)


@lru_cache(maxsize=64)
def _find_source_root(project_root: str, language: str = "python") -> str:
    """Find the source root directory for a project.

    Detects ``src/`` layout by checking (in order):
    1. ``pyproject.toml`` settings (maturin, setuptools, hatch)
    2. ``setup.cfg`` [options] package_dir
    3. Heuristic: ``src/`` exists and contains a package (dir with ``__init__.py``)

    Returns the resolved source root (e.g. ``/repo/src``), or the
    project root itself if no ``src/`` layout is detected.
    """
    root = Path(project_root).resolve()

    if language == "python":
        # --- pyproject.toml -------------------------------------------------
        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            try:
                import tomllib
            except ModuleNotFoundError:          # Python < 3.11
                try:
                    import tomli as tomllib      # type: ignore[no-redef]
                except ModuleNotFoundError:
                    tomllib = None               # type: ignore[assignment]
            if tomllib is not None:
                try:
                    data = tomllib.loads(pyproject.read_text())
                    # maturin: python-source = "src"
                    ps = (data.get("tool", {}).get("maturin", {})
                          .get("python-source"))
                    if ps:
                        candidate = root / ps
                        if candidate.is_dir():
                            return str(candidate)
                    # setuptools: [tool.setuptools.packages.find] where = ["src"]
                    where = (data.get("tool", {}).get("setuptools", {})
                             .get("packages", {}).get("find", {}).get("where"))
                    if isinstance(where, list) and where:
                        candidate = root / where[0]
                        if candidate.is_dir():
                            return str(candidate)
                    # hatch / hatchling
                    where = (data.get("tool", {}).get("hatch", {})
                             .get("build", {}).get("sources", {}).get("src"))
                    if isinstance(where, str):
                        candidate = root / where
                        if candidate.is_dir():
                            return str(candidate)
                except Exception:
                    pass

        # --- setup.cfg ------------------------------------------------------
        setup_cfg = root / "setup.cfg"
        if setup_cfg.is_file():
            try:
                import configparser
                cfg = configparser.ConfigParser()
                cfg.read(str(setup_cfg))
                pkg_dir = cfg.get("options", "package_dir", fallback=None)
                if pkg_dir:
                    # Format: "= src" or "\n= src"
                    for part in pkg_dir.splitlines():
                        part = part.strip()
                        if part.startswith("="):
                            src_dir = part[1:].strip()
                            candidate = root / src_dir
                            if candidate.is_dir():
                                return str(candidate)
            except Exception:
                pass

        # --- Heuristic: src/ with an __init__.py package --------------------
        src_dir = root / "src"
        if src_dir.is_dir():
            for child in src_dir.iterdir():
                if child.is_dir() and (child / "__init__.py").is_file():
                    return str(src_dir)

    else:
        # Generic heuristic for other languages: src/ exists
        src_dir = root / "src"
        if src_dir.is_dir():
            return str(src_dir)

    return str(root)


def _file_to_module(file_path: str, project_path: str | None) -> str:
    """Convert file path to module name.

    Detects ``src/`` layout automatically so that
    ``src/pkg/mod.py`` becomes ``pkg.mod`` rather than ``src.pkg.mod``.
    Uses the language-specific separator from config.toml.
    """
    from emend.language_registry import detect_language, get_module_separator
    language = detect_language(file_path) or "python"
    sep = get_module_separator(language)

    abs_file = Path(file_path).resolve()
    proj_root = Path(project_path or _find_project_root(file_path)).resolve()
    source_root = Path(_find_source_root(str(proj_root), language=language))

    # Use the source root if the file lives under it; otherwise fall
    # back to the project root (e.g. for test files outside src/).
    try:
        rel_path = abs_file.relative_to(source_root)
    except ValueError:
        rel_path = abs_file.relative_to(proj_root)

    module_parts = list(rel_path.parts[:-1]) + [rel_path.stem]
    return sep.join(module_parts)


# Non-dot directories to skip.  All directories starting with '.' are
# skipped automatically by the Rust scanner (emend_core.collect_python_files).
# The canonical list lives in Rust (scanner.rs); we import it here so
# Python and Rust always agree.
_SKIP_DIRS = frozenset(_rust.skip_dirs())

# Module-level file-list cache: maps (resolved project root, language) to (mtime_ns, file_list)
_file_list_cache: dict[tuple[str, str], tuple[int, list[str]]] = {}


def _collect_source_files_scandir(root_path: str, language: str = "python") -> list[str]:
    """Walk a directory tree using the Rust emend_core module."""
    from emend.language_registry import get_extensions
    exts = get_extensions(language)
    return _rust.collect_files(root_path, exts)


def _collect_git_tracked_source_files(project_root: str, language: str = "python") -> list[str] | None:
    """Return git-tracked source files, or None if not in a git repo."""
    import subprocess
    from emend.language_registry import get_extensions
    exts = get_extensions(language)

    resolved = str(Path(project_root).resolve())
    try:
        pathspecs = [f"*.{ext}" for ext in exts]
        result = subprocess.run(
            ['git', 'ls-files', '-z'] + pathspecs,
            capture_output=True, timeout=10,
            cwd=resolved,
        )
        if result.returncode != 0:
            return None
        raw = result.stdout
        if not raw:
            return []
        # git ls-files -z uses null separators; paths are relative to cwd
        rel_paths = raw.decode('utf-8', errors='replace').split('\0')
        abs_paths = []
        for p in rel_paths:
            p = p.strip()
            if p:
                abs_paths.append(str(Path(resolved) / p))
        return abs_paths
    except Exception:
        return None


def _collect_source_files(project_root: str, language: str = "python", git_tracked_only: bool = False) -> list[str]:
    """Collect all source files for *language* in project, with caching.

    Uses os.scandir for speed. Caches the file list per project root,
    invalidated when the root directory's mtime changes (which happens
    when files are added or removed).

    If *git_tracked_only* is True, uses ``git ls-files`` to only return
    files tracked by git.  Falls back to directory scan if not in a
    git repository.
    """
    if git_tracked_only:
        tracked = _collect_git_tracked_source_files(project_root, language=language)
        if tracked is not None:
            logger.info("collect_source_files: %d git-tracked files in %s", len(tracked), project_root)
            return tracked

    import os
    resolved = str(Path(project_root).resolve())
    try:
        root_mtime = os.stat(resolved).st_mtime_ns
    except OSError:
        t0 = time.monotonic()
        files = _collect_source_files_scandir(resolved, language=language)
        logger.info("collect_source_files: %d files in %.3fs (scandir, %s)", len(files), time.monotonic() - t0, resolved)
        return files

    cache_key = (resolved, language)
    cached = _file_list_cache.get(cache_key)
    if cached is not None and cached[0] == root_mtime:
        logger.debug("collect_source_files: %d files (cached, %s)", len(cached[1]), resolved)
        return cached[1]

    t0 = time.monotonic()
    files = _collect_source_files_scandir(resolved, language=language)
    logger.info("collect_source_files: %d files in %.3fs (%s)", len(files), time.monotonic() - t0, resolved)
    _file_list_cache[cache_key] = (root_mtime, files)
    return files


def _files_importing_module(project_root: str, module_dotted: str, language: str = "python") -> set[str] | None:
    """Return the set of files that import from *module_dotted*, or None if unknown.

    First tries the cached import_graph (instant).  Falls back to the Rust
    targeted import filter which text-prefilters then tree-sitter-parses
    only candidate files.

    Returns None if the filter cannot be applied (caller should fall back
    to scanning all files).
    """
    # Fast path: try cached import graph
    cached = query_import_graph(project_root, module_dotted)
    if cached is not None:
        return set(cached) if cached else set()

    source_files = _collect_source_files(project_root, language=language)
    try:
        matching = _rust.files_importing_module(source_files, module_dotted)
        return set(matching)
    except Exception:
        return None


def prefilter_files_structural(files: list[str], name: str) -> list[str]:
    """Structural pre-filter: use tree-sitter to find files containing
    an actual identifier matching name (not just substring in strings/comments).
    """
    matches = _rust.find_name_in_files(files, name)
    return list({m.file for m in matches})


def visit_project_ts(
    name_hint: str,
    project_path: str,
    target_file: str | None = None,
    candidate_files: set[str] | None = None,
    target_qnames: set[str] | None = None,
    language: str = "python",
) -> Iterator[tuple[str, str, _rust.PyScopeResolver]]:
    """Iterate over source files using tree-sitter + PyScopeResolver.

    Yields (file_path, content, resolver).
    The same resolver instance is used for all files in the batch.
    """
    t_start = time.monotonic()
    project_root = project_path
    source_files = _collect_source_files(project_root, language=language)

    if candidate_files is not None:
        source_files = [f for f in source_files
                        if f in candidate_files
                        or (target_file and str(Path(f).resolve()) == target_file)]

    # Structural pre-filter
    if name_hint:
        source_files = prefilter_files_structural(source_files, name_hint)
        if target_file and target_file not in source_files:
            source_files.append(target_file)

    # Read and filter files
    file_contents = _rust.read_and_filter_files(source_files, [name_hint] if name_hint else [])

    # QN-index pre-filter
    if target_qnames:
        filtered_contents = []
        for py_file, content in file_contents:
            if target_file and str(Path(py_file).resolve()) == target_file:
                filtered_contents.append((py_file, content))
                continue

            content_hash = hashlib.md5(
                content.encode(), usedforsecurity=False
            ).digest()
            cached_qns = _get_cached_qnames(content_hash)
            if cached_qns is not None:
                if not target_qnames.intersection(cached_qns):
                    continue
            filtered_contents.append((py_file, content))
        file_contents = filtered_contents

    # Index and yield
    for py_file, content in file_contents:
        try:
            ext = Path(py_file).suffix.lstrip('.')
            resolver = _rust.PyScopeResolver(project_root, ext)
            resolver.index_file(py_file, content)
            yield py_file, content, resolver
        except Exception:
            continue

    logger.info("visit_project_ts: finished in %.3fs", time.monotonic() - t_start)


def _get_imports(source_code: str) -> str:
    """Extract all top-level import statements as a single string."""
    from emend.language_plugins import load_plugin
    return load_plugin("python").import_handler.extract_imports(source_code)


def _add_import_text(
    import_str: str,
    position: int,
    file_path: Path,
    apply: bool,
    source_code: str
) -> str:
    """Add an import statement to a file using text manipulation.

    Args:
        import_str: Import statement to add (e.g., "import os")
        position: 0 for prepend, -1 for append
        file_path: Path to the file
        apply: Whether to apply changes
        source_code: Original source code

    Returns:
        Unified diff showing changes
    """
    from emend.language_plugins import load_plugin
    try:
        new_code = load_plugin("python").import_handler.add_import_text(
            import_str, position, source_code
        )
    except SyntaxError:
        raise ValueError(f"Cannot parse {file_path}")

    diff = _generate_diff(str(file_path), source_code, new_code)

    if apply:
        file_path.write_text(new_code)

    return diff


def get_component(selector: ExtendedSelector) -> str:
    """Get value of component.

    Args:
        selector: Extended selector with component specified

    Returns:
        String representation of the component value

    Example:
        >>> sel = parse_extended_selector("file.py::func[params]")
        >>> get_component(sel)
        'ctx, request'

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If symbol not found, invalid component for symbol type,
                   or accessor not found
    """
    # Read file
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    source_code = file_path.read_text()

    # Handle module-level components (empty symbol_path)
    if not selector.symbol_path:
        if selector.component == "imports":
            return _get_imports(source_code)
        else:
            raise ValueError(f"Component '{selector.component}' requires a symbol path")

    # Get the range for the component using Rust accelerator
    _ext = _ext_from_path(selector.file_path)
    range_info = _rust.get_symbol_component_range(
        source_code,
        selector.symbol_path,
        selector.component,
        selector.accessor,
        ext=_ext,
    )

    if range_info is None:
        # Check if symbol exists at all
        syms = _rust.collect_symbols_from_str(source_code, selector=".".join(selector.symbol_path), ext=_ext)
        if not syms:
             raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

        # Symbol exists but component not found
        kind = syms[0]["kind"]
        if kind == "class" and selector.component in ("params", "returns"):
            raise ValueError(f"Component '{selector.component}' not valid for ClassDef")
        elif kind in ("function", "async_function", "method", "async_method") and selector.component == "bases":
            raise ValueError(f"Component '{selector.component}' not valid for FunctionDef")

        raise ValueError(f"Component '{selector.component}' not found or not valid for symbol {'.'.join(selector.symbol_path)}")

    start_byte, end_byte = range_info

    # For returns, Rust returns an insertion point if it's not there.
    # get_component should raise error if it's truly not there.
    if selector.component == "returns" and start_byte == end_byte:
         raise ValueError(f"Function {'.'.join(selector.symbol_path)} has no return annotation")

    result = source_code.encode('utf-8')[start_byte:end_byte].decode('utf-8')

    if selector.component == "returns":
        # Robustly remove -> and whitespace
        return result.strip().lstrip("->").strip()
    elif selector.component == "body":
        return result.strip('\n').rstrip()
    
    return result.strip()


def _generate_diff(file_path: str, old_code: str, new_code: str) -> str:
    """Generate unified diff string."""
    old_lines = old_code.splitlines(keepends=True)
    new_lines = new_code.splitlines(keepends=True)
    return ''.join(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=file_path,
        tofile=file_path
    ))


def set_component(selector: ExtendedSelector, value: str, apply: bool = False) -> str:
    """Set value of component. Returns diff."""
    # Read file
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    source_code = file_path.read_text()

    # Get the range for the component using Rust accelerator
    _ext = _ext_from_path(selector.file_path)
    range_info = _rust.get_symbol_component_range(
        source_code,
        selector.symbol_path,
        selector.component,
        selector.accessor,
        ext=_ext,
    )

    if range_info is None:
        # Check if symbol exists at all
        syms = _rust.collect_symbols_from_str(source_code, selector=".".join(selector.symbol_path), ext=_ext)
        if not syms:
             raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

        # Match old error messages for invalid components
        kind = syms[0]["kind"]
        if kind == "class" and selector.component in ("params", "returns"):
            raise ValueError(f"Component '{selector.component}' not valid for ClassDef")
        elif kind in ("function", "async_function", "method", "async_method") and selector.component == "bases":
            raise ValueError(f"Component '{selector.component}' not valid for FunctionDef")

        raise ValueError(f"Component '{selector.component}' not found or not valid for symbol {'.'.join(selector.symbol_path)}")

    start_byte, end_byte = range_info

    # Prepare the replacement value
    replacement = value
    if selector.component == "returns" and value.strip() and not value.strip().startswith("->"):
        replacement = f" -> {value.strip()}"
    elif selector.component == "decorators" and value.strip() and not value.strip().startswith("@"):
        # If it's a single decorator without @, add it
        if "\n" not in value.strip():
            replacement = f"@{value.strip()}"
    elif selector.component == "body":
        # Ensure it starts with a newline and is indented if it's a block
        if not value.startswith("\n"):
            # Simple heuristic: find indentation of the def/class line
            # or just assume 4 spaces
            replacement = "\n    " + value.strip().replace("\n", "\n    ")

    # Apply transformation using Rust FileTransform
    transform = _rust.PyFileTransform(source_code)
    transform.replace_range(start_byte, end_byte, replacement)
    
    new_code = transform.apply()
    if new_code is None:
        raise RuntimeError("Failed to apply transformation (overlapping edits)")

    # Generate diff
    diff = _generate_diff(selector.file_path, source_code, new_code)

    # Apply changes if requested
    if apply:
        file_path.write_text(new_code)

    return diff


def add_to_component(
    selector: ExtendedSelector,
    value: str,
    position: int = -1,
    before: str | None = None,
    after: str | None = None,
    apply: bool = False,
    kind: str | None = None
) -> str:
    """Add item to list component. Returns diff."""
    # Validate mutually exclusive position options
    if before is not None and after is not None:
        raise ValueError("Cannot specify both --before and --after")

    # Validate that component is a list type
    if selector.component not in ("params", "decorators", "bases", "imports"):
        raise ValueError(f"Component '{selector.component}' is not a list component")

    # Validate that accessor is None
    if selector.accessor is not None:
        raise ValueError("add_to_component requires accessor must be None")

    # Validate kind parameter
    if kind is not None:
        if selector.component != "params":
            raise ValueError("'kind' parameter can only be used with 'params' component")
        if kind not in ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY"):
            raise ValueError(f"Invalid kind value: {kind}. Must be one of: POSITIONAL_ONLY, POSITIONAL_OR_KEYWORD, KEYWORD_ONLY")

    # Read file
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    source_code = file_path.read_text()

    # Handle module-level imports component
    if selector.component == "imports" and not selector.symbol_path:
        return _add_import_text(value, position, file_path, apply, source_code)

    # Get items and their ranges
    _ext = _ext_from_path(selector.file_path)
    items_info = _rust.get_symbol_component_list_items(
        source_code,
        selector.symbol_path,
        selector.component,
        ext=_ext,
    )

    if items_info is None:
        # Check if symbol exists at all
        syms = _rust.collect_symbols_from_str(source_code, selector=".".join(selector.symbol_path), ext=_ext)
        if not syms:
             raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")
        
        # Symbol exists but component not found
        sym_kind = syms[0]["kind"]
        if sym_kind == "class" and selector.component == "params":
            raise ValueError(f"Component '{selector.component}' not valid for ClassDef")
        elif sym_kind in ("function", "async_function", "method", "async_method") and selector.component == "bases":
            raise ValueError(f"Component '{selector.component}' not valid for FunctionDef")
        
        raise ValueError(f"Component '{selector.component}' not found or not valid for symbol {'.'.join(selector.symbol_path)}")

    # Calculate insertion index in the items list
    items = [item[0] for item in items_info]
    insert_idx = -1

    if before is not None:
        try:
            insert_idx = items.index(before)
        except ValueError:
            raise ValueError(f"{selector.component.capitalize()[:-1]} '{before}' not found")
    elif after is not None:
        try:
            insert_idx = items.index(after) + 1
        except ValueError:
            raise ValueError(f"{selector.component.capitalize()[:-1]} '{after}' not found")
    elif position == -1:
        insert_idx = len(items)
    else:
        insert_idx = position

    # Determine insertion byte offset
    transform = _rust.PyFileTransform(source_code)
    
    # Handle decorators doubling @
    val_to_add = value.strip()
    if selector.component == "decorators" and val_to_add.startswith("@"):
        val_to_add = val_to_add[1:]

    # Insert at insert_idx
    if not items_info:
        # Empty container
        replacement = val_to_add
        if selector.component == "decorators":
            # If adding first decorator, get_symbol_component_range returns the start of 'def'
            replacement = f"@{val_to_add}\n"
        elif selector.component == "bases":
            replacement = f"({val_to_add})"
        elif selector.component == "params":
            target_kind = kind or selector.pseudo_class
            if target_kind == "KEYWORD_ONLY":
                replacement = f"*, {val_to_add}"
            elif target_kind == "POSITIONAL_ONLY":
                replacement = f"{val_to_add}, /"
            else:
                replacement = val_to_add
        
        # Get the container range again to be sure
        container_range = _rust.get_symbol_component_range(
            source_code,
            selector.symbol_path,
            selector.component,
            None,
            ext=_ext,
        )
        if container_range is None:
             # Fallback: find if it's a class or function to give better error or handle it
             syms = _rust.collect_symbols_from_str(source_code, selector=".".join(selector.symbol_path), ext=_ext)
             if syms:
                 sym_kind = syms[0]["kind"]
                 if sym_kind == "class" and selector.component == "params":
                     raise ValueError(f"Component '{selector.component}' not valid for ClassDef")
                 elif sym_kind in ("function", "async_function", "method", "async_method") and selector.component == "bases":
                     raise ValueError(f"Component '{selector.component}' not valid for FunctionDef")
             raise ValueError(f"Could not find container for {selector.component}")

        cont_start, cont_end = container_range
        transform.replace_range(cont_start, cont_end, replacement)
    else:
        # Handle parameter kind for existing params
        if selector.component == "params" and (kind or selector.pseudo_class):
            target_kind = kind or selector.pseudo_class
            # Find separators
            pos_only_sep_idx = -1
            kw_only_sep_idx = -1
            star_arg_idx = -1
            star_kwarg_idx = -1
            
            for i, (name, _, _) in enumerate(items_info):
                if name == "/":
                    pos_only_sep_idx = i
                elif name == "*":
                    kw_only_sep_idx = i
                elif name.startswith("**"):
                    star_kwarg_idx = i
                elif name.startswith("*"):
                    star_arg_idx = i
            
            if target_kind == "POSITIONAL_ONLY":
                if pos_only_sep_idx == -1:
                    insert_idx = len(items_info)
                else:
                    insert_idx = min(insert_idx, pos_only_sep_idx)
            elif target_kind == "KEYWORD_ONLY":
                if kw_only_sep_idx == -1 and star_arg_idx == -1:
                    # Insert before **kwargs if it exists
                    if star_kwarg_idx != -1:
                        insert_idx = star_kwarg_idx
                    else:
                        insert_idx = len(items_info)
                    val_to_add = f"*, {val_to_add}"
                else:
                    # Insert after * or after star_arg, but before **kwargs
                    if kw_only_sep_idx != -1:
                        insert_idx = max(insert_idx, kw_only_sep_idx + 1)
                    else:
                        insert_idx = max(insert_idx, star_arg_idx + 1)
                    
                    if star_kwarg_idx != -1:
                        insert_idx = min(insert_idx, star_kwarg_idx)
            elif target_kind == "POSITIONAL_OR_KEYWORD":
                 if kw_only_sep_idx != -1:
                      insert_idx = min(insert_idx, kw_only_sep_idx)
                 elif star_arg_idx != -1:
                      insert_idx = min(insert_idx, star_arg_idx)
                 if pos_only_sep_idx != -1:
                      insert_idx = max(insert_idx, pos_only_sep_idx + 1)

        # Insert at insert_idx
        if insert_idx >= len(items_info):
            # Append
            last_item_end = items_info[-1][2]
            sep = ", "
            if selector.component == "decorators":
                sep = "\n"
                replacement = f"{sep}@{val_to_add}"
            else:
                replacement = f"{sep}{val_to_add}"
            transform.insert_after(last_item_end, replacement)
        elif insert_idx <= 0:
            # Prepend
            first_item_start = items_info[0][1]
            sep = ", "
            if selector.component == "decorators":
                sep = "\n"
                replacement = f"@{val_to_add}{sep}"
            else:
                replacement = f"{val_to_add}{sep}"
            transform.insert_before(first_item_start, replacement)
        else:
            # Insert in between
            target_start = items_info[insert_idx][1]
            sep = ", "
            if selector.component == "decorators":
                sep = "\n"
                replacement = f"@{val_to_add}{sep}"
            else:
                replacement = f"{val_to_add}{sep}"
            transform.insert_before(target_start, replacement)

    new_code = transform.apply()
    if new_code is None:
        raise ValueError(
            f"Failed to add to component '{selector.component}' in "
            f"{'.'.join(selector.symbol_path)}: overlapping byte ranges"
        )

    # Generate diff
    diff = _generate_diff(selector.file_path, source_code, new_code)

    # Apply changes if requested
    if apply:
        file_path.write_text(new_code)

    return diff


def remove_component(selector: ExtendedSelector, apply: bool = False) -> str:
    """Remove component or item. Returns diff."""
    # If no component specified, remove the entire symbol
    if selector.component is None:
        return remove_symbol(selector, apply=apply)

    # Validate that body cannot be removed
    if selector.component == "body":
        raise ValueError("Cannot remove body component")

    # Read file
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    source_code = file_path.read_text()

    # Get the range for the component using Rust accelerator
    _ext = _ext_from_path(selector.file_path)
    range_info = _rust.get_symbol_component_range(
        source_code,
        selector.symbol_path,
        selector.component,
        selector.accessor,
        ext=_ext,
    )

    if range_info is None:
        # Check if symbol exists at all
        syms = _rust.collect_symbols_from_str(source_code, selector=".".join(selector.symbol_path), ext=_ext)
        if not syms:
             raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

        # Symbol exists but component not found
        kind = syms[0]["kind"]
        if kind == "class" and selector.component in ("params", "returns"):
            raise ValueError(f"Component '{selector.component}' not valid for ClassDef")
        elif kind in ("function", "async_function", "method", "async_method") and selector.component == "bases":
            raise ValueError(f"Component '{selector.component}' not valid for FunctionDef")

        raise ValueError(f"Component '{selector.component}' not found or not valid for symbol {'.'.join(selector.symbol_path)}")

    start_byte, end_byte = range_info
    
    # Check if we are removing an individual item (accessor is present)
    # or the whole component.
    transform = _rust.PyFileTransform(source_code)
    source_bytes = source_code.encode('utf-8')

    if selector.accessor is not None:
        # Removing an individual item. Need to clean up commas/separators.
        # Check for following comma
        i = end_byte
        while i < len(source_bytes) and source_bytes[i:i+1] in (b' ', b'\t', b'\n', b'\r'):
            i += 1
        
        if i < len(source_bytes) and source_bytes[i:i+1] == b',':
            # Remove from item start through the comma and any following space
            j = i + 1
            while j < len(source_bytes) and source_bytes[j:j+1] in (b' ', b'\t'):
                j += 1
            transform.remove_range(start_byte, j)
        else:
            # Look for preceding comma
            i = start_byte
            while i > 0 and source_bytes[i-1:i] in (b' ', b'\t'):
                i -= 1
            
            if i > 0 and source_bytes[i-1:i] == b',':
                # Remove from preceding comma through the item end
                j = i - 1
                # Also remove whitespace before the comma
                while j > 0 and source_bytes[j-1:j] in (b' ', b'\t'):
                    j -= 1
                transform.remove_range(j, end_byte)
            else:
                # No comma found, just remove the item
                # For decorators, might need to remove the leading @ or trailing newline
                if selector.component == "decorators":
                    # Heuristic: remove from @ to newline
                    i = start_byte
                    while i > 0 and source_bytes[i-1:i] != b'\n' and source_bytes[i-1:i] != b'\r' and source_bytes[i-1:i] != b'@':
                        i -= 1
                    if i > 0 and source_bytes[i-1:i] == b'@':
                        i -= 1
                    
                    j = end_byte
                    while j < len(source_bytes) and source_bytes[j:j+1] in (b' ', b'\t'):
                        j += 1
                    if j < len(source_bytes) and source_bytes[j:j+1] in (b'\n', b'\r'):
                        j += 1
                        if j < len(source_bytes) and source_bytes[j-1:j+1] == b'\r\n':
                            j += 1
                    transform.remove_range(i, j)
                else:
                    transform.remove_range(start_byte, end_byte)
    else:
        # Removing whole component.
        if selector.component == "returns":
            # get_symbol_component_range for returns includes -> and leading space
            transform.remove_range(start_byte, end_byte)
        elif selector.component == "bases":
            # If removing all bases, we also want to remove parentheses if present.
            # Tree-sitter 'class_definition' has 'superclasses' node which includes parentheses.
            # Look for parentheses around the bases
            i = start_byte
            while i > 0 and source_bytes[i-1:i] in (b' ', b'\t'):
                i -= 1
            
            j = end_byte
            while j < len(source_bytes) and source_bytes[j:j+1] in (b' ', b'\t'):
                j += 1
                
            if i > 0 and source_bytes[i-1:i] == b'(' and j < len(source_bytes) and source_bytes[j:j+1] == b')':
                transform.remove_range(i-1, j+1)
            else:
                transform.remove_range(start_byte, end_byte)
        else:
            transform.remove_range(start_byte, end_byte)

    new_code = transform.apply()
    if new_code is None:
        raise ValueError(
            f"Failed to remove component '{selector.component}' from "
            f"{'.'.join(selector.symbol_path)}: overlapping byte ranges"
        )

    # Generate diff
    diff = _generate_diff(selector.file_path, source_code, new_code)

    # Apply changes if requested
    if apply:
        file_path.write_text(new_code)

    return diff


_CONTENT_REF_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\.content\}")


def _extract_string_content_from_text(text: str) -> str | None:
    """Extract the inner content of a string literal from source text.

    For a string like ``"MyClass"`` or ``'MyClass'`` returns ``MyClass``.
    Returns None for non-string text or complex strings that cannot be
    trivially unwrapped (f-strings, concatenated strings).
    """
    text = text.strip()
    try:
        result = ast.literal_eval(text)
        if isinstance(result, str):
            return result
    except (ValueError, SyntaxError):
        pass
    return None


@dataclass
class PatternMatch:
    """Represents a match of a pattern in code."""
    node_text: str | None
    captures: dict[str, str]
    line: int | None = None
    matched_text: str | None = None
    end_line: int | None = None
    col: int | None = None
    end_col: int | None = None



def _filter_matches_by_import(
    matches: list[PatternMatch],
    imported_from: str,
    file_path: str,
    project_root: str,
    content: str,
) -> list[PatternMatch]:
    """Post-filter pattern matches to only include those where the root name
    is imported from the specified module.

    Uses PyScopeResolver to resolve the qualified name of the leftmost
    name in each match and verifies it matches the target module.
    """
    if not matches:
        return []

    # Use a single resolver per file for efficiency
    resolver = _rust.PyScopeResolver(project_root)
    resolver.index_file(file_path, content)

    filtered = []
    for match in matches:
        # Extract the root name from the matched node
        # For simplicity, we use the first identifier in the matched text
        root_name = _extract_root_name(match.node_text or "")
        if not root_name:
            continue

        # Resolve QN at match position
        references = resolver.references_in_file(file_path)
        
        match_qn = None
        for qn, line, col, offset, end_offset, kind in references:
            if line == match.line and col == match.col:
                match_qn = qn
                break
        
        if match_qn and match_qn.startswith(f"{imported_from}."):
            filtered.append(match)
        elif match_qn == imported_from:
            filtered.append(match)

    return filtered


def _extract_root_name(text: str) -> str | None:
    """Extract the first identifier from a code fragment."""
    match = re.search(r"[a-zA-Z_]\w*", text)
    return match.group(0) if match else None


def _filter_matches_by_scope_local(
    matches: list[PatternMatch],
    file_path: str,
    project_root: str,
    content: str,
) -> list[PatternMatch]:
    """Post-filter pattern matches to only include those where the root name
    is locally defined (not imported).

    Uses PyScopeResolver to check the origin of each match.
    """
    if not matches:
        return []

    resolver = _rust.PyScopeResolver(project_root)
    resolver.index_file(file_path, content)

    # Build a set of names that are imported (defined via import statements).
    imported_names: set[str] = set()
    references = resolver.references_in_file(file_path)
    for qn, line, col, offset, end_offset, kind in references:
        if kind == "import":
            # Extract the local name from the qualified name
            # (e.g., "os.path.join" → "join")
            local_name = qn.rsplit(".", 1)[-1] if "." in qn else qn
            imported_names.add(local_name)

    filtered = []
    for match in matches:
        root_name = _extract_root_name(match.node_text or "")
        if not root_name:
            continue

        if root_name not in imported_names:
            filtered.append(match)

    return filtered


def _filter_matches_by_type_oracle(
    matches: list[PatternMatch],
    constraints: dict[str, tuple[str, str]],
    type_oracle: TypeOracle,
    file_path: str,
) -> list[PatternMatch]:
    """Post-filter pattern matches using inferred types from TypeOracle.

    Filters each match based on metavar type constraints (e.g., :type[X] or :returns[X]).
    """
    if not matches:
        return []

    from pathlib import Path
    from .type_oracle import parse_type_string

    # Get type info for the file
    file_types = type_oracle.infer_file(Path(file_path))

    # Read source to find capture positions
    source_lines = Path(file_path).read_text().splitlines()

    filtered = []
    for match in matches:
        keep = True
        for metavar_name, (kind, type_str) in constraints.items():
            captured_text = match.captures.get(metavar_name)
            if captured_text is None:
                keep = False
                break

            # Find the position of the captured text within the match
            match_line = match.line
            if match_line is None or match_line < 1:
                keep = False
                break

            # Look up type binding at the match position
            # Try to find the captured name in the source line
            line_idx = match_line - 1
            if line_idx >= len(source_lines):
                keep = False
                break

            line_text = source_lines[line_idx]
            col = line_text.find(captured_text)
            if col < 0:
                keep = False
                break

            binding = file_types.type_at(match_line, col + 1)  # 1-indexed col
            if binding is None:
                keep = False
                break

            if kind == "type":
                constraint_td = parse_type_string(type_str)
                if not binding.type_descriptor.matches(constraint_td):
                    keep = False
                    break
            elif kind == "returns":
                # For returns constraint, check the return type
                constraint_td = parse_type_string(type_str)
                ret_type = binding.type_descriptor.return_type
                if ret_type is None or not ret_type.matches(constraint_td):
                    keep = False
                    break

        if keep:
            filtered.append(match)

    return filtered


def find_pattern(
    pattern_str: str,
    file_path: str,
    scope: list[str] | None = None,
    inside: str | None = None,
    not_inside: str | None = None,
    imported_from: str | None = None,
    where: str | None = None,
    scope_local: bool = False,
    source_override: str | None = None,
    type_oracle: "TypeOracle | None" = None,
    language: str = "python",
) -> list[PatternMatch]:
    """Find all matches of pattern in file.

    Args:
        pattern_str: Pattern string with metavariables like "print($X)"
        file_path: Path to Python file to search
        scope: Optional symbol path to limit matches to (e.g., ["MyClass", "method"])
        inside: Optional constraint - only match inside this structure.
        not_inside: Optional constraint - only match outside this structure.
        imported_from: Optional module name - only match when the root name
                       in the pattern is imported from this module
        where: Optional constraint - only match inside a structure matching
               this pattern (e.g., 'class MyClass', 'def test_*').
               Alias for inside with pattern support.
        scope_local: If True, only match names that are locally defined
                     (not imported).
        source_override: If provided, search this source string instead of reading from file_path.
        type_oracle: Optional TypeOracle instance for :type[X] and :returns[X] constraints.

    Returns:
        List of matches with locations and captured values
    """
    # Handle --where as alias for --inside
    if where is not None:
        if inside is not None:
            raise ValueError("Cannot specify both 'where' and 'inside' parameters")
        inside = where

    # Validate inside/not_inside constraints
    if inside and not_inside:
        raise ValueError("Cannot specify both 'inside' and 'not_inside' parameters")

    # Parse pattern
    pattern = parse_pattern(pattern_str)

    # Read file (or use source_override)
    if source_override is not None:
        source_code = source_override
    else:
        file = Path(file_path)
        if not file.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        source_code = file.read_text()

    # Detect language from file extension if not provided
    if language == "python" and file_path:
        from emend.language_registry import detect_language
        language = detect_language(file_path) or "python"

    # Compile pattern and constraints to Rust IR
    rust_ir = compile_pattern_to_rust_ir(pattern_str, language=language)
    if rust_ir is None:
        raise ValueError(f"Pattern '{pattern_str}' could not be compiled to Rust IR")

    inside_ir = compile_constraint_to_rust_ir(inside, language=language) if inside else None
    not_inside_ir = compile_constraint_to_rust_ir(not_inside, language=language) if not_inside else None
    
    if inside and inside_ir is None:
        raise ValueError(f"Unknown inside/not_inside constraint: '{inside}'")
    if not_inside and not_inside_ir is None:
        raise ValueError(f"Unknown inside/not_inside constraint: '{not_inside}'")

    # Find matches using Rust engine
    ext = Path(file_path).suffix.lstrip('.') if file_path else None
    # print(f"DEBUG: find_pattern ext={ext} ir={rust_ir}")
    raw_matches = _rust.find_pattern_in_files(
        [(str(file_path), source_code)], rust_ir, inside_ir, not_inside_ir,
        extension=ext
    )


    matches = []
    for m in raw_matches:
        captures = {k: v for k, v in m[6].items() if k != "_"}
        matches.append(PatternMatch(
            node_text=m[5],
            captures=captures,
            line=m[1],
            col=m[2],
            end_line=m[3],
            end_col=m[4],
            matched_text=m[5],
        ))

    # Post-filter by scope if requested
    if scope is not None:
        from .ast_utils import find_nested_definitions, find_symbol_by_path
        symbols = find_nested_definitions(file_path)
        target_sym = find_symbol_by_path(symbols, scope)
        if target_sym:
            matches = [m for m in matches if m.line is not None and target_sym.line_start <= m.line <= target_sym.line_end]
        else:
            matches = []

    # Post-filter by import origin if requested
    if imported_from is not None:
        project_root = _find_project_root(file_path)
        matches = _filter_matches_by_import(
            matches, imported_from, file_path, project_root, source_code
        )

    # Post-filter by scope locality if requested
    if scope_local:
        project_root = _find_project_root(file_path)
        matches = _filter_matches_by_scope_local(
            matches, file_path, project_root, source_code
        )

    # Post-filter by TypeOracle type constraints
    if type_oracle is not None:
        oracle_constraints = {}
        for mv in pattern.metavars:
            if is_oracle_type_constraint(mv.type_constraint):
                oracle_constraints[mv.name] = parse_oracle_type_constraint(mv.type_constraint)
        if oracle_constraints:
            matches = _filter_matches_by_type_oracle(
                matches, oracle_constraints, type_oracle, file_path
            )

    return matches


def remove_symbol(
selector: ExtendedSelector, apply: bool = False) -> str:
    """Remove a symbol (function, class) from a file.

    Args:
        selector: Extended selector specifying the symbol to remove
        apply: If True, write changes to file. If False, return diff only.

    Returns:
        Unified diff showing the changes

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If symbol not found
    """
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    # Use tree-sitter symbols to find the target symbol's range
    from .ast_utils import find_nested_definitions, find_symbol_by_path
    symbols = find_nested_definitions(str(file_path))
    sym = find_symbol_by_path(symbols, selector.symbol_path)
    
    if sym is None:
        raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

    # Read original source
    source_code = file_path.read_text()
    lines = source_code.splitlines(keepends=True)
    
    # Symbols in tree-sitter include decorators if they are part of a decorated_definition.
    # Our NestedSymbol uses decorator_line_start if decorators are present.
    start_line = sym.decorator_line_start if sym.decorator_line_start is not None else sym.line_start
    
    # Remove the specified lines (1-indexed)
    # We want to remove the range [start_line, sym.line_end]
    start_idx = start_line - 1
    end_idx = sym.line_end
    
    new_lines = lines[:start_idx] + lines[end_idx:]
    new_code = "".join(new_lines)

    # Generate diff
    diff = _generate_diff(selector.file_path, source_code, new_code)

    # Apply changes if requested
    if apply:
        file_path.write_text(new_code)

    return diff


def get_symbol_source(selector: ExtendedSelector, dedent: bool = False) -> str:
    """Get the complete source code of a symbol including decorators.

    Args:
        selector: Extended selector specifying the symbol
        dedent: If True, remove leading indentation

    Returns:
        String containing the complete source code of the symbol

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If symbol not found
    """
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    # Handle line-based selectors (file.py:42 or file.py:10-20)
    if selector.line_start is not None:
        # Read the lines directly
        with open(file_path) as f:
            lines = f.readlines()

        # Extract the specified lines (1-indexed)
        start_idx = selector.line_start - 1
        end_idx = (selector.line_end or selector.line_start) - 1

        if start_idx < 0 or end_idx >= len(lines):
            raise ValueError(f"Line range {selector.line_start}-{selector.line_end or selector.line_start} out of bounds")

        code = ''.join(lines[start_idx:end_idx + 1])

        if dedent:
            import textwrap
            code = textwrap.dedent(code)

        return code

    # Handle symbol-based selectors
    from .ast_utils import find_nested_definitions, find_symbol_by_path
    symbols = find_nested_definitions(str(file_path))
    sym = find_symbol_by_path(symbols, selector.symbol_path)
    
    if sym is None:
        raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

    # Extract source lines
    source_code = file_path.read_text()
    lines = source_code.splitlines(keepends=True)
    
    # Symbols in tree-sitter include decorators if they are part of a decorated_definition.
    # Our NestedSymbol uses decorator_line_start if decorators are present.
    start_line = sym.decorator_line_start if sym.decorator_line_start is not None else sym.line_start
    
    # line numbers are 1-indexed
    symbol_lines = lines[start_line - 1 : sym.line_end]
    code = "".join(symbol_lines)

    # We ALWAYS dedent here because we extracted raw lines from a potentially
    # indented context (e.g. a method in a class). The parser returns positions
    # relative to the node's own start, which is effectively dedented.
    import textwrap
    code = textwrap.dedent(code)

    # If the explicit dedent flag is True, we've already done it above.
    # The expected behavior is that get_symbol_source(selector) returns
    # dedented code for the symbol.
    
    # Ensure it ends with exactly one newline to match expected test behavior
    if not code.endswith("\n"):
        code += "\n"

    return code


def _collect_names(source: str) -> set[str]:
    """Collect all Name references in code using stdlib ast."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            # Collect the leftmost name in the chain
            current = node.value
            while isinstance(current, ast.Attribute):
                current = current.value
            if isinstance(current, ast.Name):
                names.add(current.id)
    return names


def analyze_imports(symbol_source: str, source_file: str) -> list[str]:
    """Analyze which imports from source_file are needed by symbol_source.

    Args:
        symbol_source: Source code of the symbol being copied
        source_file: Path to file where symbol originated (to read imports from)

    Returns:
        List of import statement strings needed for the symbol

    Example:
        >>> source = "def func():\\n    return ast.parse('x = 1')"
        >>> imports = analyze_imports(source, "module.py")
        >>> # Returns ["import ast"] if module.py has that import
    """
    used_names = _collect_names(symbol_source)
    if not used_names:
        return []

    source_path = Path(source_file)
    if not source_path.exists():
        return []

    try:
        source_tree = ast.parse(source_path.read_text())
    except Exception:
        return []

    needed_imports = []

    for stmt in source_tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                effective_name = alias.asname or alias.name.split('.')[0]
                if effective_name in used_names:
                    if alias.asname:
                        needed_imports.append(f"import {alias.name} as {alias.asname}")
                    else:
                        needed_imports.append(f"import {alias.name}")

        elif isinstance(stmt, ast.ImportFrom):
            if stmt.names and isinstance(stmt.names[0], ast.alias) and stmt.names[0].name == '*':
                continue

            module_name = stmt.module or ''

            used_import_names = []
            for alias in stmt.names:
                effective_name = alias.asname or alias.name
                if effective_name in used_names:
                    used_import_names.append((alias.name, alias.asname))

            if used_import_names:
                import_parts = []
                for name, asname in used_import_names:
                    if asname:
                        import_parts.append(f"{name} as {asname}")
                    else:
                        import_parts.append(name)
                needed_imports.append(f"from {module_name} import {', '.join(import_parts)}")

    return needed_imports


def copy_symbol(
    selector: ExtendedSelector,
    dest_file: str,
    position: str = "end",
    dedent: bool = False,
    include_imports: bool = False,
    apply: bool = False
) -> str:
    """Copy a symbol from one location to another.

    Args:
        selector: Extended selector specifying the source symbol
        dest_file: Path to destination file
        position: Where to insert: "start", "end" (default)
        dedent: If True, dedent the source code to remove common indentation
        include_imports: If True, analyze and include necessary imports from source file
        apply: If True, write changes to file. If False, return diff only.

    Returns:
        Unified diff showing the changes to the destination file

    Raises:
        FileNotFoundError: If source file doesn't exist
        ValueError: If symbol not found
    """
    import textwrap

    # Get source code of the symbol
    source = get_symbol_source(selector)

    # Dedent if requested
    if dedent:
        source = textwrap.dedent(source)

    # Analyze and prepend imports if requested
    if include_imports:
        imports = analyze_imports(source, selector.file_path)
        if imports:
            source = "\n".join(imports) + "\n\n" + source

    # Read destination file (create if doesn't exist)
    dest_path = Path(dest_file)
    if dest_path.exists():
        dest_content = dest_path.read_text()
    else:
        dest_content = ""

    # Build new content based on position
    if position == "start":
        if dest_content:
            new_content = source + "\n\n" + dest_content
        else:
            new_content = source
    else:  # "end"
        if dest_content:
            new_content = dest_content.rstrip() + "\n\n\n" + source + "\n"
        else:
            new_content = source

    # Generate diff
    diff = _generate_diff(dest_file, dest_content, new_content)

    # Apply changes if requested
    if apply:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(new_content)

    return diff


def _is_valid_replacement(code: str) -> bool:
    """Verify if the given code string parses as a valid Python expression or statement.

    This ensures that replacements don't produce syntactically invalid code.
    """
    try:
        ast.parse(code, mode='eval')
        return True
    except SyntaxError:
        try:
            ast.parse(code, mode='exec')
            return True
        except SyntaxError:
            return False


def _substitute_metavars(
    replacement_str: str,
    captures: dict[str, str],
) -> str | None:
    """Substitute metavars in replacement string with captured code.

    Returns substituted string, or None if replacement cannot be resolved
    (e.g. ${NAME.content} on a non-string).
    """
    replacement_code = replacement_str

    # First pass: resolve ${NAME.content} references (string
    # interpolation).  These extract the inner content of a string
    # literal, stripping the surrounding quotes.  If any reference
    # cannot be resolved (e.g. the captured node is not a string
    # literal), skip the entire replacement to avoid producing
    # nonsense output.
    content_failed = False
    for ref_match in _CONTENT_REF_RE.finditer(replacement_code):
        ref_name = ref_match.group(1)
        captured = captures.get(ref_name)
        if captured is None:
            content_failed = True
            break
        content = _extract_string_content_from_text(captured)
        if content is None:
            content_failed = True
            break
        replacement_code = replacement_code.replace(
            ref_match.group(0), content
        )
    if content_failed:
        return None

    # Second pass: substitute regular metavar references ($NAME, $...NAME).
    for name, code in captures.items():
        # Replace $...NAME with the captured text (already a string from Rust)
        replacement_code = replacement_code.replace(f"$...{name}", code)
        # Replace $NAME with the captured text
        replacement_code = replacement_code.replace(f"${name}", code)

    # Clean up comma artifacts from empty ellipsis substitutions
    replacement_code = re.sub(r'(\()\s*,\s*', r'\1', replacement_code)
    replacement_code = re.sub(r'(\[)\s*,\s*', r'\1', replacement_code)
    replacement_code = re.sub(r',\s*,', ',', replacement_code)

    return replacement_code


def replace_pattern(
    pattern_str: str,
    replacement_str: str,
    file_path: str,
    scope: list[str] | None = None,
    apply: bool = False,
    inside: str | None = None,
    not_inside: str | None = None,
    where: str | None = None,
    type_oracle: TypeOracle | None = None,
    language: str = "python",
) -> tuple[str, int]:
    """Replace pattern matches with replacement template.

    Args:
        pattern_str: Pattern string with metavariables like "print($X)"
        replacement_str: Replacement template like "logger.info($X)"
        file_path: Path to Python file to transform
        scope: Optional symbol path to limit replacements to (e.g., ["MyClass", "method"])
        apply: If True, write changes to file. If False, return diff only.
        inside: Optional constraint - only replace inside this structure.
                Keywords: "def", "async def", "class", "for", "while", "try", "with", "if".
                Patterns: "def test_*", "class MyClass", "try:", "except ValueError:".
        not_inside: Optional constraint - only replace outside this structure.
                    Supports same syntax as inside.
        where: Optional constraint - alias for inside with pattern support.
        type_oracle: Optional TypeOracle instance for :type[X] and :returns[X]
                     constraints.  When present, matching is delegated to
                     ``find_pattern`` so that the oracle post-filter is applied
                     and only type-verified positions are replaced.

    Returns:
        Tuple of (diff, count) where diff is a unified diff and count is number of replacements
    """
    # Handle --where as alias for --inside
    if where is not None:
        if inside is not None:
            raise ValueError("Cannot specify both 'where' and 'inside' parameters")
        inside = where

    # Validate inside/not_inside constraints
    if inside and not_inside:
        raise ValueError("Cannot specify both 'inside' and 'not_inside' parameters")

    # Read file
    file = Path(file_path)
    if not file.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    source_code = file.read_text()

    # Find all matches using find_pattern (already migrated to tree-sitter fast paths)
    matches = find_pattern(
        pattern_str, file_path, scope=scope,
        inside=inside, not_inside=not_inside, where=where,
        type_oracle=type_oracle, language=language,
        source_override=source_code,
    )

    if not matches:
        return "", 0

    # Build a newline offset table for the source
    line_starts = [0]
    for i, ch in enumerate(source_code):
        if ch == '\n':
            line_starts.append(i + 1)

    # Use Rust transformation engine for byte-range replacements
    transform = _rust.PyFileTransform(source_code)
    replacement_count = 0
    accepted_ranges: list[tuple[int, int]] = []

    for match in matches:
        if match.line is None or match.col is None or match.end_line is None or match.end_col is None:
            continue

        # Convert line/col to byte offsets
        start_offset = line_starts[match.line - 1] + match.col
        
        if match.matched_text is not None:
            # If we have the exact matched text from Rust (potentially adjusted range),
            # use its length to determine the end offset.
            end_offset = start_offset + len(match.matched_text)
        else:
            end_offset = line_starts[match.end_line - 1] + match.end_col

        # Filter out matches that are contained within a previously accepted match
        # Since find_pattern returns matches in top-down DFS order, the first match
        # of a nested set is the outermost one.
        is_contained = False
        for a_start, a_end in accepted_ranges:
            if start_offset >= a_start and end_offset <= a_end:
                is_contained = True
                break
        if is_contained:
            continue

        # Build replacement by substituting metavars
        replacement_code = _substitute_metavars(replacement_str, match.captures)
        if replacement_code is None:
            continue

        # Verify replacement parses as valid Python
        if not _is_valid_replacement(replacement_code):
            continue

        # Apply replacement to the transform
        transform.replace_range(start_offset, end_offset, replacement_code)
        accepted_ranges.append((start_offset, end_offset))
        replacement_count += 1

    if replacement_count == 0:
        return "", 0

    # Apply all edits
    new_code = transform.apply()
    if new_code is None:
        # This should not happen due to the is_contained filter above
        logger.error("Overlapping edits detected in replace_pattern")
        return "", 0

    # Generate diff
    diff = _generate_diff(file_path, source_code, new_code)

    # Apply changes if requested
    if apply:
        file.write_text(new_code)

    return diff, replacement_count



# Cross-project semantic primitives

@dataclass
class Reference:
    """A reference to a symbol."""
    file_path: str
    line: int
    column: int
    offset: int
    is_definition: bool
    is_import: bool
    is_write: bool


def _rename_in_docstrings(content: str, old_name: str, new_name: str) -> str | None:
    """Replace old_name with new_name in all docstrings."""
    from emend.language_plugins import load_plugin
    return load_plugin("python").comment_handler.rename_in_docstrings(content, old_name, new_name)


def find_references(
    selector: ExtendedSelector,
    project_path: str | None = None,
    include_definition: bool = True,
    include_imports: bool = True,
    writes_only: bool = False,
    reads_only: bool = False,
) -> Iterator[Reference]:
    """Find all references to a symbol across the project.

    Uses Rust scope resolver for scope-aware resolution: only returns
    references that actually refer to the target symbol, not coincidental
    same-named symbols in other scopes or files.

    Args:
        selector: Symbol to find references for
        project_path: Project root (auto-detected if None)
        include_definition: Include the definition itself
        include_imports: Include import statements
        writes_only: Only return write (assignment) references
        reads_only: Only return read (load) references

    Returns:
        List of Reference objects with location info

    Raises:
        ValueError: If symbol not found
    """
    if writes_only and reads_only:
        raise ValueError("Cannot specify both writes_only and reads_only")

    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for find_references")

    # scan_root: where to collect files (respects --project for scope limiting)
    # module_root: project root for computing dotted module names (always git root)
    scan_root = project_path if project_path else _find_project_root(selector.file_path)
    module_root = _find_project_root(selector.file_path)
    resolved_target = str(Path(selector.file_path).resolve())
    target_module = _file_to_module(selector.file_path, module_root)

    # Compute the set of qualified names we're looking for
    all_target_qns = {symbol_name, f"{target_module}.{symbol_name}"}

    # Warm path: try reference_index first
    cached_refs = query_reference_index(scan_root, f"{target_module}.{symbol_name}")
    if cached_refs is None and symbol_name:
        cached_refs = query_reference_index(scan_root, symbol_name)

    if cached_refs is not None:
        def _gen_warm() -> Iterator[Reference]:
            for entry in cached_refs:
                ref_kind = entry["ref_kind"]
                is_def = ref_kind == "definition"
                is_imp = ref_kind == "import"
                is_wr = ref_kind == "write"
                if not include_definition and is_def:
                    continue
                if not include_imports and is_imp:
                    continue
                if writes_only and not is_wr:
                    continue
                if reads_only and is_wr:
                    continue
                yield Reference(
                    file_path=entry["file_path"],
                    line=entry["line"],
                    column=entry["col"],
                    offset=0,
                    is_definition=is_def,
                    is_import=is_imp,
                    is_write=is_wr,
                )
        return _gen_warm()

    # Cold path: full project scan
    language = selector.language
    candidates = _files_importing_module(scan_root, target_module, language=language)
    language = selector.language

    def _gen() -> Iterator[Reference]:
        for py_file, _content, resolver in visit_project_ts(
            name_hint=symbol_name,
            project_path=scan_root,
            target_file=resolved_target,
            candidate_files=candidates,
            target_qnames=all_target_qns,
            language=language,
        ):
            for qn, line, col, offset, end_offset, kind in resolver.references_in_file(py_file):
                if qn in all_target_qns:
                    is_def = kind == "definition"
                    is_imp = kind == "import"
                    is_wr = kind == "write"

                    if not include_definition and is_def:
                        continue
                    if not include_imports and is_imp:
                        continue
                    if writes_only and not is_wr:
                        continue
                    if reads_only and is_wr:
                        continue

                    yield Reference(
                        file_path=py_file,
                        line=line,
                        column=col,
                        offset=0,
                        is_definition=is_def,
                        is_import=is_imp,
                        is_write=is_wr,
                    )

    return _gen()


@dataclass
class Callee:
    """A function/method called by a function."""
    name: str
    qualified_name: str | None
    file_path: str | None
    line: int | None


def find_callers(
    selector: ExtendedSelector,
    project_path: str | None = None,
) -> Iterator[Reference]:
    """Find all places where a function is called across the project.

    Unlike find_references, this only returns actual call sites,
    not imports or other references.

    Args:
        selector: Symbol to find callers for
        project_path: Project root (auto-detected if None)

    Returns:
        Iterator of Reference objects at call sites
    """
    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for find_callers")

    # scan_root: where to collect files (respects --project for scope limiting)
    # module_root: project root for computing dotted module names (always git root)
    scan_root = project_path if project_path else _find_project_root(selector.file_path)
    module_root = _find_project_root(selector.file_path)
    resolved_target = str(Path(selector.file_path).resolve())
    target_module = _file_to_module(selector.file_path, module_root)

    # Use import graph to pre-filter files
    language = selector.language
    candidates = _files_importing_module(scan_root, target_module, language=language)

    all_target_qns = {symbol_name, f"{target_module}.{symbol_name}"}

    def _gen() -> Iterator[Reference]:
        for py_file, _content, resolver in visit_project_ts(
            name_hint=symbol_name,
            project_path=scan_root,
            target_file=resolved_target,
            candidate_files=candidates,
            target_qnames=all_target_qns,
            language=language,
        ):
            for qn, line, col, offset, end_offset, kind in resolver.references_in_file(py_file):
                if qn in all_target_qns and kind == "call":
                    yield Reference(
                        file_path=py_file,
                        line=line,
                        column=col,
                        offset=0,
                        is_definition=False,
                        is_import=False,
                        is_write=False,
                    )

    return _gen()


def find_callees(
    selector: ExtendedSelector,
    project_path: str | None = None,
) -> list[Callee]:
    """Find all functions/methods called inside a function.

    Args:
        selector: Function to analyze
        project_path: Project root (auto-detected if None)

    Returns:
        List of Callee objects
    """
    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for find_callees")

    file_path = selector.file_path
    try:
        content = Path(file_path).read_text()
    except FileNotFoundError:
        raise ValueError(f"File not found: {file_path}")

    # Use tree-sitter symbols to find the target symbol's range
    from .ast_utils import find_nested_definitions, find_symbol_by_path
    symbols = find_nested_definitions(file_path)
    target_sym = find_symbol_by_path(symbols, selector.symbol_path)
    if target_sym is None:
        raise ValueError(f"Symbol not found: {'.'.join(selector.symbol_path)}")

    # Use scope resolver to find all call references in the file
    project_root = project_path if project_path else _find_project_root(file_path)
    resolver = _rust.PyScopeResolver(project_root)
    resolver.index_file(file_path, content)
    
    refs = resolver.references_in_file(file_path)
    
    callees: list[Callee] = []
    seen: set[tuple[str, int]] = set()

    for qn, line, col, offset, end_offset, kind in refs:
        if kind == "call" and target_sym.line_start <= line <= target_sym.line_end:
            # deduplicate by (QN, line) to match old _CalleeCollector._seen behavior
            # (which was by name, but now we have line info too)
            name = qn.rsplit('.', 1)[-1]
            if (qn, line) not in seen:
                seen.add((qn, line))
                callees.append(Callee(
                    name=name,
                    qualified_name=qn,
                    file_path=None,  # Not easily resolvable to file here
                    line=line,
                ))

    return callees


def generate_graph(
    file_path: str,
    project_path: str | None = None,
    format: str = "plain",
) -> str:
    """Generate a call graph for all functions in a file.

    Args:
        file_path: Python file to analyze
        project_path: Project root (auto-detected if None)
        format: Output format - "plain", "json", or "dot"

    Returns:
        Graph in the requested format
    """
    from .component_selector import ExtendedSelector

    content = Path(file_path).read_text()

    raw = _rust.collect_callees(content)
    edges: dict[str, list[str]] = {name: callees for name, callees in raw}

    if format == "json":
        return json.dumps(edges, indent=2)
    elif format == "dot":
        lines = ["digraph callgraph {"]
        for caller, callees_list in edges.items():
            for callee in callees_list:
                lines.append(f'  "{caller}" -> "{callee}";')
        lines.append("}")
        return "\n".join(lines)
    else:
        # plain text
        lines = []
        for caller, callees_list in edges.items():
            if callees_list:
                lines.append(f"{caller} -> {', '.join(callees_list)}")
            else:
                lines.append(f"{caller} (no calls)")
        return "\n".join(lines)


@dataclass
class DeadSymbol:
    """A symbol detected as potentially dead (unreferenced) code."""
    file_path: str
    name: str
    kind: str  # 'function', 'class', 'async_function'
    line: int
    selector: str  # e.g. "file.py::func_name"
    reason: str  # Why it's flagged (e.g. "no references found")
    last_reference_commit: str | None = None  # git commit that last touched this symbol


# Decorator prefixes that indicate a symbol is an entry point / framework hook
_ENTRY_POINT_DECORATORS = frozenset({
    'app.command', 'app.route', 'app.get', 'app.post', 'app.put',
    'app.delete', 'app.patch',
    'pytest.fixture', 'fixture',
    'staticmethod', 'classmethod', 'property',
    'abstractmethod', 'abc.abstractmethod',
    'override',
    'overload', 'typing.overload',
    'click.command', 'click.group',
    'celery.task',
    'register',
})

# Decorator base names that indicate entry points
_ENTRY_POINT_DECORATOR_BASENAMES = frozenset({
    'route', 'get', 'post', 'put', 'delete', 'patch', 'head', 'options',
    'sync_get', 'sync_post', 'sync_put', 'sync_delete', 'sync_patch',
    'websocket', 'websocket_route',
    'command', 'task', 'hook', 'listener',
    'receiver', 'signal', 'handler', 'middleware',
    'register', 'export',
})

# Names that are conventional entry points and should never be flagged
_ENTRY_POINT_NAMES = frozenset({
    'main', 'setup', 'teardown', 'configure',
    'setUp', 'tearDown', 'setUpClass', 'tearDownClass',
    'setUpModule', 'tearDownModule',
})


def _is_dunder(name: str) -> bool:
    """Check if a name is a dunder (double underscore) name."""
    return name.startswith('__') and name.endswith('__') and len(name) > 4


def _is_likely_entry_point(name: str, kind: str, decorators: list[str], depth: int) -> bool:
    """Check if a symbol is likely an entry point based on heuristics.

    Entry points are symbols that are invoked by frameworks or conventions
    rather than explicit code references.
    """
    # Dunder methods/functions are always entry points
    if _is_dunder(name):
        return True

    # Conventional entry-point names
    if name in _ENTRY_POINT_NAMES:
        return True

    # Names starting with test_ or Test (pytest discovery)
    # Names starting with describe_ (pytest-describe convention)
    if name.startswith('test_') or name.startswith('Test') or name.startswith('describe_'):
        return True

    # Private names (single underscore prefix) at depth > 1 are methods,
    # which may be called via getattr or framework internals
    # We only flag private top-level symbols

    # Check decorators
    for dec in decorators:
        # Strip @ prefix if present
        dec_name = dec[1:] if dec.startswith('@') else dec
        # Strip arguments: @app.command("name") -> app.command
        if '(' in dec_name:
            dec_name = dec_name[:dec_name.index('(')]

        if dec_name in _ENTRY_POINT_DECORATORS:
            return True

        # Check basename: @anything.route -> "route" is entry point
        basename = dec_name.rsplit('.', 1)[-1] if '.' in dec_name else dec_name
        if basename in _ENTRY_POINT_DECORATOR_BASENAMES:
            return True

    return False


def _get_all_exported_names(content: str) -> set[str]:
    """Extract names listed in __all__ from file content."""
    try:
        tree = ast.parse(content)
    except Exception:
        return set()

    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if isinstance(target, ast.Name) and target.id == '__all__':
                value = stmt.value
                if isinstance(value, (ast.List, ast.Tuple)):
                    for el in value.elts:
                        if isinstance(el, ast.Constant) and isinstance(el.value, str):
                            names.add(el.value)
    return names



def _get_last_reference_commit(file_path: str, symbol_name: str) -> str | None:
    """Use ``git log -S`` to find the last commit that added/removed *symbol_name*.

    Returns a one-line summary like ``abc1234 2024-01-15 Fix: remove usage``
    or None if git is unavailable or nothing found.
    """
    import subprocess
    cwd = str(Path(file_path).resolve().parent)
    try:
        result = subprocess.run(
            ['git', 'log', '-S', symbol_name, '--format=%h %ai %s',
             '-1', '--', file_path],
            capture_output=True, text=True, timeout=10,
            cwd=cwd,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _find_dead_code_cached(
    project_path: str,
    kind: str | None,
    include_private: bool,
    exclude_references_from: list[str] | None,
    strings_count_as_references: bool,
    all_files: bool,
    entry_point_decorators: list[str] | None = None,
    entry_point_names: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> list[DeadSymbol]:
    """Index-accelerated dead code detection — single SQL query.

    Uses ``symbol_index`` and ``reference_index`` from ``parse.db``.
    The heavy lifting (candidate filtering + reference checking) is a
    single ``NOT EXISTS`` query; only the lightweight string-literal
    post-filter runs in Python on the small result set.
    """
    import sqlite3 as _sql3

    scan_root = str(Path(project_path).resolve())

    def _glob_to_like(pattern: str) -> str:
        """Convert a path pattern (possibly with globs) to SQL LIKE.

        Supports ``*`` (any chars in one segment), ``**`` (any path
        segments), and ``?`` (single char).  Patterns without glob
        characters are treated as directory prefixes.
        """
        has_glob = "*" in pattern or "?" in pattern
        if not has_glob:
            resolved = str(Path(pattern).resolve())
            safe = resolved.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            return f"{safe}%"
        # Glob pattern: resolve relative portion if not starting with * or /
        if not pattern.startswith("*") and not Path(pattern).is_absolute():
            pattern = str(Path(scan_root) / pattern)
        # Escape literal LIKE chars before converting globs.
        pattern = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        # Order matters: ** before * to avoid double replacement.
        pattern = pattern.replace("**", "%").replace("*", "%").replace("?", "_")
        if not pattern.endswith("%"):
            pattern += "%"
        return pattern

    # Ensure the index is fresh; build it if necessary.
    if not _ensure_index_fresh(scan_root):
        logger.info("dead_code: index stale/missing — warming caches")
        warm_caches(scan_root, type_engine="none")

    project_root = _find_project_root(scan_root)
    worktree_id = _get_worktree_id(project_root)
    cache_dir = _cache_db_dir(project_root)
    db_path = cache_dir / "parse.db"

    conn = _sql3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")

    try:
        # ---- Build the single query ---------------------------------
        conditions = [
            "si.depth = 1",
            "si.is_entry_point = 0",
            "si.is_exported = 0",
            "si.has_noqa = 0",
        ]
        params: list = []

        if kind == "function":
            conditions.append("si.kind IN ('function', 'async_function')")
        elif kind == "class":
            conditions.append("si.kind = 'class'")
        else:
            # By default, only analyze functions, async functions, methods,
            # and classes.  Variables are excluded since module-level
            # assignments are often configs/constants.
            conditions.append(
                "si.kind IN ('function', 'async_function', 'method', "
                "'async_method', 'class')"
            )

        if not include_private:
            # Exclude _private names (but keep dunders — they're already
            # filtered by is_entry_point).
            conditions.append(
                "(si.name NOT LIKE '\\_%' ESCAPE '\\' OR si.name LIKE '\\_\\_%\\_\\_%' ESCAPE '\\')"
            )

        # Exclude entire directories from analysis (supports globs).
        if exclude_paths:
            for pattern in exclude_paths:
                conditions.append("si.file_path NOT LIKE ? ESCAPE '\\'")
                params.append(_glob_to_like(pattern))

        # Custom entry point names: filter at SQL level.
        if entry_point_names:
            placeholders = ", ".join("?" for _ in entry_point_names)
            conditions.append(f"si.name NOT IN ({placeholders})")
            params.extend(entry_point_names)

        # Reference exclusion: ignore references from certain dirs (supports globs).
        exclude_clause = ""
        if exclude_references_from:
            like_parts = []
            for pattern in exclude_references_from:
                like_parts.append("ri.file_path NOT LIKE ? ESCAPE '\\'")
                params.append(_glob_to_like(pattern))
            exclude_clause = " AND " + " AND ".join(like_parts)

        where = " AND ".join(conditions)

        query = f"""
            SELECT si.name, si.qualified_name, si.kind, si.file_path,
                   si.line, si.decorators
            FROM symbol_index si
            INNER JOIN file_manifest fm
              ON si.content_hash = fm.content_hash
                 AND si.file_path = fm.path
                 AND fm.worktree_id = ?
            WHERE {where}
              AND NOT EXISTS (
                SELECT 1 FROM reference_index ri
                WHERE (ri.target_qn = si.qualified_name
                       OR ri.target_qn = si.module_qn)
                  AND NOT (ri.file_path = si.file_path
                           AND ri.line = si.line)
                  {exclude_clause}
              )
            ORDER BY si.file_path, si.line
        """

        rows = conn.execute(query, [worktree_id] + params).fetchall()
        conn.close()

        # Post-filter: custom entry point decorators.
        if entry_point_decorators and rows:
            ep_decs = set(entry_point_decorators)
            # Also build a basename set for flexible matching.
            ep_basenames = {d.rsplit(".", 1)[-1] for d in ep_decs}
            filtered = []
            for row in rows:
                dec_str = row[5]  # si.decorators column
                if dec_str:
                    skip = False
                    for dec in dec_str.split(","):
                        # Strip @ prefix and arguments:
                        # @app.command("name") -> app.command
                        dec_clean = dec.strip()
                        if dec_clean.startswith("@"):
                            dec_clean = dec_clean[1:]
                        if "(" in dec_clean:
                            dec_clean = dec_clean[:dec_clean.index("(")]
                        if dec_clean in ep_decs:
                            skip = True
                            break
                        basename = dec_clean.rsplit(".", 1)[-1] if "." in dec_clean else dec_clean
                        if basename in ep_basenames:
                            skip = True
                            break
                    if skip:
                        continue
                filtered.append(row)
            rows = filtered

        if not rows:
            return []

        # ---- String-literal post-filter (Python) ---------------------
        # The reference_index only captures AST-level Name/Attribute
        # references.  String literals need a quick text scan.  This
        # runs on the small set of candidates returned by SQL (~tens).
        if not strings_count_as_references:
            return [
                DeadSymbol(
                    file_path=fp, name=name, kind=sym_kind,
                    line=line, selector=f"{fp}::{qname}",
                    reason="no references found",
                )
                for name, qname, sym_kind, fp, line, _decs in rows
            ]

        # Collect names that need string checking (length > 3).
        str_names = {name for name, _, _, _, _, _ in rows if len(name) > 3}

        if str_names:
            # Use Rust batch-read with name hints for fast file scanning.
            source_files = _collect_source_files(
                scan_root, git_tracked_only=not all_files,
            )

            # Build exclude matchers for string scanning.
            _exclude_prefixes: list[str] = []
            _exclude_globs: list[str] = []
            if exclude_references_from:
                import fnmatch as _fnmatch
                for pattern in exclude_references_from:
                    if "*" in pattern or "?" in pattern:
                        # Normalise relative globs to absolute for matching.
                        if not pattern.startswith("*") and not Path(pattern).is_absolute():
                            pattern = str(Path(scan_root) / pattern)
                        # Ensure trailing * so fnmatch matches files inside dirs.
                        if not pattern.endswith("*"):
                            pattern = pattern.rstrip("/") + "/*"
                        _exclude_globs.append(pattern)
                    else:
                        _exclude_prefixes.append(str(Path(pattern).resolve()))

            def _is_excluded_ref(path: str) -> bool:
                if _exclude_prefixes and any(path.startswith(p) for p in _exclude_prefixes):
                    return True
                if _exclude_globs:
                    return any(_fnmatch.fnmatch(path, g) for g in _exclude_globs)
                return False

            # file → content cache (only files containing a candidate name)
            file_cache: dict[str, str] = {}
            try:
                matched = _rust.read_and_filter_files(
                    source_files, list(str_names),
                )
                for fp, content in matched:
                    r = str(Path(fp).resolve())
                    if _is_excluded_ref(r):
                        continue
                    file_cache[r] = content
            except Exception:
                pass

            # Also read definition files for same-file string checks.
            for _, _, _, fp, _, _ in rows:
                r = str(Path(fp).resolve())
                if r not in file_cache:
                    try:
                        file_cache[r] = Path(fp).read_text()
                    except Exception:
                        pass

            # Build name → set of files containing it (excluding def file).
            _strip_re = re.compile(r"'[^']*'|\"[^\"]*\"")
            names_with_str_ref: set[tuple[str, str]] = set()  # (file_path, name)
            for name, qname, sym_kind, fp, line, _decs in rows:
                if len(name) <= 3:
                    continue
                r = str(Path(fp).resolve())

                # Same-file: check for name in strings on non-def lines.
                content = file_cache.get(r)
                if content and name in content:
                    for i, lt in enumerate(content.splitlines(), 1):
                        if i == line or name not in lt:
                            continue
                        cleaned = _strip_re.sub("", lt)
                        if name not in cleaned:
                            names_with_str_ref.add((fp, name))
                            break

                if (fp, name) in names_with_str_ref:
                    continue

                # Cross-file: any other file containing the name.
                for other_r, other_content in file_cache.items():
                    if other_r == r:
                        continue
                    if name in other_content:
                        names_with_str_ref.add((fp, name))
                        break
        else:
            names_with_str_ref = set()

        dead_symbols = []
        for name, qname, sym_kind, fp, line, _decs in rows:
            if (fp, name) in names_with_str_ref:
                continue
            dead_symbols.append(
                DeadSymbol(
                    file_path=fp, name=name, kind=sym_kind,
                    line=line, selector=f"{fp}::{qname}",
                    reason="no references found",
                )
            )
        return dead_symbols

    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise


def find_dead_code(
    project_path: str,
    kind: str | None = None,
    include_private: bool = False,
    exclude_references_from: list[str] | None = None,
    strings_count_as_references: bool = True,
    show_last_reference: bool = True,
    all_files: bool = False,
    entry_point_decorators: list[str] | None = None,
    entry_point_names: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> Iterator[DeadSymbol]:
    """Find potentially dead (unreferenced) code in a project.

    Uses ``symbol_index`` and ``reference_index`` from ``parse.db`` for
    fast lookups.  When the index is stale or missing it is automatically
    rebuilt so that the result is always based on current sources.

    Args:
        project_path: Project root directory.
        kind: Optional filter: 'function', 'class', or None for all.
        include_private: If True, include _private symbols (excluded by default).
        exclude_references_from: Directories/globs to exclude when scanning for
            references (e.g. ``["tests/"]``).  Symbols are still collected from
            these paths but references *in* them are ignored.
        strings_count_as_references: If True (default), string literals that
            contain the symbol name are treated as references.  This reduces
            false positives from dynamic dispatch, serialization, and similar.
        show_last_reference: If True (default), annotate each dead symbol with
            the last ``git log -S`` commit that touched its name.
        all_files: If True, scan all Python files (including untracked).
            By default only git-tracked files are scanned when inside a
            git repository.
        entry_point_decorators: Additional decorator names to treat as entry
            points (e.g. ``["my_framework.handler"]``).  Symbols with these
            decorators are never flagged as dead code.
        entry_point_names: Additional function/class names to treat as entry
            points (e.g. ``["plugin_init"]``).  Symbols with these names are
            never flagged as dead code.
        exclude_paths: Directories to exclude entirely from dead code analysis.
            Symbols defined in these paths are never reported.

    Yields:
        DeadSymbol objects sorted by file path and line number.
    """
    t0 = time.monotonic()
    dead_symbols = _find_dead_code_cached(
        project_path,
        kind=kind,
        include_private=include_private,
        exclude_references_from=exclude_references_from,
        strings_count_as_references=strings_count_as_references,
        all_files=all_files,
        entry_point_decorators=entry_point_decorators,
        entry_point_names=entry_point_names,
        exclude_paths=exclude_paths,
    )
    logger.info(
        "dead_code: %d dead symbols in %.3fs",
        len(dead_symbols), time.monotonic() - t0,
    )

    if show_last_reference and dead_symbols:
        from concurrent.futures import ThreadPoolExecutor

        def _git_lookup(d: DeadSymbol) -> tuple[DeadSymbol, str | None]:
            return d, _get_last_reference_commit(d.file_path, d.name)

        with ThreadPoolExecutor() as pool:
            for d, commit in pool.map(_git_lookup, dead_symbols):
                d.last_reference_commit = commit
                yield d
    else:
        yield from dead_symbols



# visit_project_ts yields (py_file, content, resolver)

def rename_symbol(
    selector: ExtendedSelector,
    new_name: str,
    project_path: str | None = None,
    in_hierarchy: bool = True,
    docs: bool = False,
    unsure: bool = False,
    apply: bool = False,
) -> dict[str, str]:
    """Rename a symbol across the entire project.

    Uses Tree-sitter and PyScopeResolver for scope-aware renaming:
    only renames references that actually refer to the target symbol,
    not coincidental same-named symbols in other scopes or files.

    Args:
        selector: Symbol to rename
        new_name: New name for the symbol
        project_path: Project root (auto-detected if None)
        in_hierarchy: Also rename in class hierarchies
        docs: Also rename in docstrings
        unsure: Rename uncertain occurrences
        apply: If True, write changes. If False, return diffs only.

    Returns:
        Dict mapping file_path -> unified diff of changes

    Raises:
        ValueError: If symbol not found
    """
    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for rename_symbol")

    # scan_root: where to collect files (respects --project for scope limiting)
    # module_root: project root for computing dotted module names (always git root)
    scan_root = project_path if project_path else _find_project_root(selector.file_path)
    module_root = _find_project_root(selector.file_path)
    resolved_target = str(Path(selector.file_path).resolve())
    target_module = _file_to_module(selector.file_path, module_root)

    # Use fully qualified name for matching
    target_qn = f"{target_module}.{symbol_name}" if target_module else symbol_name

    # Use import graph to pre-filter files
    language = selector.language
    candidates = _files_importing_module(scan_root, target_module, language=language)

    diffs = {}
    for py_file, content, resolver in visit_project_ts(
        name_hint=symbol_name,
        project_path=scan_root,
        target_file=resolved_target,
        candidate_files=candidates,
        target_qnames={target_qn},
        language=language,
    ):
        references = resolver.references_in_file(py_file)
        transform = _rust.PyFileTransform(content)
        changed = False

        for qn, line, col, offset, end_offset, kind in references:
            if qn == target_qn:
                # Check if the text at the position matches symbol_name
                # (to avoid renaming aliases or coincidental names in attributes)
                # Now using end_offset for better precision!
                if content[offset:end_offset].endswith(symbol_name):
                    transform.replace_range(end_offset - len(symbol_name), end_offset, new_name)
                    changed = True

        if not changed:
            continue

        new_content = transform.apply()
        if new_content is None:
            continue

        # Apply docstring renaming if requested -- but only in files where
        # the scope-aware code rename found changes.
        if docs:
            docs_result = _rename_in_docstrings(new_content, symbol_name, new_name)
            if docs_result is not None:
                new_content = docs_result

        diff = _generate_diff(py_file, content, new_content)
        diffs[py_file] = diff

        if apply:
            Path(py_file).write_text(new_content)

    return diffs


def move_symbol(
    selector: ExtendedSelector,
    dest_file: str,
    position: str = "end",
    dedent: bool = False,
    update_imports: bool = True,
    project_path: str | None = None,
    apply: bool = False,
) -> dict[str, str]:
    """Move a symbol to another file with import updates.

    1. Copies the symbol to the destination file
    2. Removes the symbol from the source file
    3. Updates all import statements that reference the symbol

    Args:
        selector: Symbol to move
        dest_file: Destination file path
        position: Where to insert ("start" or "end")
        dedent: If True, dedent the source code to remove common indentation
        update_imports: If True, update imports across project
        project_path: Project root (auto-detected if None)
        apply: If True, write changes. If False, return diffs only.

    Returns:
        Dict mapping file_path -> unified diff of changes

    Raises:
        ValueError: If symbol not found
    """
    diffs = {}
    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for move_symbol")

    # Step 1: Copy symbol to destination
    copy_diff = copy_symbol(selector, dest_file, position=position, dedent=dedent, apply=apply)
    diffs[dest_file] = copy_diff

    # Step 2: Remove from source
    remove_diff = remove_symbol(selector, apply=apply)
    diffs[selector.file_path] = remove_diff

    # Step 3: Update imports if requested
    if update_imports:
        import_diffs = _update_imports_for_move(
            selector.file_path,
            dest_file,
            symbol_name,
            project_path,
            apply=apply,
        )
        diffs.update(import_diffs)

    return diffs


def _update_imports_for_move(
    source_file: str,
    dest_file: str,
    symbol_name: str,
    project_path: str | None,
    apply: bool,
) -> dict[str, str]:
    """Update imports across project when a symbol moves."""
    diffs = {}

    # Get module names
    source_module = _file_to_module(source_file, project_path)
    dest_module = _file_to_module(dest_file, project_path)

    # Resolve skip paths
    resolved_source = str(Path(source_file).resolve())
    resolved_dest = str(Path(dest_file).resolve())
    proj_root = _find_project_root(project_path or source_file)

    target_qn = f"{source_module}.{symbol_name}"
    from emend.language_registry import detect_language
    language = detect_language(source_file) or "python"

    for py_file, content, resolver in visit_project_ts(
        name_hint=symbol_name,
        project_path=proj_root,
        language=language,
    ):
        resolved_py = str(Path(py_file).resolve())
        if resolved_py == resolved_source or resolved_py == resolved_dest:
            continue

        transform = _rust.PyFileTransform(content)
        changed = False

        references = resolver.references_in_file(py_file)
        
        for i, (qn, line, col, offset, end_offset, kind) in enumerate(references):
            if kind != "import":
                continue
            
            if qn == target_qn:
                # This could be 'from source_module import symbol_name'
                # or 'import source_module.symbol_name'
                
                # Search backwards for the module reference in the same statement
                module_ref = None
                for j in range(i - 1, -1, -1):
                    pqn, pl, pc, po, peo, pk = references[j]
                    if pk != "import": continue
                    # Heuristic: must be on same or previous line
                    if pl < line - 1: break
                    if pqn == source_module:
                        module_ref = (po, peo)
                        break
                
                if module_ref:
                    # 'from source_module import ...'
                    transform.replace_range(module_ref[0], module_ref[1], dest_module)
                    changed = True
                else:
                    # 'import source_module.symbol_name'
                    # Replace the 'source_module' part of the QN
                    if content[offset : offset + len(source_module)] == source_module:
                        transform.replace_range(offset, offset + len(source_module), dest_module)
                        changed = True

        if not changed:
            continue

        new_content = transform.apply()
        if new_content is None or new_content == content:
            continue

        diff = _generate_diff(py_file, content, new_content)
        diffs[py_file] = diff

        if apply:
            Path(py_file).write_text(new_content)

    return diffs
def _rename_module_references(
    project_root: str,
    old_module: str,
    new_module: str,
    apply: bool,
    language: str = "python",
) -> dict[str, str]:
    """Update all imports from old_module to new_module across the project."""
    diffs = {}

    from emend.language_registry import get_module_separator
    sep = get_module_separator(language)

    # hint for structural filter
    name_hint = old_module.rsplit(sep, 1)[-1]

    for py_file, content, resolver in visit_project_ts(
        name_hint=name_hint,
        project_path=project_root,
        language=language,
    ):
        transform = _rust.PyFileTransform(content)
        changed = False

        for qn, line, col, offset, end_offset, kind in resolver.references_in_file(py_file):
            if kind != "import":
                continue

            # Exact match: import old_module or from old_module import ...
            if qn == old_module:
                transform.replace_range(offset, end_offset, new_module)
                changed = True
            # Prefix match: import old_module.sub or from old_module.sub import ...
            elif qn.startswith(old_module + sep):
                prefix_len = len(old_module)
                # Verify that the source at offset matches old_module
                if content[offset : offset + prefix_len] == old_module:
                    transform.replace_range(offset, offset + prefix_len, new_module)
                    changed = True

        if not changed:
            continue

        new_content = transform.apply()
        if new_content is None or new_content == content:
            continue

        diff = _generate_diff(py_file, content, new_content)
        diffs[py_file] = diff

        if apply:
            Path(py_file).write_text(new_content)

    return diffs


def move_module(
    source_path: str,
    destination: str,
    project_path: str | None = None,
    apply: bool = False
) -> dict[str, str]:
    """Move a module to another package, updating imports.

    Args:
        source_path: Path to module file to move
        destination: Destination package path like 'pkg.subpkg' or folder path
        project_path: Project root (auto-detected if None)
        apply: If True, write changes. If False, return diffs only.

    Returns:
        Dict mapping file_path -> unified diff of changes
    """
    import shutil
    import os

    project_root = _find_project_root(project_path or source_path)
    old_module = _file_to_module(source_path, project_root)

    # Resolve destination to a directory path
    if '.' in destination and not os.path.isdir(destination):
        # Dotted module path like "pkg.subpkg"
        dest_dir = Path(project_root) / Path(destination.replace('.', '/'))
    else:
        # Could be a relative path or absolute path
        dest_dir_candidate = Path(destination)
        if not dest_dir_candidate.is_absolute():
            dest_dir = Path(project_root) / dest_dir_candidate
        else:
            dest_dir = dest_dir_candidate

    # New file location
    new_path = dest_dir / Path(source_path).name
    new_module = _file_to_module(str(new_path), project_root)

    # Update all imports across project
    from emend.language_registry import detect_language
    language = detect_language(source_path) or "python"
    diffs = _rename_module_references(project_root, old_module, new_module, apply, language=language)

    if apply:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), str(new_path))
        return {}

    # For dry-run, describe the file move
    description = f"Move {source_path} -> {new_path}"
    diffs["__description__"] = description
    return diffs


def rename_module(
    file_path: str,
    new_name: str,
    project_path: str | None = None,
    apply: bool = False
) -> dict[str, str]:
    """Rename a module file, updating imports across the project.

    Args:
        file_path: Path to module file to rename
        new_name: New name for the module (without .py extension)
        project_path: Project root (auto-detected if None)
        apply: If True, write changes. If False, return diffs only.

    Returns:
        Dict mapping file_path -> unified diff of changes
    """
    project_root = _find_project_root(project_path or file_path)
    old_module = _file_to_module(file_path, project_root)
    from emend.language_registry import detect_language, get_module_separator
    language = detect_language(file_path) or "python"
    sep = get_module_separator(language)

    parts = old_module.rsplit(sep, 1)
    new_module = f"{parts[0]}{sep}{new_name}" if len(parts) > 1 else new_name

    diffs = _rename_module_references(project_root, old_module, new_module, apply, language=language)

    ext = Path(file_path).suffix
    if apply:
        new_path = Path(file_path).parent / f"{new_name}{ext}"
        Path(file_path).rename(new_path)
        return {}

    # For dry-run, describe the file rename
    new_path = Path(file_path).parent / f"{new_name}{ext}"
    description = f"Rename {file_path} -> {new_path}"
    diffs["__description__"] = description
    return diffs


# ============================================================================
# Unified Commands (lookup, edit) - simplified interface combining multiple
# commands with convenient aliases
# ============================================================================

def _cmd_lookup_single_selector(
    selector: ExtendedSelector,
    file_or_pattern: str,
    case_insensitive: bool = False,
    smart_case: bool = False,
    json_output: bool = False,
    metadata: bool = False,
    paths_only: bool = False,
    count: bool = False,
    dedent: bool = False,
) -> str:
    """Lookup logic for a single (non-glob) selector."""
    # Handle line-based selectors with metadata - find containing symbol
    if selector.line_start is not None and metadata:
        from emend.ast_utils import find_nested_definitions, find_symbol_by_line
        file_path = Path(selector.file_path)
        symbols = find_nested_definitions(str(file_path))
        symbol = find_symbol_by_line(symbols, selector.line_start, selector.line_end)

        if symbol is None:
            print(f"No symbol found at line {selector.line_start}")
            raise SystemExit(1)

        selector = ExtendedSelector(
            file_path=selector.file_path,
            symbol_path=symbol.path,
        )

    # Handle metadata output
    if metadata:
        from emend.ast_utils import find_nested_definitions, find_symbol_by_path
        file_path = Path(selector.file_path)
        symbols = find_nested_definitions(str(file_path))
        symbol = find_symbol_by_path(symbols, selector.symbol_path)

        if symbol is None:
            raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

        selector_path = f"{selector.file_path}::{'.'.join(symbol.path)}"
        total_lines = symbol.line_end - symbol.line_start + 1

        with open(selector.file_path) as f:
            lines = f.readlines()
        offset_start = sum(len(line) for line in lines[:symbol.line_start - 1])
        offset_end = sum(len(line) for line in lines[:symbol.line_end])

        output = [
            selector_path,
            "-" * 50,
            f"  Lines: {symbol.line_start}-{symbol.line_end} ({total_lines} lines)",
            f"  Offset: {offset_start}-{offset_end}",
        ]

        if symbol.decorators:
            decs_with_prefix = [f"@{d}" if not d.startswith('@') else d for d in symbol.decorators]
            dec_str = ", ".join(decs_with_prefix)
            output.append(f"  Decorators: {dec_str}")

        if symbol.parameters:
            param_names = ", ".join(symbol.parameters)
            output.append(f"  Parameters: {len(symbol.parameters)} ({param_names})")

        output.append(f"  Kind: {symbol.kind}")

        return "\n".join(output) + "\n"

    # If wildcard without component and with query flags, treat as query
    if selector.has_wildcards() and not selector.component and (count or paths_only or json_output):
        from emend.query import cmd_query

        old_stdout = sys.stdout
        sys.stdout = buffer = io.StringIO()
        try:
            cmd_query(
                filepath=file_or_pattern,
                kinds=None,
                names=None,
                decorators=None,
                returns_patterns=None,
                in_classes=None,
                depths=None,
                params=None,
                case_insensitive=case_insensitive,
                smart_case=smart_case,
                output_json=json_output,
                paths_only=paths_only,
                count_only=count,
            )
        finally:
            sys.stdout = old_stdout

        return buffer.getvalue()

    # If component specified, act like get
    if selector.component:
        if selector.has_wildcards():
            from emend.ast_utils import find_nested_definitions, expand_wildcard_path
            file_path = Path(selector.file_path)
            symbols = find_nested_definitions(str(file_path))
            matched_symbols = expand_wildcard_path(symbols, selector.symbol_path)

            if not matched_symbols:
                raise ValueError(f"No symbols match pattern {'.'.join(selector.symbol_path)}")

            results = []
            for sym in matched_symbols:
                specific_selector = ExtendedSelector(
                    file_path=selector.file_path,
                    symbol_path=sym.path,
                    component=selector.component,
                    accessor=selector.accessor,
                    pseudo_class=selector.pseudo_class,
                )
                try:
                    result = get_component(specific_selector)
                    if json_output:
                        results.append({"symbol": '.'.join(sym.path), "value": result})
                    else:
                        results.append(result)
                except (ValueError, FileNotFoundError):
                    pass

            if json_output:
                return json.dumps(results, indent=2)
            else:
                return '\n'.join(results)
        else:
            return get_component(selector)
    else:
        # No component - act like show
        if selector.has_wildcards():
            from emend.ast_utils import find_nested_definitions, expand_wildcard_path
            file_path = Path(selector.file_path)
            symbols = find_nested_definitions(str(file_path))
            matched_symbols = expand_wildcard_path(symbols, selector.symbol_path)

            if not matched_symbols:
                raise ValueError(f"No symbols match pattern {'.'.join(selector.symbol_path)}")

            results = []
            for sym in matched_symbols:
                specific_selector = ExtendedSelector(
                    file_path=selector.file_path,
                    symbol_path=sym.path,
                )
                try:
                    result = get_symbol_source(specific_selector, dedent=dedent)
                    results.append(result)
                except (ValueError, FileNotFoundError):
                    pass

            return '\n'.join(results)
        return get_symbol_source(selector, dedent=dedent)


def cmd_lookup(
    file_or_pattern: str,
    selector_str: str | None = None,
    kind: list[str] | None = None,
    name: list[str] | None = None,
    has_decorator: list[str] | None = None,
    returns: list[str] | None = None,
    in_class: list[str] | None = None,
    depth: list[str] | None = None,
    has_param: list[str] | None = None,
    case_insensitive: bool = False,
    smart_case: bool = False,
    json_output: bool = False,
    metadata: bool = False,
    paths_only: bool = False,
    count: bool = False,
    dedent: bool = False,
    matching: str | None = None,
    type_oracle: TypeOracle | None = None,
    out: "IO[str] | None" = None,
) -> str:
    """Unified lookup command combining get, query, and show.

    If selector_str contains component (e.g., [params], [returns]), acts like get.
    If filter flags provided, acts like query.
    Otherwise acts like show (display source code).
    """
    # If filter flags provided, act as query
    if any([kind, name, has_decorator, returns, in_class, depth, has_param]):
        from emend.query import cmd_query

        # Expand file globs for query mode
        import glob as glob_mod
        from emend.language_registry import matches_language
        files_to_query = []
        fop = Path(file_or_pattern)
        if fop.is_dir():
            files_to_query = [str(f) for f in fop.rglob("*.py")]
        elif '*' in file_or_pattern or '?' in file_or_pattern:
            files_to_query = [f for f in glob_mod.glob(file_or_pattern, recursive=True) if matches_language(f, "python")]
        else:
            files_to_query = [file_or_pattern]

        if out is not None and not count:
            # Streaming path: write each file's output directly to out as it completes
            for fpath in files_to_query:
                old_stdout = sys.stdout
                sys.stdout = out
                try:
                    cmd_query(
                        filepath=fpath,
                        kinds=kind,
                        names=name,
                        decorators=has_decorator,
                        returns_patterns=returns,
                        in_classes=in_class,
                        depths=depth,
                        params=has_param,
                        case_insensitive=case_insensitive,
                        smart_case=smart_case,
                        output_json=json_output,
                        paths_only=paths_only,
                        count_only=False,
                        type_oracle=type_oracle,
                    )
                finally:
                    sys.stdout = old_stdout
                out.flush()
            return ''

        all_output = []
        total_count_val = 0
        for fpath in files_to_query:
            old_stdout = sys.stdout
            sys.stdout = buffer = io.StringIO()
            try:
                cmd_query(
                    filepath=fpath,
                    kinds=kind,
                    names=name,
                    decorators=has_decorator,
                    returns_patterns=returns,
                    in_classes=in_class,
                    depths=depth,
                    params=has_param,
                    case_insensitive=case_insensitive,
                    smart_case=smart_case,
                    output_json=json_output,
                    paths_only=paths_only,
                    count_only=count,
                    type_oracle=type_oracle,
                )
            finally:
                sys.stdout = old_stdout
            result = buffer.getvalue()
            if result:
                if count:
                    try:
                        total_count_val += int(result.strip())
                    except ValueError:
                        all_output.append(result)
                else:
                    all_output.append(result)

        if count:
            return str(total_count_val) + '\n'
        return ''.join(all_output)

    # Parse selector if provided
    if selector_str:
        selector = parse_extended_selector(selector_str)

        # Reject line selectors with file globs
        if selector.has_file_glob() and selector.line_start is not None:
            raise ValueError("Line selectors cannot be combined with file globs")

        # Multi-file dispatch for file globs
        if selector.has_file_glob():
            expanded_files = selector.expand_file_glob()

            if out is not None and not matching:
                # Streaming path: write each file's result to out as it completes
                any_results = False
                for fpath in expanded_files:
                    concrete = selector.with_file_path(fpath)
                    try:
                        result = _cmd_lookup_single_selector(
                            concrete,
                            file_or_pattern=fpath,
                            case_insensitive=case_insensitive,
                            smart_case=smart_case,
                            json_output=json_output,
                            metadata=metadata,
                            paths_only=paths_only,
                            count=count,
                            dedent=dedent,
                        )
                        if result:
                            out.write(result)
                            if not result.endswith('\n'):
                                out.write('\n')
                            out.flush()
                            any_results = True
                    except (ValueError, FileNotFoundError):
                        continue
                if not any_results:
                    raise ValueError(f"No symbols found matching {selector_str}")
                return ''

            all_results = []
            for fpath in expanded_files:
                concrete = selector.with_file_path(fpath)
                try:
                    result = _cmd_lookup_single_selector(
                        concrete,
                        file_or_pattern=fpath,
                        case_insensitive=case_insensitive,
                        smart_case=smart_case,
                        json_output=json_output,
                        metadata=metadata,
                        paths_only=paths_only,
                        count=count,
                        dedent=dedent,
                    )
                    if result:
                        all_results.append(result)
                except (ValueError, FileNotFoundError):
                    continue

            if not all_results:
                raise ValueError(f"No symbols found matching {selector_str}")

            combined = '\n'.join(all_results)

            # Apply --matching filter if specified
            if matching:
                combined = _apply_matching_filter(combined, matching, selector, expanded_files, json_output)

            return combined

        result = _cmd_lookup_single_selector(
            selector,
            file_or_pattern=file_or_pattern,
            case_insensitive=case_insensitive,
            smart_case=smart_case,
            json_output=json_output,
            metadata=metadata,
            paths_only=paths_only,
            count=count,
            dedent=dedent,
        )

        # Apply --matching filter for single-file selectors
        if matching and result:
            result = _apply_matching_filter(
                result, matching, selector, [selector.file_path], json_output
            )

        return result
    else:
        raise ValueError("No selector provided")


def _apply_matching_filter(
    lookup_result: str,
    matching_pattern: str,
    selector: ExtendedSelector,
    files: list[str],
    json_output: bool = False,
) -> str:
    """Filter lookup results to only symbols whose body matches a pattern."""
    filtered_parts = []
    for part in lookup_result.split('\n'):
        part = part.strip()
        if not part:
            continue
        # Try to parse as a selector path (file.py::Symbol.path format)
        if '::' in part:
            try:
                sel = parse_extended_selector(part)
                source = get_symbol_source(sel)
                matches = find_pattern(matching_pattern, sel.file_path, source_override=source)
                if matches:
                    filtered_parts.append(part)
            except (ValueError, FileNotFoundError):
                filtered_parts.append(part)
        else:
            # For source code output, check the whole result against the pattern
            for fpath in files:
                try:
                    matches = find_pattern(matching_pattern, fpath, source_override=lookup_result)
                    if matches:
                        return lookup_result
                except (ValueError, FileNotFoundError):
                    pass
            return ""

    return '\n'.join(filtered_parts)


def _merge_type_filter(
    selector: ExtendedSelector,
    returns_filter: list[str] | None,
) -> list[str] | None:
    """Merge a selector's :returns[X] type_filter into the returns_filter list.

    If the selector has a ``type_filter`` like ``returns[str]``, the type
    string is appended to (or creates) the returns_filter list so the
    existing returns-based filtering logic handles it.
    """
    if selector.type_filter is None:
        return returns_filter
    # Parse "returns[str]" or "type[Connection]"
    tf = selector.type_filter
    bracket = tf.index("[")
    kind = tf[:bracket]
    type_string = tf[bracket + 1:-1]
    if kind == "returns":
        merged = list(returns_filter) if returns_filter else []
        merged.append(type_string)
        return merged
    # For :type[X], pass through as-is (future: filter by inferred type)
    return returns_filter


def _expand_selector_with_returns_filter(
    selector: ExtendedSelector,
    returns_filter: list[str],
    type_oracle: TypeOracle | None = None,
) -> list[ExtendedSelector]:
    """Expand a selector to only include symbols matching a returns filter.

    Uses annotation-based matching, falling back to type oracle when available.
    Returns concrete selectors for each matching symbol.
    """
    import fnmatch as _fnmatch
    from .query import _collect_symbols, _filter_by_returns_with_oracle

    file_path = Path(selector.file_path)
    if not file_path.exists():
        return []
    source = file_path.read_text()
    symbols = _collect_symbols(file_path, source)

    # Build type index if oracle available
    file_types = None
    if type_oracle is not None:
        file_types = type_oracle.infer_file(file_path)

    result = []
    for symbol in symbols:
        # Extract symbol's path segments from its full path (file.py::Class.method → [Class, method])
        parts = symbol.path.split("::")
        sym_path = parts[1].split(".") if len(parts) > 1 else [symbol.name]

        # Check if symbol matches the selector's symbol_path pattern
        if len(sym_path) != len(selector.symbol_path):
            continue
        match = True
        for seg, pat in zip(sym_path, selector.symbol_path):
            if pat != "*" and not _fnmatch.fnmatch(seg, pat):
                match = False
                break
        if not match:
            continue

        # Check returns filter
        if not _filter_by_returns_with_oracle(
            symbol, returns_filter, case_insensitive=False, file_types=file_types,
        ):
            continue

        # Create concrete selector for this symbol
        concrete = ExtendedSelector(
            file_path=selector.file_path,
            symbol_path=sym_path,
            component=selector.component,
            accessor=selector.accessor,
            pseudo_class=selector.pseudo_class,
        )
        result.append(concrete)

    return result


def _cmd_edit_single(
    selector: ExtendedSelector,
    value: str | None = None,
    rm: bool = False,
    apply: bool = False,
) -> str:
    """Edit logic for a single (non-glob) selector."""
    if rm or value == "":
        return remove_component(selector, apply=apply)

    if selector.pseudo_class is not None:
        raise ValueError(
            f"Cannot use pseudo-class '{selector.pseudo_class}' with 'edit' command. "
            "Use 'add' command to insert new items."
        )

    if value is not None:
        return set_component(selector, value, apply=apply)

    raise ValueError("No operation specified (provide value or --rm)")


def cmd_edit(
    selector_str: str,
    value: str | None = None,
    rm: bool = False,
    apply: bool = False,
    returns_filter: list[str] | None = None,
    type_oracle: TypeOracle | None = None,
) -> str:
    """Edit or replace existing symbol components.

    - If rm=True or value="", remove the component or symbol
    - If accessor present + value, modify specific item (e.g., [params][x])
    - If no accessor + value, replace entire component (e.g., [returns])
    - If returns_filter or selector :returns[X] specified, only edit symbols
      whose return type matches (annotation first, then inferred via oracle)
    """
    selector = parse_extended_selector(selector_str)

    # Merge selector type_filter into returns_filter
    returns_filter = _merge_type_filter(selector, returns_filter)

    # When returns_filter is specified, expand the selector to only include
    # symbols that match the return type constraint.
    if returns_filter:
        files = (
            selector.expand_file_glob()
            if selector.has_file_glob()
            else [selector.file_path]
        )
        all_results = []
        for fpath in files:
            concrete_base = selector.with_file_path(fpath) if fpath != selector.file_path else selector
            matching = _expand_selector_with_returns_filter(
                concrete_base, returns_filter, type_oracle,
            )
            for concrete in matching:
                try:
                    result = _cmd_edit_single(concrete, value=value, rm=rm, apply=apply)
                    if result:
                        all_results.append(result)
                except (ValueError, FileNotFoundError):
                    continue
        if not all_results:
            raise ValueError(f"No symbols found matching {selector_str} with --returns {returns_filter}")
        return '\n'.join(all_results)

    # Multi-file dispatch for file globs
    if selector.has_file_glob():
        expanded_files = selector.expand_file_glob()
        all_results = []
        for fpath in expanded_files:
            concrete = selector.with_file_path(fpath)
            try:
                result = _cmd_edit_single(concrete, value=value, rm=rm, apply=apply)
                if result:
                    all_results.append(result)
            except (ValueError, FileNotFoundError):
                continue
        if not all_results:
            raise ValueError(f"No symbols found matching {selector_str}")
        return '\n'.join(all_results)

    return _cmd_edit_single(selector, value=value, rm=rm, apply=apply)


def _cmd_add_single(
    selector: ExtendedSelector,
    value: str,
    before: str | None = None,
    after: str | None = None,
    at: int | None = None,
    apply: bool = False,
) -> str:
    """Add logic for a single (non-glob) selector."""
    position = at if at is not None else -1
    kind = selector.pseudo_class if selector.pseudo_class else None
    return add_to_component(
        selector,
        value,
        position=position,
        before=before,
        after=after,
        apply=apply,
        kind=kind,
    )


def cmd_add(
    selector_str: str,
    value: str,
    before: str | None = None,
    after: str | None = None,
    at: int | None = None,
    apply: bool = False,
    returns_filter: list[str] | None = None,
    type_oracle: TypeOracle | None = None,
) -> str:
    """Add new items to symbol components.

    - Position can be specified with --at, --before, or --after
    - Default is to append to end
    - Pseudo-class (e.g., :KEYWORD_ONLY) specifies parameter kind
    - If returns_filter or selector :returns[X] specified, only add to symbols
      whose return type matches (annotation first, then inferred via oracle)
    """
    selector = parse_extended_selector(selector_str)

    # Merge selector type_filter into returns_filter
    returns_filter = _merge_type_filter(selector, returns_filter)

    # When returns_filter is specified, expand the selector to only include
    # symbols that match the return type constraint.
    if returns_filter:
        files = (
            selector.expand_file_glob()
            if selector.has_file_glob()
            else [selector.file_path]
        )
        all_results = []
        for fpath in files:
            concrete_base = selector.with_file_path(fpath) if fpath != selector.file_path else selector
            matching = _expand_selector_with_returns_filter(
                concrete_base, returns_filter, type_oracle,
            )
            for concrete in matching:
                try:
                    result = _cmd_add_single(concrete, value=value, before=before, after=after, at=at, apply=apply)
                    if result:
                        all_results.append(result)
                except (ValueError, FileNotFoundError):
                    continue
        if not all_results:
            raise ValueError(f"No symbols found matching {selector_str} with --returns {returns_filter}")
        return '\n'.join(all_results)

    # Multi-file dispatch for file globs
    if selector.has_file_glob():
        expanded_files = selector.expand_file_glob()
        all_results = []
        for fpath in expanded_files:
            concrete = selector.with_file_path(fpath)
            try:
                result = _cmd_add_single(concrete, value=value, before=before, after=after, at=at, apply=apply)
                if result:
                    all_results.append(result)
            except (ValueError, FileNotFoundError):
                continue
        if not all_results:
            raise ValueError(f"No symbols found matching {selector_str}")
        return '\n'.join(all_results)

    return _cmd_add_single(selector, value=value, before=before, after=after, at=at, apply=apply)
