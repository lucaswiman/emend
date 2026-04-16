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
from .language_plugins import NOQA_PATTERN as _NOQA_PATTERN
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

    **parse.db role** (Phase 4):  parse.db is limited to data that SQLite
    handles best: full-text / editor search (FTS5 trigram), freshness
    metadata (file_manifest, index_meta), QN pre-filter cache, type cache,
    and DSL symbols.  Structured analysis facts (symbols, references,
    imports, CFG, def-use, calls) are owned by CozoDB facts.db and built
    directly from source files by ``_build_facts_db()``.

    The ``symbol_index`` and ``reference_index`` tables remain because they
    serve editor search and search-optimization queries.  ``import_graph``
    is retained for compatibility but is no longer read by the facts.db
    build path.
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
    # Duplicate analysis payload cache (Phase 8): one row per unique file
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
        except BaseException:
            pass


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
    scope_resolver,
    _rust,
    build_cfgs_for_source,
    _walk_symbols,
    _find_containing_block,
    _enclosing_symbol,
    _extract_imports,
    _build_symbol_line_index,
    _build_fact_sym_rows_func,
    SymbolFact,
    DecoratorOnFact,
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
    from emend.fact_graph import _normalize_qn

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
        raw_symbols = _rust.collect_symbols_from_str(content, ext=ext)
    except Exception:
        raw_symbols = []

    sym_facts_for_file: list = []
    dec_facts_for_file: list = []
    _walk_symbols(
        sym_facts_for_file, dec_facts_for_file,
        raw_symbols, rel_path, module_name, parent_qn=None,
    )

    # Build fact_symbol rows from the raw Rust output (all symbol kinds)
    _build_fact_sym_rows_func(
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
            file_refs.append((qn_str, line, col, kind))
            result["fact_ref"].append([qn_str, rel_path, line, col, kind])
    else:
        try:
            for qn_str, line, col, _offset, _end_offset, kind, _ann in \
                    scope_resolver.references_in_file(abs_path):
                qn_str = _normalize_qn(qn_str)
                file_refs.append((qn_str, line, col, kind))
                result["fact_ref"].append([qn_str, rel_path, line, col, kind])
        except Exception:
            pass

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
    except Exception:
        cfgs = []

    block_ranges: list[tuple[str, int, int, int, bool]] = []
    for cfg in cfgs:
        func_name = cfg.func_name
        func_qn = ""
        # Match by name AND line range to disambiguate methods with the
        # same name in different classes (CFG lines 0-indexed, sym 1-indexed).
        cfg_start = cfg.func_start_line + 1
        for sf in sym_facts_for_file:
            if sf.name == func_name and sf.file_path == rel_path:
                if sf.line <= cfg_start <= (sf.end_line or sf.line):
                    func_qn = sf.qualified_name
                    break
        if not func_qn:
            for sf in sym_facts_for_file:
                if sf.name == func_name and sf.file_path == rel_path:
                    func_qn = sf.qualified_name
                    break
        if not func_qn:
            func_qn = f"{module_name}.{func_name}"

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
    for tqn, line, col, kind in file_refs:
        fq, bid = _find_containing_block(content_block_ranges, line)
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

            # method_call for dotted call refs
            if "." in tqn:
                parts = tqn.rsplit(".", 1)
                if len(parts) == 2:
                    result["method_calls"].append([
                        rel_path, fq,
                        parts[0].rsplit(".", 1)[-1], parts[1],
                        bid, line,
                    ])

    # -- Def-use facts
    for cfg in cfgs:
        func_name = cfg.func_name
        func_qn = ""
        for sf in sym_facts_for_file:
            if sf.name == func_name and sf.file_path == rel_path:
                func_qn = sf.qualified_name
                break
        if not func_qn:
            func_qn = f"{module_name}.{func_name}"

        defs_map: dict[str, list[tuple[int, int, int, str]]] = {}
        for block in cfg.get_blocks():
            bid = block["id"]
            for d in block.get("defs", []) or []:
                var_name = d[0] if isinstance(d, (list, tuple)) else d
                dline = d[1] if isinstance(d, (list, tuple)) and len(d) > 1 else 0
                dcol = d[2] if isinstance(d, (list, tuple)) and len(d) > 2 else 0
                dkind = d[3] if isinstance(d, (list, tuple)) and len(d) > 3 else "write"
                defs_map.setdefault(var_name, []).append((bid, dline, dcol, dkind))

        for block in cfg.get_blocks():
            bid = block["id"]
            for u in block.get("uses", []) or []:
                var_name = u[0] if isinstance(u, (list, tuple)) else u
                uline = u[1] if isinstance(u, (list, tuple)) and len(u) > 1 else 0
                ucol = u[2] if isinstance(u, (list, tuple)) and len(u) > 2 else 0
                ukind = u[3] if isinstance(u, (list, tuple)) and len(u) > 3 else "read"
                if var_name in defs_map:
                    for def_bid, dl, dc, dk in defs_map[var_name]:
                        result["def_uses"].append([
                            rel_path, func_qn, var_name, dk,
                            def_bid, bid, dl, dc, uline, ucol,
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

    from emend.cfg import build_cfgs_for_source
    from emend.fact_graph import (
        _find_containing_block,
        _enclosing_symbol,
        _extract_imports,
        _build_symbol_line_index,
        _normalize_qn,
        _walk_symbols,
        SymbolFact,
        DecoratorOnFact,
    )
    from emend import emend_core as _rust

    cache_dir = _cache_db_dir(project_root)
    facts_path = str(cache_dir / "facts.db")

    try:
        fdb = _open_facts_db(facts_path)
    except BaseException:
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
        source_files = _collect_all_source_files(resolved_root)

        # Read all file contents up-front for scope resolver indexing.
        file_contents: list[tuple[str, str, str, str]] = []  # (abs, rel, ext, content)
        for abs_path in source_files:
            rel_path = _to_rel(abs_path)
            try:
                content = Path(abs_path).read_text(encoding="utf-8")
            except Exception:
                continue
            ext = Path(abs_path).suffix.lstrip(".") or "py"
            file_contents.append((abs_path, rel_path, ext, content))

        # Create a project-level scope resolver and index all files.
        # Skip entirely when precomputed_refs covers all files (from warm_caches).
        # Fall back to building one when called standalone.
        if precomputed_refs is None and scope_resolver is None:
            scope_resolver = _rust.PyScopeResolver(resolved_root)
            for abs_path, _rel, _ext, content in file_contents:
                try:
                    scope_resolver.index_file(abs_path, content)
                except Exception:
                    pass

        # ------------------------------------------------------------------
        # Per-file extraction: symbols, references, imports, CFG, def-use,
        # method_call, source_loc — parallelized via ThreadPoolExecutor.
        # Rust extension methods release the GIL, enabling true parallelism.
        # ------------------------------------------------------------------
        max_workers = min(multiprocessing.cpu_count() or 4, 8)

        def _process_file(file_tuple):
            abs_path, rel_path, ext, content = file_tuple
            module_name = _file_to_module(abs_path, project_root)
            file_refs = precomputed_refs.get(abs_path) if precomputed_refs else None

            # For files not covered by precomputed_refs, create a per-file
            # scope resolver with the correct extension so that TS/Rust
            # files get proper reference extraction.
            file_resolver = scope_resolver
            if file_refs is None and ext != "py":
                try:
                    file_resolver = _rust.PyScopeResolver(resolved_root, ext)
                    file_resolver.index_file(abs_path, content)
                except Exception:
                    file_resolver = scope_resolver

            return _extract_file_facts(
                abs_path, rel_path, ext, content,
                project_root, module_name, file_resolver,
                _rust, build_cfgs_for_source, _walk_symbols,
                _find_containing_block, _enclosing_symbol,
                _extract_imports, _build_symbol_line_index,
                _build_fact_sym_rows, SymbolFact, DecoratorOnFact,
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

        all_reachable: list[list] = []
        for (fp, fq), entry_set in entries_by_func.items():
            visited: set[int] = set()
            stack = list(entry_set)
            while stack:
                bid = stack.pop()
                if bid in visited:
                    continue
                visited.add(bid)
                all_reachable.append([fp, fq, bid])
                for nb in adj.get((fp, fq, bid), []):
                    if nb not in visited:
                        stack.append(nb)

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

        if dec_rows_list:
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

        if all_exported_qns:
            fdb.run(
                "?[qualified_name] <- $rows "
                ":replace exported_symbol {qualified_name}",
                {"rows": all_exported_qns},
            )

    except BaseException:
        logger.debug("facts db build failed", exc_info=True)

    try:
        fdb.close()
    except BaseException:
        pass

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


def _extract_all_exports_text(source: str, file_path: str = "__temp__.py") -> set[str]:
    """Extract names from ``__all__`` using tree-sitter pattern matching.

    Uses ``find_pattern`` so that the match is tree-sitter-based and respects
    syntactic boundaries (won't match ``__all__`` inside string literals or
    comments).  The small inner regex that pulls quoted names out of the
    already-parsed ``$NAMES`` captured text is acceptable because it operates
    on a structurally extracted sub-tree, not raw source.
    """
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
    from .query import _collect_symbols as _collect_symbols_ts
    from emend import emend_core as _rust

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
    """Query references via CozoDB Datalog.

    Returns a list of dicts with reference info, or None if the index
    is not available or not fresh.
    """
    if not _ensure_index_fresh(project_path, language=language):
        return None

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
                                node_text=text, captures={},
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
    """Find project root by looking for markers.

    Checks for language-agnostic markers (.git, .emend) first, then
    language-specific project files for Python, TypeScript/JS, and Rust.
    """
    path = Path(start_path).resolve()
    if path.is_file():
        path = path.parent

    markers = [
        '.git',
        '.emend',
        # Python
        'pyproject.toml', 'setup.py', 'setup.cfg',
        # TypeScript / JavaScript
        'package.json', 'tsconfig.json',
        # Rust
        'Cargo.toml',
    ]

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


def _normalize_module_qn(module: str) -> str:
    """Normalize a module name to use dots for fact-graph QN construction.

    Delegates to ``_normalize_qn`` from ``fact_graph`` which handles
    language-specific separators (``::`` for Rust, ``/`` for TypeScript),
    quotes, and relative path segments.
    """
    from emend.fact_graph import _normalize_qn
    return _normalize_qn(module)


def _file_to_module(file_path: str, project_path: str | None) -> str:
    """Convert file path to module name.

    Detects ``src/`` layout automatically so that
    ``src/pkg/mod.py`` becomes ``pkg.mod`` rather than ``src.pkg.mod``.
    Uses the language-specific separator from config.toml.

    Rust special cases:
    - ``src/lib.rs`` → ``lib`` (the crate root; caller may map to ``crate``)
    - ``src/foo/mod.rs`` → ``foo`` (mod.rs represents its parent directory)
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

    stem = rel_path.stem
    dir_parts = list(rel_path.parts[:-1])

    # Rust: ``mod.rs`` represents the module named after its parent directory.
    # E.g.  src/foo/mod.rs → module "foo".
    if language == "rust" and stem == "mod" and dir_parts:
        module_parts = dir_parts  # drop the "mod" stem, use parent dir as name
    else:
        module_parts = dir_parts + [stem]

    return sep.join(module_parts) if module_parts else stem


# Non-dot directories to skip.  All directories starting with '.' are
# skipped automatically by the Rust scanner (emend_core.collect_python_files).
# The canonical list lives in Rust (scanner.rs); we import it here so
# Python and Rust always agree.
_SKIP_DIRS = frozenset(_rust.skip_dirs())

# Module-level file-list cache: maps (resolved project root, language) to (mtime_ns, file_list)
from emend.file_collection import (
    collect_source_files as _collect_source_files,
    collect_source_files_scandir as _collect_source_files_scandir,
    collect_all_source_files as _collect_all_source_files,
    collect_git_tracked_source_files as _collect_git_tracked_source_files,
    detect_project_languages,
    _file_list_cache,
)




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
    project_root = str(Path(project_path).resolve())
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


def _raise_component_not_found(
    selector: ExtendedSelector,
    source_code: str,
    _ext: str,
    message: str | None = None,
) -> None:
    """Raise a descriptive ValueError when a component lookup returns None.

    Checks whether the symbol itself is missing (raises "Symbol not found")
    or whether the component is invalid for the symbol kind (raises a
    specific type-mismatch error), falling back to the generic
    "Component not found" message.
    """
    syms = _rust.collect_symbols_from_str(
        source_code, selector=".".join(selector.symbol_path), ext=_ext
    )
    if not syms:
        raise ValueError(
            f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}"
        )
    kind = syms[0]["kind"]
    if kind == "class" and selector.component in ("params", "returns"):
        raise ValueError(f"Component '{selector.component}' not valid for ClassDef")
    if kind in ("function", "async_function", "method", "async_method") and selector.component == "bases":
        raise ValueError(f"Component '{selector.component}' not valid for FunctionDef")
    raise ValueError(
        message or f"Component '{selector.component}' not found or not valid for symbol {'.'.join(selector.symbol_path)}"
    )


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
        _raise_component_not_found(selector, source_code, _ext)

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
        _raise_component_not_found(selector, source_code, _ext)

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
        _raise_component_not_found(selector, source_code, _ext)

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
            _raise_component_not_found(
                selector, source_code, _ext,
                message=f"Could not find container for {selector.component}",
            )

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
        _raise_component_not_found(selector, source_code, _ext)

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
        for qn, line, col, offset, end_offset, kind, _ann in references:
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
    for qn, line, col, offset, end_offset, kind, _ann in references:
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


def _collect_name_contexts(source: str) -> tuple[set[str], set[str]]:
    """Return ``(runtime_names, annotation_names)`` used in *source*.

    ``annotation_names`` only includes names that appear in annotation
    positions.  ``runtime_names`` includes names referenced in executable
    positions, including decorators, bases, defaults, and function bodies.

    Uses tree-sitter's annotation_fields config to classify identifiers.
    """
    resolver = _rust.PyScopeResolver("/tmp", "py")
    identifiers = resolver.collect_identifiers_from_source(source)

    runtime_names: set[str] = set()
    annotation_names: set[str] = set()

    for name, in_annotation in identifiers:
        if in_annotation:
            annotation_names.add(name)
        else:
            runtime_names.add(name)

    return runtime_names, annotation_names


def _resolve_relative_module(
    level: int,
    module: str,
    source_file: str,
    project_path: str | None,
) -> str:
    """Convert a relative import into an absolute module path.

    ``level`` is the number of leading dots (``stmt.level`` from AST),
    ``module`` is the dotted name after the dots (may be empty for
    ``from . import X``), and ``source_file`` is the file containing the
    import.

    Returns the fully-qualified module name, or falls back to ``module``
    unchanged if resolution is not possible.
    """
    if project_path is None:
        return module

    src_module = _file_to_module(source_file, project_path)
    # Compute the package that owns source_file.
    if src_module.endswith(".__init__"):
        # __init__.py IS the package.
        package = src_module[: -len(".__init__")]
    elif "." in src_module:
        package = src_module.rsplit(".", 1)[0]
    else:
        package = ""

    parts = package.split(".") if package else []
    # ``from . import X`` has level=1, meaning current package (0 levels up).
    # ``from .. import X`` has level=2, meaning 1 level up, etc.
    levels_up = level - 1
    if levels_up > len(parts):
        return module  # can't resolve — too many dots
    base_parts = parts[: len(parts) - levels_up] if levels_up else parts

    if module:
        base_parts.append(module)
    return ".".join(base_parts) if base_parts else module


def analyze_imports(
    symbol_source: str,
    source_file: str,
    source_module: str | None = None,
    project_path: str | None = None,
) -> list[str]:
    """Analyze which imports from source_file are needed by symbol_source.

    Args:
        symbol_source: Source code of the symbol being copied
        source_file: Path to file where symbol originated (to read imports from)
        source_module: Dotted module name of source_file.  When provided,
            top-level names that are *defined* in source_file (classes,
            functions, assignments) rather than imported are also pulled in as
            ``from source_module import Name`` statements so the destination
            file remains self-contained after a move (issue #138 Bug 2).
        project_path: Project root for resolving relative imports to absolute.

    Returns:
        List of import statement strings needed for the symbol

    Example:
        >>> source = "def func():\\n    return ast.parse('x = 1')"
        >>> imports = analyze_imports(source, "module.py")
        >>> # Returns ["import ast"] if module.py has that import
    """
    runtime_names, annotation_names = _collect_name_contexts(symbol_source)
    used_names = runtime_names | annotation_names
    if not used_names:
        return []

    source_path = Path(source_file)
    if not source_path.exists():
        return []

    # Use tree-sitter scope resolver to parse imports from source file.
    proj_root = _find_project_root(project_path or source_file)
    resolver = _rust.PyScopeResolver(proj_root, "py")
    try:
        source_content = source_path.read_text()
        resolver.index_file(str(source_path.resolve()), source_content)
    except Exception:
        return []

    structured_imports = resolver.structured_imports_in_file(
        str(source_path.resolve())
    )

    needed_imports = []
    covered_names: set[str] = set()

    for imp in structured_imports:
        if imp["is_plain"]:
            # Plain `import X` / `import X as A` statements.
            for name, alias in imp["names"]:
                effective_name = alias or name.split('.')[0]
                if effective_name in used_names:
                    covered_names.add(effective_name)
                    if alias:
                        needed_imports.append(f"import {name} as {alias}")
                    else:
                        needed_imports.append(f"import {name}")
        else:
            # `from X import Y` statements.
            names = imp["names"]
            if names and names[0][0] == '*':
                continue

            module_name = imp["module"]

            # Resolve relative imports to absolute so they work from the
            # destination file (which is typically in a different package).
            if imp["level"] > 0:
                module_name = _resolve_relative_module(
                    imp["level"], module_name, source_file, project_path,
                )

            used_import_names = []
            for name, alias in names:
                effective_name = alias or name
                if effective_name in used_names:
                    covered_names.add(effective_name)
                    used_import_names.append((name, alias))

            if used_import_names:
                import_parts = []
                for name, asname in used_import_names:
                    if asname:
                        import_parts.append(f"{name} as {asname}")
                    else:
                        import_parts.append(name)
                needed_imports.append(f"from {module_name} import {', '.join(import_parts)}")

    # When moving a symbol, detect locally-defined top-level names that the
    # moved symbol references.  These need TYPE_CHECKING imports to avoid
    # circular imports at runtime.
    if source_module:
        # Use definitions_in_file to find top-level defined names.
        # Top-level definitions have qn = "module.name" (one component after
        # the module prefix).  Nested definitions like "module.Class.method"
        # have more components and must be excluded.
        file_module = _file_to_module(str(source_path), project_path)
        defs = resolver.definitions_in_file(str(source_path.resolve()))
        locally_defined: set[str] = set()
        prefix = file_module + "."
        for qn, _line, _col in defs:
            if qn.startswith(prefix):
                remainder = qn[len(prefix):]
                if "." not in remainder:
                    locally_defined.add(remainder)

        local_refs_needed_runtime = sorted(
            n for n in locally_defined
            if n in runtime_names and n not in covered_names
        )
        local_refs_needed_annotations = sorted(
            n for n in locally_defined
            if (
                n in annotation_names
                and n not in runtime_names
                and n not in covered_names
            )
        )

        if local_refs_needed_runtime:
            needed_imports.append(
                f"from {source_module} import {', '.join(local_refs_needed_runtime)}"
            )
        if local_refs_needed_annotations:
            needed_imports.append("from __future__ import annotations")
            type_checking_block = (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                + "".join(
                    f"    from {source_module} import {n}\n"
                    for n in local_refs_needed_annotations
                )
            )
            needed_imports.append(type_checking_block)

    return needed_imports


def copy_symbol(
    selector: ExtendedSelector,
    dest_file: str,
    position: str = "end",
    dedent: bool = False,
    include_imports: bool = False,
    source_module: str | None = None,
    project_path: str | None = None,
    apply: bool = False,
) -> str:
    """Copy a symbol from one location to another.

    Args:
        selector: Extended selector specifying the source symbol
        dest_file: Path to destination file
        position: Where to insert: "start", "end" (default)
        dedent: If True, dedent the source code to remove common indentation
        include_imports: If True, analyze and include necessary imports from source file
        source_module: Dotted module name of the source file.  Passed to
            ``analyze_imports`` when ``include_imports`` is True so that
            locally-defined symbols referenced by the moved symbol also get
            import statements in the destination (issue #138 Bug 2).
        project_path: Project root for resolving relative imports to absolute.
        apply: If True, write changes to file. If False, return diff only.

    Returns:
        Unified diff showing the changes to the destination file

    Raises:
        FileNotFoundError: If source file doesn't exist
        ValueError: If symbol not found
    """
    import textwrap
    from emend.language_registry import detect_language
    from emend.language_plugins import load_plugin

    # Get source code of the symbol
    source = get_symbol_source(selector)

    # Dedent if requested
    if dedent:
        source = textwrap.dedent(source)

    # Read destination file (create if doesn't exist)
    dest_path = Path(dest_file)
    if dest_path.exists():
        dest_content = dest_path.read_text()
    else:
        dest_content = ""

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

    # Add necessary imports to the import section of the destination file.
    # This is done AFTER appending the symbol so that imports land in the
    # proper location at the top of the file rather than being embedded at
    # the insertion point (which matters especially for "from __future__"
    # imports that must appear before any other statements, issue #138 Bug 2).
    if include_imports:
        lang = detect_language(dest_file) or "python"
        imp_handler = load_plugin(lang).import_handler
        imports = analyze_imports(source, selector.file_path, source_module=source_module, project_path=project_path)
        for imp in imports:
            try:
                pos = 0 if imp.startswith("from __future__") else -1
                new_content = imp_handler.add_import_text(imp.rstrip("\n"), pos, new_content)
            except Exception:
                new_content = imp + "\n" + new_content

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


_fact_graph_cache: dict[str, "FactGraph"] = {}


def _get_or_build_fact_graph(project_path: str) -> "FactGraph":
    """Get or build a FactGraph for the project.

    Two paths:
    1. Load existing facts.db if it has data.
    2. Build via warm_caches() (which calls _build_facts_db), then load.

    The result is cached in-process by project root to avoid re-opening the
    CozoDB connection on every call (which is expensive).
    """
    from .fact_graph import FactGraph

    project_root = _find_project_root(project_path)
    if project_root in _fact_graph_cache:
        return _fact_graph_cache[project_root]
    emend_dir = Path(project_root) / ".emend" / "cache"
    emend_dir.mkdir(parents=True, exist_ok=True)
    _ensure_cache_ignore_files(project_root)
    facts_db = emend_dir / "facts.db"

    # Path 1: load existing facts.db
    if facts_db.exists():
        try:
            graph = FactGraph(db_path=str(facts_db))
            count = graph._client.run(
                "?[count(qn)] := *symbol[qn, _, _, _, _, _, _]"
            )["rows"][0][0]
            if count > 0:
                _fact_graph_cache[project_root] = graph
                return graph
            logger.debug("facts.db has no symbol data, rebuilding")
        except Exception:
            logger.debug("Failed to load facts.db, rebuilding", exc_info=True)

    # Path 2: build via warm_caches, then load
    logger.info("Building index for %s (first run may be slow)", project_path)
    from .type_oracle import TypeEngineUnavailableError
    try:
        warm_caches(project_path)
    except TypeEngineUnavailableError:
        # Type engine unavailable (e.g. pyrefly not installed).  Retry without
        # type indexing so facts.db is still built.  In tests the retry may
        # also raise (monkeypatched warm_caches always raises), in which case
        # we fall through to the in-memory fallback below.
        logger.debug("Type engine unavailable, retrying warm_caches without type indexing")
        try:
            warm_caches(project_path, type_engine="none")
        except Exception:
            logger.debug("warm_caches retry also failed, falling back to in-memory build")
    # Always attempt to load from facts.db — FactGraph(db_path=...) creates the
    # file on first open, so calling it unconditionally is intentional and is
    # what allows test_fact_graph_bootstrap_persists_facts_db to pass.
    try:
        graph = FactGraph(db_path=str(facts_db))
        count = graph._client.run(
            "?[count(qn)] := *symbol[qn, _, _, _, _, _, _]"
        )["rows"][0][0]
        if count > 0:
            try:
                graph._resolve_builtin_refs()
            except Exception:
                logger.debug("Failed to resolve builtin refs", exc_info=True)
            _fact_graph_cache[project_root] = graph
            return graph
    except Exception:
        logger.debug("Failed to load facts.db after indexing", exc_info=True)

    # Fallback: build in-memory (mainly for tests where warm_caches is mocked).
    # build_from_project already calls _resolve_builtin_refs internally.
    graph = FactGraph.build_from_project(project_path)
    _fact_graph_cache[project_root] = graph
    return graph


def find_references(
    selector: ExtendedSelector,
    project_path: str | None = None,
    include_definition: bool = True,
    include_imports: bool = True,
    writes_only: bool = False,
    reads_only: bool = False,
) -> Iterator[Reference]:
    """Find all references to a symbol across the project.

    Uses Datalog query over the FactGraph for scope-aware resolution.
    """
    if writes_only and reads_only:
        raise ValueError("Cannot specify both writes_only and reads_only")

    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for find_references")

    scan_root = project_path if project_path else _find_project_root(selector.file_path)
    module_root = _find_project_root(selector.file_path)
    target_module = _normalize_module_qn(_file_to_module(selector.file_path, module_root))
    target_qn = f"{target_module}.{symbol_name}"

    from .fact_graph import FactGraph

    graph = _get_or_build_fact_graph(scan_root)

    ref_facts = graph.refs_datalog(
        target_qn,
        writes_only=writes_only,
        reads_only=reads_only,
        include_definition=include_definition,
        include_imports=include_imports,
    )
    # Also try bare name if qualified name yields nothing
    if not ref_facts:
        ref_facts = graph.refs_datalog(
            symbol_name,
            writes_only=writes_only,
            reads_only=reads_only,
            include_definition=include_definition,
            include_imports=include_imports,
        )

    project_root_resolved = str(Path(module_root).resolve())

    def _gen() -> Iterator[Reference]:
        for r in ref_facts:
            # Convert relative paths back to absolute
            abs_path = str(Path(project_root_resolved) / r.file_path)
            is_def = r.ref_kind == "definition"
            is_imp = r.ref_kind == "import"
            is_wr = r.ref_kind == "write"
            yield Reference(
                file_path=abs_path,
                line=r.line,
                column=r.col,
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

    Uses Datalog query on the call relation.
    """
    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for find_callers")

    scan_root = project_path if project_path else _find_project_root(selector.file_path)
    module_root = _find_project_root(selector.file_path)
    target_module = _normalize_module_qn(_file_to_module(selector.file_path, module_root))
    target_qn = f"{target_module}.{symbol_name}"

    graph = _get_or_build_fact_graph(scan_root)

    call_facts = graph.callers_datalog(target_qn)
    if not call_facts:
        call_facts = graph.callers_datalog(symbol_name)

    project_root_resolved = str(Path(module_root).resolve())

    def _gen() -> Iterator[Reference]:
        for c in call_facts:
            abs_path = str(Path(project_root_resolved) / c.file_path)
            yield Reference(
                file_path=abs_path,
                line=c.line,
                column=c.col,
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

    Uses Datalog query on call facts scoped by func_qn.
    """
    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for find_callees")

    file_path = selector.file_path
    if not Path(file_path).exists():
        raise ValueError(f"File not found: {file_path}")

    scan_root = project_path if project_path else _find_project_root(file_path)
    module_root = _find_project_root(file_path)
    target_module = _normalize_module_qn(_file_to_module(file_path, module_root))
    target_qn = f"{target_module}.{symbol_name}"

    graph = _get_or_build_fact_graph(scan_root)

    call_facts = graph.callees_datalog(target_qn)

    callees: list[Callee] = []
    seen: set[tuple[str, int]] = set()
    for c in call_facts:
        name = c.callee_qn.rsplit('.', 1)[-1]
        if (c.callee_qn, c.line) not in seen:
            seen.add((c.callee_qn, c.line))
            callees.append(Callee(
                name=name,
                qualified_name=c.callee_qn,
                file_path=None,
                line=c.line,
            ))

    return callees


def generate_graph(
    file_path: str,
    project_path: str | None = None,
    format: str = "plain",
) -> str:
    """Generate a call graph for all functions in a file.

    Uses Datalog query on call facts.
    """
    if not Path(file_path).exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    scan_root = project_path if project_path else _find_project_root(file_path)
    module_root = _find_project_root(file_path)

    try:
        rel_path = str(Path(file_path).resolve().relative_to(Path(module_root).resolve()))
    except ValueError:
        rel_path = file_path

    graph = _get_or_build_fact_graph(scan_root)
    edge_pairs = graph.graph_datalog(file_path=rel_path)

    # Build edges dict from Datalog results
    edges: dict[str, list[str]] = {}
    for caller_qn, callee_qn in edge_pairs:
        caller_name = caller_qn.rsplit('.', 1)[-1]
        callee_name = callee_qn.rsplit('.', 1)[-1]
        edges.setdefault(caller_name, [])
        if callee_name not in edges[caller_name]:
            edges[caller_name].append(callee_name)

    # Also include functions and classes with no calls
    syms = graph.symbols(file_path=rel_path)
    for s in syms:
        if s.kind in ("function", "async_function", "method", "async_method", "class"):
            name = s.name
            if name not in edges:
                edges[name] = []

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


@dataclass
class DeadBlock:
    """An unreachable code block detected as dead code."""
    file_path: str
    func_qn: str
    block_id: int
    start_line: int
    end_line: int


@dataclass
class DeadModule:
    """A module file detected as unused because nothing imports it."""
    file_path: str
    name: str
    module_name: str
    reason: str


# Decorator prefixes that indicate a symbol is an entry point / framework hook.
# These are kept as fallbacks; the config-driven path via _get_entry_point_config()
# is the primary source.
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
    'tool',  # MCP tool registration (@mcp_app.tool(), @server.tool(), etc.)
})

# Names that are conventional entry points and should never be flagged
_ENTRY_POINT_NAMES = frozenset({
    'main', 'setup', 'teardown', 'configure',
    'setUp', 'tearDown', 'setUpClass', 'tearDownClass',
    'setUpModule', 'tearDownModule',
})


@lru_cache(maxsize=8)
def _get_entry_point_config(language: str = "python") -> dict:
    """Return the entry-point heuristic config for *language* from config.toml.

    Returns a dict with keys:
        ``decorators``         — frozenset of full decorator names (dotted).
        ``decorator_basenames``— frozenset of decorator base-names (last component).
        ``names``              — frozenset of conventional entry-point function names.
        ``name_prefixes``      — list of name prefixes that mark entry points.
        ``has_dunders``        — bool: whether dunder names are entry points.

    Falls back to the hardcoded Python frozensets for unknown languages.
    """
    from emend.language_registry import load_config
    config = load_config(language)
    dc = config.get("dead_code", {})
    if dc:
        return {
            "decorators": frozenset(dc.get("entry_point_decorators", [])),
            "decorator_basenames": frozenset(dc.get("entry_point_decorator_basenames", [])),
            "names": frozenset(dc.get("entry_point_names", [])),
            "name_prefixes": list(dc.get("entry_point_name_prefixes", [])),
            "has_dunders": bool(dc.get("has_dunders", False)),
        }
    # Fallback for unknown languages: use Python defaults
    return {
        "decorators": _ENTRY_POINT_DECORATORS,
        "decorator_basenames": _ENTRY_POINT_DECORATOR_BASENAMES,
        "names": _ENTRY_POINT_NAMES,
        "name_prefixes": ["test_", "Test", "describe_"],
        "has_dunders": True,
    }


def _is_dunder(name: str) -> bool:
    """Check if a name is a dunder (double underscore) name."""
    return name.startswith('__') and name.endswith('__') and len(name) > 4


def _is_likely_entry_point(
    name: str,
    kind: str,
    decorators: list[str],
    depth: int,
    language: str = "python",
) -> bool:
    """Check if a symbol is likely an entry point based on heuristics.

    Entry points are symbols that are invoked by frameworks or conventions
    rather than explicit code references.

    Args:
        name: Symbol name.
        kind: Symbol kind (function, class, method, …).
        decorators: List of decorator strings applied to the symbol.
        depth: Nesting depth (1 = top-level).
        language: Source language — loads heuristics from config.toml.
            Defaults to ``"python"`` for backward compatibility.
    """
    ep = _get_entry_point_config(language)

    # Dunder methods/functions are entry points only for languages that have them.
    if ep["has_dunders"] and _is_dunder(name):
        return True

    # Conventional entry-point names
    if name in ep["names"]:
        return True

    # Name-prefix heuristics (e.g. test_, Test, describe_)
    for prefix in ep["name_prefixes"]:
        if name.startswith(prefix):
            return True

    # Private names (single underscore prefix) at depth > 1 are methods,
    # which may be called via getattr or framework internals.
    # We only flag private top-level symbols.

    # Check decorators
    for dec in decorators:
        # Strip @ prefix if present (Python style: @app.route)
        # Also strip Rust attribute wrapper: #[test] → test
        dec_name = dec
        if dec_name.startswith('#[') and dec_name.endswith(']'):
            dec_name = dec_name[2:-1]
        elif dec_name.startswith('@'):
            dec_name = dec_name[1:]
        # Strip arguments: @app.command("name") -> app.command
        if '(' in dec_name:
            dec_name = dec_name[:dec_name.index('(')]
        dec_name = dec_name.strip()

        if dec_name in ep["decorators"]:
            return True

        # Check basename: @anything.route -> "route" is entry point
        basename = dec_name.rsplit('.', 1)[-1] if '.' in dec_name else dec_name
        if basename in ep["decorator_basenames"]:
            return True

    return False


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


def _string_literal_filter(
    candidates: list["DeadSymbol"],
    scan_root: str,
    all_files: bool,
    exclude_references_from: list[str] | None,
) -> list["DeadSymbol"]:
    """Filter out dead code candidates that have string-literal references.

    Scans project source files for occurrences of each candidate's name in
    string literals or other non-reference contexts.  This reduces false
    positives from dynamic dispatch, serialization, and similar patterns.
    """
    str_names = {d.name for d in candidates if len(d.name) > 3}
    if not str_names:
        return candidates

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
    for _fp in source_files:
        _r = str(Path(_fp).resolve())
        if _is_excluded_ref(_r):
            continue
        try:
            _content = Path(_fp).read_text(errors="replace")
        except Exception:
            continue
        if any(n in _content for n in str_names):
            file_cache[_r] = _content

    names_with_str_ref: set[tuple[str, str]] = set()
    for d in candidates:
        if len(d.name) <= 3:
            continue
        r = str(Path(d.file_path).resolve())

        content = file_cache.get(r)
        if content and d.name in content:
            for i, lt in enumerate(content.splitlines(), 1):
                if i == d.line or d.name not in lt:
                    continue
                names_with_str_ref.add((d.file_path, d.name))
                break

        if (d.file_path, d.name) in names_with_str_ref:
            continue

        for other_r, other_content in file_cache.items():
            if other_r == r:
                continue
            if d.name in other_content:
                names_with_str_ref.add((d.file_path, d.name))
                break

    return [d for d in candidates if (d.file_path, d.name) not in names_with_str_ref]


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
    if name.startswith('test_') or p.stem.endswith('_test'):
        return True
    stem = p.stem
    if stem.endswith('.test') or stem.endswith('.spec'):
        return True
    parts = p.parts
    if 'tests' in parts or 'test' in parts or '__tests__' in parts:
        return True
    return False


def _is_test_symbol(selector: str) -> bool:
    """Check if a selector refers to a test symbol."""
    if '::' in selector:
        sym_part = selector.split('::', 1)[1]
        # e.g. test_foo or TestFoo or TestFoo.test_method
        first_name = sym_part.split('.')[0]
        if first_name.startswith('test_') or first_name.startswith('Test'):
            return True
        # TypeScript/JavaScript test framework conventions
        if first_name in ('describe', 'it', 'test'):
            return True
    return False


def _find_impact_via_fact_graph(
    changed_selectors: list[str],
    proj_root: str,
    max_depth: int = 10,
) -> ImpactResult | None:
    """Compute impact using a Datalog query on the persisted ``facts.db``.

    Uses the CozoDB ``facts.db`` that is populated by ``emend index``.
    The query constructs a call graph from ``fact_reference`` (kind == "call")
    joined with ``fact_symbol`` (to find the enclosing function), then
    computes the transitive reverse-caller closure via recursive Datalog.

    Returns None if facts.db is unavailable (caller falls back to BFS).
    """
    fdb = _get_facts_db(proj_root)
    if fdb is None:
        return None

    # Resolve selectors to module-qualified names (mqn) in facts.db.
    changed_mqns: set[str] = set()
    sel_to_mqn: dict[str, str] = {}
    mqn_to_sel: dict[str, str] = {}

    for sel_str in changed_selectors:
        try:
            sel = parse_extended_selector(sel_str)
        except Exception:
            continue
        if not sel.symbol_path:
            continue
        name = sel.symbol_path[-1]
        # Try both the raw file_path and a relative version.
        for fp in (sel.file_path, _try_relative(sel.file_path, proj_root)):
            if fp is None:
                continue
            try:
                result = fdb.run(
                    "?[mqn] := *fact_symbol[fp, mqn, name, _, _, _, _, _, _, _, _, _, _, _, _, _], "
                    "fp == $fp, name == $name",
                    {"fp": fp, "name": name},
                )
                if result["rows"]:
                    mqn = result["rows"][0][0]
                    changed_mqns.add(mqn)
                    sel_to_mqn[sel_str] = mqn
                    mqn_to_sel[mqn] = sel_str
                    break
            except Exception:
                continue

    if not changed_mqns:
        return ImpactResult(
            changed_symbols=changed_selectors,
            impacted_symbols=[],
            impacted_tests=[],
            edges=[],
        )

    # Build a Datalog query against the persisted facts.db schema:
    #
    #   fact_symbol: (fp, mqn) => (name, qn, kind, line, end_line, ...)
    #   fact_reference: (tqn, fp, line, col) => (kind)
    #
    # A "call" is a fact_reference where kind == "call".  To find the
    # caller, we join with fact_symbol to find which function encloses
    # the reference line.
    seed_rows = ", ".join(f'["{mqn}"]' for mqn in changed_mqns)

    rules = [f"changed[x] <- [{seed_rows}]\n"]

    # call_edge: derive (caller_mqn, callee_mqn) from references + enclosing symbols
    rules.append(
        'call_edge[caller_mqn, callee_mqn] := '
        '*fact_reference[callee_mqn, fp, ref_line, _, kind], kind == "call", '
        '*fact_symbol[fp, caller_mqn, _, _, caller_kind, caller_line, caller_end, _, _, _, _, _, _, _, _, _], '
        'caller_kind in ["function", "async_function", "method", "async_method"], '
        'caller_line <= ref_line, ref_line <= caller_end\n'
    )

    # Also match references by qn (short qualified name)
    rules.append(
        'call_edge[caller_mqn, callee_mqn] := '
        '*fact_symbol[_, callee_mqn, _, callee_qn, _, _, _, _, _, _, _, _, _, _, _, _], '
        'callee_qn != "", '
        '*fact_reference[callee_qn, fp, ref_line, _, kind], kind == "call", '
        '*fact_symbol[fp, caller_mqn, _, _, caller_kind, caller_line, caller_end, _, _, _, _, _, _, _, _, _], '
        'caller_kind in ["function", "async_function", "method", "async_method"], '
        'caller_line <= ref_line, ref_line <= caller_end\n'
    )

    # Depth-bounded transitive reverse-caller closure
    rules.append(
        "layer_0[caller] := call_edge[caller, callee], changed[callee]\n"
    )
    for i in range(1, max_depth):
        rules.append(
            f"layer_{i}[caller] := call_edge[caller, mid], layer_{i - 1}[mid]\n"
        )
    for i in range(max_depth):
        rules.append(f"impacted[x] := layer_{i}[x]\n")

    # Edges: witness pairs
    rules.append(
        "edge[caller, callee] := impacted[caller], call_edge[caller, callee], changed[callee]\n"
    )
    if max_depth > 1:
        for i in range(1, max_depth):
            rules.append(
                f"edge[caller, mid] := layer_{i}[caller], call_edge[caller, mid], layer_{i - 1}[mid]\n"
            )

    # Return impacted symbols with file paths for selector construction
    rules.append(
        "?[caller_mqn, caller_fp, caller_name, callee_mqn] := "
        "edge[caller_mqn, callee_mqn], not changed[caller_mqn], "
        "*fact_symbol[caller_fp, caller_mqn, caller_name, _, _, _, _, _, _, _, _, _, _, _, _, _]"
    )

    try:
        result = fdb.run("".join(rules))
    except Exception:
        logger.debug("facts.db impact query failed", exc_info=True)
        return None

    # Build the result
    impacted: list[str] = []
    all_edges: list[ImpactEdge] = []
    seen_impacted: set[str] = set()

    abs_root = str(Path(proj_root).resolve())
    for row in result["rows"]:
        caller_mqn, caller_fp, caller_name, callee_mqn = row[0], row[1], row[2], row[3]

        # Convert relative path back to absolute for selectors.
        if not Path(caller_fp).is_absolute():
            caller_fp = str(Path(abs_root) / caller_fp)

        # Build selector for the caller
        if caller_mqn not in mqn_to_sel:
            mqn_to_sel[caller_mqn] = f"{caller_fp}::{caller_name}"
        caller_sel = mqn_to_sel[caller_mqn]
        callee_sel = mqn_to_sel.get(callee_mqn, callee_mqn)

        all_edges.append(ImpactEdge(
            source=callee_sel,
            target=caller_sel,
            kind="calls",
        ))

        if caller_sel not in seen_impacted and caller_sel not in changed_selectors:
            seen_impacted.add(caller_sel)
            impacted.append(caller_sel)

    # Identify impacted tests
    impacted_tests: list[str] = []
    all_impacted = changed_selectors + impacted

    # Build set of decorator-based test symbols from fact graph (e.g. Rust #[test])
    test_decorated_sels: set[str] = set()
    try:
        deco_result = fdb.run(
            '?[sqn] := *decorator_on[sqn, dec], '
            'dec in ["test", "tokio::test"]'
        )
        for row in deco_result["rows"]:
            mqn = row[0]
            if mqn in mqn_to_sel:
                test_decorated_sels.add(mqn_to_sel[mqn])
    except Exception:
        pass

    for sel_str in all_impacted:
        file_part = sel_str.split('::', 1)[0] if '::' in sel_str else sel_str
        if _is_test_file(file_part) or _is_test_symbol(sel_str) or sel_str in test_decorated_sels:
            if sel_str not in impacted_tests:
                impacted_tests.append(sel_str)
                for edge in all_edges:
                    if edge.target == sel_str:
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


def _try_relative(path: str, root: str) -> str | None:
    """Try to make *path* relative to *root*; return None on failure."""
    try:
        return str(Path(path).relative_to(Path(root).resolve()))
    except ValueError:
        return None


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
        max_depth: Maximum depth for transitive closure (default 10).

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

    # Datalog query on the persisted facts.db.
    dl_result = _find_impact_via_fact_graph(
        changed_selectors, proj_root, max_depth=max_depth,
    )
    if dl_result is not None:
        return dl_result

    # facts.db unavailable — warm the index and retry once.
    try:
        warm_caches(proj_root, type_engine="none")
    except Exception:
        pass
    dl_result = _find_impact_via_fact_graph(
        changed_selectors, proj_root, max_depth=max_depth,
    )
    if dl_result is not None:
        return dl_result

    return ImpactResult(
        changed_symbols=changed_selectors,
        impacted_symbols=[],
        impacted_tests=[],
        edges=[],
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
    unused_modules: bool = False,
) -> Iterator[DeadSymbol | DeadBlock | DeadModule]:
    """Find potentially dead (unreferenced) code in a project.

    Uses ``dead_code_unified()`` Datalog query over the FactGraph for
    combined reachable-block + unreferenced-symbol detection.  String
    literal filtering stays as a Python post-filter.

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
        unused_modules: If True, also report Python module files that have no
            incoming imports from non-excluded project files.

    Yields:
        DeadBlock items for unreachable code blocks, then DeadSymbol objects
        sorted by file path and line number, then optional DeadModule objects.
    """
    t0 = time.monotonic()
    scan_root = str(Path(project_path).resolve())

    # Build the FactGraph and run the unified Datalog dead code query.
    graph = _get_or_build_fact_graph(project_path)

    # Detect project languages and merge entry-point configs from all.
    detected_langs = detect_project_languages(scan_root)
    all_ep_decorators: list[str] = []
    all_ep_basenames: list[str] = []
    all_ep_names: list[str] = []
    all_ep_prefixes: list[str] = []
    for lang in (detected_langs or ["python"]):
        ep = _get_entry_point_config(lang)
        all_ep_decorators.extend(ep["decorators"])
        all_ep_basenames.extend(ep["decorator_basenames"])
        all_ep_names.extend(ep["names"])
        all_ep_prefixes.extend(ep["name_prefixes"])
    # Add user-supplied overrides
    if entry_point_decorators:
        all_ep_decorators.extend(entry_point_decorators)
        all_ep_basenames.extend(
            d.rsplit(".", 1)[-1] for d in entry_point_decorators
        )
    if entry_point_names:
        all_ep_names.extend(entry_point_names)

    project_root_resolved = str(Path(_find_project_root(project_path)).resolve())

    # Convert exclude_references_from to relative paths for the fact graph
    excl_ref_paths: list[str] | None = None
    excl_ref_segments: list[str] | None = None  # For ** glob patterns
    if exclude_references_from:
        excl_ref_paths = []
        excl_ref_segments = []
        for excl_path in exclude_references_from:
            if excl_path.startswith("**/"):
                # Extract directory segment for str_includes matching
                segment = excl_path[3:].rstrip("/")
                if segment:
                    excl_ref_segments.append(segment)
            elif "*" in excl_path or "?" in excl_path:
                continue  # Complex globs not supported in Datalog
            else:
                resolved = str(Path(excl_path).resolve())
                try:
                    rel = str(Path(resolved).relative_to(project_root_resolved))
                except ValueError:
                    rel = resolved
                excl_ref_paths.append(rel)

    raw_dead, raw_unreachable = graph.dead_code_unified(
        entry_point_decorators=all_ep_decorators + all_ep_basenames,
        entry_point_names=all_ep_names,
        entry_point_prefixes=all_ep_prefixes,
        exclude_reference_paths=excl_ref_paths if excl_ref_paths else None,
        exclude_reference_segments=excl_ref_segments if excl_ref_segments else None,
    )

    # Build file content cache for noqa checking
    _file_lines_cache: dict[str, list[str]] = {}

    def _has_noqa(fp: str, line: int) -> bool:
        if fp not in _file_lines_cache:
            try:
                _file_lines_cache[fp] = Path(fp).read_text(errors="replace").splitlines()
            except Exception:
                _file_lines_cache[fp] = []
        lines = _file_lines_cache[fp]
        if 0 < line <= len(lines):
            if _NOQA_RE.search(lines[line - 1]):
                return True
        return False

    # Convert SymbolFact results to DeadSymbol, applying Python post-filters.
    dead_symbols: list[DeadSymbol] = []
    for sym in raw_dead:
        abs_fp = (
            str(Path(project_root_resolved) / sym.file_path)
            if not Path(sym.file_path).is_absolute()
            else sym.file_path
        )

        # Kind filter
        if kind == "function" and sym.kind not in ("function", "async_function"):
            continue
        if kind == "class" and sym.kind != "class":
            continue

        # Private filter
        if not include_private and sym.name.startswith("_") and not sym.name.startswith("__"):
            continue

        # Exclude paths filter
        if exclude_paths:
            import fnmatch
            skip = False
            for ep in exclude_paths:
                if fnmatch.fnmatch(abs_fp, ep) or fnmatch.fnmatch(abs_fp, ep + "*"):
                    skip = True
                    break
                if "**" in ep:
                    pat_re = ep.replace("**", "*")
                    if fnmatch.fnmatch(abs_fp, pat_re) or fnmatch.fnmatch(abs_fp, pat_re + "*"):
                        skip = True
                        break
            if skip:
                continue

        # noqa suppression
        if _has_noqa(abs_fp, sym.line):
            continue

        # Skip symbols in test files — they are entry points by convention
        if _is_test_file(abs_fp):
            continue

        dead_symbols.append(DeadSymbol(
            file_path=abs_fp,
            name=sym.name,
            kind=sym.kind,
            line=sym.line,
            selector=f"{abs_fp}::{sym.qualified_name}",
            reason="no references found",
        ))

    # String-literal post-filter
    if strings_count_as_references:
        dead_symbols = _string_literal_filter(
            dead_symbols, scan_root, all_files, exclude_references_from,
        )

    logger.info(
        "dead_code: %d dead symbols in %.3fs",
        len(dead_symbols), time.monotonic() - t0,
    )

    def _path_is_excluded(file_path: str, patterns: list[str] | None) -> bool:
        if not patterns:
            return False
        import fnmatch

        for pattern in patterns:
            if fnmatch.fnmatch(file_path, pattern) or fnmatch.fnmatch(file_path, pattern + "*"):
                return True
            if "**" in pattern:
                relaxed = pattern.replace("**", "*")
                if fnmatch.fnmatch(file_path, relaxed) or fnmatch.fnmatch(file_path, relaxed + "*"):
                    return True
        return False

    def _reference_file_is_excluded(file_path: str) -> bool:
        if _is_test_file(file_path):
            return exclude_references_from is not None
        if not exclude_references_from:
            return False
        import fnmatch

        try:
            rel_path = str(Path(file_path).resolve().relative_to(project_root_resolved))
        except ValueError:
            rel_path = file_path

        for pattern in exclude_references_from:
            if fnmatch.fnmatch(file_path, pattern) or fnmatch.fnmatch(rel_path, pattern):
                return True
            if fnmatch.fnmatch(file_path, pattern + "*") or fnmatch.fnmatch(rel_path, pattern + "*"):
                return True
            if pattern.startswith("**/"):
                segment = pattern[3:].rstrip("/")
                if segment and segment in Path(rel_path).parts:
                    return True
        return False

    # Yield unreachable blocks first
    for ub in raw_unreachable:
        abs_fp = (
            str(Path(project_root_resolved) / ub.file_path)
            if not Path(ub.file_path).is_absolute()
            else ub.file_path
        )
        # Look up block line range from source_loc
        try:
            loc_result = graph._client.run(
                '?[line, end_line] := *source_loc[$fp, "block", $loc_id, line, _, end_line, _]',
                {"fp": ub.file_path, "loc_id": f"{ub.func_qn}:{ub.block_id}"},
            )
            if loc_result["rows"]:
                start_line = loc_result["rows"][0][0]
                end_line = loc_result["rows"][0][1]
            else:
                continue  # Skip blocks without line info
        except Exception:
            continue

        # Skip blocks with no real lines
        if start_line <= 0:
            continue

        yield DeadBlock(
            file_path=abs_fp,
            func_qn=ub.func_qn,
            block_id=ub.block_id,
            start_line=start_line,
            end_line=end_line,
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

    if not unused_modules:
        return

    source_files = _collect_source_files(
        project_root_resolved,
        language="python",
        git_tracked_only=not all_files,
    )
    from emend.fact_graph import _extract_imports

    imported_targets: set[str] = set()
    for abs_file in source_files:
        abs_path = Path(abs_file).resolve()
        if not abs_path.exists():
            continue
        if _reference_file_is_excluded(str(abs_path)):
            continue
        try:
            content = abs_path.read_text(encoding="utf-8")
        except Exception:
            continue
        for imp in _extract_imports(str(abs_path), content):
            imported_targets.add(imp.imported_module)
            if imp.imported_name:
                imported_targets.add(f"{imp.imported_module}.{imp.imported_name}")

    candidate_modules: list[DeadModule] = []
    scan_root_path = Path(scan_root).resolve()

    for abs_file in source_files:
        abs_path = Path(abs_file).resolve()
        if not abs_path.exists():
            continue
        if not abs_path.is_relative_to(scan_root_path):
            continue
        if abs_path.name in {"__init__.py", "__main__.py"}:
            continue
        if _is_test_file(str(abs_path)):
            continue
        if _path_is_excluded(str(abs_path), exclude_paths):
            continue
        if not include_private and abs_path.stem.startswith("_"):
            continue
        module_name = _file_to_module(str(abs_path), project_root_resolved)
        if module_name in imported_targets:
            continue
        candidate_modules.append(
            DeadModule(
                file_path=str(abs_path),
                name=abs_path.stem,
                module_name=module_name,
                reason="module is never imported",
            )
        )

    candidate_modules.sort(key=lambda m: m.file_path)
    yield from candidate_modules


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
    uses CozoDB Datalog queries on the persisted ``facts.db`` to
    iteratively identify symbols that become dead after the deletion
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
    target_module = _normalize_module_qn(_file_to_module(selector.file_path, module_root))
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
        # Compute cascade via CozoDB queries on the persisted facts.db.
        # Iteratively finds callees of deleted symbols, then checks
        # whether each callee has references outside the delete set.
        fdb = _get_facts_db(scan_root)
        if fdb is not None:
            changed = True
            while changed:
                changed = False
                # Build inline relation for current delete set
                del_rows = ", ".join(f'["{qn}"]' for qn in delete_qns)
                try:
                    # Find callees of deleted symbols that have no
                    # external references (references not from deleted symbols).
                    result = fdb.run(
                        f'deleted[mqn] <- [{del_rows}]\n'
                        # Find callees: symbols called by deleted functions
                        'callee_of_deleted[callee_mqn] := '
                        '  deleted[caller_mqn], '
                        '  *fact_reference[callee_mqn, fp, ref_line, _, kind], kind == "call", '
                        '  *fact_symbol[fp, caller_mqn, _, _, caller_kind, caller_line, caller_end, _, _, _, _, _, _, _, _, _], '
                        '  caller_kind in ["function", "async_function", "method", "async_method"], '
                        '  caller_line <= ref_line, ref_line <= caller_end, '
                        '  not deleted[callee_mqn]\n'
                        # Also match by short qn
                        'callee_of_deleted[callee_mqn] := '
                        '  deleted[caller_mqn], '
                        '  *fact_symbol[_, callee_mqn, _, callee_qn, _, _, _, _, _, _, _, _, _, _, _, _], '
                        '  callee_qn != "", '
                        '  *fact_reference[callee_qn, fp, ref_line, _, kind], kind == "call", '
                        '  *fact_symbol[fp, caller_mqn, _, _, caller_kind, caller_line, caller_end, _, _, _, _, _, _, _, _, _], '
                        '  caller_kind in ["function", "async_function", "method", "async_method"], '
                        '  caller_line <= ref_line, ref_line <= caller_end, '
                        '  not deleted[callee_mqn]\n'
                        # Has external ref: reference from a non-deleted symbol
                        'has_ext_ref[mqn] := '
                        '  callee_of_deleted[mqn], '
                        '  *fact_reference[mqn, ref_fp, ref_line, _, _], '
                        '  *fact_symbol[sym_fp, mqn, _, _, _, sym_line, _, _, _, _, _, _, _, _, _, _], '
                        '  not (ref_fp == sym_fp, ref_line == sym_line), '
                        '  *fact_symbol[ref_fp, ref_mqn, _, _, ref_kind, ref_start, ref_end, _, _, _, _, _, _, _, _, _], '
                        '  ref_kind in ["function", "async_function", "method", "async_method"], '
                        '  ref_start <= ref_line, ref_line <= ref_end, '
                        '  not deleted[ref_mqn]\n'
                        # Cascade candidates: callees with no external refs
                        '?[mqn, name, kind, fp, line] := '
                        '  callee_of_deleted[mqn], not has_ext_ref[mqn], '
                        '  *fact_symbol[fp, mqn, name, _, kind, line, _, depth, _, _, _, _, _, is_entry, is_exported, _], '
                        '  depth == 1, is_entry == false, is_exported == false, '
                        '  not starts_with(name, "test_"), not starts_with(name, "Test"), '
                        '  not (starts_with(name, "__"), ends_with(name, "__"))\n'
                    )
                    for row in result["rows"]:
                        mqn, name, sym_kind, fp, line = row
                        if mqn not in delete_qns:
                            # Convert relative path back to absolute.
                            abs_fp = str(Path(scan_root) / fp) if not Path(fp).is_absolute() else fp
                            sym_selector = f"{abs_fp}::{name}"
                            delete_set.append({
                                "selector": sym_selector,
                                "file_path": abs_fp,
                                "name": name,
                                "kind": sym_kind,
                                "line": line,
                                "reason": "only referenced by deleted symbol(s)",
                            })
                            delete_qns.add(mqn)
                            changed = True
                except Exception:
                    logger.debug("CozoDB cascade query failed", exc_info=True)

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
    target_module = _normalize_module_qn(_file_to_module(selector.file_path, module_root))

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

        for qn, line, col, offset, end_offset, kind, _ann in references:
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

    # Compute source module name so that locally-defined symbols referenced by
    # the moved symbol get ``from source_module import Name`` statements added
    # to the destination file (issue #138 Bug 2).
    source_module = _file_to_module(selector.file_path, project_path)

    # Before removing the symbol, use the tree-sitter scope resolver to check
    # whether the source file has non-definition/non-import references to the
    # moved symbol (e.g. calls, type annotations).  After removal the name
    # becomes unresolved and the resolver can no longer see it.
    source_has_other_refs = _source_has_remaining_refs(
        selector.file_path, symbol_name, project_path,
    )

    # Step 1: Copy symbol to destination (include_imports=True so the moved
    # symbol carries its own import dependencies into the destination file,
    # fixing issue #138 Bug 2).
    copy_diff = copy_symbol(
        selector, dest_file, position=position, dedent=dedent,
        include_imports=True, source_module=source_module,
        project_path=project_path, apply=apply,
    )
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
            source_has_other_refs=source_has_other_refs,
        )
        diffs.update(import_diffs)

    return diffs


def _source_has_remaining_refs(
    source_file: str,
    symbol_name: str,
    project_path: str | None,
) -> bool:
    """Check whether *source_file* references *symbol_name* outside its definition.

    Uses the tree-sitter scope resolver on the **current** (pre-removal) content
    so that all references are still resolvable.  Returns True when there are
    read/write/call references to the symbol beyond its own definition and
    import sites — meaning the source file will need an import after the symbol
    is removed.
    """
    source_path = Path(source_file)
    try:
        content = source_path.read_text()
    except FileNotFoundError:
        return False

    proj_root = str(
        Path(project_path or _find_project_root(source_file)).resolve()
    )
    ext = source_path.suffix.lstrip(".")
    resolver = _rust.PyScopeResolver(proj_root, ext)
    resolved = str(source_path.resolve())
    resolver.index_file(resolved, content)

    target_suffix = f".{symbol_name}"
    return any(
        kind in ("read", "write", "call")
        for qn, _line, _col, _off, _end, kind, _ann
        in resolver.references_in_file(resolved)
        if qn.endswith(target_suffix) or qn == symbol_name
    )


def _split_or_retarget_import(
    content: str,
    py_file: str,
    source_module: str,
    dest_module: str,
    symbol_name: str,
    resolver: object = None,
) -> str | None:
    """Rewrite ``from source_module import ...`` statements for a symbol move.

    For each ``from source_module import A, B, C`` where ``symbol_name`` is one
    of the names:

    * If ``symbol_name`` is the *only* name, simply change the module:
      ``from dest_module import symbol_name``.
    * If there are *other* names in the statement, split it into two separate
      import lines so that sibling names are not inadvertently retargeted to
      ``dest_module`` (issue #138 Bug 1).

    Returns the rewritten file content string, or ``None`` if no change was
    needed.
    """
    structured_imports = resolver.structured_imports_in_file(py_file)

    original_content = content
    lines = content.splitlines(keepends=True)

    # Precompute cumulative line offsets for O(1) lookup per import.
    line_offsets = [0]
    for line in lines:
        line_offsets.append(line_offsets[-1] + len(line))

    # Collect (stmt_start_byte, stmt_end_byte, replacement_text) tuples;
    # applied in reverse order to preserve earlier byte offsets.
    replacements: list[tuple[int, int, str]] = []

    for imp in structured_imports:
        if imp["is_plain"]:
            continue
        if imp["module"] != source_module:
            continue
        if imp["level"]:
            continue

        names = imp["names"]
        moved_aliases = [(n, a) for n, a in names if n == symbol_name]
        if not moved_aliases:
            continue

        remaining_aliases = [(n, a) for n, a in names if n != symbol_name]

        def _alias_str(name: str, alias: str | None) -> str:
            if alias:
                return f"{name} as {alias}"
            return name

        # Preserve the indentation of the original import statement.
        start_line = imp["start_line"]
        orig_line = lines[start_line - 1] if start_line - 1 < len(lines) else ""
        indent = orig_line[: len(orig_line) - len(orig_line.lstrip())]

        moved_line = (
            f"{indent}from {dest_module} import "
            + ", ".join(_alias_str(n, a) for n, a in moved_aliases)
        )

        if remaining_aliases:
            remaining_line = (
                f"{indent}from {source_module} import "
                + ", ".join(_alias_str(n, a) for n, a in remaining_aliases)
            )
            replacement = moved_line + "\n" + remaining_line
        else:
            replacement = moved_line

        stmt_start = line_offsets[imp["start_line"] - 1]
        stmt_end = line_offsets[imp["end_line"]]
        replacements.append((stmt_start, stmt_end, replacement + "\n"))

    if not replacements:
        return None

    replacements.sort(key=lambda x: x[0], reverse=True)
    for start, end, repl in replacements:
        content = content[:start] + repl + content[end:]

    return content if content != original_content else None


def _update_imports_for_move(
    source_file: str,
    dest_file: str,
    symbol_name: str,
    project_path: str | None,
    apply: bool,
    source_has_other_refs: bool = False,
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

        changed = False

        # Use tree-sitter to handle multi-name imports correctly.
        # When the consumer has 'from source_mod import A, B' and only A is
        # moved, we must split the statement instead of rewriting just the
        # module name (which would drag B to dest_mod — issue #138 Bug 1).
        new_content = _split_or_retarget_import(
            content, py_file, source_module, dest_module, symbol_name,
            resolver=resolver,
        )
        if new_content is not None and new_content != content:
            changed = True
        else:
            # Fallback: handle dotted 'import source_module.symbol_name' style
            # that the AST splitter does not cover.
            transform = _rust.PyFileTransform(content)

            references = resolver.references_in_file(py_file)

            for i, (qn, line, col, offset, end_offset, kind, _ann) in enumerate(references):
                if kind != "import":
                    continue

                if qn == target_qn:
                    # Only handle 'import source_module.symbol_name' (dotted)
                    # style; 'from source_module import ...' is handled above.
                    if content[offset : offset + len(source_module)] == source_module:
                        transform.replace_range(
                            offset, offset + len(source_module), dest_module
                        )
                        changed = True

            if changed:
                new_content = transform.apply()
            else:
                new_content = None

        if not changed or new_content is None or new_content == content:
            continue

        diff = _generate_diff(py_file, content, new_content)
        diffs[py_file] = diff

        if apply:
            Path(py_file).write_text(new_content)

    # If the source file has read/write/call references to the moved symbol
    # (detected by the caller via tree-sitter scope resolver on pre-removal
    # content), add an import so the source file doesn't break at runtime.
    if source_has_other_refs and dest_module:
        try:
            source_content = Path(source_file).read_text()
        except FileNotFoundError:
            source_content = None

        if source_content is not None:
            import_stmt = f"from {dest_module} import {symbol_name}"
            from emend.language_plugins import load_plugin
            try:
                new_source_content = load_plugin(language).import_handler.add_import_text(
                    import_stmt, 0, source_content
                )
            except Exception:
                new_source_content = None

            if new_source_content and new_source_content != source_content:
                diff = _generate_diff(source_file, source_content, new_source_content)
                diffs[source_file] = diff
                if apply:
                    Path(source_file).write_text(new_source_content)

    return diffs


def _resolve_relative_import_qn(
    qn: str,
    file_path: str,
    project_root: str,
    sep: str = ".",
    src_text: str | None = None,
) -> str | None:
    """Resolve a relative-import QN like ``.models`` to an absolute QN like ``pkg.models``.

    The Rust resolver emits QNs such as ``.models`` for ``from .models import X``
    and ``..util`` for ``from ..util import Y``.  We resolve these by computing the
    containing package from the file path.

    For ``from . import X`` style imports (bare name after dot-only relative), the
    Rust resolver adds an extra separator dot to the QN (e.g. ``..models`` instead
    of ``.models``).  When *src_text* is provided and does not start with a dot,
    we compensate by reducing the dot count.

    Returns the absolute module QN, or ``None`` if resolution fails.
    """
    if not qn.startswith("."):
        return None

    dot_count = len(qn) - len(qn.lstrip("."))
    relative_part = qn[dot_count:]

    # For ``from . import X`` the source text is just the bare name (no dots),
    # but the Rust resolver produces QN ``..X`` with an extra separator dot.
    # Compensate so that the dot count reflects the actual import level.
    if src_text is not None and not src_text.startswith("."):
        dot_count = max(1, dot_count - 1)

    module = _file_to_module(file_path, project_root)
    package = module.rsplit(".", 1)[0] if "." in module else None
    parts = package.split(sep) if package else []

    levels_up = dot_count - 1
    if levels_up > len(parts):
        return None

    base_parts = parts[: len(parts) - levels_up] if levels_up else parts
    if relative_part:
        return sep.join(base_parts + [relative_part]) if base_parts else relative_part
    else:
        return sep.join(base_parts) if base_parts else None


def _replace_module_in_strings(
    content: str,
    old_module: str,
    new_module: str,
    full_name_only: bool = False,
    file_path: str = "_.py",
    language: str = "python",
) -> str:
    """Replace occurrences of old_module inside string literals in *content*.

    Uses tree-sitter to identify string literal nodes (via the
    ``{type: string, value: null}`` any-string pattern), so comments and
    non-string contexts are correctly ignored regardless of language.

    Handles:
    - Full dotted module path inside strings (for ``importlib.import_module("pkg.models")``).
    - Bare module name when it is the entire string content (for ``__all__``
      entries like ``"models"``) — only when *full_name_only* is False.

    When *full_name_only* is True, only the full dotted module path is replaced.
    This avoids false positives when scanning files that have no import
    relationship with the module (e.g. an unrelated ``TABLE = "models"``).
    """
    from emend.language_registry import get_extensions

    exts = get_extensions(language)
    ext = exts[0] if exts else Path(file_path).suffix.lstrip(".")
    any_string_ir: dict = {"type": "string", "value": None}
    matches = _rust.find_pattern_in_files(
        [(file_path, content)], any_string_ir, extension=ext,
    )

    old_bare = old_module.rsplit(".", 1)[-1]
    new_bare = new_module.rsplit(".", 1)[-1]

    lines = content.splitlines(keepends=True)

    # Collect (char_start, char_end, replacement) tuples.
    replacements: list[tuple[int, int, str]] = []

    for _file, start_line, start_col, end_line, end_col, text, _caps in matches:
        char_start = sum(len(lines[i]) for i in range(start_line - 1)) + start_col
        char_end = sum(len(lines[i]) for i in range(end_line - 1)) + end_col

        # Determine the string's inner content (without surrounding quotes).
        if text[:3] in ('"""', "'''"):
            inner = text[3:-3]
        else:
            inner = text[1:-1]

        new_text = text
        # Replace full dotted module path (most specific).
        if old_module in text:
            new_text = re.sub(
                r'(?<![.\w])' + re.escape(old_module) + r'(?![.\w])',
                new_module,
                text,
            )
        # Replace bare module name only when it is the entire string content.
        elif not full_name_only and inner == old_bare:
            if text[:3] in ('"""', "'''"):
                new_text = text[:3] + new_bare + text[-3:]
            else:
                new_text = text[0] + new_bare + text[-1]

        if new_text != text:
            replacements.append((char_start, char_end, new_text))

    if not replacements:
        return content

    # Apply in reverse order to preserve earlier offsets.
    replacements.sort(key=lambda x: x[0], reverse=True)
    for start, end, repl in replacements:
        content = content[:start] + repl + content[end:]

    return content


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

        old_bare_mod = old_module.rsplit(sep, 1)[-1]
        new_bare_mod = new_module.rsplit(sep, 1)[-1]

        for qn, line, col, offset, end_offset, kind, _ann in resolver.references_in_file(py_file):
            # Resolve relative QNs (e.g. ".models" -> "pkg.models") so that
            # the comparison against old_module works correctly.
            resolved_qn = qn
            src_text = content[offset:end_offset]
            if qn.startswith("."):
                resolved = _resolve_relative_import_qn(qn, py_file, project_root, sep, src_text=src_text)
                if resolved is not None:
                    resolved_qn = resolved

            if kind == "import":
                # Exact match: import old_module or from old_module import ...
                if resolved_qn == old_module:
                    if qn.startswith(".") and resolved_qn != qn:
                        # Relative import: preserve leading dots, replace only the module name.
                        if src_text.startswith("."):
                            # Text includes dots (e.g. ``from .models import VALUE``).
                            dot_count = len(qn) - len(qn.lstrip("."))
                            new_relative = "." * dot_count + new_bare_mod
                        else:
                            # Bare name (e.g. ``from . import models``); dots are in
                            # the ``from .`` part, not in the captured text span.
                            new_relative = new_bare_mod
                        transform.replace_range(offset, end_offset, new_relative)
                    else:
                        transform.replace_range(offset, end_offset, new_module)
                    changed = True
                # Prefix match: import old_module.sub or from old_module.sub import ...
                elif resolved_qn.startswith(old_module + sep):
                    prefix_len = len(old_module)
                    if content[offset : offset + prefix_len] == old_module:
                        transform.replace_range(offset, offset + prefix_len, new_module)
                        changed = True
                    elif qn.startswith(".") and resolved_qn != qn:
                        # Relative sub-module import: replace old bare name at offset.
                        dot_count = len(qn) - len(qn.lstrip("."))
                        relative_module_part = qn[dot_count:]
                        if relative_module_part == old_bare_mod or relative_module_part.startswith(old_bare_mod + sep):
                            if content[offset : offset + len(old_bare_mod)] == old_bare_mod:
                                transform.replace_range(offset, offset + len(old_bare_mod), new_bare_mod)
                                changed = True

            elif kind in ("read", "write"):
                # Attribute access through a module binding, e.g. ``models.VALUE``
                # after ``from . import models``.  The bare module name in the
                # source text must be updated to match the new module name.
                if resolved_qn == old_module and src_text == old_bare_mod:
                    transform.replace_range(offset, end_offset, new_bare_mod)
                    changed = True

        # Check if string literals might contain the old module name.
        old_bare_name = old_module.rsplit(sep, 1)[-1]
        strings_may_need_update = old_module in content or old_bare_name in content

        if changed:
            final_content = transform.apply() or content
            if strings_may_need_update:
                final_content = _replace_module_in_strings(
                    final_content, old_module, new_module,
                    file_path=py_file, language=language,
                )
        elif strings_may_need_update:
            final_content = _replace_module_in_strings(
                content, old_module, new_module,
                file_path=py_file, language=language,
            )
        else:
            continue

        if final_content == content:
            continue

        diff = _generate_diff(py_file, content, final_content)
        diffs[py_file] = diff

        if apply:
            Path(py_file).write_text(final_content)

    # Third pass: string-literal replacements in files that the structural pre-filter
    # may have excluded (e.g. files with importlib.import_module("pkg.models") but no
    # import statement mentioning "models" as an identifier that tree-sitter picks up).
    #
    # Only match the full dotted module name here — NOT the bare name.  Files
    # processed in the first/second pass already get bare-name string updates
    # (for __all__ entries etc.), but files with no import relationship should
    # not have coincidental bare-name strings like TABLE = "models" rewritten.
    already_processed = set(diffs.keys())
    all_source_files = _collect_source_files(project_root, language=language)
    for py_file in all_source_files:
        if py_file in already_processed:
            continue
        try:
            content = Path(py_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Quick substring check before the heavier tree-sitter scan.
        if old_module not in content:
            continue
        final_content = _replace_module_in_strings(
            content, old_module, new_module, full_name_only=True,
            file_path=py_file, language=language,
        )
        if final_content == content:
            continue
        diff = _generate_diff(py_file, content, final_content)
        diffs[py_file] = diff
        if apply:
            Path(py_file).write_text(final_content)

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


def _dispatch_with_returns_filter(
    selector_str: str,
    selector: ExtendedSelector,
    returns_filter: list[str] | None,
    type_oracle: TypeOracle | None,
    single_fn: Callable[[ExtendedSelector], str],
) -> str:
    """Common dispatch logic for cmd_edit and cmd_add.

    Handles:
    - Returns-filter expansion: expand wildcard selector to matching symbols
    - File-glob dispatch: iterate over multiple matching files
    - Single-selector fall-through

    *single_fn* is called with each concrete selector and should return a diff
    string (empty string = no change).
    """
    if returns_filter:
        files = (
            selector.expand_file_glob()
            if selector.has_file_glob()
            else [selector.file_path]
        )
        all_results = []
        for fpath in files:
            concrete_base = selector.with_file_path(fpath) if fpath != selector.file_path else selector
            for concrete in _expand_selector_with_returns_filter(
                concrete_base, returns_filter, type_oracle
            ):
                try:
                    result = single_fn(concrete)
                    if result:
                        all_results.append(result)
                except (ValueError, FileNotFoundError):
                    continue
        if not all_results:
            raise ValueError(f"No symbols found matching {selector_str} with --returns {returns_filter}")
        return '\n'.join(all_results)

    if selector.has_file_glob():
        expanded_files = selector.expand_file_glob()
        all_results = []
        for fpath in expanded_files:
            concrete = selector.with_file_path(fpath)
            try:
                result = single_fn(concrete)
                if result:
                    all_results.append(result)
            except (ValueError, FileNotFoundError):
                continue
        if not all_results:
            raise ValueError(f"No symbols found matching {selector_str}")
        return '\n'.join(all_results)

    return single_fn(selector)


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

    def _single(sel: ExtendedSelector) -> str:
        return _cmd_edit_single(sel, value=value, rm=rm, apply=apply)

    return _dispatch_with_returns_filter(
        selector_str, selector, returns_filter, type_oracle, _single
    )


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

    def _single(sel: ExtendedSelector) -> str:
        return _cmd_add_single(sel, value=value, before=before, after=after, at=at, apply=apply)

    return _dispatch_with_returns_filter(
        selector_str, selector, returns_filter, type_oracle, _single
    )
