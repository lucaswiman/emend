"""Cache management: SQLite parse.db and CozoDB facts.db."""
from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING
import hashlib
import logging
import threading as _threading

if TYPE_CHECKING:
    import sqlite3

from ..language_plugins import NOQA_PATTERN as _NOQA_PATTERN
from emend.errors import BUG_EXCEPTIONS

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "4"

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
    (pre-create before spawning workers). Keeping the DDL in one place
    prevents the two call-sites from drifting out of sync.

    parse.db holds data SQLite handles best: full-text / editor search
    (FTS5 trigram), freshness metadata (file_manifest, index_meta), the QN
    pre-filter cache, type cache, and DSL symbols. Structured analysis facts
    (symbols, references, imports, CFG, def-use, calls) are owned by CozoDB
    facts.db. ``symbol_index`` and ``reference_index`` remain for editor
    search; ``import_graph`` is retained for compatibility but is no longer
    read by the facts.db build path.
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
    # DSL tables for embedded language symbols and cross-language links
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dsl_symbols ("
        "  id INTEGER PRIMARY KEY,"
        "  name TEXT NOT NULL,"
        "  kind TEXT NOT NULL,"
        "  dsl TEXT NOT NULL,"
        "  host_file TEXT NOT NULL,"
        "  host_start_line INTEGER NOT NULL,"
        "  host_start_col INTEGER NOT NULL,"
        "  host_end_line INTEGER NOT NULL,"
        "  host_end_col INTEGER NOT NULL,"
        "  content_hash BLOB NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dsl_name "
        "ON dsl_symbols(name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dsl_host "
        "ON dsl_symbols(host_file)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dsl_hash "
        "ON dsl_symbols(content_hash)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dsl_links ("
        "  id INTEGER PRIMARY KEY,"
        "  dsl_symbol_name TEXT NOT NULL,"
        "  dsl_symbol_file TEXT NOT NULL,"
        "  target_qn TEXT NOT NULL,"
        "  target_file TEXT,"
        "  target_line INTEGER,"
        "  strategy TEXT NOT NULL,"
        "  confidence REAL NOT NULL,"
        "  content_hash BLOB NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dsl_link_target "
        "ON dsl_links(target_qn)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dsl_link_hash "
        "ON dsl_links(content_hash)"
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
    conn.commit()


# ---------------------------------------------------------------------------
# Disk cache connection (lazy singleton)
# ---------------------------------------------------------------------------
_disk_cache_conn: 'sqlite3.Connection | None' = None
_disk_cache_lock = _threading.Lock()
_disk_cache_checked = False


def _get_disk_cache() -> "sqlite3.Connection | None":
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
            from .project_iter import _find_project_root
            from .index import _ensure_cache_ignore_files
            root = _find_project_root(".")
            cache_dir = _cache_db_dir(root)
            cache_dir.mkdir(parents=True, exist_ok=True)
            _ensure_cache_ignore_files(root)
            db_path = cache_dir / "parse.db"
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            _init_cache_schema(conn)
            _disk_cache_conn = conn
            logger.debug("disk cache opened at %s", db_path)
        except BUG_EXCEPTIONS:
            raise
        except Exception:
            logger.debug("disk cache unavailable", exc_info=True)
            _disk_cache_conn = None
    return _disk_cache_conn


# ---------------------------------------------------------------------------
# CozoDB facts database (lazy singleton, separate from parse.db)
# ---------------------------------------------------------------------------

_facts_db_cache: dict[str, object] = {}  # project_root → CozoDB client
_facts_db_lock = _threading.Lock()

_FACTS_SCHEMA = """\
{:ensure fact_symbol {
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

{:ensure fact_reference {
    tqn: String,
    fp: String,
    line: Int,
    col: Int
    =>
    kind: String
}}

{:ensure fact_import {
    fp: String,
    mod: String
}}

{:ensure symbol {
    qualified_name: String
    =>
    file_path: String,
    name: String,
    kind: String,
    line: Int,
    end_line: Int,
    parent: String default ""
}}

{:ensure reference {
    symbol_qn: String,
    file_path: String,
    line: Int,
    col: Int
    =>
    ref_kind: String,
    func_qn: String default "",
    block_id: Int default -1
}}

{:ensure call {
    caller_qn: String,
    callee_qn: String,
    file_path: String,
    line: Int,
    col: Int
    =>
    func_qn: String default "",
    block_id: Int default -1
}}

{:ensure cfg_block {
    file_path: String,
    func_qn: String,
    block_id: Int
    =>
    is_entry: Bool default false,
    is_exit: Bool default false
}}

{:ensure cfg_edge {
    file_path: String,
    func_qn: String,
    from_block: Int,
    to_block: Int,
    edge_kind: String,
    from_line: Int,
    to_line: Int
}}

{:ensure def_use {
    file_path: String,
    func_qn: String,
    var_name: String,
    kind: String default "write",
    def_block: Int,
    use_block: Int
    =>
    def_line: Int default 0,
    def_col: Int default 0,
    use_line: Int default 0,
    use_col: Int default 0
}}

{:ensure method_call {
    file_path: String,
    func_qn: String,
    receiver: String,
    method: String,
    block_id: Int,
    line: Int
}}

{:ensure source_loc {
    file_path: String,
    loc_kind: String,
    loc_id: String
    =>
    line: Int,
    col: Int default 0,
    end_line: Int default 0,
    rel_line: Int default 0
}}

{:ensure import {
    importing_file: String,
    imported_module: String,
    imported_name: String default "",
    line: Int
    =>
    alias: String default ""
}}

{:ensure decorator_on {
    symbol_qn: String,
    decorator: String
}}

{:ensure ref_by_block {
    file_path: String,
    func_qn: String,
    block_id: Int,
    symbol_qn: String
}}

{:ensure reachable_block {
    file_path: String,
    func_qn: String,
    block_id: Int
}}
"""


def _open_facts_db(db_path: str):
    """Open (or create) a CozoDB facts database at *db_path*.

    Uses ``:ensure`` (not ``:create``) so that existing data is preserved
    when the database is reopened.
    """
    from emend.fact_graph import _create_cozo_client

    client = _create_cozo_client(db_path)
    for stmt in _FACTS_SCHEMA.strip().split("\n\n"):
        stmt = stmt.strip()
        if stmt:
            try:
                client.run(stmt)
            except Exception:
                logger.debug("facts schema statement failed (already exists?)", exc_info=True)
    return client


def _get_facts_db(project_root: str | None = None):
    """Return a lazily-initialized CozoDB facts database for *project_root*, or None.

    If *project_root* is None, derives from the current working directory.
    Returns None if the facts.db doesn't exist yet (i.e. no dual-write has
    populated it), so callers fall back to SQLite.
    """
    if project_root is None:
        try:
            from .project_iter import _find_project_root
            project_root = _find_project_root(".")
        except OSError:
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
        except BUG_EXCEPTIONS:
            raise
        except Exception:
            logger.debug("facts db unavailable", exc_info=True)
            return None


def _delete_facts_for_file(fdb, file_path: str) -> None:
    """Delete all facts for a given file path from all relations."""
    for query in (
        "?[fp, mqn] := *fact_symbol[fp, mqn, _, _, _, _, _, _, _, _, _, _, _, _, _, _], "
        "fp == $fp  :rm fact_symbol {fp => }",
        "?[tqn, fp, line, col] := *fact_reference[tqn, fp, line, col, _], "
        "fp == $fp  :rm fact_reference {tqn, fp, line, col => }",
        "?[fp, mod] := *fact_import[fp, mod], "
        "fp == $fp  :rm fact_import {fp, mod}",
        # FactGraph-style relations
        "?[sq, fp, line, col] := *reference[sq, fp, line, col, _, _, _], "
        "fp == $fp  :rm reference {sq, fp, line, col => }",
        "?[cqn, eqn, fp, line, col] := *call[cqn, eqn, fp, line, col, _, _], "
        "fp == $fp  :rm call {cqn, eqn, fp, line, col => }",
        "?[eqn, cqn, fp, line, col] := *call_by_callee[eqn, cqn, fp, line, col, _, _], "
        "fp == $fp  :rm call_by_callee {eqn, cqn, fp, line, col => }",
        "?[fp, cqn, eqn, line, col] := *call_by_file[fp, cqn, eqn, line, col, _, _], "
        "fp == $fp  :rm call_by_file {fp, cqn, eqn, line, col => }",
        "?[fp, fq, bid] := *cfg_block[fp, fq, bid, _, _], "
        "fp == $fp  :rm cfg_block {fp, fq, bid => }",
        "?[fp, fq, fb, tb, ek, fl, tl] := *cfg_edge[fp, fq, fb, tb, ek, fl, tl], "
        "fp == $fp  :rm cfg_edge {fp, fq, fb, tb, ek, fl, tl}",
        "?[fp, fq, vn, k, db, ub] := *def_use[fp, fq, vn, k, db, ub, _, _, _, _], "
        "fp == $fp  :rm def_use {fp, fq, vn, k, db, ub => }",
        "?[fp, fq, r, m, bid, line] := *method_call[fp, fq, r, m, bid, line], "
        "fp == $fp  :rm method_call {fp, fq, r, m, bid, line}",
        "?[fp, lk, lid] := *source_loc[fp, lk, lid, _, _, _, _], "
        "fp == $fp  :rm source_loc {fp, lk, lid => }",
        "?[fp, im, iname, line] := *import[fp, im, iname, line, _], "
        "fp == $fp  :rm import {fp, im, iname, line => }",
        "?[fp, fq, bid, sq] := *ref_by_block[fp, fq, bid, sq], "
        "fp == $fp  :rm ref_by_block {fp, fq, bid, sq}",
        "?[fp, fq, bid] := *reachable_block[fp, fq, bid], "
        "fp == $fp  :rm reachable_block {fp, fq, bid}",
        "?[sq, fp, line] := *module_level_ref[sq, fp, line], "
        "fp == $fp  :rm module_level_ref {sq, fp, line}",
    ):
        try:
            fdb.run(query, {"fp": file_path})
        except Exception:
            pass  # relation may not exist yet in older facts.db files


def _build_fact_sym_rows(
    out: list[list],
    raw_symbols: list[dict],
    rel_path: str,
    module_name: str,
    abs_path: str,
    project_root: str,
    content: str,
) -> None:
    """Build fact_symbol rows from raw Rust symbol output.

    Includes all symbol kinds (function, class, variable, etc.) with full
    metadata, matching what the old parse.db symbol_index contained.
    """
    # Normalize to dots so QNs are consistent across all tables.
    from .project_iter import _normalize_module_qn
    from .index import _extract_all_exports_text, _extract_noqa_lines
    from .deadcode import _is_likely_entry_point
    module_name = _normalize_module_qn(module_name)
    from emend.language_registry import detect_language
    lang = detect_language(abs_path) or "python"
    if lang == "python":
        exported_names = _extract_all_exports_text(content, abs_path)
    else:
        from emend.language_registry import detect_exported_names
        exported_names = detect_exported_names(content, lang)
    noqa_lines = _extract_noqa_lines(content)

    def _walk(symbols: list[dict], parent_prefix: str = "") -> None:
        for d in symbols:
            kind = d.get("kind", "")
            name = d["name"]
            path_parts = list(d.get("path", []))
            if path_parts:
                dotted = ".".join(path_parts)
                mqn = f"{module_name}.{dotted}"
            else:
                dotted = name
                mqn = f"{module_name}.{name}"

            depth = len(path_parts) if path_parts else 1
            parent = ""
            if depth > 1 and "." in mqn:
                parent = mqn.rsplit(".", 1)[0]

            decs = d.get("decorators", []) or []
            decs_str = ",".join(decs) if decs else ""
            sig = ""
            params = d.get("parameters", [])
            returns = d.get("returns", "") or ""
            if params is not None and kind in ("function", "method"):
                ret_str = f" -> {returns}" if returns else ""
                sig = f"def {name}({', '.join(params)}){ret_str}"

            bases = d.get("bases", []) or []
            bases_str = ",".join(bases) if bases else ""

            is_entry = _is_likely_entry_point(name, kind, decs, depth)
            is_exported = name in exported_names
            has_noqa = d.get("line", 0) in noqa_lines

            out.append([
                rel_path, mqn, name, dotted, kind,
                d.get("line", 0), d.get("end_line", 0), depth,
                parent, bases_str, sig, returns, decs_str,
                bool(is_entry), bool(is_exported), bool(has_noqa),
            ])

            children = d.get("children", [])
            if children:
                _walk(children, mqn)

    _walk(raw_symbols)


def _extract_file_facts(
    abs_path: str,
    rel_path: str,
    ext: str,
    content: str,
    project_root: str,
    module_name: str,
    scope_resolver=None,
    fact_symbol_rows_builder=None,
    precomputed_refs: list[tuple] | None = None,
) -> dict:
    """Extract all analysis facts for a single file.

    Returns a dict of lists for each relation type.
    Thread-safe — only reads from the shared scope_resolver, no writes.

    Args:
        precomputed_refs: Optional list of (target_qn, line, col, ref_kind)
            tuples from parse.db's reference_index.  When provided, skips
            the expensive scope_resolver.references_in_file() call.
    """
    from emend import emend_core
    from emend.cfg import build_cfgs_for_source
    from emend.fact_graph import (
        _build_method_call_facts,
        _build_symbol_line_index,
        _enclosing_symbol,
        _extract_imports,
        _find_containing_block,
        _map_ref_kind,
        _normalize_qn,
        _resolve_cfg_func_qn,
        _walk_symbols,
    )

    result: dict[str, list] = {
        "fact_sym": [], "fact_ref": [], "fact_imp": [], "fg_sym": [],
        "dec": [], "cfg_blocks": [], "cfg_edges": [], "fg_refs": [],
        "calls": [], "calls_by_callee": [], "calls_by_file": [],
        "def_uses": [], "method_calls": [], "source_locs": [],
        "imports": [], "ref_by_block": [], "module_level_refs": [],
        "exported_qns": [],
    }

    # -- Extract symbols via Rust
    try:
        raw_symbols = emend_core.collect_symbols_from_str(content, ext=ext)
    except Exception:
        logger.debug("symbol extraction failed for %s", rel_path, exc_info=True)
        raw_symbols = []

    sym_facts_for_file: list = []
    dec_facts_for_file: list = []
    _walk_symbols(
        sym_facts_for_file, dec_facts_for_file,
        raw_symbols, rel_path, module_name, parent_qn=None,
    )

    # Build fact_symbol rows from the raw Rust output (all symbol kinds)
    if fact_symbol_rows_builder is not None:
        fact_symbol_rows_builder(
            result["fact_sym"], raw_symbols, rel_path, module_name,
            abs_path, project_root, content,
        )

    # Populate FactGraph-style symbol rows (filtered)
    for sf in sym_facts_for_file:
        result["fg_sym"].append([
            sf.qualified_name, sf.file_path, sf.name, sf.kind,
            sf.line, sf.end_line, sf.parent or "",
        ])

    # Populate decorator_on
    for df in dec_facts_for_file:
        result["dec"].append([df.symbol_qn, df.decorator])

    # Collect exported symbol QNs (for TS/Rust visibility-based entry points)
    from emend.language_registry import detect_language as _detect_lang_eff
    _lang_eff = _detect_lang_eff(abs_path) or "python"
    if _lang_eff != "python":
        from emend.language_registry import detect_exported_names as _detect_exports_eff
        exported_names = _detect_exports_eff(content, _lang_eff)
        if exported_names:
            for sf in sym_facts_for_file:
                if sf.name in exported_names:
                    result["exported_qns"].append([sf.qualified_name])

    # -- Extract references (pre-computed or via Rust scope resolver)
    file_refs: list[tuple] = []
    if precomputed_refs is not None:
        for qn_str, line, col, kind in precomputed_refs:
            qn_str = _normalize_qn(qn_str)
            kind = _map_ref_kind(kind)
            file_refs.append((qn_str, line, col, kind))
            result["fact_ref"].append([qn_str, rel_path, line, col, kind])
    else:
        try:
            raw_refs = scope_resolver.references_in_file(abs_path)
        except Exception:
            logger.debug("reference extraction failed for %s", rel_path, exc_info=True)
            raw_refs = []
        for qn_str, line, col, _offset, _end_offset, kind, _ann in raw_refs:
            qn_str = _normalize_qn(qn_str)
            kind = _map_ref_kind(kind)
            file_refs.append((qn_str, line, col, kind))
            result["fact_ref"].append([qn_str, rel_path, line, col, kind])

    # -- Extract imports (all languages)
    # Detailed imports via _extract_imports (dispatches by language for TS/Rust).
    for imp in _extract_imports(rel_path, content):
        result["imports"].append([
            imp.importing_file, imp.imported_module,
            imp.imported_name or "", imp.line,
            imp.alias or "",
        ])

    # Populate fact_imp from structured imports (Python only).
    # Uses the already-computed _extract_imports results to avoid a second regex pass.
    if ext == "py":
        seen_modules: set[str] = set()
        for imp_row in result["imports"]:
            mod = imp_row[1]  # imported_module
            if mod and mod not in seen_modules:
                seen_modules.add(mod)
                result["fact_imp"].append([rel_path, mod])

    # -- source_loc (from symbol facts)
    for sf in sym_facts_for_file:
        result["source_locs"].append([
            sf.file_path, "symbol", sf.qualified_name,
            sf.line, 0, sf.end_line, 0,
        ])

    # -- CFG
    try:
        cfgs = build_cfgs_for_source(content, ext=ext)
    except BUG_EXCEPTIONS:
        raise
    except Exception:
        logger.debug("CFG build failed for %s", rel_path, exc_info=True)
        cfgs = []

    block_ranges: list[tuple[str, int, int, int, bool]] = []
    for cfg in cfgs:
        func_qn = _resolve_cfg_func_qn(cfg, sym_facts_for_file, rel_path, module_name)

        for block in cfg.get_blocks():
            bid = block["id"]
            result["cfg_blocks"].append([
                rel_path, func_qn, bid,
                bid == cfg.entry, bid == cfg.exit,
            ])
            has_content = bool(
                block.get("statements")
                or block.get("defs")
                or block.get("uses")
            )
            # Tree-sitter lines are 0-indexed; convert to 1-indexed
            # for consistency with reference lines and source_loc.
            block_ranges.append((func_qn, bid, block["start_line"] + 1, block["end_line"] + 1, has_content))

        for edge in cfg.get_edges():
            result["cfg_edges"].append([
                rel_path, func_qn,
                edge["from"], edge["to"], edge["kind"], 0, 0,
            ])

    block_ranges.sort(key=lambda x: (x[2], -(x[3] - x[2])))

    # -- source_loc entries for blocks (for unreachable block reporting)
    # Only store blocks with real content (statements, defs, or uses)
    # to avoid reporting empty structural join blocks as unreachable.
    for func_qn_br, bid_br, start_line_br, end_line_br, has_content_br in block_ranges:
        if start_line_br > 0 and has_content_br:
            result["source_locs"].append([
                rel_path, "block", f"{func_qn_br}:{bid_br}",
                start_line_br, 0, end_line_br, 0,
            ])

    # -- Block-tagged references, calls, method_calls
    # Filter out empty structural blocks (exit/join blocks with 0-0 ranges)
    # which get converted to (1,1) and can incorrectly match references.
    content_block_ranges = [br for br in block_ranges if br[4]]
    symbol_ranges = _build_symbol_line_index(sym_facts_for_file, rel_path)

    # Pre-compute definition-site locations to exclude class/fn name
    # references at their own definition line from ref_by_block.
    _sym_def_lines = {(sf.qualified_name, sf.line) for sf in sym_facts_for_file}
    _block_for_line: dict[int, tuple[str, int]] = {}
    for tqn, line, col, kind in file_refs:
        block = _block_for_line.get(line) or _find_containing_block(
            content_block_ranges, line,
        )
        _block_for_line[line] = block
        fq, bid = block
        result["fg_refs"].append([tqn, rel_path, line, col, kind, fq, bid])
        # ref_by_block: only for refs with real block data, excluding
        # definition-site "references" to avoid inflating live_ref.
        if fq and bid >= 0 and (tqn, line) not in _sym_def_lines:
            result["ref_by_block"].append([rel_path, fq, bid, tqn])
        else:
            result["module_level_refs"].append([tqn, rel_path, line])

        if kind == "call":
            caller = _enclosing_symbol(symbol_ranges, line)
            caller_qn = caller if caller is not None else module_name
            result["calls"].append([caller_qn, tqn, rel_path, line, col, fq, bid])
            result["calls_by_callee"].append([tqn, caller_qn, rel_path, line, col, fq, bid])
            result["calls_by_file"].append([rel_path, caller_qn, tqn, line, col, fq, bid])

    # Method-call location conventions are shared with the public builders:
    # 0-based lines and explicit sentinels for module-level code.
    raw_method_refs = [
        (qn, line, col, 0, 0, kind, None)
        for qn, line, col, kind in file_refs
    ]
    for fact in _build_method_call_facts(
        raw_method_refs, rel_path, content_block_ranges, normalize_qn=False,
    ):
        result["method_calls"].append([
            fact.file_path, fact.func_qn, fact.receiver, fact.method,
            fact.block_id, fact.line,
        ])

    from emend.fact_graph import build_def_use_facts
    for du in build_def_use_facts(cfgs, sym_facts_for_file, rel_path, module_name):
        result["def_uses"].append([
            du.file_path, du.func_qn, du.var_name, du.kind,
            du.def_block, du.use_block,
            du.def_line, du.def_col, du.use_line, du.use_col,
        ])

    # CFGs cover functions only.  Preserve module-level data flow using the
    # same reference stream regardless of which builder invoked us.
    from emend.location_resolver import MODULE_LEVEL_BLOCK, MODULE_LEVEL_FUNC
    module_defs: dict[str, list[tuple[int, int]]] = {}
    module_uses: dict[str, list[tuple[int, int]]] = {}
    for qn, line, col, kind in file_refs:
        if _find_containing_block(content_block_ranges, line) != ("", -1):
            continue
        name = qn.rsplit(".", 1)[-1]
        target = module_defs if kind == "write" else module_uses
        if kind in ("write", "read", "call"):
            target.setdefault(name, []).append((line - 1, col))
    for name, uses in module_uses.items():
        for def_line, def_col in module_defs.get(name, []):
            for use_line, use_col in uses:
                result["def_uses"].append([
                    rel_path, MODULE_LEVEL_FUNC, name, "write",
                    MODULE_LEVEL_BLOCK, MODULE_LEVEL_BLOCK,
                    def_line, def_col, use_line, use_col,
                ])

    return result


def _build_facts_db(
    project_root: str,
    scope_resolver=None,
    precomputed_refs: dict[str, list[tuple]] | None = None,
) -> None:
    """Build CozoDB facts.db directly from source files.

    Extracts all analysis facts (symbols, references, imports, CFG, def-use,
    etc.) directly from source files using the Rust emend_core extractors.
    Called once after indexing completes from the main process.

    This is the canonical path for populating facts.db — it does NOT read
    from parse.db's structured-analysis tables (symbol_index, reference_index,
    import_graph).  Those SQLite tables exist only for editor search and
    QN pre-filtering; Cozo owns all structured analysis data.

    When called from ``warm_caches()``, pre-computed references from the
    index phase can be passed via *precomputed_refs* to skip the expensive
    scope resolver (~34s on Django-sized projects).

    Args:
        project_root: Root directory of the project.
        scope_resolver: Optional pre-built PyScopeResolver from the index
            phase.  When provided, skips the expensive per-file index_file()
            calls.
        precomputed_refs: Optional dict mapping absolute file paths to lists
            of ``(target_qn, line, col, ref_kind)`` tuples.  When provided,
            skips scope resolver entirely for reference extraction.
    """
    from concurrent.futures import ThreadPoolExecutor
    import multiprocessing

    from emend.fact_graph import _bfs_reachable_blocks
    from emend import emend_core as _rust

    cache_dir = _cache_db_dir(project_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    facts_path = str(cache_dir / "facts.db")

    try:
        fdb = _open_facts_db(facts_path)
    except BUG_EXCEPTIONS:
        raise
    except Exception:
        logger.debug("could not open facts db at %s", facts_path, exc_info=True)
        return

    resolved_root = str(Path(project_root).resolve())

    def _to_rel(abs_path: str) -> str:
        """Convert an absolute file path to relative (to project root)."""
        try:
            return str(Path(abs_path).relative_to(resolved_root))
        except ValueError:
            return abs_path

    try:
        # Discover source files directly from the filesystem, for all
        # languages present in the project (auto-detected).
        from emend.file_collection import collect_all_source_files as _collect_all_source_files
        from .project_iter import _file_to_module
        source_files = _collect_all_source_files(resolved_root)

        # Read all file contents up-front for scope resolver indexing.
        file_contents: list[tuple[str, str, str, str]] = []  # (abs, rel, ext, content)
        for abs_path in source_files:
            rel_path = _to_rel(abs_path)
            try:
                content = Path(abs_path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            ext = Path(abs_path).suffix.lstrip(".") or "py"
            file_contents.append((abs_path, rel_path, ext, content))

        # Create a project-level scope resolver whenever precomputed references
        # do not cover every discovered Python file.  ``warm_caches(src/)`` may
        # legitimately provide only a subset while facts.db remains
        # project-wide; missing files must still contribute references.
        covered_paths = set(precomputed_refs) if precomputed_refs is not None else set()
        needs_resolver = precomputed_refs is None or any(
            ext == "py" and abs_path not in covered_paths
            for abs_path, _rel, ext, _content in file_contents
        )
        if needs_resolver and scope_resolver is None:
            scope_resolver = _rust.PyScopeResolver(resolved_root)
            for abs_path, _rel, _ext, content in file_contents:
                try:
                    scope_resolver.index_file(abs_path, content)
                except Exception:
                    logger.debug("scope indexing failed for %s", abs_path, exc_info=True)

        # ------------------------------------------------------------------
        # Per-file extraction: symbols, references, imports, CFG, def-use,
        # method_call, source_loc — parallelized via ThreadPoolExecutor.
        # Rust extension methods release the GIL, enabling true parallelism.
        # ------------------------------------------------------------------
        max_workers = min(multiprocessing.cpu_count() or 4, 8)

        def _process_file(file_tuple):
            abs_path, rel_path, ext, content = file_tuple
            module_name = _file_to_module(abs_path, project_root)
            file_refs = (
                precomputed_refs.get(abs_path)
                if precomputed_refs is not None
                else None
            )

            # For files not covered by precomputed_refs, create a per-file
            # scope resolver with the correct extension so that TS/Rust
            # files get proper reference extraction.
            file_resolver = scope_resolver
            if file_refs is None and ext != "py":
                try:
                    file_resolver = _rust.PyScopeResolver(resolved_root, ext)
                    file_resolver.index_file(abs_path, content)
                except Exception:
                    logger.debug(
                        "per-file scope resolver failed for %s", abs_path,
                        exc_info=True,
                    )
                    file_resolver = scope_resolver

            return _extract_file_facts(
                abs_path, rel_path, ext, content,
                project_root, module_name, file_resolver,
                _build_fact_sym_rows,
                precomputed_refs=file_refs,
            )

        # Collect all per-file results
        cozo_fact_sym: list[list] = []
        cozo_fact_ref: list[list] = []
        cozo_fact_imp: list[list] = []
        cozo_fg_sym: list[list] = []
        dec_rows_list: list[list] = []
        all_cfg_blocks: list[list] = []
        all_cfg_edges: list[list] = []
        all_fg_refs: list[list] = []
        all_calls: list[list] = []
        all_calls_by_callee: list[list] = []
        all_calls_by_file: list[list] = []
        all_def_uses: list[list] = []
        all_method_calls: list[list] = []
        all_source_locs: list[list] = []
        all_imports: list[list] = []
        all_ref_by_block: list[list] = []
        all_module_level_refs: list[list] = []
        all_exported_qns: list[list] = []

        _KEYS_TO_LISTS = {
            "fact_sym": cozo_fact_sym, "fact_ref": cozo_fact_ref,
            "fact_imp": cozo_fact_imp, "fg_sym": cozo_fg_sym,
            "dec": dec_rows_list, "cfg_blocks": all_cfg_blocks,
            "cfg_edges": all_cfg_edges, "fg_refs": all_fg_refs,
            "calls": all_calls, "calls_by_callee": all_calls_by_callee,
            "calls_by_file": all_calls_by_file, "def_uses": all_def_uses,
            "method_calls": all_method_calls, "source_locs": all_source_locs,
            "imports": all_imports, "ref_by_block": all_ref_by_block,
            "module_level_refs": all_module_level_refs,
            "exported_qns": all_exported_qns,
        }

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for file_result in executor.map(_process_file, file_contents):
                for key, target in _KEYS_TO_LISTS.items():
                    target.extend(file_result[key])

        # -- Compute reachable blocks via BFS from entry blocks
        entries_by_func: dict[tuple[str, str], set[int]] = {}
        adj: dict[tuple[str, str, int], list[int]] = {}
        for row in all_cfg_blocks:
            fp, fq, bid, is_entry, _ = row
            if is_entry:
                entries_by_func.setdefault((fp, fq), set()).add(bid)
        for row in all_cfg_edges:
            fp, fq, fb, tb = row[0], row[1], row[2], row[3]
            adj.setdefault((fp, fq, fb), []).append(tb)

        all_reachable = _bfs_reachable_blocks(entries_by_func, adj)

        # -- Batch CozoDB writes using :replace for atomic swap
        fdb.run(
            "?[fp, mqn, name, qn, kind, line, end_line, depth, "
            "parent, bases, sig, returns, decs, is_entry, is_exported, has_noqa] <- $rows "
            ":replace fact_symbol {fp, mqn => name, qn, kind, line, end_line, depth, "
            "parent, bases, sig, returns, decs, is_entry, is_exported, has_noqa}",
            {"rows": cozo_fact_sym},
        )

        fdb.run(
            "?[tqn, fp, line, col, kind] <- $rows "
            ":replace fact_reference {tqn, fp, line, col => kind}",
            {"rows": cozo_fact_ref},
        )

        fdb.run(
            "?[fp, mod] <- $rows "
            ":replace fact_import {fp, mod}",
            {"rows": cozo_fact_imp},
        )

        fdb.run(
            "?[qn, fp, name, kind, line, end_line, parent] <- $rows "
            ":replace symbol {qn => fp, name, kind, line, end_line, parent}",
            {"rows": cozo_fg_sym},
        )

        fdb.run(
            "?[symbol_qn, decorator] <- $rows "
            ":replace decorator_on {symbol_qn, decorator}",
            {"rows": dec_rows_list},
        )

        fdb.run(
            "?[symbol_qn, file_path, line, col, ref_kind, func_qn, block_id] <- $rows "
            ":replace reference {symbol_qn, file_path, line, col => ref_kind, func_qn, block_id}",
            {"rows": all_fg_refs},
        )

        fdb.run(
            "?[caller_qn, callee_qn, file_path, line, col, func_qn, block_id] <- $rows "
            ":replace call {caller_qn, callee_qn, file_path, line, col => func_qn, block_id}",
            {"rows": all_calls},
        )

        fdb.run(
            "?[callee_qn, caller_qn, file_path, line, col, func_qn, block_id] <- $rows "
            ":replace call_by_callee {callee_qn, caller_qn, file_path, line, col => func_qn, block_id}",
            {"rows": all_calls_by_callee},
        )

        fdb.run(
            "?[file_path, caller_qn, callee_qn, line, col, func_qn, block_id] <- $rows "
            ":replace call_by_file {file_path, caller_qn, callee_qn, line, col => func_qn, block_id}",
            {"rows": all_calls_by_file},
        )

        fdb.run(
            "?[file_path, func_qn, block_id, is_entry, is_exit] <- $rows "
            ":replace cfg_block {file_path, func_qn, block_id => is_entry, is_exit}",
            {"rows": all_cfg_blocks},
        )

        fdb.run(
            "?[file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line] <- $rows "
            ":replace cfg_edge {file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line}",
            {"rows": all_cfg_edges},
        )

        fdb.run(
            "?[file_path, func_qn, var_name, kind, def_block, use_block, "
            "def_line, def_col, use_line, use_col] <- $rows "
            ":replace def_use {file_path, func_qn, var_name, kind, def_block, use_block "
            "=> def_line, def_col, use_line, use_col}",
            {"rows": all_def_uses},
        )

        fdb.run(
            "?[file_path, func_qn, receiver, method, block_id, line] <- $rows "
            ":replace method_call {file_path, func_qn, receiver, method, block_id, line}",
            {"rows": all_method_calls},
        )

        fdb.run(
            "?[file_path, loc_kind, loc_id, line, col, end_line, rel_line] <- $rows "
            ":replace source_loc {file_path, loc_kind, loc_id => line, col, end_line, rel_line}",
            {"rows": all_source_locs},
        )

        fdb.run(
            "?[importing_file, imported_module, imported_name, line, alias] <- $rows "
            ":replace import {importing_file, imported_module, imported_name, line => alias}",
            {"rows": all_imports},
        )

        fdb.run(
            "?[file_path, func_qn, block_id, symbol_qn] <- $rows "
            ":replace ref_by_block {file_path, func_qn, block_id, symbol_qn}",
            {"rows": all_ref_by_block},
        )

        fdb.run(
            "?[file_path, func_qn, block_id] <- $rows "
            ":replace reachable_block {file_path, func_qn, block_id}",
            {"rows": all_reachable},
        )

        fdb.run(
            "?[symbol_qn, file_path, line] <- $rows "
            ":replace module_level_ref {symbol_qn, file_path, line}",
            {"rows": all_module_level_refs},
        )

        fdb.run(
            "?[qualified_name] <- $rows "
            ":replace exported_symbol {qualified_name}",
            {"rows": all_exported_qns},
        )

    except BUG_EXCEPTIONS:
        raise
    except Exception:
        logger.debug("facts db build failed", exc_info=True)
    finally:
        try:
            fdb.close()
        except Exception:
            logger.debug("facts db close failed", exc_info=True)

    # Invalidate the singleton cache so the next _get_facts_db() call
    # opens a fresh handle that sees the newly written data.
    key = str(Path(project_root).resolve())
    _facts_db_cache.pop(key, None)


# ---------------------------------------------------------------------------
# Qualified-name index cache: per-file set of QN strings for pre-filtering
# ---------------------------------------------------------------------------
# After the first cross-project operation populates this cache, subsequent
# operations can skip MetadataWrapper for files whose QN set doesn't overlap
# with the target.  Content-hash keyed, persisted in the same SQLite DB.
