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

from emend.errors import BUG_EXCEPTIONS

logger = logging.getLogger(__name__)


def _scan_venv_files(site_packages: Path, language="python") -> dict[str, tuple[int, int, int, int, int]]:
    """Discover indexable files and return identities without reading contents."""
    from emend.file_collection import collect_source_files_scandir, source_file_identities

    files = collect_source_files_scandir(
        str(site_packages),
        language=language,
        skip_dirs=["__pycache__", "*.dist-info", "*.egg-info"],
    )
    return source_file_identities(files)


def _venv_db_path(project_root: str, language="python") -> Path:
    """Return the path to the venv-specific parse cache DB."""
    from .cache import _cache_db_dir
    name = "parse_venv.db" if language == "python" else f"parse_environment_{language}.db"
    return _cache_db_dir(project_root) / name


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
    from emend.analysis_store import EXTRACTION_ARTIFACT_VERSION
    from emend.language_registry import config_identity

    site_packages = resolve_environment_path(project_root, language)
    if site_packages is None:
        return None
    context = repr((EXTRACTION_ARTIFACT_VERSION, config_identity(language)))

    db_path = _venv_db_path(project_root, language)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if not site_packages.is_dir():
        return None

    try:
        conn = _sql3.connect(str(db_path), timeout=10)
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
        current = _scan_venv_files(site_packages, language)
        previous = {
            row[0]: tuple(row[1:6])
            for row in conn.execute(
                "SELECT path, device, inode, size, mtime_ns, ctime_ns FROM venv_files"
            )
        }
        metadata = dict(conn.execute("SELECT key, value FROM venv_meta"))
        environment_changed = (
            metadata.get("environment_path") != str(site_packages)
            or metadata.get("context") != context
        )
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
                reset=environment_changed, language=language, context=context,
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
    conn, sp: Path, changed, removed, current, project_root, *, context, reset=False, language="python"
) -> None:
    """Apply one inventory delta atomically."""
    from emend.analysis_store import AnalysisStore
    from emend.symbol_projection import _symbol_info_view
    from emend.language_registry import get_extensions, get_module_separator
    from emend import emend_core

    changed = list(changed)
    removed = list(removed)
    sym_rows: list[tuple] = []
    indexed_files = []
    store = AnalysisStore.open(project_root)
    resolver = emend_core.PyScopeResolver(
        str(sp), get_extensions(language)[0], module_root=str(sp)
    )
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
        separator = get_module_separator(language)
        module_qn = resolver.module_name_for_file(str(fpath))

        for sym in symbols:
            parts = sym.path.split("::", 1)
            dotted = parts[1] if len(parts) > 1 else sym.name
            m_qn = f"{module_qn}{separator}{dotted}" if module_qn else dotted
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
        conn.executemany(
            "INSERT OR REPLACE INTO venv_meta VALUES (?, ?)",
            (("environment_path", str(sp)), ("context", context)),
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
            from emend.language_registry import get_module_separator
            params.extend([qualified_name, qualified_name,
                           qualified_name + get_module_separator(language) + "%"])

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
