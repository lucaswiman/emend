"""Venv symbol index — separate cache populated from site-packages.

The venv index is built lazily on first lookup and refreshed when the venv's
site-packages directory mtime changes.  It uses the same ``symbol_index``
schema as the project cache but lives in a separate ``parse_venv.db`` so
project re-indexing does not invalidate library symbols.
"""
from __future__ import annotations
from pathlib import Path
import hashlib
import logging

logger = logging.getLogger(__name__)


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
