"""Cache management: SQLite parse.db and CozoDB facts.db."""
from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING
import logging

if TYPE_CHECKING:
    import sqlite3

from emend.analysis_store import AnalysisStore
from emend.errors import BUG_EXCEPTIONS

logger = logging.getLogger(__name__)

# Parse/index cache version remains shared with ``index.py``.  FactGraph has
# its own marker because its Cozo relation shape can change independently.
_SCHEMA_VERSION = "14"


def _initialize_cache_connection(conn: sqlite3.Connection) -> None:
    """Configure and durably initialize an independently owned connection."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _init_cache_schema(conn)
    conn.commit()


def _resolve_shared_data_root(project_root: str) -> Path:
    """Return the main checkout root for user-managed shared data.

    Cache databases are deliberately excluded: they describe one worktree's
    mutable source snapshot and therefore live in that worktree. Only durable
    user data such as mappings is shared with the main checkout.
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
    """Compatibility delegate to the project-scoped analysis owner."""
    return AnalysisStore.open(project_root).cache_dir


def _knowledge_db_dir(project_root: str | Path) -> Path:
    """Return the directory for user-managed mapping data.

    Unlike cache data, mappings are user-created content that cannot be
    recomputed, so they live directly in ``.emend/`` rather than
    ``.emend/cache/``.
    """
    main_root = _resolve_shared_data_root(str(project_root))
    return main_root / ".emend"


def _get_worktree_id(project_root: str) -> str:
    """Compatibility delegate for the store's stable project identity."""
    return str(AnalysisStore.open(project_root).project_root)


def _drop_cache_columns(conn: sqlite3.Connection, table: str, obsolete: set[str]) -> None:
    """Rebuild an owned cache table without requiring SQLite 3.35 DROP COLUMN."""
    columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
    kept = [column for column in columns if column[1] not in obsolete]
    if len(kept) == len(columns):
        return

    names = ['"' + column[1].replace('"', '""') + '"' for column in kept]
    definitions = [
        f"{name} {column[2]}"
        + (" NOT NULL" if column[3] else "")
        + (f" DEFAULT {column[4]}" if column[4] is not None else "")
        for name, column in zip(names, kept)
    ]
    keys = [name for _, name in sorted(
        (column[5], name) for name, column in zip(names, kept) if column[5]
    )]
    if keys:
        definitions.append(f"PRIMARY KEY ({', '.join(keys)})")
    projection = ", ".join(["rowid", *names])
    # Preserve rowids for FTS joins, and leave the original table intact if
    # copying fails. The savepoint also works inside the owner's transaction.
    conn.execute("SAVEPOINT cache_columns")
    try:
        conn.execute(f"CREATE TABLE {table}_new ({', '.join(definitions)})")
        conn.execute(
            f"INSERT INTO {table}_new ({projection}) SELECT {projection} FROM {table}"
        )
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    except BaseException:
        conn.execute("ROLLBACK TO cache_columns")
        raise
    finally:
        conn.execute("RELEASE cache_columns")


def _init_cache_schema(conn: sqlite3.Connection) -> None:
    """Create all cache tables and indexes if they don't exist (idempotent).

    Called from ``_get_disk_cache()`` (lazy init) and ``warm_caches()``
    (pre-create before spawning workers). Keeping the DDL in one place
    prevents the two call-sites from drifting out of sync.

    parse.db holds data SQLite handles best: full-text / editor search
    (FTS5 trigram), freshness metadata (file_manifest, index_meta), the QN
    pre-filter cache. Structured analysis facts
    (symbols, references, imports, CFG, def-use, calls) are owned by CozoDB
    facts.db. ``symbol_index`` and ``reference_index`` remain for editor
    search.
    """
    # Preserve symbol rowids: FTS rows refer to them. These fields and tables
    # were populated by old indexers but no longer have consumers.
    for table in ("import_graph", "dsl_symbols", "dsl_links"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    for index in ("idx_sym_hash", "idx_ref_hash", "idx_manifest_hash"):
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    for table, obsolete in (
        ("symbol_index", {"content_hash", "is_entry_point", "is_exported", "has_noqa"}),
        ("reference_index", {"content_hash"}),
        ("file_manifest", {"mtime_ns", "size", "indexed_at"}),
        ("venv_files", {"content_hash"}),
    ):
        _drop_cache_columns(conn, table, obsolete)

    qn_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(qn_index)").fetchall()
    }
    if qn_columns and "file_path" not in qn_columns:
        # Disposable cache migration from the old content-only identity.
        conn.execute("DROP TABLE qn_index")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS qn_index ("
        "file_path TEXT NOT NULL, hash BLOB NOT NULL, qnames BLOB, "
        "PRIMARY KEY (file_path, hash))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS index_meta "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS file_manifest ("
        "  worktree_id TEXT NOT NULL DEFAULT '',"
        "  path TEXT NOT NULL,"
        "  content_hash BLOB NOT NULL,"
        "  PRIMARY KEY (worktree_id, path)"
        ")"
    )
    if "scope_hash" not in {row[1] for row in conn.execute("PRAGMA table_info(file_manifest)")}:
        conn.execute("ALTER TABLE file_manifest ADD COLUMN scope_hash BLOB")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS symbol_index ("
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
        "  decorators TEXT"
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
        "CREATE INDEX IF NOT EXISTS idx_sym_kind ON symbol_index(kind)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reference_index ("
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
    # Duplicate analysis payload cache: one row per unique file
    # content (keyed by MD5 hash). ``data`` is zlib-compressed pickle of the
    # per-file subtree/sequence payload. ``version`` allows cache invalidation
    # when the payload schema changes.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dup_cache ("
        "  hash TEXT PRIMARY KEY,"
        "  version TEXT NOT NULL,"
        "  data BLOB NOT NULL"
        ")"
    )


# ---------------------------------------------------------------------------
# Disk cache connection (project-keyed owner)
# ---------------------------------------------------------------------------

def _get_disk_cache(project_root: str | Path = ".") -> "sqlite3.Connection | None":
    """Return the connection owned by *project_root*'s AnalysisStore."""
    try:
        return AnalysisStore.open(project_root).connection(_init_cache_schema)
    except BUG_EXCEPTIONS:
        raise
    except Exception:
        logger.debug("disk cache unavailable", exc_info=True)
        return None


# Qualified-name index cache: per-file set of QN strings for pre-filtering
# ---------------------------------------------------------------------------
# After the first cross-project operation populates this cache, subsequent
# operations can skip scope resolution for files whose QN set doesn't overlap
# with the target.  File-revision keyed, persisted in the same SQLite DB.
