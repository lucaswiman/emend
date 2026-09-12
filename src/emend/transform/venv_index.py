"""Venv symbol index — separate cache populated from site-packages.

The venv index is built lazily and refreshed from a cheap metadata inventory.
Only added or changed files are parsed; deleted files are removed.  It uses the
same ``symbol_index`` schema as the project cache but lives in a separate
``parse_venv.db`` so project re-indexing does not invalidate library symbols.
"""
from __future__ import annotations
from pathlib import Path
import hashlib
import logging
import os

from emend.errors import BUG_EXCEPTIONS

logger = logging.getLogger(__name__)


def _scan_venv_files(site_packages: Path) -> dict[str, tuple[int, int, int, int, int]]:
    """Discover indexable files and return identities without reading contents."""
    from emend.analysis_store import AnalysisStore
    from emend.file_collection import collect_source_files_scandir

    found = {}
    for path in collect_source_files_scandir(str(site_packages), language="python"):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        found[path] = AnalysisStore._stat_identity(stat)
    return found


def _venv_db_path(project_root: str) -> Path:
    """Return the path to the venv-specific parse cache DB."""
    from .cache import _cache_db_dir
    return _cache_db_dir(project_root) / "parse_venv.db"


def _ensure_venv_index(project_root: str, language: str = "python") -> Path | None:
    """Build or refresh the venv symbol index.

    Creates ``parse_venv.db`` in ``.emend/cache/`` with the same
    ``symbol_index`` schema as the project cache.  A metadata walk detects
    nested additions, edits, and deletions; unchanged files are not reparsed.

    Returns the DB path, or ``None`` if venv lookup is disabled / no venv.
    """
    import sqlite3 as _sql3
    from .cache import _initialize_cache_connection

    from emend.project_config import resolve_environment_path

    site_packages = resolve_environment_path(project_root, language)
    if site_packages is None:
        return None

    db_path = _venv_db_path(project_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if not site_packages.is_dir():
        return None

    try:
        conn = _sql3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except _sql3.Error:
        logger.debug("could not open parse_venv.db", exc_info=True)
        return None

    try:
        # Create schema if needed
        _initialize_cache_connection(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS venv_files ("
            "path TEXT PRIMARY KEY, device INTEGER NOT NULL, inode INTEGER NOT NULL, "
            "size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL, "
            "content_hash BLOB NOT NULL)"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS venv_meta "
                     "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        current = _scan_venv_files(site_packages)
        previous = {
            row[0]: tuple(row[1:6])
            for row in conn.execute(
                "SELECT path, device, inode, size, mtime_ns, ctime_ns FROM venv_files"
            )
        }
        row = conn.execute(
            "SELECT value FROM venv_meta WHERE key = 'environment_path'"
        ).fetchone()
        environment_changed = row is None or row[0] != str(site_packages)
        changed = [Path(path) for path, identity in current.items()
                   if environment_changed or previous.get(path) != identity]
        removed = (
            previous.keys()
            if environment_changed
            else previous.keys() - current.keys()
        )
        if environment_changed or changed or removed:
            _update_venv_index(
                conn, site_packages, changed, removed, current, project_root,
                reset=environment_changed,
            )
        conn.close()
        return db_path
    except _sql3.Error:
        logger.debug("venv index freshness check failed; rebuilding", exc_info=True)
        try:
            conn.close()
        except _sql3.Error:
            pass

    return None


def _update_venv_index(
    conn, sp: Path, changed, removed, current, project_root, *, reset=False
) -> None:
    """Apply one inventory delta atomically."""
    from emend.analysis_store import AnalysisStore
    from emend.symbol_projection import _symbol_info_view

    changed = list(changed)
    removed = list(removed)
    sym_rows: list[tuple] = []
    indexed_files = []
    store = AnalysisStore.open(project_root)
    for fpath in changed:
        try:
            content = fpath.read_text(errors="replace")
        except OSError:
            # Unreadable file (permissions, dangling symlink) — skip it.
            continue

        content_hash = hashlib.md5(content.encode(), usedforsecurity=False).digest()

        try:
            symbols = _symbol_info_view(
                store.symbols(content, fpath.suffix.lstrip(".")), str(fpath)
            )
        except BUG_EXCEPTIONS:
            raise
        except Exception:
            # Unparseable library file; expected in site-packages scans.
            logger.debug("symbol collection failed for %s", fpath, exc_info=True)
            continue
        indexed_files.append((str(fpath), *current[str(fpath)], content_hash))

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

    with conn:
        if reset:
            conn.execute("DELETE FROM symbol_index")
            conn.execute("DELETE FROM venv_files")
        for path in removed:
            conn.execute("DELETE FROM symbol_index WHERE file_path = ?", (path,))
            conn.execute("DELETE FROM venv_files WHERE path = ?", (path,))
        for fpath in changed:
            conn.execute("DELETE FROM symbol_index WHERE file_path = ?", (str(fpath),))
            conn.execute("DELETE FROM venv_files WHERE path = ?", (str(fpath),))
        if sym_rows:
            conn.executemany(
                "INSERT INTO symbol_index "
                "(content_hash, file_path, name, qualified_name, module_qn, kind, "
                "line, end_line, depth, parent, signature, returns, decorators, "
                "is_entry_point, is_exported, has_noqa) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                sym_rows,
            )
        if indexed_files:
            conn.executemany(
                "INSERT OR REPLACE INTO venv_files "
                "(path, device, inode, size, mtime_ns, ctime_ns, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", indexed_files,
            )
        conn.execute(
            "INSERT OR REPLACE INTO venv_meta VALUES ('environment_path', ?)",
            (str(sp),),
        )
    logger.info(
        "Venv index: updated %d symbols from %d changed files; removed %d files",
        len(sym_rows), len(changed), len(removed),
    )


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
    lookup and incrementally refreshed from the current file inventory.

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
    except _sql3.Error:
        logger.debug("could not open parse_venv.db for lookup", exc_info=True)
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
    except _sql3.Error:
        logger.debug("venv symbol lookup query failed", exc_info=True)
        try:
            conn.close()
        except _sql3.Error:
            pass
        return []
