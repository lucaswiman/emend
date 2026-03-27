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
    worktrees share a single parse.db.
    """
    root = Path(project_root).resolve()
    
    # 1. Check for git (regular or worktree)
    git_path = root / ".git"
    if git_path.exists():
        if git_path.is_file():
            # Worktree: .git is a file like "gitdir: /main/.git/worktrees/foo"
            try:
                text = git_path.read_text().strip()
                if text.startswith("gitdir:"):
                    gitdir = Path(text.split(":", 1)[1].strip())
                    if not gitdir.is_absolute():
                        gitdir = (root / gitdir).resolve()
                    commondir_file = gitdir / "commondir"
                    if commondir_file.is_file():
                        commondir = commondir_file.read_text().strip()
                        main_git_dir = (gitdir / commondir).resolve()
                        return main_git_dir.parent
            except OSError:
                pass
        else:
            # Regular git repo
            return root

    # 2. Check for .emend marker
    if (root / ".emend").is_dir():
        return root

    # 3. Fall back to project_root unchanged
    return root


def _cache_db_dir(project_root: str | Path) -> Path:
    """Return the directory for the shared cache DB."""
    main_root = _resolve_cache_root(str(project_root))
    return main_root / ".emend" / "cache"


def _knowledge_db_dir(project_root: str | Path) -> Path:
    """Return the directory for user-managed mapping data.

    Unlike cache data, mappings are user-created content that cannot be
    recomputed, so they live directly in ``.emend/`` rather than
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
        "  bases TEXT,"
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
# CozoDB facts database (lazy singleton, separate from parse.db)
# ---------------------------------------------------------------------------

_facts_db_cache: dict[str, object] = {}  # project_root → CozoDB client
_facts_db_lock = _threading.Lock()

_FACTS_SCHEMA = """\
{:create fact_symbol {
    fp: String,
    mqn: String
    =>
    name: String,
    qn: String default "",
    kind: String,
    line: Int,
    end_line: Int,
    depth: Int default 1,
    parent: String default "",
    bases: String default "",
    sig: String default "",
    returns: String default "",
    decs: String default "",
    is_entry: Bool default false,
    is_exported: Bool default false,
    has_noqa: Bool default false
}}

{:create fact_reference {
    tqn: String,
    fp: String,
    line: Int,
    col: Int
    =>
    kind: String
}}

{:create fact_import {
    fp: String,
    mod: String
}}
"""


def _facts_db_path_from_parse_db(parse_db_path: str | Path) -> str:
    """Derive the CozoDB facts.db path from a parse.db path."""
    return str(Path(parse_db_path).parent / "facts.db")


def _open_facts_db(db_path: str):
    """Open (or create) a CozoDB facts database at *db_path*."""
    try:
        from emend import emend_core as _rust
        client = _rust.PyCozoDb("sqlite", db_path)
    except (ImportError, AttributeError):
        from pycozo import Client  # type: ignore[import-untyped]
        client = Client("sqlite", db_path)

    # Create relations if they don't exist.
    for stmt in _FACTS_SCHEMA.strip().split("\n\n"):
        stmt = stmt.strip()
        if stmt:
            try:
                client.run(stmt)
            except Exception:
                pass  # Already exists
    return client


def _get_facts_db(project_root: str | None = None):
    """Return a lazily-initialized CozoDB facts database for *project_root*, or None.

    If *project_root* is None, derives from the current working directory.
    Returns None if the facts.db doesn't exist yet (i.e. no dual-write has
    populated it), so callers fall back to SQLite.
    """
    if project_root is None:
        try:
            project_root = _find_project_root(".")
        except Exception:
            return None

    key = str(Path(project_root).resolve())
    cached = _facts_db_cache.get(key)
    if cached is not None:
        return cached

    with _facts_db_lock:
        # Double-check after acquiring lock
        cached = _facts_db_cache.get(key)
        if cached is not None:
            return cached

        try:
            cache_dir = _cache_db_dir(project_root)
            db_path = cache_dir / "facts.db"
            if not db_path.exists():
                logger.debug("facts db not found at %s", db_path)
                return None
            client = _open_facts_db(str(db_path))
            _facts_db_cache[key] = client
            logger.debug("facts db opened at %s", db_path)
            return client
        except Exception as exc:
            logger.debug("facts db unavailable: %s", exc)
            return None


def _write_facts_batch(
    facts_db_path: str,
    file_paths_to_clear: list[str],
    sym_rows: list[tuple],
    import_rows: list[tuple],
    ref_rows: list[tuple],
) -> None:
    """Write a batch of facts to the CozoDB facts database.

    Called from ``_index_batch`` subprocesses.  Each subprocess opens its
    own CozoDB connection (SQLite WAL handles concurrent writers).

    Args:
        facts_db_path: Path to the facts.db file.
        file_paths_to_clear: File paths whose old facts should be removed.
        sym_rows: Symbol tuples from _index_batch (17-element).
        import_rows: Import tuples (content_hash, file_path, module).
        ref_rows: Reference tuples (content_hash, qn, file_path, line, col, kind).
    """
    try:
        fdb = _open_facts_db(facts_db_path)
    except BaseException:
        return

    try:
        # Delete old facts for changed files
        if file_paths_to_clear:
            for fp in file_paths_to_clear:
                try:
                    fdb.run(
                        "?[fp, mqn] := *fact_symbol[fp, mqn, _, _, _, _, _, _, _, _, _, _, _, _, _, _], "
                        "fp == $fp  :rm fact_symbol {fp => }", {"fp": fp}
                    )
                except Exception:
                    pass
                try:
                    fdb.run(
                        "?[tqn, fp, line, col] := *fact_reference[tqn, fp, line, col, _], "
                        "fp == $fp  :rm fact_reference {tqn, fp, line, col => }", {"fp": fp}
                    )
                except Exception:
                    pass
                try:
                    fdb.run(
                        "?[fp, mod] := *fact_import[fp, mod], "
                        "fp == $fp  :rm fact_import {fp, mod}", {"fp": fp}
                    )
                except Exception:
                    pass

        # Insert symbols
        if sym_rows:
            # sym_rows: (hash, file_path, name, qn, module_qn, kind, line, end_line,
            #            depth, parent, bases, sig, returns, decs, is_entry, is_exported, has_noqa)
            cozo_sym = [
                [r[1], r[4], r[2], r[3], r[5], r[6], r[7], r[8],
                 r[9] or "", r[10] or "", r[11] or "", r[12] or "", r[13] or "",
                 bool(r[14]), bool(r[15]), bool(r[16])]
                for r in sym_rows
            ]
            fdb.run(
                "?[fp, mqn, name, qn, kind, line, end_line, depth, "
                "parent, bases, sig, returns, decs, is_entry, is_exported, has_noqa] <- $rows "
                ":put fact_symbol {fp, mqn => name, qn, kind, line, end_line, depth, "
                "parent, bases, sig, returns, decs, is_entry, is_exported, has_noqa}",
                {"rows": cozo_sym},
            )

        # Insert references
        if ref_rows:
            # ref_rows: (hash, target_qn, file_path, line, col, kind)
            cozo_ref = [[r[1], r[2], r[3], r[4], r[5]] for r in ref_rows]
            fdb.run(
                "?[tqn, fp, line, col, kind] <- $rows "
                ":put fact_reference {tqn, fp, line, col => kind}",
                {"rows": cozo_ref},
            )

        # Insert imports
        if import_rows:
            # import_rows: (hash, file_path, module)
            cozo_imp = [[r[1], r[2]] for r in import_rows]
            fdb.run(
                "?[fp, mod] <- $rows "
                ":put fact_import {fp, mod}",
                {"rows": cozo_imp},
            )

    except BaseException:
        logger.debug("facts db write failed", exc_info=True)

    try:
        fdb.close()
    except BaseException:
        pass


def _populate_facts_db(project_root: str) -> None:
    """Populate CozoDB facts.db from SQLite parse.db.

    Called once after indexing completes (from the main process) to avoid
    SQLite lock panics from concurrent subprocess CozoDB writes.  Reads
    all symbols, references, and imports from parse.db and bulk-inserts
    them into facts.db.
    """
    import sqlite3 as _sql3

    cache_dir = _cache_db_dir(project_root)
    db_path = cache_dir / "parse.db"
    if not db_path.exists():
        return

    facts_path = str(cache_dir / "facts.db")

    try:
        fdb = _open_facts_db(facts_path)
    except BaseException:
        return

    try:
        conn = _sql3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")

        worktree_id = _get_worktree_id(project_root)

        # Symbols
        sym_rows = conn.execute(
            "SELECT si.file_path, si.module_qn, si.name, si.qualified_name, si.kind, "
            "si.line, si.end_line, si.depth, si.parent, si.bases, si.signature, "
            "si.returns, si.decorators, si.is_entry_point, si.is_exported, si.has_noqa "
            "FROM symbol_index si "
            "INNER JOIN file_manifest fm "
            "  ON si.content_hash = fm.content_hash AND si.file_path = fm.path "
            "  AND fm.worktree_id = ?",
            (worktree_id,),
        ).fetchall()

        if sym_rows:
            cozo_sym = [
                [r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                 r[8] or "", r[9] or "", r[10] or "", r[11] or "", r[12] or "",
                 bool(r[13]), bool(r[14]), bool(r[15])]
                for r in sym_rows
            ]
            fdb.run(
                "?[fp, mqn, name, qn, kind, line, end_line, depth, "
                "parent, bases, sig, returns, decs, is_entry, is_exported, has_noqa] <- $rows "
                ":put fact_symbol {fp, mqn => name, qn, kind, line, end_line, depth, "
                "parent, bases, sig, returns, decs, is_entry, is_exported, has_noqa}",
                {"rows": cozo_sym},
            )

        # References
        ref_rows = conn.execute(
            "SELECT ri.target_qn, ri.file_path, ri.line, ri.col, ri.ref_kind "
            "FROM reference_index ri "
            "INNER JOIN file_manifest fm "
            "  ON ri.content_hash = fm.content_hash AND ri.file_path = fm.path "
            "  AND fm.worktree_id = ?",
            (worktree_id,),
        ).fetchall()

        if ref_rows:
            cozo_ref = [list(r) for r in ref_rows]
            fdb.run(
                "?[tqn, fp, line, col, kind] <- $rows "
                ":put fact_reference {tqn, fp, line, col => kind}",
                {"rows": cozo_ref},
            )

        # Imports
        imp_rows = conn.execute(
            "SELECT ig.file_path, ig.imported_module "
            "FROM import_graph ig "
            "INNER JOIN file_manifest fm "
            "  ON ig.content_hash = fm.content_hash AND ig.file_path = fm.path "
            "  AND fm.worktree_id = ?",
            (worktree_id,),
        ).fetchall()

        if imp_rows:
            cozo_imp = [list(r) for r in imp_rows]
            fdb.run(
                "?[fp, mod] <- $rows "
                ":put fact_import {fp, mod}",
                {"rows": cozo_imp},
            )

        conn.close()
    except BaseException:
        logger.debug("facts db population failed", exc_info=True)

    try:
        fdb.close()
    except BaseException:
        pass


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
            conn.commit()
            conn.close()
        except Exception:
            pass

    # NOTE: CozoDB dual-write is NOT done here — it's done by the caller
    # (_populate_facts_db) after all workers complete, to avoid SQLite
    # lock panics from concurrent subprocess writes.

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
    language: str = "python",
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
            _src_root = _find_source_root(project_root, language=language)
            _index_batch((str(db_path), _src_root, project_root, files_to_index))
            # Populate CozoDB facts.db from the freshly-written SQLite data.
            try:
                _populate_facts_db(project_root)
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
                        try:
                            fdb.run(
                                "?[fp, mqn] := *fact_symbol[fp, mqn, _, _, _, _, _, _, _, _, _, _, _, _, _, _], "
                                "fp == $fp  :rm fact_symbol {fp => }", {"fp": dp}
                            )
                            fdb.run(
                                "?[tqn, fp, line, col] := *fact_reference[tqn, fp, line, col, _], "
                                "fp == $fp  :rm fact_reference {tqn, fp, line, col => }", {"fp": dp}
                            )
                            fdb.run(
                                "?[fp, mod] := *fact_import[fp, mod], "
                                "fp == $fp  :rm fact_import {fp, mod}", {"fp": dp}
                            )
                        except BaseException:
                            pass
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

    project_root = _find_project_root(project_path)

    # Try CozoDB facts database first.
    results = _query_symbol_index_cozo(
        project_root,
        name_pattern=name_pattern,
        kind=kind,
        file_path=file_path,
        qualified_name=qualified_name,
        limit=limit,
    )
    if results is None:
        # Fallback to SQLite
        results = _query_symbol_index_sqlite(
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
            resolved = str(Path(file_path).resolve())
            clauses.append("fp == $file_path")
            params["file_path"] = resolved

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
        return [
            {
                "name": r[0],
                "qualified_name": r[1],
                "kind": r[2],
                "file_path": r[3],
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
        logger.debug("CozoDB query_symbol_index failed, falling back", exc_info=True)
        return None


def _query_symbol_index_sqlite(
    project_root: str,
    *,
    name_pattern: str | None = None,
    kind: str | None = None,
    file_path: str | None = None,
    qualified_name: str | None = None,
    limit: int = 0,
) -> list[dict] | None:
    """Query symbol_index via SQLite (legacy fallback)."""
    import sqlite3 as _sql3

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
    language: str = "python",
) -> list[dict] | None:
    """Query references via CozoDB (with SQLite fallback).

    Returns a list of dicts with reference info, or None if the index
    is not available or not fresh.
    """
    if not _ensure_index_fresh(project_path, language=language):
        return None

    project_root = _find_project_root(project_path)

    # Try CozoDB first
    fdb = _get_facts_db(project_root)
    if fdb is not None:
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
            return [
                {"file_path": r[0], "line": r[1], "col": r[2], "ref_kind": r[3]}
                for r in result["rows"]
            ]
        except Exception:
            logger.debug("CozoDB query_reference_index failed, falling back", exc_info=True)

    # SQLite fallback
    import sqlite3 as _sql3

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
        results = [
            {"file_path": r[0], "line": r[1], "col": r[2], "ref_kind": r[3]}
            for r in rows
        ]
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
    """Query for files importing a module (CozoDB with SQLite fallback).

    Returns file paths, or None if index not available.
    """
    project_root = _find_project_root(project_path)

    # Try CozoDB first
    fdb = _get_facts_db(project_root)
    if fdb is not None:
        try:
            result = fdb.run(
                "?[fp] := *fact_import[fp, mod], mod == $mod",
                {"mod": imported_module},
            )
            return [r[0] for r in result["rows"]]
        except Exception:
            logger.debug("CozoDB query_import_graph failed, falling back", exc_info=True)

    # SQLite fallback
    import sqlite3 as _sql3

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

    # Phase 5: populate CozoDB facts.db from SQLite.
    # Done here (main process, single-threaded) to avoid SQLite lock
    # panics from concurrent subprocess writes.
    try:
        _populate_facts_db(project_root)
    except BaseException:
        logger.debug("warm_caches: facts db population failed", exc_info=True)

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

    Language-specific detection:

    **Python** -- checks (in order):
    1. ``pyproject.toml`` settings (maturin, setuptools, hatch)
    2. ``setup.cfg`` [options] package_dir
    3. Heuristic: ``src/`` exists and contains a package (dir with ``__init__.py``)

    **Rust** -- checks ``Cargo.toml`` for ``[lib] path`` and ``src/`` directory.

    **TypeScript** -- checks ``tsconfig.json`` for ``rootDir``/``baseUrl`` and ``src/``.

    **Other languages** -- heuristic: ``src/`` exists.

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

    elif language == "rust":
        # Rust: check Cargo.toml for [lib] path or default src/
        cargo_toml = root / "Cargo.toml"
        if cargo_toml.is_file():
            try:
                import tomllib
            except ModuleNotFoundError:
                try:
                    import tomli as tomllib  # type: ignore[no-redef]
                except ModuleNotFoundError:
                    tomllib = None  # type: ignore[assignment]
            if tomllib is not None:
                try:
                    data = tomllib.loads(cargo_toml.read_text())
                    lib_path = data.get("lib", {}).get("path")
                    if lib_path:
                        candidate = (root / lib_path).parent
                        if candidate.is_dir():
                            return str(candidate)
                except Exception:
                    pass
        src_dir = root / "src"
        if src_dir.is_dir():
            return str(src_dir)

    elif language == "typescript":
        # TypeScript: check tsconfig.json for rootDir/baseUrl
        tsconfig = root / "tsconfig.json"
        if tsconfig.is_file():
            try:
                import json
                import re as _re
                raw = tsconfig.read_text()
                # Strip JSONC features: // comments, /* */ comments, trailing commas
                raw = _re.sub(r'//[^\n]*', '', raw)
                raw = _re.sub(r'/\*.*?\*/', '', raw, flags=_re.DOTALL)
                raw = _re.sub(r',\s*([}\]])', r'\1', raw)
                data = json.loads(raw)
                root_dir = data.get("compilerOptions", {}).get("rootDir")
                if root_dir:
                    candidate = root / root_dir
                    if candidate.is_dir():
                        return str(candidate)
                base_url = data.get("compilerOptions", {}).get("baseUrl")
                if base_url and base_url != ".":
                    candidate = root / base_url
                    if candidate.is_dir():
                        return str(candidate)
            except Exception:
                pass
        src_dir = root / "src"
        if src_dir.is_dir():
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


def _get_imports(source_code: str, language: str = "python") -> str:
    """Extract all top-level import statements as a single string."""
    from emend.language_plugins import load_plugin
    return load_plugin(language).import_handler.extract_imports(source_code)


def _add_import_text(
    import_str: str,
    position: int,
    file_path: Path,
    apply: bool,
    source_code: str,
    language: str = "python",
) -> str:
    """Add an import statement to a file using text manipulation.

    Args:
        import_str: Import statement to add (e.g., "import os")
        position: 0 for prepend, -1 for append
        file_path: Path to the file
        apply: Whether to apply changes
        source_code: Original source code
        language: Source language for import handling

    Returns:
        Unified diff showing changes
    """
    from emend.language_plugins import load_plugin
    try:
        new_code = load_plugin(language).import_handler.add_import_text(
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
            return _get_imports(source_code, language=selector.language)
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
        return _add_import_text(value, position, file_path, apply, source_code, language=selector.language)

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
        file_path: Path to source file to search
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

    # Auto-detect language from file extension when caller used the default
    if language == "python" and file_path:
        from emend.language_registry import detect_language
        detected = detect_language(file_path)
        if detected:
            language = detected

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


def _is_valid_replacement(code: str, language: str = "python") -> bool:
    """Verify if the given code string parses as valid syntax.

    For Python, uses the stdlib ``ast`` module.  For other languages, attempts
    a tree-sitter parse and checks that the tree has no ERROR nodes.  Falls
    back to ``True`` (accept the replacement) if parsing is unavailable.
    """
    if language == "python":
        try:
            ast.parse(code, mode='eval')
            return True
        except SyntaxError:
            try:
                ast.parse(code, mode='exec')
                return True
            except SyntaxError:
                return False
    else:
        # For non-Python languages, use tree-sitter validation via Rust
        try:
            from emend.language_registry import get_extensions
            exts = get_extensions(language)
            ext = exts[0] if exts else None
            if ext:
                return _rust.validate_syntax(code, ext)
        except (AttributeError, Exception):
            pass
        # If no tree-sitter validation is available, accept the replacement
        return True


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
        file_path: Path to source file to transform
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

        # Verify replacement parses as valid syntax
        if not _is_valid_replacement(replacement_code, language=language):
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


def _rename_in_docstrings(content: str, old_name: str, new_name: str, language: str = "python") -> str | None:
    """Replace old_name with new_name in all docstrings/doc comments."""
    from emend.language_plugins import load_plugin
    return load_plugin(language).comment_handler.rename_in_docstrings(content, old_name, new_name)


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


def _dead_code_postfilter(
    rows: list[tuple],
    scan_root: str,
    all_files: bool,
    strings_count_as_references: bool,
    entry_point_decorators: list[str] | None,
    exclude_references_from: list[str] | None,
) -> list[DeadSymbol]:
    """Shared post-filter for dead code candidates.

    Applies entry-point decorator filtering and string-literal scanning
    to the candidate rows returned by either CozoDB or SQLite.

    Args:
        rows: List of (name, qname, kind, file_path, line, decorators) tuples.
    """
    # Post-filter: custom entry point decorators.
    if entry_point_decorators and rows:
        ep_decs = set(entry_point_decorators)
        ep_basenames = {d.rsplit(".", 1)[-1] for d in ep_decs}
        filtered = []
        for row in rows:
            dec_str = row[5]  # decorators column
            if dec_str:
                skip = False
                for dec in dec_str.split(","):
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

    if not strings_count_as_references:
        return [
            DeadSymbol(
                file_path=fp, name=name, kind=sym_kind,
                line=line, selector=f"{fp}::{qname}",
                reason="no references found",
            )
            for name, qname, sym_kind, fp, line, _decs in rows
        ]

    # String-literal post-filter
    str_names = {name for name, _, _, _, _, _ in rows if len(name) > 3}

    names_with_str_ref: set[tuple[str, str]] = set()
    if str_names:
        source_files = _collect_source_files(
            scan_root, git_tracked_only=not all_files,
        )

        _exclude_prefixes: list[str] = []
        _exclude_globs: list[str] = []
        if exclude_references_from:
            import fnmatch as _fnmatch
            for pattern in exclude_references_from:
                if "*" in pattern or "?" in pattern:
                    if not pattern.startswith("*") and not Path(pattern).is_absolute():
                        pattern = str(Path(scan_root) / pattern)
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

        for _, _, _, fp, _, _ in rows:
            r = str(Path(fp).resolve())
            if r not in file_cache:
                try:
                    file_cache[r] = Path(fp).read_text()
                except Exception:
                    pass

        _strip_re = re.compile(r"'[^']*'|\"[^\"]*\"")
        for name, qname, sym_kind, fp, line, _decs in rows:
            if len(name) <= 3:
                continue
            r = str(Path(fp).resolve())

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

            for other_r, other_content in file_cache.items():
                if other_r == r:
                    continue
                if name in other_content:
                    names_with_str_ref.add((fp, name))
                    break

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


def _find_dead_code_cozo(
    scan_root: str,
    kind: str | None,
    include_private: bool,
    exclude_references_from: list[str] | None,
    entry_point_names: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> list[tuple] | None:
    """CozoScript dead code candidate query.

    Returns rows as (name, qualified_name, kind, file_path, line, decorators)
    tuples, or None if CozoDB is unavailable.  Post-filtering for
    entry_point_decorators and string literals is done by the caller.
    """
    fdb = _get_facts_db(_find_project_root(scan_root))
    if fdb is None:
        return None

    try:
        # Build the Datalog query piece by piece.
        # has_ref: symbols that have at least one external reference
        # (by qualified_name or module_qn, excluding self-references).
        rules = [
            "has_ref[mqn] := "
            "*fact_reference[mqn, ref_fp, ref_line, _, _], "
            "*fact_symbol[sym_fp, mqn, _, _, _, sym_line, _, _, _, _, _, _, _, _, _, _], "
            "not (ref_fp == sym_fp, ref_line == sym_line)",
        ]

        # Also check references via module_qn
        rules.append(
            "has_ref[mqn] := "
            "*fact_symbol[_, mqn, _, qn, _, _, _, _, _, _, _, _, _, _, _, _], "
            "qn != \"\", "
            "*fact_reference[qn, ref_fp, ref_line, _, _], "
            "*fact_symbol[sym_fp, mqn, _, _, _, sym_line, _, _, _, _, _, _, _, _, _, _], "
            "not (ref_fp == sym_fp, ref_line == sym_line)"
        )

        # If exclude_references_from, add a more selective has_ref
        if exclude_references_from:
            # For now, fall back to SQLite for this complex case
            # (CozoDB lacks LIKE/GLOB for path matching)
            return None

        # Build candidate conditions
        candidate_clauses = [
            "*fact_symbol[fp, mqn, name, qn, kind, line, end_line, depth, "
            "parent, bases, sig, returns, decs, is_entry, is_exported, has_noqa]",
            "depth == 1",
            "is_entry == false",
            "is_exported == false",
            "has_noqa == false",
            "not has_ref[mqn]",
        ]

        if kind == "function":
            candidate_clauses.append('kind in ["function", "async_function"]')
        elif kind == "class":
            candidate_clauses.append('kind == "class"')
        else:
            candidate_clauses.append(
                'kind in ["function", "async_function", "method", "async_method", "class"]'
            )

        if not include_private:
            candidate_clauses.append(
                '(not starts_with(name, "_") or starts_with(name, "__"))'
            )

        if exclude_paths:
            for ep in exclude_paths:
                resolved = str(Path(ep).resolve()) if not ep.startswith("*") else ep
                if "*" not in resolved:
                    candidate_clauses.append(f'not starts_with(fp, "{resolved}")')
                else:
                    # Complex glob — fall back to SQLite
                    return None

        if entry_point_names:
            # CozoDB doesn't have NOT IN for inline lists easily;
            # filter in Python post-processing instead
            pass

        rules.append(
            "?[name, qn, kind, fp, line, decs] := "
            + ", ".join(candidate_clauses)
            + "\n:order fp, line"
        )

        query = "\n".join(rules)
        result = fdb.run(query)

        rows = [tuple(r) for r in result["rows"]]

        # Post-filter entry_point_names in Python
        if entry_point_names:
            ep_set = set(entry_point_names)
            rows = [r for r in rows if r[0] not in ep_set]

        return rows
    except Exception:
        logger.debug("CozoDB dead code query failed, falling back to SQLite", exc_info=True)
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
    language: str = "python",
) -> list[DeadSymbol]:
    """Index-accelerated dead code detection.

    Uses CozoDB Datalog when available, with SQLite ``parse.db`` fallback.
    The heavy lifting (candidate filtering + reference checking) is a
    Datalog query (or SQL ``NOT EXISTS``); the lightweight string-literal
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
    if not _ensure_index_fresh(scan_root, language=language):
        logger.info("dead_code: index stale/missing — warming caches")
        warm_caches(scan_root, type_engine="none", language=language)

    # Try CozoDB Datalog path first (returns candidate rows or None).
    cozo_rows = _find_dead_code_cozo(
        scan_root, kind, include_private, exclude_references_from,
        entry_point_names=entry_point_names,
        exclude_paths=exclude_paths,
    )
    if cozo_rows is not None:
        # cozo_rows: (name, qn, kind, file_path, line, decorators)
        # Apply the same post-filtering as the SQLite path below.
        rows = cozo_rows
        # Jump past the SQL query section to the shared post-filter.
        # (We reuse the exact same decorator and string-literal logic.)
        return _dead_code_postfilter(
            rows, scan_root, all_files, strings_count_as_references,
            entry_point_decorators, exclude_references_from,
        )

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

        return _dead_code_postfilter(
            rows, scan_root, all_files, strings_count_as_references,
            entry_point_decorators, exclude_references_from,
        )

    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Impact analysis
# ---------------------------------------------------------------------------

@dataclass
class ImpactEdge:
    """A witness edge showing why a symbol is impacted."""
    source: str  # selector of the causing symbol
    target: str  # selector of the impacted symbol
    kind: str  # "calls", "references", "test"


@dataclass
class ImpactResult:
    """Result of impact analysis."""
    changed_symbols: list[str]  # selectors of directly changed symbols
    impacted_symbols: list[str]  # selectors of transitively impacted symbols
    impacted_tests: list[str]  # test file paths or test selectors
    edges: list[ImpactEdge]  # witness edges


def _parse_diff_to_changed_files(diff_text: str) -> list[tuple[str, list[int]]]:
    """Parse unified diff output to extract changed file paths and line numbers.

    Returns a list of (file_path, changed_lines) tuples where changed_lines
    are the line numbers in the *new* version of the file that were modified.
    """
    results: list[tuple[str, list[int]]] = []
    current_file: str | None = None
    changed_lines: list[int] = []

    for line in diff_text.splitlines():
        # Detect file header: +++ b/path/to/file.py
        if line.startswith('+++ b/'):
            # Save previous file if any
            if current_file is not None:
                results.append((current_file, changed_lines))
            current_file = line[6:]  # strip '+++ b/'
            changed_lines = []
        # Detect hunk header: @@ -old_start,old_count +new_start,new_count @@
        elif line.startswith('@@') and current_file is not None:
            m = re.search(r'\+(\d+)(?:,(\d+))?', line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                # Track all lines in the hunk range as potentially changed
                changed_lines.extend(range(start, start + count))

    # Don't forget the last file
    if current_file is not None:
        results.append((current_file, changed_lines))

    return results


def _parse_diff_to_selectors(
    diff_spec: str,
    project_path: str,
) -> list[str]:
    """Run ``git diff`` and map changed lines to symbol selectors.

    Args:
        diff_spec: Git diff specification (e.g. ``"HEAD"``, ``"abc..def"``).
        project_path: Project root directory (used as cwd for git).

    Returns:
        List of selector strings for symbols touched by the diff.
    """
    import subprocess

    result = subprocess.run(
        ['git', 'diff', '-U0', diff_spec],
        capture_output=True, text=True, timeout=30,
        cwd=project_path,
    )
    if result.returncode != 0:
        raise ValueError(
            f"git diff failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    changed_files = _parse_diff_to_changed_files(result.stdout)
    if not changed_files:
        return []

    from .ast_utils import find_nested_definitions, find_symbol_by_line

    selectors: list[str] = []
    seen: set[str] = set()

    for file_rel, lines in changed_files:
        file_path = str(Path(project_path) / file_rel)
        if not Path(file_path).is_file():
            continue

        # Only process source files we can parse
        from emend.language_registry import is_source_file
        if not is_source_file(file_path):
            continue

        try:
            symbols = find_nested_definitions(file_path)
        except Exception:
            continue

        for line_no in lines:
            sym = find_symbol_by_line(symbols, line_no)
            if sym is not None:
                sel = f"{file_path}::{'.'.join(sym.path)}"
                if sel not in seen:
                    seen.add(sel)
                    selectors.append(sel)

    return selectors


def _is_test_file(file_path: str) -> bool:
    """Check if a file is a test file by path heuristics."""
    p = Path(file_path)
    name = p.name
    if name.startswith('test_') or name.endswith('_test.py'):
        return True
    parts = p.parts
    if 'tests' in parts or 'test' in parts:
        return True
    return False


def _is_test_symbol(selector: str) -> bool:
    """Check if a selector refers to a test symbol."""
    if '::' in selector:
        sym_part = selector.split('::', 1)[1]
        # e.g. test_foo or TestFoo or TestFoo.test_method
        first_name = sym_part.split('.')[0]
        return first_name.startswith('test_') or first_name.startswith('Test')
    return False


def find_impact(
    selectors: list[ExtendedSelector] | None = None,
    diff_spec: str | None = None,
    project_path: str | None = None,
    max_depth: int = 10,
) -> ImpactResult:
    """Compute the transitive set of impacted symbols from changed symbols or a diff.

    Either *selectors* or *diff_spec* must be provided.

    Args:
        selectors: Directly specified changed symbols.
        diff_spec: Git diff specification (e.g. ``"HEAD"``, ``"abc..def"``).
            Parsed to extract changed symbols automatically.
        project_path: Project root (auto-detected if None).
        max_depth: Maximum BFS depth for transitive closure (default 10).

    Returns:
        ImpactResult with changed symbols, impacted symbols, tests, and edges.

    Raises:
        ValueError: If neither selectors nor diff_spec is provided, or on git errors.
    """
    if not selectors and not diff_spec:
        raise ValueError("Either selectors or diff_spec must be provided")

    # Resolve project root
    if project_path:
        proj_root = project_path
    elif selectors:
        proj_root = _find_project_root(selectors[0].file_path)
    else:
        proj_root = _find_project_root('.')

    # Step 1: Determine changed symbols
    changed_selectors: list[str] = []

    if selectors:
        for sel in selectors:
            if sel.symbol_path:
                changed_selectors.append(
                    f"{sel.file_path}::{'.'.join(sel.symbol_path)}"
                )

    if diff_spec:
        diff_sels = _parse_diff_to_selectors(diff_spec, proj_root)
        changed_selectors.extend(diff_sels)

    if not changed_selectors:
        return ImpactResult(
            changed_symbols=[],
            impacted_symbols=[],
            impacted_tests=[],
            edges=[],
        )

    # Step 2: BFS to compute transitive reverse-caller closure
    from .ast_utils import find_nested_definitions, find_symbol_by_line

    all_edges: list[ImpactEdge] = []
    visited: set[str] = set(changed_selectors)
    impacted: list[str] = []
    frontier: list[str] = list(changed_selectors)

    for _depth in range(max_depth):
        next_frontier: list[str] = []

        for sel_str in frontier:
            try:
                sel = parse_extended_selector(sel_str)
            except Exception:
                continue

            if not sel.symbol_path:
                continue

            try:
                callers = list(find_callers(sel, project_path=proj_root))
            except (ValueError, FileNotFoundError):
                continue

            for caller_ref in callers:
                caller_file = caller_ref.file_path
                caller_line = caller_ref.line

                try:
                    symbols = find_nested_definitions(caller_file)
                    caller_sym = find_symbol_by_line(symbols, caller_line)
                except Exception:
                    continue

                if caller_sym is None:
                    continue

                caller_sel = f"{caller_file}::{'.'.join(caller_sym.path)}"

                all_edges.append(ImpactEdge(
                    source=sel_str,
                    target=caller_sel,
                    kind="calls",
                ))

                if caller_sel not in visited:
                    visited.add(caller_sel)
                    impacted.append(caller_sel)
                    next_frontier.append(caller_sel)

        frontier = next_frontier
        if not frontier:
            break

    # Step 3: Identify impacted tests
    impacted_tests: list[str] = []
    all_impacted = changed_selectors + impacted

    for sel_str in all_impacted:
        if '::' in sel_str:
            file_part = sel_str.split('::', 1)[0]
        else:
            file_part = sel_str

        if _is_test_file(file_part) or _is_test_symbol(sel_str):
            if sel_str not in impacted_tests:
                impacted_tests.append(sel_str)
                # Add a "test" edge from the changed symbol to the test
                # Find the first edge that led to this test
                for edge in all_edges:
                    if edge.target == sel_str:
                        # Already has a "calls" edge; add a "test" annotation
                        all_edges.append(ImpactEdge(
                            source=edge.source,
                            target=sel_str,
                            kind="test",
                        ))
                        break

    return ImpactResult(
        changed_symbols=changed_selectors,
        impacted_symbols=impacted,
        impacted_tests=impacted_tests,
        edges=all_edges,
    )


# ---------------------------------------------------------------------------
# Semantic context — situational awareness for code agents
# ---------------------------------------------------------------------------

# Default decorators that indicate a symbol is an external interface
_EXTERNAL_INTERFACE_DECORATORS = frozenset({
    'app.route', 'app.get', 'app.post', 'app.put', 'app.delete', 'app.patch',
    'router.get', 'router.post', 'router.put', 'router.delete', 'router.patch',
    'api_view', 'action',
    'rpc_endpoint', 'grpc_method',
    'click.command', 'click.group',
    'app.command',
    'strawberry.mutation', 'strawberry.query', 'strawberry.subscription',
    'graphene.resolve',
    'task', 'celery.task', 'shared_task',
    'webhook', 'endpoint',
    'message_handler', 'event_handler',
})

_EXTERNAL_INTERFACE_BASENAMES = frozenset({
    'route', 'get', 'post', 'put', 'delete', 'patch', 'head', 'options',
    'command', 'task', 'endpoint', 'webhook',
    'mutation', 'query', 'subscription',
    'rpc', 'grpc', 'api',
})

# Patterns in callees that indicate async side effects
_ASYNC_SIDE_EFFECT_PATTERNS = frozenset({
    'delay', 'apply_async', 'send_task',
    'submit', 'create_task', 'ensure_future',
    'run_in_executor',
})

# Patterns in callees that indicate I/O or external effects
_SIDE_EFFECT_CALLEE_PATTERNS = {
    'db_write': {'save', 'commit', 'add', 'delete', 'update', 'insert',
                 'execute', 'executemany', 'bulk_create', 'bulk_update'},
    'network': {'request', 'get', 'post', 'put', 'fetch', 'urlopen', 'send'},
    'file_io': {'write', 'open', 'unlink', 'remove', 'rename', 'mkdir'},
    'cache': {'set', 'delete', 'clear', 'invalidate'},
}

# Caching decorators that may need invalidation on mutations
_CACHE_DECORATORS = frozenset({
    'cache', 'lru_cache', 'cached_property', 'cache_page',
    'cache_control', 'memoize', 'cacheable',
})

# Regex for detecting a name inside string literals (matches dead code approach)
_STRING_LITERAL_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def _parse_decorator_name(dec: str) -> tuple[str, str]:
    """Return (full_name, basename) from a raw decorator string."""
    dec_clean = dec.lstrip('@').split('(')[0].strip()
    dec_basename = dec_clean.rsplit('.', 1)[-1] if '.' in dec_clean else dec_clean
    return dec_clean, dec_basename


@dataclass
class Danger:
    """A potential hazard the agent should know about before editing."""
    level: str  # "high", "medium", "low"
    category: str
    message: str
    evidence: str  # file:line or brief code snippet


@dataclass
class DataFlow:
    """A data input or output of the symbol."""
    name: str
    type_annotation: str | None = None
    flows_to: list[str] | None = None
    flows_from: list[str] | None = None
    note: str | None = None


@dataclass
class SideEffect:
    """A side effect performed by the symbol."""
    kind: str  # 'db_write', 'network', 'file_io', 'cache', 'async_task', 'external_call'
    target: str
    evidence: str


@dataclass
class CallerInfo:
    """A caller of the symbol."""
    symbol: str  # selector-style path
    file: str
    line: int
    kind: str = "direct"  # "direct", "test", "indirect"


@dataclass
class TestInfo:
    """Test coverage information."""
    direct: list[str]
    indirect: list[str]


@dataclass
class SemanticContext:
    """Full semantic dossier on a symbol — the agent's situational awareness."""
    symbol: str  # qualified name
    kind: str
    file: str
    line: int
    end_line: int

    # Signature
    parameters: list[str]
    returns: str | None
    decorators: list[str]
    is_async: bool

    # The whole point — what could bite you
    dangers: list[Danger]

    # Data flow
    data_in: list[DataFlow]
    data_out: list[DataFlow]
    side_effects: list[SideEffect]

    # Relationships
    callers: list[CallerInfo]
    callees: list[str]
    references_count: int

    # Tests
    tests: TestInfo

    def to_dict(self) -> dict:
        """Serialize to JSON-friendly dict."""
        d: dict = {
            "symbol": self.symbol,
            "kind": self.kind,
            "file": self.file,
            "line": self.line,
            "end_line": self.end_line,
            "signature": {
                "parameters": self.parameters,
                "returns": self.returns,
                "decorators": self.decorators,
                "is_async": self.is_async,
            },
            "dangers": [
                {"level": dg.level, "category": dg.category,
                 "message": dg.message, "evidence": dg.evidence}
                for dg in self.dangers
            ],
            "flow": {
                "data_in": [
                    {k: v for k, v in {
                        "name": di.name, "type": di.type_annotation,
                        "flows_from": di.flows_from, "note": di.note,
                    }.items() if v is not None}
                    for di in self.data_in
                ],
                "data_out": [
                    {k: v for k, v in {
                        "name": do.name, "type": do.type_annotation,
                        "flows_to": do.flows_to, "note": do.note,
                    }.items() if v is not None}
                    for do in self.data_out
                ],
                "side_effects": [
                    {"kind": se.kind, "target": se.target, "evidence": se.evidence}
                    for se in self.side_effects
                ],
            },
            "callers": [
                {"symbol": c.symbol, "file": c.file, "line": c.line, "kind": c.kind}
                for c in self.callers
            ],
            "callees": self.callees,
            "references_count": self.references_count,
            "tests": {
                "direct": self.tests.direct,
                "indirect": self.tests.indirect,
            },
        }
        return d


def semantic_context(
    selector: ExtendedSelector,
    project_path: str | None = None,
    extra_interface_decorators: list[str] | None = None,
) -> SemanticContext:
    """Build a semantic dossier on a symbol.

    Composes callers, callees, references, and heuristic danger
    detection into a single structured result that gives an agent
    full situational awareness before making changes.

    Args:
        selector: Symbol to analyze.
        project_path: Project root (auto-detected if None).
        extra_interface_decorators: Additional decorator names that
            indicate external interfaces.

    Returns:
        SemanticContext with dangers, flow, callers, tests, etc.
    """
    from .ast_utils import find_nested_definitions, find_symbol_by_path

    file_path = selector.file_path
    symbol_path = selector.symbol_path
    if not symbol_path:
        raise ValueError("Symbol path is required for semantic_context")

    project_root = project_path or _find_project_root(file_path)

    # ---- Resolve the symbol -----------------------------------------------
    symbols = find_nested_definitions(file_path)
    target = find_symbol_by_path(symbols, symbol_path)
    if target is None:
        raise ValueError(f"Symbol not found: {'.'.join(symbol_path)}")

    qualified_name = f"{file_path}::{'.'.join(symbol_path)}"
    is_async = target.kind in ('async_function', 'async_method')

    if not Path(file_path).exists():
        raise ValueError(f"File not found: {file_path}")

    # ---- Gather callers (partition test/non-test in one pass) -------------
    callers_list: list[CallerInfo] = []
    test_caller_count = 0
    non_test_caller_count = 0
    try:
        for ref in find_callers(selector, project_path=project_root):
            is_test = _is_test_file(ref.file_path)
            callers_list.append(CallerInfo(
                symbol=ref.file_path + f":{ref.line}",
                file=ref.file_path,
                line=ref.line,
                kind="test" if is_test else "direct",
            ))
            if is_test:
                test_caller_count += 1
            else:
                non_test_caller_count += 1
    except Exception as exc:
        logger.debug("semantic_context: find_callers failed: %s", exc)

    # ---- Gather callees ---------------------------------------------------
    callees_list: list[str] = []
    try:
        for callee in find_callees(selector, project_path=project_root):
            callees_list.append(callee.qualified_name or callee.name)
    except Exception as exc:
        logger.debug("semantic_context: find_callees failed: %s", exc)

    # ---- Count references -------------------------------------------------
    ref_count = 0
    try:
        for _ in find_references(selector, project_path=project_root,
                                 include_definition=False, include_imports=False):
            ref_count += 1
    except Exception as exc:
        logger.debug("semantic_context: find_references failed: %s", exc)

    # ---- Build interface decorators set -----------------------------------
    iface_decorators = set(_EXTERNAL_INTERFACE_DECORATORS)
    iface_basenames = set(_EXTERNAL_INTERFACE_BASENAMES)
    if extra_interface_decorators:
        for d in extra_interface_decorators:
            iface_decorators.add(d)
            if '.' in d:
                iface_basenames.add(d.rsplit('.', 1)[-1])
            else:
                iface_basenames.add(d)

    # ---- Detect dangers ---------------------------------------------------
    dangers: list[Danger] = []

    # Parse decorators once, reuse for interface + caching checks
    parsed_decorators = [_parse_decorator_name(dec) for dec in target.decorators]

    # 1. External interface decorators
    for dec_clean, dec_basename in parsed_decorators:
        if dec_clean in iface_decorators or dec_basename in iface_basenames:
            dangers.append(Danger(
                level="high",
                category="external_interface",
                message=f"Decorated with @{dec_clean} — signature is part of external API/protocol",
                evidence=f"{file_path}:{target.decorator_line_start or target.line_start}",
            ))

    # 2. Async side effects in callees
    for callee_name in callees_list:
        short_name = callee_name.rsplit('.', 1)[-1] if '.' in callee_name else callee_name
        if short_name in _ASYNC_SIDE_EFFECT_PATTERNS:
            dangers.append(Danger(
                level="high",
                category="async_side_effect",
                message=f"Calls {callee_name}() — triggers async/background work that completes after return",
                evidence=f"{file_path} (callee)",
            ))

    # 3. String references to this symbol (dynamic dispatch risk)
    # Uses same regex approach as dead code string scanning
    symbol_name = symbol_path[-1]
    if len(symbol_name) > 3:
        try:
            source_files = _collect_source_files(project_root)
            matched = _rust.read_and_filter_files(source_files, [symbol_name])
            str_ref_files: list[str] = []
            for fp, content in matched:
                for line_text in content.splitlines():
                    if symbol_name not in line_text:
                        continue
                    # Strip non-string content; if name disappears, it was in a string
                    stripped = _STRING_LITERAL_RE.sub("", line_text)
                    if symbol_name in line_text and symbol_name not in stripped:
                        str_ref_files.append(fp)
                        break
            if str_ref_files:
                dangers.append(Danger(
                    level="medium",
                    category="dynamic_reference",
                    message=f"Name '{symbol_name}' appears as string literal — renaming may miss dynamic references",
                    evidence=", ".join(str_ref_files[:3]) + (
                        f" (+{len(str_ref_files) - 3} more)" if len(str_ref_files) > 3 else ""
                    ),
                ))
        except Exception:
            pass  # best-effort

    # 4. High fan-out (many callers)
    if non_test_caller_count >= 10:
        dangers.append(Danger(
            level="high",
            category="high_fan_out",
            message=f"Called from {non_test_caller_count} non-test locations — changes have wide blast radius",
            evidence=f"{len(callers_list)} total callers ({non_test_caller_count} non-test)",
        ))
    elif non_test_caller_count >= 5:
        dangers.append(Danger(
            level="medium",
            category="high_fan_out",
            message=f"Called from {non_test_caller_count} non-test locations",
            evidence=f"{len(callers_list)} total callers ({non_test_caller_count} non-test)",
        ))

    # 5. Caching decorators (may need invalidation on mutations)
    for dec_clean, dec_basename in parsed_decorators:
        if dec_basename in _CACHE_DECORATORS:
            dangers.append(Danger(
                level="medium",
                category="caching",
                message=f"Decorated with @{dec_clean} — results are cached, mutations may serve stale data",
                evidence=f"{file_path}:{target.decorator_line_start or target.line_start}",
            ))

    # 6. No test coverage
    if test_caller_count == 0 and target.kind in ('function', 'async_function', 'method', 'async_method'):
        dangers.append(Danger(
            level="medium",
            category="no_test_coverage",
            message="No test files call this symbol directly",
            evidence="0 test callers found",
        ))

    # ---- Build data flow info ---------------------------------------------
    data_in: list[DataFlow] = []
    for param in target.parameters:
        # Parse "name: type = default" or just "name"
        param_name = param.split(':')[0].split('=')[0].strip()
        param_type = None
        if ':' in param:
            param_type = param.split(':', 1)[1].split('=')[0].strip()
        if param_name and param_name not in ('self', 'cls'):
            data_in.append(DataFlow(
                name=param_name,
                type_annotation=param_type,
            ))

    data_out: list[DataFlow] = []
    # Get return type from source if available
    # (NestedSymbol doesn't have returns, so we check SymbolInfo)
    try:
        from .query import query_symbols
        sym_infos = query_symbols(file_path, selector_str=qualified_name)
        if sym_infos and sym_infos[0].returns:
            data_out.append(DataFlow(
                name="return",
                type_annotation=sym_infos[0].returns,
            ))
    except Exception:
        pass

    # ---- Detect side effects from callees ---------------------------------
    # Build a prefix to identify local-scope callees (e.g., set.add on local vars)
    _module = _file_to_module(file_path, project_root)
    _local_prefix = f"{_module}.{'.'.join(symbol_path)}."
    side_effects: list[SideEffect] = []
    for callee_name in callees_list:
        # Skip builtins, unqualified names, and local-scope operations
        if (callee_name.startswith('builtins.') or
                '.' not in callee_name or
                callee_name.startswith(_local_prefix)):
            continue
        short = callee_name.rsplit('.', 1)[-1]
        for effect_kind, patterns in _SIDE_EFFECT_CALLEE_PATTERNS.items():
            if short in patterns:
                side_effects.append(SideEffect(
                    kind=effect_kind,
                    target=callee_name,
                    evidence=f"calls {callee_name}()",
                ))
                break
        if short in _ASYNC_SIDE_EFFECT_PATTERNS:
            side_effects.append(SideEffect(
                kind="async_task",
                target=callee_name,
                evidence=f"calls {callee_name}()",
            ))

    # ---- Classify tests ---------------------------------------------------
    direct_tests = [c.symbol for c in callers_list if c.kind == "test"]
    tests = TestInfo(direct=direct_tests, indirect=[])

    return SemanticContext(
        symbol=qualified_name,
        kind=target.kind,
        file=file_path,
        line=target.line_start,
        end_line=target.line_end,
        parameters=target.parameters,
        returns=data_out[0].type_annotation if data_out else None,
        decorators=target.decorators,
        is_async=is_async,
        dangers=dangers,
        data_in=data_in,
        data_out=data_out,
        side_effects=side_effects,
        callers=callers_list,
        callees=callees_list,
        references_count=ref_count,
        tests=tests,
    )


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


@dataclass
class DeletePlan:
    """A plan for safe-deleting a symbol and its cascade targets."""
    target: str  # selector of the original target
    deletions: list[dict]  # [{selector, file_path, name, kind, line, reason}]
    diffs: dict[str, str]  # file_path -> unified diff


def safe_delete(
    selector: ExtendedSelector,
    cascade: bool = False,
    project_path: str | None = None,
    apply: bool = False,
) -> DeletePlan:
    """Delete a symbol and optionally cascade to newly-dead dependents.

    Without ``--cascade``, removes the target symbol only.  With cascade,
    iteratively identifies symbols that become dead after the deletion
    (i.e. symbols whose *only* remaining callers are in the delete set)
    and includes them in the plan.

    Args:
        selector: Symbol to delete.
        cascade: If True, transitively delete newly-dead dependents.
        project_path: Project root (auto-detected if None).
        apply: If True, write changes to files.

    Returns:
        A ``DeletePlan`` with the list of deletions and per-file diffs.
    """
    from .ast_utils import find_nested_definitions, find_symbol_by_path

    scan_root = project_path or _find_project_root(selector.file_path)

    # ----- Phase 1: Build the delete set via BFS -------------------------
    delete_set: list[dict] = []  # [{selector_str, file_path, name, kind, line, reason}]
    delete_qns: set[str] = set()  # qualified names already scheduled

    # Seed with the target.
    file_path = str(Path(selector.file_path).resolve())
    symbols = find_nested_definitions(file_path)
    target_sym = find_symbol_by_path(symbols, selector.symbol_path)
    if target_sym is None:
        raise ValueError(
            f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}"
        )

    module_root = _find_project_root(selector.file_path)
    target_module = _file_to_module(selector.file_path, module_root)
    target_name = selector.symbol_path[-1]
    target_qn = f"{target_module}.{target_name}" if target_module else target_name
    selector_str = f"{selector.file_path}::{'::'.join(selector.symbol_path)}"

    delete_set.append({
        "selector": selector_str,
        "file_path": file_path,
        "name": target_name,
        "kind": target_sym.kind,
        "line": target_sym.line_start,
        "reason": "target of delete",
    })
    delete_qns.add(target_qn)

    if cascade:
        # BFS: for each symbol in the delete set, find its callees, then
        # check whether each callee has references outside the delete set.
        queue_idx = 0
        while queue_idx < len(delete_set):
            entry = delete_set[queue_idx]
            queue_idx += 1

            # Build a selector for the current entry.
            entry_sel = parse_extended_selector(entry["selector"])

            try:
                callees = find_callees(entry_sel, project_path=scan_root)
            except (ValueError, FileNotFoundError):
                continue

            for callee in callees:
                qn = callee.qualified_name
                if not qn or qn in delete_qns:
                    continue

                # Resolve callee to a file via the symbol index.
                sym_rows = query_symbol_index(scan_root, qualified_name=qn)
                if not sym_rows:
                    # Try just the name — the QN might be short.
                    sym_rows = query_symbol_index(
                        scan_root, name_pattern=callee.name
                    )
                    if sym_rows:
                        sym_rows = [
                            r for r in sym_rows
                            if r.get("qualified_name", "").endswith(callee.name)
                        ]
                if not sym_rows:
                    continue

                # For each matching symbol, check remaining references.
                for sym_row in sym_rows:
                    sym_qn = sym_row.get("qualified_name", "")
                    sym_fp = sym_row["file_path"]
                    sym_name = sym_row["name"]
                    if sym_qn in delete_qns:
                        continue

                    # Skip entry points / dunders / test functions.
                    if sym_row.get("is_entry_point") or sym_row.get("is_exported"):
                        continue
                    if sym_name.startswith("__") and sym_name.endswith("__"):
                        continue
                    if sym_name.startswith("test_") or sym_name.startswith("Test"):
                        continue

                    # Query all non-definition references.  The reference
                    # index stores target_qn as the module-qualified name,
                    # so try both the short qualified_name and the full
                    # module_qn (derived from the file path).
                    all_refs = query_reference_index(scan_root, sym_qn)
                    if all_refs is None or not all_refs:
                        # Try with the module-qualified name.
                        mod_qn = _file_to_module(sym_fp, scan_root)
                        full_qn = f"{mod_qn}.{sym_name}" if mod_qn else sym_name
                        refs2 = query_reference_index(scan_root, full_qn)
                        if refs2:
                            all_refs = refs2
                        elif all_refs is None:
                            continue

                    # Filter out definition/import references and refs from
                    # symbols we are already planning to delete.  Import
                    # refs are excluded because they become unused once
                    # all call-site references are removed.
                    external_refs = []
                    for ref in all_refs:
                        if ref["ref_kind"] in ("definition", "import"):
                            continue
                        # Check if the reference comes from a file+line
                        # that is inside a symbol we're deleting.
                        ref_from_deleted = False
                        for d in delete_set:
                            if ref["file_path"] == d["file_path"]:
                                # Rough containment check: if ref line is
                                # within the symbol's range.
                                d_sel = parse_extended_selector(d["selector"])
                                try:
                                    d_syms = find_nested_definitions(d["file_path"])
                                    d_sym = find_symbol_by_path(
                                        d_syms, d_sel.symbol_path
                                    )
                                    if d_sym and d_sym.line_start <= ref["line"] <= d_sym.line_end:
                                        ref_from_deleted = True
                                        break
                                except Exception:
                                    pass
                        if not ref_from_deleted:
                            external_refs.append(ref)

                    if not external_refs:
                        sym_selector = f"{sym_fp}::{sym_name}"
                        delete_set.append({
                            "selector": sym_selector,
                            "file_path": sym_fp,
                            "name": sym_name,
                            "kind": sym_row.get("kind", "function"),
                            "line": sym_row.get("line", 0),
                            "reason": f"only referenced by deleted symbol(s)",
                        })
                        delete_qns.add(sym_qn)

    # ----- Phase 2: Apply deletions and collect diffs --------------------
    # Group by file, process in reverse line order to avoid offset shifts.
    from collections import defaultdict
    by_file: dict[str, list[dict]] = defaultdict(list)
    for d in delete_set:
        by_file[d["file_path"]].append(d)

    all_diffs: dict[str, str] = {}

    for fpath, entries in by_file.items():
        fp = Path(fpath)
        if not fp.exists():
            continue
        source_code = fp.read_text()
        lines = source_code.splitlines(keepends=True)

        # Sort by line descending so we remove from bottom first.
        entries.sort(key=lambda e: e["line"], reverse=True)

        for entry in entries:
            sel = parse_extended_selector(entry["selector"])
            syms = find_nested_definitions(fpath)
            sym = find_symbol_by_path(syms, sel.symbol_path)
            if sym is None:
                continue

            start_line = (
                sym.decorator_line_start
                if sym.decorator_line_start is not None
                else sym.line_start
            )
            start_idx = start_line - 1
            end_idx = sym.line_end
            lines = lines[:start_idx] + lines[end_idx:]

        new_code = "".join(lines)
        diff = _generate_diff(fpath, source_code, new_code)
        if diff:
            all_diffs[fpath] = diff
            if apply:
                fp.write_text(new_code)

    return DeletePlan(
        target=selector_str,
        deletions=delete_set,
        diffs=all_diffs,
    )


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
            docs_result = _rename_in_docstrings(new_content, symbol_name, new_name, language=language)
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
        from emend.language_registry import is_source_file, get_extensions
        files_to_query = []
        fop = Path(file_or_pattern)
        if fop.is_dir():
            # Collect all known source files under the directory
            files_to_query = [str(f) for f in fop.rglob("*") if f.is_file() and is_source_file(str(f))]
        elif '*' in file_or_pattern or '?' in file_or_pattern:
            files_to_query = [f for f in glob_mod.glob(file_or_pattern, recursive=True) if is_source_file(f)]
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
