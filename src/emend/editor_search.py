"""Fast search interface for editor plugins.

Provides an ``EditorSearchEngine`` backed by the emend SQLite index
(``parse.db``) with FTS5 trigram indexing for sub-100ms interactive
search.  A lightweight newline-delimited JSON-RPC protocol runs
over stdio so the editor process stays warm across keystrokes.

Architecture overview
---------------------
1. **FTS5 trigram index** on ``symbol_fts`` enables substring matching
   on symbol names, qualified names, and signatures in <5ms.
2. **Multi-strategy search** combines exact, prefix, substring, and
   fuzzy matching with relevance scoring to rank results.
3. **Partial pattern normalization** auto-closes incomplete patterns
   (``foo(bar, $`` → ``foo(bar, $_)``) so partial keystrokes yield
   useful results.
4. **Long-running server** (``emend editor-server``) keeps the DB
   connection and FTS index warm, amortizing Python startup cost.

Why *not* a Rust SQLite extension
---------------------------------
We evaluated a custom Rust extension for fuzzy matching / custom
tokenizers and concluded it's unnecessary at this stage:

- FTS5 trigram (built into SQLite ≥ 3.34) already handles substring
  matching well enough for interactive typeahead.
- The result set from SQL is small (<200 rows) so Python-side scoring
  is <1ms.
- The real latency bottleneck is Python startup (~200ms), solved by
  the long-running server — not SQL query time (~2ms).
- A Rust extension would add deployment complexity (.so distribution,
  SQLite version compatibility) for marginal gain.

If profiling later reveals that trigram matching is insufficient (e.g.
camelCase-aware tokenization or Levenshtein distance scoring), a Rust
extension can be added to the existing ``emend-core`` crate without
changing the public API.
"""

from __future__ import annotations

import ast
import json
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _ordered_sources(*sources: str) -> list[str]:
    ordered: list[str] = []
    for source in sources:
        if source and source not in ordered:
            ordered.append(source)
    return ordered

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    """Wrapper returned by all search methods."""

    items: list[dict]
    elapsed_ms: float
    mode: str  # "symbol", "pattern", "selector", "reference", "file_symbols"
    truncated: bool = False
    query: str = ""
    indexing: bool = False
    provenance: str = ""  # "indexed", "pattern", "grep", "selector", "files"
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Lightweight serialization (avoids ``dataclasses.asdict`` deep-copy)."""
        d: dict = {
            "items": self.items,
            "elapsed_ms": self.elapsed_ms,
            "mode": self.mode,
            "truncated": self.truncated,
            "query": self.query,
        }
        if self.indexing:
            d["indexing"] = True
        if self.provenance:
            d["provenance"] = self.provenance
        if self.sources:
            d["sources"] = self.sources
        return d


def _timed(method):
    """Decorator that stamps ``elapsed_ms`` and ``query`` on the SearchResult.

    Expects the first positional argument after ``self`` to be the query string.
    """
    import functools
    import inspect

    # Identify the name of the first parameter after 'self' so we can
    # extract the query value regardless of positional/keyword usage.
    params = list(inspect.signature(method).parameters)
    query_param = params[1] if len(params) > 1 else None

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        t0 = time.monotonic()
        result = method(self, *args, **kwargs)
        result.elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
        # Extract the query value from positional or keyword args.
        if args:
            result.query = args[0]
        elif query_param and query_param in kwargs:
            result.query = kwargs[query_param]
        return result

    return wrapper


# Column names returned by ``SELECT rowid, name, ... FROM symbol_index``
_SYM_FIELDS_WITH_ROWID = (
    "name", "qualified_name", "kind", "file_path",
    "line", "end_line", "signature", "returns", "depth", "parent",
)

# Column names when rowid is not in the SELECT (e.g. file_symbols)
_SYM_FIELDS_NO_ROWID = _SYM_FIELDS_WITH_ROWID


def _row_to_symbol_dict(row: tuple, *, has_rowid: bool = True) -> dict:
    """Map a ``symbol_index`` row tuple to a dict.

    When *has_rowid* is True, ``row[0]`` is the rowid and fields start at
    index 1.  Otherwise fields start at index 0.
    """
    offset = 1 if has_rowid else 0
    fields = _SYM_FIELDS_WITH_ROWID
    return {fields[i]: row[i + offset] for i in range(len(fields))}


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

_IDENT_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])|_")


def _split_identifier(name: str) -> list[str]:
    """Split ``name`` at snake_case and camelCase boundaries."""
    return [s for s in _IDENT_BOUNDARY.split(name) if s]


def is_fuzzy_subsequence(search: str, path: str, max_subs: int = 1) -> bool:
    """Check if search is a subsequence of path with at most max_subs substitutions.

    Single linear traversal of path. O(n + m) time, O(max_subs) space.
    """
    s = search.lower()
    p = path.lower()
    n, m = len(s), len(p)

    if n == 0:
        return True
    if n > m + max_subs:
        return False

    # si[k] = number of search chars matched so far using exactly k substitutions.
    # -1 means this state is not yet active.
    si = [0] + [-1] * max_subs

    for c in p:
        # Process states from most-subs to least-subs to avoid double-advancing.
        for k in range(max_subs, -1, -1):
            if si[k] < 0 or si[k] >= n:
                continue
            if s[si[k]] == c:
                # Exact match: advance this state.
                si[k] += 1
            elif k < max_subs:
                # Mismatch: fork a new state with one more substitution.
                new_si = si[k] + 1
                if si[k + 1] < new_si:
                    si[k + 1] = new_si
            # else: mismatch with no subs remaining — skip this path char.

        if any(x >= n for x in si):
            return True

    return any(x >= n for x in si)


def _score_file(file_path: str, query: str) -> float:
    """Score a file path against *query*.  Higher is more relevant."""
    q = query.lower()
    p = file_path.lower()
    basename = Path(file_path).name.lower()

    # Exact basename match
    if basename == q:
        return 1100.0

    # Basename prefix
    if basename.startswith(q):
        return 1080.0 - (len(basename) - len(q))

    # Path substring — ranked above symbol matches so file navigation
    # is prioritised when the user starts typing a filename.
    if q in p:
        return 1050.0 - min((len(p) - len(q)), 100)

    # Fuzzy subsequence (below most symbol matches)
    if is_fuzzy_subsequence(q, p):
        return 300.0 - min((len(p) - len(q)), 100)

    return 0.0


def _score_symbol(name: str, qualified_name: str, query: str) -> float:
    """Score a symbol against *query*.  Higher is more relevant."""
    q = query.lower()
    n = name.lower()

    # Exact match
    if n == q:
        return 1000.0

    # Prefix match — most common interactive pattern
    if n.startswith(q):
        return 900.0 - min((len(name) - len(query)) * 2, 100)

    # Segment-start match (matches at _ or camelCase boundary)
    segments = _split_identifier(name)
    for i, seg in enumerate(segments):
        if seg.lower().startswith(q):
            return 800.0 - i * 50 - min((len(name) - len(query)) * 2, 100)

    # Contiguous substring match
    idx = n.find(q)
    if idx >= 0:
        # Bonus if just after a word boundary
        if idx > 0 and name[idx - 1] == "_":
            return 700.0 - min(idx * 5, 100)
        return 600.0 - min(idx * 5, 100)

    # Qualified-name substring
    qn = qualified_name.lower()
    if q in qn:
        return 400.0

    # Fuzzy match — query chars appear in order
    j = 0
    for ch in q:
        pos = n.find(ch, j)
        if pos < 0:
            return 0.0
        j = pos + 1
    return 200.0 - min(j * 2, 100)


# ---------------------------------------------------------------------------
# Partial pattern normalization
# ---------------------------------------------------------------------------

# Common Python keywords we don't treat as search literals
_KEYWORDS = frozenset({
    "True", "False", "None", "and", "or", "not", "in", "is",
    "if", "else", "elif", "for", "while", "return", "import",
    "from", "as", "def", "class", "with", "try", "except",
    "finally", "raise", "pass", "break", "continue", "yield",
    "assert", "del", "global", "nonlocal", "lambda", "async", "await",
})

_IDENT_RE = re.compile(r"[a-zA-Z_]\w*")


def normalize_partial_pattern(raw: str) -> tuple[str | None, list[str]]:
    """Try to make an incomplete pattern parseable.

    Returns ``(normalized_pattern_or_None, literal_hints)``.

    *literal_hints* are always returned even if normalization fails,
    so callers can fall back to index-based literal search.
    """
    # Extract meaningful identifiers for index fallback
    literals: list[str] = []
    for m in _IDENT_RE.finditer(raw):
        tok = m.group()
        if tok not in _KEYWORDS and not tok.startswith("$"):
            literals.append(tok)

    # ----- normalize -----
    normalized = raw.rstrip()

    # Trailing bare ``$`` → ``$_``
    normalized = re.sub(r"\$\s*$", "$_", normalized)
    # Trailing ``$...`` without name → ``$...TAIL`` (metavar names are uppercase)
    normalized = re.sub(r"\$\.\.\.\s*$", "$...TAIL", normalized)

    # Close unclosed grouping characters
    for open_ch, close_ch in [("(", ")"), ("[", "]"), ("{", "}")]:
        diff = normalized.count(open_ch) - normalized.count(close_ch)
        if diff > 0:
            normalized += close_ch * diff

    # Quick sanity: try to parse
    try:
        from emend.pattern import parse_pattern
        parse_pattern(normalized)
        return normalized, literals
    except Exception:
        pass

    return None, literals


# ---------------------------------------------------------------------------
# FTS5 management
# ---------------------------------------------------------------------------


def _fts5_available(conn: sqlite3.Connection) -> bool:
    """Return True if FTS5 with trigram tokenizer is supported."""
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe "
            "USING fts5(x, tokenize='trigram')"
        )
        conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        return True
    except Exception:
        return False


def rebuild_fts(conn: sqlite3.Connection) -> int:
    """(Re)build the FTS5 indexes from ``symbol_index``.

    Returns the total number of rows indexed (0 if FTS5 is unavailable).
    """
    if not _fts5_available(conn):
        logger.debug("FTS5 trigram not available — skipping FTS build")
        return 0

    # Symbol index
    conn.execute("DROP TABLE IF EXISTS symbol_fts")
    conn.execute(
        "CREATE VIRTUAL TABLE symbol_fts USING fts5("
        "  name, qualified_name, signature,"
        "  tokenize='trigram'"
        ")"
    )
    conn.execute(
        "INSERT INTO symbol_fts(rowid, name, qualified_name, signature) "
        "SELECT rowid, name, qualified_name, COALESCE(signature, '') "
        "FROM symbol_index"
    )

    # File index
    conn.execute("DROP TABLE IF EXISTS file_fts")
    conn.execute(
        "CREATE VIRTUAL TABLE file_fts USING fts5("
        "  file_path,"
        "  tokenize='trigram'"
        ")"
    )
    conn.execute(
        "INSERT INTO file_fts(file_path) "
        "SELECT DISTINCT file_path FROM symbol_index"
    )

    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM symbol_fts").fetchone()[0]
    file_count = conn.execute("SELECT COUNT(*) FROM file_fts").fetchone()[0]
    logger.debug("FTS index rebuilt: %d symbols, %d files", count, file_count)
    return count + file_count


# ---------------------------------------------------------------------------
# EditorSearchEngine
# ---------------------------------------------------------------------------


class EditorSearchEngine:
    """Fast, index-backed search engine for editor plugins.

    Keeps a SQLite connection open for the lifetime of the engine
    so queries avoid connection setup overhead.  The FTS5 trigram
    index is lazily built on first search if it doesn't exist.

    SQLite tuning applied at connect time:
    - ``PRAGMA mmap_size = 268435456`` (256 MiB memory-mapped I/O)
    - ``PRAGMA cache_size = -65536`` (64 MiB page cache)
    - ``PRAGMA journal_mode = WAL`` (concurrent reads)
    """

    def __init__(self, project_path: str) -> None:
        from emend.transform import _find_project_root, _cache_db_dir

        self.project_root = _find_project_root(project_path)
        self.db_path = _cache_db_dir(self.project_root) / "parse.db"
        self._conn: sqlite3.Connection | None = None
        self._fts_ready = False
        self._fts_available: bool | None = None

        # Background reindex state
        self._indexing = False
        self._index_complete_pending = False
        self._index_thread: threading.Thread | None = None
        self._index_lock = threading.Lock()

        # Query history (session-scoped, most recent first)
        self._query_history: list[dict] = []
        self._query_history_max = 100
        self._project_file_cache: tuple[int, list[str]] | None = None

        # Hot buffer snapshots (unsaved editor content)
        self._hot_buffers: dict[str, str] = {}  # resolved file_path -> content
        self._hot_buffer_versions: dict[str, int] = {}  # resolved file_path -> version

        # Cache expensive CFG construction for synchronous completion ranking.
        self._completion_cfg_cache: OrderedDict[str, list[Any]] = OrderedDict()
        self._completion_cfg_cache_max = 8

    # -- connection ---------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), timeout=10)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA mmap_size=268435456")
            self._conn.execute("PRAGMA cache_size=-65536")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._fts_ready = False
        
        # Close KB if it was lazy-initialized
        if hasattr(self, "_kb"):
            try:
                self._kb.close()  # type: ignore[attr-defined]
            except Exception:
                pass
            delattr(self, "_kb")

    def _set_result_sources(self, result: SearchResult, *sources: str) -> SearchResult:
        ordered = _ordered_sources(*sources)
        result.sources = ordered
        if ordered:
            result.provenance = " + ".join(ordered)
        return result

    def _collect_project_files(self) -> list[str]:
        root = Path(self.project_root).resolve()
        try:
            root_mtime = root.stat().st_mtime_ns
        except OSError:
            root_mtime = -1

        if (
            self._project_file_cache is not None
            and self._project_file_cache[0] == root_mtime
        ):
            return self._project_file_cache[1]

        files: list[str] = []
        try:
            proc = subprocess.run(
                ["git", "ls-files", "-z"],
                capture_output=True,
                check=False,
                cwd=str(root),
                timeout=5,
            )
            if proc.returncode == 0:
                files = [
                    str(root / rel_path)
                    for rel_path in proc.stdout.decode(
                        "utf-8", errors="replace"
                    ).split("\0")
                    if rel_path
                ]
        except Exception:
            logger.debug("git ls-files fallback failed", exc_info=True)

        if not files:
            ignored_dirs = {
                ".emend",
                ".git",
                ".hg",
                ".mypy_cache",
                ".pytest_cache",
                ".ruff_cache",
                ".svn",
                ".venv",
                "__pycache__",
                "node_modules",
                "target",
                "venv",
            }
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    dirname
                    for dirname in dirnames
                    if dirname not in ignored_dirs
                ]
                for filename in filenames:
                    files.append(str(Path(dirpath) / filename))

        self._project_file_cache = (root_mtime, files)
        return files

    # -- FTS ----------------------------------------------------------------

    def _ensure_fts(self) -> bool:
        """Ensure the FTS index exists and is populated.  Returns availability."""
        if self._fts_ready:
            return self._fts_available is True

        conn = self._get_conn()

        if self._fts_available is None:
            self._fts_available = _fts5_available(conn)
        if not self._fts_available:
            self._fts_ready = True
            return False

        # Check if FTS tables exist and have rows.
        try:
            fts_count = conn.execute(
                "SELECT COUNT(*) FROM symbol_fts"
            ).fetchone()[0]
            file_fts_count = conn.execute(
                "SELECT COUNT(*) FROM file_fts"
            ).fetchone()[0]
        except Exception:
            fts_count = 0
            file_fts_count = 0

        if fts_count == 0 or file_fts_count == 0:
            try:
                sym_count = conn.execute(
                    "SELECT COUNT(*) FROM symbol_index"
                ).fetchone()[0]
            except sqlite3.Error:
                sym_count = 0
            if sym_count > 0:
                rebuild_fts(conn)

        self._fts_ready = True
        return True

    # -- background reindex -------------------------------------------------

    @property
    def is_indexing(self) -> bool:
        """True while a background reindex is running."""
        return self._indexing

    def start_background_reindex(self) -> bool:
        """Start a background reindex thread.

        Returns True if a new reindex was started, False if one is already
        running or the index is already fresh.
        """
        with self._index_lock:
            if self._indexing:
                return False
            self._indexing = True
            self._index_complete_pending = False

        thread = threading.Thread(
            target=self._background_reindex_worker,
            daemon=True,
            name="emend-reindex",
        )
        self._index_thread = thread
        thread.start()
        return True

    def _background_reindex_worker(self) -> None:
        """Worker that runs in a background thread.

        Uses ``_ensure_index_fresh`` which opens its own SQLite connection,
        so there is no contention with the main-thread connection.
        """
        try:
            from emend.transform import _ensure_index_fresh

            _ensure_index_fresh(self.project_root)
        except Exception:
            logger.debug("Background reindex failed", exc_info=True)
        finally:
            with self._index_lock:
                self._indexing = False
                self._index_complete_pending = True

    def check_index_complete(self) -> bool:
        """Check if a background reindex just completed.

        Returns True (exactly once per reindex) when the background thread
        finished.  The caller should rebuild FTS and notify the client.
        """
        with self._index_lock:
            if self._index_complete_pending:
                self._index_complete_pending = False
                return True
        return False

    def finalize_reindex(self) -> None:
        """Rebuild FTS after a background reindex completed.

        Must be called from the main thread (uses the engine's connection).
        """
        conn = self._get_conn()
        rebuild_fts(conn)
        self._fts_ready = True

    # -- unified search -----------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        file_scope: str | None = None,
        kind: str | None = None,
        include_map: bool = False,
    ) -> SearchResult:
        """Auto-detect mode and dispatch."""
        t0 = time.monotonic()

        if include_map:
            kb = _get_kb(self)
            resolved = kb.resolve_selector(query)
            if resolved and resolved != query:
                query = resolved

        if query.startswith("/") and query.endswith("/") and len(query) > 2:
            result = self._search_grep(
                query[1:-1], limit=limit, file_scope=file_scope
            )
            self._set_result_sources(result, "grep")
        elif "::" in query:
            result = self._search_selector(query, limit=limit)
            self._set_result_sources(result, "selector")
        elif "$" in query:
            result = self._search_pattern(
                query, limit=limit, file_scope=file_scope
            )
            self._set_result_sources(result, "pattern")
        elif re.match(r'\s*(?:async\s+)?(?:def|class)\s+\w*[*?]', query):
            result = self._search_pattern(
                query, limit=limit, file_scope=file_scope
            )
            self._set_result_sources(result, "pattern")
        elif "/" in query or any(query.endswith(ext) for ext in (".py", ".ts", ".js", ".rs", ".go", ".c", ".cpp", ".h")):
            # Prioritize file search for path-like queries
            result = self._search_files(query, limit=limit)
            if not result.items:
                result = self._search_symbols(
                    query, limit=limit, file_scope=file_scope, kind=kind
                )
            elif not result.sources:
                self._set_result_sources(result, "files")
        else:
            result = self._search_symbols(
                query, limit=limit, file_scope=file_scope, kind=kind
            )
            if not result.sources:
                self._set_result_sources(result, "indexed")

        result.elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
        result.query = query
        result.indexing = self._indexing
        self._record_query(query, result)
        return result

    # -- query history ------------------------------------------------------

    def _record_query(self, query: str, result: SearchResult) -> None:
        """Record a query in the session history."""
        if not query or not query.strip():
            return
        entry = {
            "query": query,
            "mode": result.mode,
            "result_count": len(result.items),
            "provenance": result.provenance,
            "timestamp": time.time(),
        }
        # Deduplicate: remove existing entry for same query
        self._query_history = [
            e for e in self._query_history if e["query"] != query
        ]
        self._query_history.insert(0, entry)
        # Cap size
        if len(self._query_history) > self._query_history_max:
            self._query_history = self._query_history[:self._query_history_max]

    def query_history(self, *, limit: int = 50) -> SearchResult:
        """Return recent query history entries."""
        items = self._query_history[:limit]
        return SearchResult(
            items=items,
            elapsed_ms=0,
            mode="query_history",
        )

    # -- symbol search ------------------------------------------------------

    @_timed
    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 50,
        file_scope: str | None = None,
        kind: str | None = None,
    ) -> SearchResult:
        return self._search_symbols(
            query, limit=limit, file_scope=file_scope, kind=kind
        )

    def _search_symbols(
        self,
        query: str,
        *,
        limit: int = 50,
        file_scope: str | None = None,
        kind: str | None = None,
    ) -> SearchResult:
        conn = self._get_conn()
        query_lower = query.lower()

        # Collect candidate rows keyed by rowid → dict
        candidates: dict[int, dict] = {}
        # Cap per-strategy rows to avoid pulling the whole index for short queries.
        per_strategy_limit = max(limit * 4, 200)

        base = (
            "SELECT rowid, name, qualified_name, kind, file_path, "
            "line, end_line, signature, returns, depth, parent "
            "FROM symbol_index"
        )

        _FTS_JOIN = (
            "SELECT s.rowid, s.name, s.qualified_name, s.kind, s.file_path, "
            "s.line, s.end_line, s.signature, s.returns, s.depth, s.parent "
            "FROM symbol_fts f JOIN symbol_index s ON f.rowid = s.rowid "
            "WHERE f.{column} MATCH ?"
        )

        def _add(sql: str, params: tuple) -> None:
            try:
                for row in conn.execute(sql, params):
                    rid = row[0]
                    if rid not in candidates:
                        candidates[rid] = _row_to_symbol_dict(row)
            except Exception:
                pass

        # Strategy 1: exact name (uses idx_sym_name)
        _add(f"{base} WHERE name = ? LIMIT ?", (query, per_strategy_limit))

        # Strategy 2: case-insensitive exact
        if query != query_lower:
            _add(
                f"{base} WHERE lower(name) = ? AND name != ? LIMIT ?",
                (query_lower, query, per_strategy_limit),
            )

        # Strategy 3: prefix (uses idx_sym_name B-tree range scan)
        _add(
            f"{base} WHERE name >= ? AND name < ? AND name != ? LIMIT ?",
            (query, query[:-1] + chr(ord(query[-1]) + 1) if query else "~", query, per_strategy_limit),
        )

        # Strategy 4: case-insensitive prefix
        _add(
            f"{base} WHERE lower(name) LIKE ? AND lower(name) != ? LIMIT ?",
            (query_lower + "%", query_lower, per_strategy_limit),
        )

        # FTS5 strategies (needs ≥ 3 chars)
        fts_ok = len(query) >= 3 and self._ensure_fts()
        if fts_ok:
            fts_q = '"' + query.replace('"', '""') + '"'

            # Strategy 5: FTS5 trigram substring on name
            _add(
                _FTS_JOIN.format(column="name") + " LIMIT ?",
                (fts_q, per_strategy_limit),
            )

        # Strategy 6: qualified-name search for dotted queries
        if "." in query:
            _add(
                f"{base} WHERE qualified_name LIKE ? LIMIT ?",
                ("%" + query + "%", per_strategy_limit),
            )
            if fts_ok:
                _add(
                    _FTS_JOIN.format(column="qualified_name") + " LIMIT ?",
                    (fts_q, per_strategy_limit),
                )

        # Strategy 7: signature substring (for parameter-based search)
        if fts_ok:
            _add(
                _FTS_JOIN.format(column="signature") + " LIMIT ?",
                (fts_q, per_strategy_limit),
            )

        # Apply post-filters
        items = list(candidates.values())
        if file_scope:
            items = [c for c in items if file_scope in c["file_path"]]
        if kind:
            items = [c for c in items if c["kind"] == kind]

        # Venv fallback: if no candidates from the project index, try venv
        if not items and not file_scope:
            try:
                from emend.transform import lookup_venv_symbol
                venv_results = lookup_venv_symbol(
                    self.project_root,
                    name_pattern=query if "*" in query or "?" in query else None,
                    qualified_name=query if "." in query else None,
                    limit=limit,
                )
                # For bare names, also try name_pattern
                if not venv_results and "." not in query:
                    venv_results = lookup_venv_symbol(
                        self.project_root,
                        name_pattern=query,
                        limit=limit,
                    )
                for vr in venv_results:
                    qn = vr.get("qualified_name", vr["name"])
                    score = round(_score_symbol(vr["name"], qn, query), 1)
                    items.append({**vr, "score": score})
            except Exception:
                logger.debug("Venv symbol lookup failed", exc_info=True)

        has_symbol_results = bool(items)

        # Score and rank
        scored = [
            (round(_score_symbol(c["name"], c["qualified_name"], query), 1), c)
            for c in items
        ]
        
        # Include file matches
        file_results = self._search_files(query, limit=limit)
        for fr in file_results.items:
            scored.append((fr["score"], fr))

        scored.sort(key=lambda x: (-x[0], len(x[1].get("name", ""))))

        truncated = len(scored) > limit
        top = scored[:limit]

        return SearchResult(
            items=[{**c, "score": s} for s, c in top],
            elapsed_ms=0,
            mode="symbol",
            truncated=truncated,
            provenance=" + ".join(_ordered_sources(
                "indexed" if has_symbol_results else "",
                "files" if file_results.items else "",
            )),
            sources=_ordered_sources(
                "indexed" if has_symbol_results else "",
                "files" if file_results.items else "",
            ),
        )

    def _search_files(self, query: str, limit: int = 50) -> SearchResult:
        """Search for files matching the query."""
        conn = self._get_conn()
        q_lower = query.lower()

        candidates: set[str] = set()
        candidate_cap = max(limit * 4, 200)

        # Strategy 1: exact basename or substring via the index when available.
        base_sql = "SELECT DISTINCT file_path FROM symbol_index"
        try:
            fts_ok = len(query) >= 3 and self._ensure_fts()
            if fts_ok:
                fts_q = '"' + query.replace('"', '""') + '"'
                sql = "SELECT file_path FROM file_fts WHERE file_path MATCH ? LIMIT ?"
                candidates.update(
                    r[0] for r in conn.execute(sql, (fts_q, candidate_cap))
                )
            else:
                sql = f"{base_sql} WHERE lower(file_path) LIKE ? LIMIT ?"
                candidates.update(
                    r[0]
                    for r in conn.execute(
                        sql, ("%" + q_lower + "%", candidate_cap)
                    )
                )

            if len(candidates) < candidate_cap:
                for row in conn.execute(base_sql):
                    fp = row[0]
                    if is_fuzzy_subsequence(query, fp):
                        candidates.add(fp)
                        if len(candidates) >= candidate_cap:
                            break
        except sqlite3.Error:
            logger.debug("indexed file search unavailable", exc_info=True)

        # Filesystem fallback keeps file hits visible even when the index
        # is stale, missing, or does not cover non-source files.
        if len(candidates) < candidate_cap:
            for fp in self._collect_project_files():
                display_path = (
                    os.path.relpath(fp, self.project_root)
                    if os.path.isabs(fp)
                    else fp
                )
                if q_lower in display_path.lower() or is_fuzzy_subsequence(query, display_path):
                    candidates.add(fp)
                    if len(candidates) >= candidate_cap:
                        break

        items = []
        for fp in candidates:
            display_path = (
                os.path.relpath(fp, self.project_root)
                if os.path.isabs(fp)
                else fp
            )
            score = _score_file(display_path, query)
            if score > 0:
                items.append({
                    "kind": "file",
                    "name": Path(fp).name,
                    "file_path": fp,
                    "line": 1,
                    "score": score,
                })
        
        items.sort(key=lambda x: -x["score"])
        return SearchResult(
            items=items[:limit],
            elapsed_ms=0,
            mode="symbol",
            provenance="files" if items else "",
            sources=["files"] if items else [],
        )

    # -- pattern search -----------------------------------------------------

    @_timed
    def search_pattern(
        self,
        pattern: str,
        *,
        limit: int = 50,
        file_scope: str | None = None,
    ) -> SearchResult:
        return self._search_pattern(pattern, limit=limit, file_scope=file_scope)

    def _search_pattern(
        self,
        pattern: str,
        *,
        limit: int = 50,
        file_scope: str | None = None,
    ) -> SearchResult:
        """Search for a code pattern, with partial-input support."""
        normalized, literals = normalize_partial_pattern(pattern)

        # If we have a valid (possibly normalized) pattern, use the full engine
        if normalized is not None:
            return self._search_pattern_full(
                normalized, limit=limit, file_scope=file_scope
            )

        # Fall back to literal index search
        if literals:
            return self._search_literals(
                literals, limit=limit, file_scope=file_scope
            )

        return SearchResult(items=[], elapsed_ms=0, mode="pattern")

    def _search_pattern_full(
        self,
        pattern: str,
        *,
        limit: int = 50,
        file_scope: str | None = None,
    ) -> SearchResult:
        """Run a full pattern search via the shared pipeline.

        Delegates to ``find_pattern_in_project`` which handles:
        1. Index prefilter (SQLite)
        2. Rust string-contains filter
        3. Rust tree-sitter batch (when applicable)
        4. Pattern matching fallback (parallel)

        The SQLite index connection is passed through so the shared
        backend can do the index prefilter without opening a second DB.
        """
        from emend.transform import (
            find_pattern_in_project,
            _collect_source_files_scandir,
        )

        scope_path = file_scope or self.project_root
        scope_resolved = Path(scope_path).resolve()

        if scope_resolved.is_file():
            file_paths = [str(scope_resolved)]
        else:
            file_paths = _collect_source_files_scandir(str(scope_resolved))

        project_matches = find_pattern_in_project(
            pattern, file_paths,
            index_conn=self._get_conn(),
            limit=limit,
        )

        items: list[dict] = []
        for pm in project_matches:
            items.append({
                "file_path": pm.file_path,
                "line": pm.match.line,
                "end_line": pm.match.end_line,
                "col": pm.match.col,
                "end_col": pm.match.end_col,
                "matched_text": pm.match.matched_text,
            })

        return SearchResult(
            items=items, elapsed_ms=0, mode="pattern",
            truncated=len(items) >= limit,
        )

    def _search_literals(
        self,
        literals: list[str],
        *,
        limit: int = 50,
        file_scope: str | None = None,
    ) -> SearchResult:
        """Fall back: search reference_index for literal identifiers."""
        conn = self._get_conn()
        items: list[dict] = []

        for lit in literals:
            try:
                sql = (
                    "SELECT target_qn, file_path, line, col, ref_kind "
                    "FROM reference_index WHERE target_qn LIKE ?"
                )
                params: list[Any] = ["%" + lit + "%"]
                if file_scope:
                    sql += " AND file_path LIKE ?"
                    params.append("%" + file_scope + "%")
                sql += " ORDER BY file_path, line LIMIT ?"
                params.append(limit)

                for row in conn.execute(sql, params):
                    items.append({
                        "target_qn": row[0],
                        "file_path": row[1],
                        "line": row[2],
                        "col": row[3],
                        "ref_kind": row[4],
                    })
            except Exception:
                pass

        return SearchResult(
            items=items[:limit],
            elapsed_ms=0,
            mode="pattern",
            truncated=len(items) > limit,
        )

    # -- grep (regex) search ------------------------------------------------

    def _search_grep(
        self,
        pattern: str,
        *,
        limit: int = 50,
        file_scope: str | None = None,
    ) -> SearchResult:
        """Search project files using rg or grep with a PCRE regex."""
        t0 = time.monotonic()
        scope = file_scope or self.project_root

        rg = shutil.which("rg")
        if rg:
            cmd = [
                rg, "--pcre2", "--no-heading", "--line-number",
                "--color", "never", "--max-count", str(limit * 2),
                pattern, scope,
            ]
        else:
            # Use git ls-files piped to grep for speed and .gitignore respect.
            grep = shutil.which("ggrep") or shutil.which("grep") or "grep"
            git = shutil.which("git")
            if git and Path(scope).joinpath(".git").exists():
                # git grep supports PCRE with -P
                cmd = [git, "-C", scope, "grep", "-Pn", "--no-color", pattern]
            else:
                cmd = [
                    grep, "-rPn", "--color=never",
                    "--exclude-dir=.git", "--exclude-dir=.venv",
                    "--exclude-dir=node_modules", "--exclude-dir=__pycache__",
                    pattern, scope,
                ]

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
            )
            raw_lines = proc.stdout.splitlines()
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logging.getLogger("emend.editor").warning("grep search failed: %s", exc)
            return SearchResult(
                items=[],
                elapsed_ms=(time.monotonic() - t0) * 1000,
                mode="grep",
            )

        items: list[dict] = []
        for line in raw_lines:
            if len(items) >= limit:
                break
            # Format: file:line:matched_text  (git grep omits the leading path)
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            file_path, line_no_str, matched_text = parts
            try:
                line_no = int(line_no_str)
            except ValueError:
                continue
            # Resolve relative paths from git grep to absolute.
            abs_path = str(Path(scope).joinpath(file_path).resolve())
            items.append({
                "file_path": abs_path,
                "line": line_no,
                "end_line": line_no,
                "matched_text": matched_text.strip(),
                "name": matched_text.strip()[:60],
                "kind": "",
            })

        return SearchResult(
            items=items,
            elapsed_ms=(time.monotonic() - t0) * 1000,
            mode="grep",
            truncated=len(items) >= limit,
        )

    # -- selector resolution ------------------------------------------------

    @_timed
    def resolve_selector(
        self, selector: str, *, limit: int = 50
    ) -> SearchResult:
        return self._search_selector(selector, limit=limit)

    def _search_selector(self, selector: str, *, limit: int = 50) -> SearchResult:
        """Resolve a (possibly partial) selector against the index."""
        conn = self._get_conn()

        # Split on ::
        if "::" not in selector:
            # Bare name — treat as symbol search
            return self._search_symbols(selector, limit=limit)

        file_part, sym_part = selector.split("::", 1)
        sym_part = sym_part.strip()
        file_part = file_part.strip()

        base = (
            "SELECT rowid, name, qualified_name, kind, file_path, "
            "line, end_line, signature, returns, depth, parent "
            "FROM symbol_index"
        )

        conditions: list[str] = []
        params: list[Any] = []

        # File filter
        if file_part:
            if "*" in file_part or "?" in file_part:
                conditions.append("file_path GLOB ?")
                params.append("*" + file_part.lstrip("*"))
            else:
                conditions.append("file_path LIKE ?")
                params.append("%" + file_part + "%")

        # Symbol filter — handle dotted paths and prefix matching
        if sym_part:
            parts = sym_part.split(".")
            if len(parts) == 1:
                name = parts[0]
                if "*" in name or "?" in name:
                    conditions.append("name GLOB ?")
                    params.append(name)
                else:
                    # Prefix match on name
                    conditions.append("(name = ? OR name LIKE ?)")
                    params.extend([name, name + "%"])
            else:
                # Dotted path — match qualified_name
                qn_pattern = ".".join(parts)
                conditions.append(
                    "(qualified_name = ? OR qualified_name LIKE ?)"
                )
                params.extend([qn_pattern, qn_pattern + "%"])

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"{base} WHERE {where} ORDER BY name, file_path, line LIMIT ?"
        params.append(limit)

        items: list[dict] = []
        try:
            for row in conn.execute(sql, params):
                d = _row_to_symbol_dict(row)
                d["score"] = 1000.0
                items.append(d)
        except Exception as exc:
            logger.debug("Selector query failed: %s", exc)

        return SearchResult(
            items=items,
            elapsed_ms=0,
            mode="selector",
            truncated=len(items) >= limit,
        )

    # -- reference search ---------------------------------------------------

    def search_references(
        self,
        qualified_name: str,
        *,
        limit: int = 100,
        ref_kind: str | None = None,
    ) -> SearchResult:
        t0 = time.monotonic()
        conn = self._get_conn()

        sql = (
            "SELECT target_qn, file_path, line, col, ref_kind "
            "FROM reference_index WHERE target_qn = ?"
        )
        params: list[Any] = [qualified_name]
        if ref_kind:
            sql += " AND ref_kind = ?"
            params.append(ref_kind)
        sql += " ORDER BY file_path, line LIMIT ?"
        params.append(limit)

        items: list[dict] = []
        try:
            for row in conn.execute(sql, params):
                items.append({
                    "target_qn": row[0],
                    "file_path": row[1],
                    "line": row[2],
                    "col": row[3],
                    "ref_kind": row[4],
                })
        except Exception as exc:
            logger.debug("Reference query failed: %s", exc)

        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return SearchResult(
            items=items,
            elapsed_ms=elapsed,
            mode="reference",
            truncated=len(items) >= limit,
            query=qualified_name,
        )

    # -- DSL goto-definition ------------------------------------------------

    def _goto_dsl_fallback(self, file: str, line: int) -> SearchResult:
        """Fallback for goto_definition: resolve DSL symbols at the cursor line."""
        from emend.dsl import (
            detect_dsl_regions, extract_sql_symbols, resolve_orm_links,
            DslKind,
        )

        items: list[dict] = []
        try:
            regions = detect_dsl_regions(file)
            for region in regions:
                if region.host_start_line <= line <= region.host_end_line and region.dsl == DslKind.SQL:
                    symbols = extract_sql_symbols(region)
                    if symbols:
                        links = resolve_orm_links(symbols, self.project_root)
                        for lnk in links:
                            items.append({
                                "name": lnk.target_qualified_name.split(".")[-1],
                                "kind": "class",
                                "file_path": lnk.target_file,
                                "line": lnk.target_line,
                                "col": 0,
                                "qualified_name": lnk.target_qualified_name,
                            })
                    break
        except Exception as e:
            logger.warning("_goto_dsl_fallback error: %s", e)

        return SearchResult(items=items, elapsed_ms=0, mode="symbol")

    # -- file symbols (outline) ---------------------------------------------

    def _symbols_from_source(
        self, file_path: str, source: str, limit: int = 500
    ) -> list[dict]:
        """Parse symbols from source text using the Rust extension."""
        from emend.transform import _rust
        ext = Path(file_path).suffix.lstrip('.') or 'py'
        items: list[dict] = []
        try:
            raw_symbols = _rust.collect_symbols_from_str(source, ext=ext)
            for sym in raw_symbols:
                if sym.get("kind") in ("variable", "reference"):
                    continue
                item = {
                    "name": sym.get("name", ""),
                    "qualified_name": sym.get("qualified_name", sym.get("name", "")),
                    "kind": sym.get("kind", ""),
                    "file_path": file_path,
                    "line": sym.get("line", 0),
                    "end_line": sym.get("end_line", sym.get("line", 0)),
                    "signature": sym.get("signature", ""),
                    "returns": sym.get("returns"),
                    "depth": sym.get("depth", 0),
                    "parent": sym.get("parent"),
                }
                items.append(item)
                if len(items) >= limit:
                    break
        except Exception as exc:
            logger.debug("_symbols_from_source failed: %s", exc)
        return items

    def file_symbols(
        self, file_path: str, *, content: str | None = None, limit: int = 500
    ) -> SearchResult:
        t0 = time.monotonic()

        # Resolve to absolute path for matching
        resolved = str(Path(file_path).resolve())

        # Prefer: explicit content param > hot buffer > persistent index
        source = content if content is not None else self._hot_buffers.get(resolved)

        items: list[dict] = []
        if source is not None:
            # Parse symbols directly from source text
            items = self._symbols_from_source(resolved, source, limit)
        else:
            # Fall back to persistent index
            conn = self._get_conn()
            try:
                for row in conn.execute(
                    "SELECT name, qualified_name, kind, file_path, "
                    "line, end_line, signature, returns, depth, parent "
                    "FROM symbol_index WHERE file_path = ? "
                    "ORDER BY line",
                    (resolved,),
                ):
                    items.append(_row_to_symbol_dict(row, has_rowid=False))
            except Exception as exc:
                logger.debug("File symbols query failed: %s", exc)

        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return SearchResult(
            items=items,
            elapsed_ms=elapsed,
            mode="file_symbols",
            truncated=len(items) >= limit,
            query=file_path,
        )

    # -- index status -------------------------------------------------------

    def status(self) -> SearchResult:
        t0 = time.monotonic()
        conn = self._get_conn()

        info: dict[str, Any] = {"available": True}
        try:
            info["symbol_count"] = conn.execute(
                "SELECT COUNT(*) FROM symbol_index"
            ).fetchone()[0]
            info["reference_count"] = conn.execute(
                "SELECT COUNT(*) FROM reference_index"
            ).fetchone()[0]
            info["file_count"] = conn.execute(
                "SELECT COUNT(*) FROM file_manifest"
            ).fetchone()[0]

            for row in conn.execute("SELECT key, value FROM index_meta"):
                info[row[0]] = row[1]

            # Check FTS status
            try:
                info["fts_count"] = conn.execute(
                    "SELECT COUNT(*) FROM symbol_fts"
                ).fetchone()[0]
            except Exception:
                info["fts_count"] = 0
        except Exception as exc:
            info["available"] = False
            info["error"] = str(exc)

        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return SearchResult(
            items=[info],
            elapsed_ms=elapsed,
            mode="status",
            query="status",
        )

    # -- hot buffer protocol ---------------------------------------------------

    def _buffer_store(self, file: str, content: str, version: int, op: str) -> SearchResult:
        """Store buffer content and return a buffer SearchResult."""
        t0 = time.monotonic()
        resolved = str(Path(file).resolve())
        self._hot_buffers[resolved] = content
        self._hot_buffer_versions[resolved] = version
        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return SearchResult(
            items=[{"file": resolved, "version": version}],
            elapsed_ms=elapsed,
            mode="buffer",
            query=f"{op} {file}",
        )

    def buffer_open(self, file: str, content: str, version: int = 0) -> SearchResult:
        """Register an open buffer with its current content."""
        return self._buffer_store(file, content, version, "buffer_open")

    def buffer_update(self, file: str, content: str, version: int = 0) -> SearchResult:
        """Update the content of an open buffer."""
        return self._buffer_store(file, content, version, "buffer_update")

    def buffer_close(self, file: str) -> SearchResult:
        """Remove a buffer from the hot buffer cache."""
        t0 = time.monotonic()
        resolved = str(Path(file).resolve())
        removed = resolved in self._hot_buffers
        self._hot_buffers.pop(resolved, None)
        self._hot_buffer_versions.pop(resolved, None)
        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return SearchResult(
            items=[{"file": resolved, "removed": removed}],
            elapsed_ms=elapsed,
            mode="buffer",
            query=f"buffer_close {file}",
        )

    def get_hot_content(self, file_path: str) -> str | None:
        """Return hot buffer content for a file, or None if not buffered."""
        resolved = str(Path(file_path).resolve())
        return self._hot_buffers.get(resolved)

    def _read_file_or_hot(self, file_path: str) -> str | None:
        """Read file content, preferring hot buffer over disk.

        Returns None if file doesn't exist and has no hot buffer.
        """
        resolved = str(Path(file_path).resolve())
        hot = self._hot_buffers.get(resolved)
        if hot is not None:
            return hot
        p = Path(resolved)
        if p.exists():
            return p.read_text()
        return None

    def _get_completion_cfgs(self, source: str) -> list[Any]:
        """Return CFGs for *source*, caching the expensive build step."""
        from emend.cfg import build_cfgs_for_source

        source_hash = hashlib.sha1(source.encode("utf-8")).hexdigest()
        cached = self._completion_cfg_cache.get(source_hash)
        if cached is not None:
            self._completion_cfg_cache.move_to_end(source_hash)
            return cached

        cfgs = build_cfgs_for_source(source)
        self._completion_cfg_cache[source_hash] = cfgs
        self._completion_cfg_cache.move_to_end(source_hash)
        while len(self._completion_cfg_cache) > self._completion_cfg_cache_max:
            self._completion_cfg_cache.popitem(last=False)
        return cfgs

    def goto_definition(self, file: str, line: int, col: int) -> SearchResult:
        """Find the definition of the symbol at the given position.

        Uses the scope resolver to trace the reference back to its binding site.
        Works for local variables, parameters, loop variables, etc.
        """
        from emend.transform import _rust
        t0 = time.monotonic()

        logger.debug(f"goto_definition: file={file}, line={line}, col={col}")

        file_path = Path(file).resolve()
        if not file_path.exists() and str(file_path) not in self._hot_buffers:
            logger.debug(f"goto_definition: file not found: {file_path}")
            return SearchResult(items=[], elapsed_ms=0, mode="symbol")

        # Parse with scope resolver.  PyScopeResolver now falls back to
        # python_default() when the project config fails to load, so it no
        # longer raises on malformed TOML (see scope_py.rs).
        try:
            ext = file_path.suffix.lstrip('.')
            resolver = _rust.PyScopeResolver(str(self.project_root), extension=ext)
            content = self._read_file_or_hot(str(file_path))
            if content is None:
                return SearchResult(items=[], elapsed_ms=0, mode="symbol")
            resolver.index_file(str(file_path), content)
            refs = resolver.references_in_file(str(file_path))
            logger.debug(f"goto_definition: found {len(refs)} references in file")

            # Also get bindings (for parameters and other definitions)
            bindings = []
            try:
                scopes = resolver.scopes_in_file(str(file_path))
                for scope_kind, scope_start, scope_end, scope_bindings in scopes:
                    for b_name, b_kind, b_line, b_col in scope_bindings:
                        # scopes_in_file returns 0-based line numbers, convert to 1-based
                        binding_line_1based = b_line + 1
                        bindings.append((f"{b_name}", binding_line_1based, b_col, b_kind))
                logger.debug(f"goto_definition: found {len(bindings)} bindings in scopes")
            except Exception as e:
                logger.debug(f"goto_definition: error getting bindings: {e}")
        except Exception as exc:
            logger.debug("Scope resolver failed: %s", exc)
            return SearchResult(items=[], elapsed_ms=0, mode="symbol")

        # Find the reference at (line, col) by extracting the word at cursor position
        # and matching by identifier name rather than column proximity
        target_qn = None
        # Determine QN separator based on file extension
        qn_sep = "/" if file.endswith((".ts", ".tsx", ".js", ".jsx")) else "."

        # Extract the identifier at the cursor position from the source
        lines = content.split('\n')
        if line <= len(lines):
            line_text = lines[line - 1]
            # Find word boundaries around the cursor position
            # Allow col to be up to len(line_text) + 1 (cursor 1 past end)
            if col >= 1 and col <= len(line_text) + 1:
                # Move col to be 0-based for string indexing
                cursor_idx = min(col - 1, len(line_text) - 1)
                identifier = ""
                # If cursor is at/past end and line is empty, skip
                if cursor_idx < 0:
                    logger.debug(f"goto_definition: empty line or cursor at start, skipping identifier extraction")
                else:
                    # Find start of identifier (move left while alphanumeric/underscore)
                    start = cursor_idx
                    while start > 0 and (line_text[start-1].isalnum() or line_text[start-1] == '_'):
                        start -= 1
                    # Handle case where cursor is not on identifier (e.g., on whitespace)
                    if not (line_text[cursor_idx].isalnum() or line_text[cursor_idx] == '_'):
                        # Try moving RIGHT first to find an identifier (more natural for "cursor before word")
                        right = cursor_idx + 1
                        while right < len(line_text) and not (line_text[right].isalnum() or line_text[right] == '_'):
                            right += 1
                        # Try moving LEFT to find an identifier
                        left = cursor_idx
                        while left > 0 and not (line_text[left].isalnum() or line_text[left] == '_'):
                            left -= 1

                        found_right = right < len(line_text) and (line_text[right].isalnum() or line_text[right] == '_')
                        found_left = left >= 0 and (line_text[left].isalnum() or line_text[left] == '_')

                        if found_right and found_left:
                            # Pick the closer one; prefer right on ties (cursor-before-word is more common)
                            if (right - cursor_idx) <= (cursor_idx - left):
                                cursor_idx = right
                            else:
                                cursor_idx = left
                        elif found_right:
                            cursor_idx = right
                        elif found_left:
                            cursor_idx = left
                        else:
                            logger.debug(f"goto_definition: cursor not on identifier, skipping reference search")

                        # Recompute start from the chosen cursor position
                        start = cursor_idx
                        while start > 0 and (line_text[start-1].isalnum() or line_text[start-1] == '_'):
                            start -= 1

                    if cursor_idx >= 0 and (line_text[cursor_idx].isalnum() or line_text[cursor_idx] == '_'):
                        # Find end of identifier (move right while alphanumeric/underscore)
                        end = cursor_idx
                        while end < len(line_text) and (line_text[end].isalnum() or line_text[end] == '_'):
                            end += 1
                        identifier = line_text[start:end]
                        logger.debug(f"goto_definition: extracted identifier='{identifier}' from cursor at col={col}")

                        # Find the reference with matching identifier (last component of QN)
                        for qn, r_line, r_col, r_offset, r_end_offset, r_kind, _ann in refs:
                            if r_line == line:
                                qn_parts = qn.split(qn_sep)
                                qn_last = qn_parts[-1]

                                if qn_last == identifier:
                                    logger.debug(f"goto_definition: MATCH found target_qn={qn}")
                                    target_qn = qn
                                    break

                        # If no reference found, check bindings (for parameters, etc.)
                        if not target_qn:
                            # First try exact line match
                            for b_name, b_line, b_col, b_kind in bindings:
                                if b_line == line and b_name == identifier:
                                    logger.debug(f"goto_definition: MATCH found binding {b_name} at line {b_line}")
                                    target_qn = b_name
                                    break

                            # If still not found, search in enclosing scopes (for parameters in parent function/class)
                            if not target_qn:
                                # Find bindings with matching name in any line before current line
                                matching_bindings = [(b_name, b_line, b_col, b_kind) for b_name, b_line, b_col, b_kind in bindings if b_name == identifier and b_line < line]
                                if matching_bindings:
                                    # Use the most recent one (highest line number)
                                    matching_bindings.sort(key=lambda x: -x[1])
                                    b_name, b_line, b_col, b_kind = matching_bindings[0]
                                    logger.debug(f"goto_definition: MATCH found binding {b_name} in parent scope at line {b_line}")
                                    target_qn = b_name

        if not target_qn:
            logger.debug(f"goto_definition: no target_qn found at line={line}, col={col}, trying DSL fallback")
            # DSL fallback: if cursor is inside an embedded DSL region (e.g. SQL
            # string), resolve table/column names to host-language definitions.
            dsl_result = self._goto_dsl_fallback(str(file_path), line)
            if dsl_result.items:
                dsl_result.elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
                return dsl_result
            return SearchResult(items=[], elapsed_ms=0, mode="symbol")

        # 1. Local definition in the same file
        local_defs = []
        all_refs = []
        import_refs = []
        resolved_qn = target_qn
        for qn, r_line, r_col, r_offset, r_end_offset, r_kind, _ann in refs:
            # Match by exact QN, or by suffix when target_qn is a bare name from bindings
            if qn == target_qn or (qn_sep not in target_qn and qn.endswith(qn_sep + target_qn)):
                resolved_qn = qn  # upgrade to fully-qualified name
                all_refs.append((r_line, r_col))
                if r_kind == "import":
                    import_refs.append((r_line, r_col))
                if r_kind in ("definition", "write"):
                    local_defs.append((r_line, r_col))

        if local_defs:
            local_defs.sort()
            r_line, r_col = local_defs[0]
            item = {
                "name": resolved_qn.split(qn_sep)[-1],
                "kind": "variable",
                "file_path": str(file_path),
                "line": r_line,
                "col": r_col,
                "qualified_name": resolved_qn,
            }
            res = SearchResult(items=[item], elapsed_ms=0, mode="symbol")
            res.elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            return res

        # 2. Cross-file definition: resolve imported/global targets via symbol index.
        # Import references should resolve to the imported symbol, not back to the
        # import statement in the current file.
        # This handles module-level symbols and imported names.
        symbol_result = self._search_symbols(resolved_qn, limit=1)
        if symbol_result.items:
            symbol_result.elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            return symbol_result

        import_result = self._resolve_imported_symbol_location(resolved_qn)
        if import_result is not None:
            import_result.elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            return import_result

        # If the only in-file hit was an import and we couldn't resolve it through
        # the symbol index, don't bounce back to the import line.
        if import_refs:
            return SearchResult(
                items=[],
                elapsed_ms=round((time.monotonic() - t0) * 1000, 2),
                mode="symbol",
            )

        if all_refs:
            all_refs.sort()
            r_line, r_col = all_refs[0]
            item = {
                "name": resolved_qn.split(qn_sep)[-1],
                "kind": "variable",
                "file_path": str(file_path),
                "line": r_line,
                "col": r_col,
                "qualified_name": resolved_qn,
            }
            return SearchResult(
                items=[item],
                elapsed_ms=round((time.monotonic() - t0) * 1000, 2),
                mode="symbol",
            )

        return symbol_result

    def _resolve_imported_symbol_location(self, qualified_name: str) -> SearchResult | None:
        """Resolve ``pkg.mod.Symbol``-style imports to a project file + line."""
        if "." not in qualified_name:
            return None
        # Skip absolute-path QNs produced by the scope resolver for method
        # references (e.g. ``/.home.user...module.method``).  Splitting on
        # '.' would produce '/' as the first module component, causing
        # Path('/').with_suffix('.py') to raise ValueError.
        if qualified_name.startswith("/"):
            return None

        from emend.ast_utils import find_nested_definitions, find_symbol_by_path

        parts = qualified_name.split(".")
        for split_at in range(len(parts) - 1, 0, -1):
            module_name = ".".join(parts[:split_at])
            symbol_path = parts[split_at:]
            for candidate in self._module_candidates(module_name):
                if not candidate.is_file():
                    continue
                try:
                    symbol = find_symbol_by_path(
                        find_nested_definitions(str(candidate)),
                        symbol_path,
                    )
                except Exception:
                    continue
                if symbol is None:
                    continue
                return SearchResult(
                    items=[{
                        "name": symbol_path[-1],
                        "kind": getattr(symbol, "kind", "symbol"),
                        "file_path": str(candidate),
                        "line": symbol.line_start,
                        "qualified_name": qualified_name,
                    }],
                    elapsed_ms=0,
                    mode="symbol",
                )
        return None

    def _module_candidates(self, module_name: str) -> list[Path]:
        """Return plausible project-local files for a dotted Python module."""
        module_rel = Path(*module_name.split("."))
        return [
            (self.project_root / module_rel).with_suffix(".py"),
            self.project_root / module_rel / "__init__.py",
        ]

    # -- incremental re-index -----------------------------------------------

    def reindex(self) -> SearchResult:
        """Re-index stale files and rebuild FTS."""
        t0 = time.monotonic()

        from emend.transform import _ensure_index_fresh

        fresh = _ensure_index_fresh(self.project_root)
        # Rebuild FTS after any re-indexing
        conn = self._get_conn()
        fts_count = rebuild_fts(conn)
        self._fts_ready = True

        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return SearchResult(
            items=[{"fresh": fresh, "fts_count": fts_count}],
            elapsed_ms=elapsed,
            mode="reindex",
            query="reindex",
        )

    def _rename(self, qualified_name: str, new_name: str, file: str, apply: bool) -> SearchResult:
        from emend.transform import rename_symbol
        from emend.component_selector import ExtendedSelector
        t0 = time.monotonic()
        selector = ExtendedSelector(file_path=file, symbol_path=qualified_name.split("."))
        diffs = rename_symbol(selector, new_name, project_path=str(self.project_root), apply=apply)
        items = [{"file_path": fp, "diff": diff} for fp, diff in diffs.items()]
        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return SearchResult(items=items, elapsed_ms=elapsed, mode="symbol", query=f"rename {qualified_name}")

    def rename_preview(self, qualified_name: str, new_name: str, file: str = "") -> SearchResult:
        """Dry-run rename, return list of changes."""
        return self._rename(qualified_name, new_name, file, apply=False)

    def rename_apply(self, qualified_name: str, new_name: str, file: str = "") -> SearchResult:
        """Apply rename across the project."""
        return self._rename(qualified_name, new_name, file, apply=True)

    def _replace(self, pattern: str, replacement: str, file: str, inside: str | None, not_inside: str | None, apply: bool) -> SearchResult:
        from emend.transform import replace_pattern
        from emend.cli import resolve_files
        t0 = time.monotonic()
        items: list[dict] = []
        search_path = file if file else str(self.project_root)
        files, _ = resolve_files(search_path)
        for fp in (str(f) for f in files):
            try:
                diff, count = replace_pattern(
                    pattern, replacement, fp,
                    inside=inside, not_inside=not_inside,
                    apply=apply,
                )
                if count > 0:
                    items.append({"file_path": fp, "diff": diff, "count": count})
            except Exception:
                continue
        mode = "replace_apply" if apply else "replace_preview"
        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return SearchResult(items=items, elapsed_ms=elapsed, mode=mode, query=f"{pattern} -> {replacement}")

    def replace_preview(self, pattern: str, replacement: str, file: str = "", inside: str | None = None, not_inside: str | None = None) -> SearchResult:
        """Dry-run pattern replace, return diffs."""
        return self._replace(pattern, replacement, file, inside, not_inside, apply=False)

    def replace_apply(self, pattern: str, replacement: str, file: str = "", inside: str | None = None, not_inside: str | None = None) -> SearchResult:
        """Apply pattern replace, return diffs of applied changes."""
        return self._replace(pattern, replacement, file, inside, not_inside, apply=True)

    def _move(self, qualified_name: str, dest_file: str, file: str, apply: bool) -> SearchResult:
        from emend.transform import move_symbol
        from emend.component_selector import ExtendedSelector
        t0 = time.monotonic()
        selector = ExtendedSelector(file_path=file, symbol_path=qualified_name.split("."))
        diffs = move_symbol(selector, dest_file, project_path=str(self.project_root), apply=apply)
        items = [{"file_path": fp, "diff": diff} for fp, diff in diffs.items()]
        mode = "move_apply" if apply else "move_preview"
        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return SearchResult(items=items, elapsed_ms=elapsed, mode=mode, query=f"move {qualified_name}")

    def move_preview(self, qualified_name: str, dest_file: str, file: str = "") -> SearchResult:
        """Dry-run move, return diffs."""
        return self._move(qualified_name, dest_file, file, apply=False)

    def move_apply(self, qualified_name: str, dest_file: str, file: str = "") -> SearchResult:
        """Apply move across the project."""
        return self._move(qualified_name, dest_file, file, apply=True)

    def callers(self, qualified_name: str, file: str = "", limit: int = 50) -> SearchResult:
        """Find call sites for a symbol."""
        from emend.transform import find_callers
        from emend.component_selector import ExtendedSelector
        t0 = time.monotonic()

        selector = ExtendedSelector(file_path=file, symbol_path=qualified_name.split("."))
        refs = list(find_callers(selector, project_path=str(self.project_root)))

        items: list[dict] = []
        for ref in refs[:limit]:
            items.append({
                "name": qualified_name.split(".")[-1],
                "kind": "reference",
                "file_path": ref.file_path,
                "line": ref.line,
                "end_line": ref.line,
            })

        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return SearchResult(items=items, elapsed_ms=elapsed, mode="callers", query=f"callers of {qualified_name}")

    def callees(self, qualified_name: str, file: str = "", limit: int = 50) -> SearchResult:
        """Find functions called by a symbol."""
        from emend.transform import find_callees
        from emend.component_selector import ExtendedSelector
        t0 = time.monotonic()

        selector = ExtendedSelector(file_path=file, symbol_path=qualified_name.split("."))
        callee_list = find_callees(selector, project_path=str(self.project_root))

        items: list[dict] = []
        for callee in callee_list[:limit]:
            items.append({
                "name": callee.name,
                "kind": "function",
                "file_path": callee.file_path or file,
                "line": callee.line,
                "end_line": callee.line,
                "qualified_name": callee.qualified_name or callee.name,
            })

        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return SearchResult(items=items, elapsed_ms=elapsed, mode="callees", query=f"callees of {qualified_name}")

    def impact(self, qualified_name: str, file: str = "", limit: int = 50) -> SearchResult:
        """Compute transitive impact of a symbol change."""
        from emend.transform import find_impact
        from emend.component_selector import ExtendedSelector
        t0 = time.monotonic()

        selector = ExtendedSelector(file_path=file, symbol_path=qualified_name.split("."))
        result = find_impact(selectors=[selector], project_path=str(self.project_root))

        items: list[dict] = []
        # impacted_symbols are selector strings like "file.py::Class.method"
        for sel_str in result.impacted_symbols[:limit]:
            parts = sel_str.split("::", 1)
            fp = parts[0] if parts else ""
            sym_part = parts[1] if len(parts) > 1 else sel_str
            name = sym_part.rsplit(".", 1)[-1] if sym_part else sel_str
            items.append({
                "name": name,
                "kind": "function",
                "file_path": fp,
                "line": 1,
                "end_line": 1,
                "qualified_name": sym_part,
            })
        # Also include impacted tests
        for test_str in result.impacted_tests[:limit]:
            parts = test_str.split("::", 1)
            fp = parts[0] if parts else ""
            sym_part = parts[1] if len(parts) > 1 else test_str
            name = sym_part.rsplit(".", 1)[-1] if sym_part else test_str
            items.append({
                "name": name,
                "kind": "function",
                "file_path": fp,
                "line": 1,
                "end_line": 1,
                "qualified_name": sym_part,
            })

        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return SearchResult(
            items=items,
            elapsed_ms=elapsed,
            mode="impact",
            query=f"impact of {qualified_name}",
        )

    def check_duplicates(
        self,
        file: str,
        mode: str = "all",
        limit: int = 10,
        min_lines: int = 5,
        min_score: float = 0.0,
    ) -> SearchResult:
        """Check if *file* duplicates code elsewhere in the project.

        Returns a SearchResult where each item is a duplicate cluster summarized
        as ``(kind, score, primary_location, other_file:line, members_json)``.
        """
        from emend.duplicate import check_file_duplicates

        t0 = time.monotonic()
        clusters = check_file_duplicates(
            file_path=file,
            project_path=str(self.project_root),
            mode=mode,
            limit=limit,
            min_lines=min_lines,
            min_score=min_score,
        )

        items: list[dict] = []
        for cluster in clusters:
            if not cluster.members:
                continue
            primary = cluster.members[0]
            items.append({
                "name": f"{cluster.kind}:{cluster.explanation}",
                "kind": "duplicate",
                "file_path": primary.file,
                "line": primary.start_line,
                "end_line": primary.end_line,
                "score": cluster.score,
                "members": [
                    {
                        "file": m.file,
                        "symbol": m.symbol,
                        "start_line": m.start_line,
                        "end_line": m.end_line,
                        "node_count": m.node_count,
                        "stmt_count": m.stmt_count,
                    }
                    for m in cluster.members
                ],
            })

        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return SearchResult(
            items=items,
            elapsed_ms=elapsed,
            mode="check_duplicates",
            query=f"dupes in {file}",
        )

    def types_at_cursor(self, file: str, line: int = 0, col: int = 0) -> SearchResult:
        """Return type information for the symbol at cursor position."""
        t0 = time.monotonic()

        items: list[dict] = []
        try:
            from emend.type_oracle import create_type_oracle
            oracle = create_type_oracle(engine="pyrefly", project_root=str(self.project_root))
            file_types = oracle.get_file_types(file)
            if file_types:
                file_types.build_index()
                # Try to find type at the given position
                binding = file_types._by_position.get((line, col))
                if binding:
                    items.append({
                        "name": binding.name,
                        "type": binding.raw_type,
                        "line": binding.line,
                        "file_path": file,
                    })
                else:
                    # Fall back: find closest binding on the given line
                    for b in file_types.bindings:
                        if b.line == line:
                            items.append({
                                "name": b.name,
                                "type": b.raw_type,
                                "line": b.line,
                                "file_path": file,
                            })
        except Exception as e:
            import logging
            logging.getLogger("emend.editor").debug(f"types_at_cursor failed: {e}")

        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return SearchResult(items=items, elapsed_ms=elapsed, mode="types", query=f"types at {file}:{line}")

    def complete(self, prefix: str, limit: int = 20, file: str = "", line: int = 0, col: int = 0) -> SearchResult:
        """Return completion candidates for the given prefix.

        When *file* is provided, imported names in that file are included
        as candidates (union with the project symbol index).

        When *line* and *col* are provided, local variables from the
        enclosing scopes are ranked first.
        """
        t0 = time.monotonic()
        conn = self._get_conn()
        seen: set[str] = set()
        items: list[dict] = []

        is_dotted = "." in prefix

        # 1. Local variables from PyScopeResolver
        local_names: set[str] = set()
        if file and line > 0 and not is_dotted:
            try:
                from emend.transform import _rust
                resolver = _rust.PyScopeResolver(str(self.project_root))
                source = self._read_file_or_hot(file) or ""
                resolver.index_file(str(file), source)
                # scopes_in_file returns (kind, start_line, end_line, [(name, kind, line, col)])
                # Note: Rust uses 0-based line numbers, Vim uses 1-based, so subtract 1
                scopes = resolver.scopes_in_file(str(file))

                # Sort scopes by size (smallest first) to find the most specific enclosing scope
                # But actually we want ALL enclosing scopes (locals, then outer, etc.)
                enclosing_scopes = []
                zero_based_line = line - 1  # Convert from Vim's 1-based to 0-based
                for kind, s_start, s_end, bindings in scopes:
                    if s_start <= zero_based_line <= s_end:
                        size = s_end - s_start
                        enclosing_scopes.append((size, bindings))

                enclosing_scopes.sort(key=lambda x: x[0])  # innermost first

                # Try to build CFG data for more precise reachability analysis
                cfg_defs_before_cursor: set[str] | None = None
                cfg_func_range: tuple[int, int] | None = None
                try:
                    cfg_t0 = time.monotonic()
                    cfgs = self._get_completion_cfgs(source)
                    # Find the CFG for the function containing the cursor (prefer innermost)
                    cursor_cfg = None
                    for c in cfgs:
                        if c.func_start_line <= zero_based_line <= c.func_end_line:
                            if cursor_cfg is None or (c.func_end_line - c.func_start_line) < (cursor_cfg.func_end_line - cursor_cfg.func_start_line):
                                cursor_cfg = c
                    if cursor_cfg is not None:
                        cfg_func_range = (cursor_cfg.func_start_line, cursor_cfg.func_end_line)
                        # Find which block the cursor is in
                        cursor_block_id = None
                        for block in cursor_cfg.get_blocks():
                            if block['start_line'] <= zero_based_line <= block['end_line']:
                                cursor_block_id = block['id']
                                break
                        if cursor_block_id is not None:
                            # Build reverse reachability: which blocks can reach cursor_block?
                            edges = cursor_cfg.get_edges()
                            pred: dict[int, list[int]] = {}
                            for e in edges:
                                pred.setdefault(e['to'], []).append(e['from'])
                            # BFS backwards from cursor block
                            reachable_to_cursor: set[int] = set()
                            queue = deque([cursor_block_id])
                            while queue:
                                bid = queue.popleft()
                                if bid in reachable_to_cursor:
                                    continue
                                reachable_to_cursor.add(bid)
                                queue.extend(pred.get(bid, []))
                            # Collect defs from blocks that can reach cursor
                            cfg_defs_before_cursor = set()
                            for block in cursor_cfg.get_blocks():
                                if block['id'] in reachable_to_cursor:
                                    for def_tuple in block['defs']:
                                        d_name = def_tuple[0]
                                        d_line = def_tuple[1]
                                        # Only count defs on lines before cursor
                                        if d_line <= zero_based_line:
                                            cfg_defs_before_cursor.add(d_name)
                    cfg_elapsed = (time.monotonic() - cfg_t0) * 1000
                    if cfg_elapsed > 50:
                        logger.debug("CFG analysis took %.1fms (slow)", cfg_elapsed)
                except Exception as exc:
                    logger.debug("CFG-informed completion failed: %s", exc)

                for _, bindings in enclosing_scopes:
                    for b_name, b_kind, b_line, b_col in bindings:
                        # Only include if it matches prefix
                        if not b_name.startswith(prefix.split(".")[-1]):
                            continue
                        if b_name not in seen:
                            seen.add(b_name)
                            local_names.add(b_name)

                            # CFG-informed scoring: only apply CFG analysis to
                            # variables within the same function as the cursor.
                            # Outer scope variables use the line-number heuristic.
                            is_param = b_kind.lower() == "parameter"
                            in_cfg_func = (
                                cfg_func_range is not None
                                and cfg_func_range[0] <= b_line <= cfg_func_range[1]
                            )
                            if is_param:
                                # Parameters are always in scope
                                score = 1800
                            elif cfg_defs_before_cursor is not None and in_cfg_func:
                                # Use CFG reachability for same-function locals
                                if b_name in cfg_defs_before_cursor:
                                    score = 2000
                                else:
                                    score = 400
                            else:
                                # Fallback: simple line-number heuristic
                                if b_line <= zero_based_line:
                                    score = 2000
                                else:
                                    score = 400  # defined after cursor

                            menu = "[local]" if score >= 1800 else "[local?]"
                            items.append({
                                "word": b_name,
                                "kind": b_kind.lower(),
                                "menu": menu,
                                "score": score,
                            })
            except Exception as exc:
                logger.debug("Local completion failed: %s", exc)

        # Collect imported names from the current file for local completions.
        import_names: dict[str, str] = {}  # local_name -> qualified source
        source = ""
        if file:
            source = self._read_file_or_hot(file) or ""
            import_names = self._extract_import_names(file)

        if is_dotted:
            # Dotted prefix: e.g. "Foo.bar" -> search qualified names
            parts = prefix.rsplit(".", 1)
            parent = parts[0]
            member_prefix = parts[1] if len(parts) > 1 else ""

            # Resolve parent through imports (e.g. DocumentFilter -> document_api...DocumentFilter)
            resolved_parent = import_names.get(parent, parent)

            if file and source:
                local_attr_items, inferred_parents = self._complete_local_attributes(
                    parent=parent,
                    member_prefix=member_prefix,
                    source=source,
                    line=line,
                    import_names=import_names,
                    seen=seen,
                    limit=limit,
                )
                items.extend(local_attr_items)
                if len(items) < limit:
                    items.extend(
                        self._complete_source_parent_members(
                            inferred_parents=inferred_parents,
                            member_prefix=member_prefix,
                            source=source,
                            seen=seen,
                            limit=limit - len(items),
                        )
                    )
            else:
                inferred_parents = []

            # Search local project symbol index
            parents_to_check = inferred_parents + [resolved_parent, parent]
            checked_parents: set[str] = set()
            
            while parents_to_check:
                p = parents_to_check.pop(0)
                if p in checked_parents:
                    continue
                checked_parents.add(p)

                pattern = f"*{p}.{member_prefix}*"
                sql = (
                    "SELECT DISTINCT name, qualified_name, kind FROM symbol_index "
                    "WHERE qualified_name GLOB ? LIMIT ?"
                )
                for row in conn.execute(sql, (pattern, limit)):
                    if row[0] not in seen:
                        seen.add(row[0])
                        items.append({
                            "word": row[0],
                            "kind": row[2],
                            "menu": f"[{row[1]}]",
                            "score": 1000,
                        })

                # Follow bases if p is a class
                sql_bases = "SELECT bases FROM symbol_index WHERE qualified_name = ? OR name = ? LIMIT 1"
                row_bases = conn.execute(sql_bases, (p, p)).fetchone()
                if row_bases and row_bases[0]:
                    for b in row_bases[0].split(","):
                        b = b.strip()
                        if b and b not in checked_parents:
                            parents_to_check.append(b)

            # Enrich dotted completions via reference_index: find members referenced on parent
            if len(items) < limit:
                try:
                    ref_sql = (
                        "SELECT DISTINCT target_qn FROM reference_index "
                        "WHERE target_qn GLOB ? LIMIT ?"
                    )
                    for p in checked_parents:
                        ref_pattern = f"*{p}.{member_prefix}*"
                        for row in conn.execute(ref_sql, (ref_pattern, limit - len(items))):
                            target_qn = row[0]
                            # Extract the member name (segment immediately after the parent)
                            suffix = target_qn.split(f"{p}.", 1)[-1] if f"{p}." in target_qn else ""
                            member_name = suffix.split(".")[0] if suffix else ""
                            if member_name and member_name.startswith(member_prefix) and member_name not in seen:
                                seen.add(member_name)
                                items.append({
                                    "word": member_name,
                                    "kind": "reference",
                                    "menu": f"[ref:{target_qn}]",
                                    "score": 800,
                                })
                except Exception as exc:
                    logger.debug("Reference-based completion failed: %s", exc)

            # Fallback: resolve through KB module mappings for cross-project symbols
            if not items:
                items = self._complete_via_mapping(
                    resolved_parent, member_prefix, limit, seen
                )
        else:
            # 2. Imported names matching prefix (case-sensitive)
            for local_name, source in import_names.items():
                if local_name.startswith(prefix) and local_name not in seen:
                    seen.add(local_name)
                    items.append({
                        "word": local_name,
                        "kind": "import",
                        "menu": f"[{source}]",
                        "score": 500,
                    })

            # 3. Symbols by case-sensitive prefix (GLOB is case-sensitive)
            sql = (
                "SELECT DISTINCT name, kind, file_path FROM symbol_index "
                "WHERE name GLOB ? LIMIT ?"
            )
            for row in conn.execute(sql, (prefix + "*", limit)):
                if row[0] not in seen:
                    seen.add(row[0])
                    # Score module items in current file higher than imports
                    is_local_module = file and Path(row[2]).resolve() == Path(file).resolve()
                    score = 1500 if is_local_module else 100
                    items.append({
                        "word": row[0],
                        "kind": row[1],
                        "menu": "[sym]",
                        "score": score,
                    })

        # Sort by score (desc) and word length (asc)
        items.sort(key=lambda x: (-x.get("score", 0), len(x["word"])))

        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return SearchResult(items=items[:limit], elapsed_ms=elapsed, mode="complete", query=prefix)

    def _complete_local_attributes(
        self,
        parent: str,
        member_prefix: str,
        source: str,
        line: int,
        import_names: dict[str, str],
        seen: set[str],
        limit: int,
    ) -> tuple[list[dict], list[str]]:
        """Return attribute completions seen on a local receiver plus inferred type targets."""
        import ast as _ast

        try:
            tree = _ast.parse(self._normalize_completion_source(source, line, parent))
        except Exception:
            return [], []

        scope_node = self._find_enclosing_scope_node(tree, line)
        if scope_node is None:
            return [], []

        attr_names: set[str] = set()
        inferred_parents: list[str] = []
        inferred_seen: set[str] = set()

        for node in _ast.walk(scope_node):
            if isinstance(node, _ast.Attribute) and isinstance(node.value, _ast.Name):
                if node.value.id == parent:
                    attr_names.add(node.attr)
            elif isinstance(node, (_ast.Assign, _ast.AnnAssign)):
                candidate = self._infer_receiver_target(node, parent, import_names)
                if candidate and candidate not in inferred_seen:
                    inferred_seen.add(candidate)
                    inferred_parents.append(candidate)

        items: list[dict] = []
        for name in sorted(attr_names):
            if member_prefix and not name.startswith(member_prefix):
                continue
            if name in seen:
                continue
            seen.add(name)
            items.append({
                "word": name,
                "kind": "attribute",
                "menu": f"[scope:{parent}]",
                "score": 2500,
            })
            if len(items) >= limit:
                break

        return items, inferred_parents

    def _normalize_completion_source(self, source: str, line: int, parent: str) -> str:
        """Patch the active line so incomplete ``parent.`` remains parseable."""
        if line <= 0:
            return source
        lines = source.splitlines()
        if not 1 <= line <= len(lines):
            return source
        current = lines[line - 1]
        lines[line - 1] = re.sub(
            rf"\b{re.escape(parent)}\.(?=\s*(?:#.*)?$)",
            f"{parent}.__emend_complete__",
            current,
        )
        return "\n".join(lines) + ("\n" if source.endswith("\n") else "")

    def _complete_source_parent_members(
        self,
        inferred_parents: list[str],
        member_prefix: str,
        source: str,
        seen: set[str],
        limit: int,
    ) -> list[dict]:
        """Complete members from class definitions available in the current source."""
        import ast as _ast

        if limit <= 0 or not inferred_parents:
            return []
        try:
            tree = _ast.parse(source)
        except Exception:
            return []

        target_names = {parent.rsplit(".", 1)[-1] for parent in inferred_parents}
        items: list[dict] = []
        for node in ast.walk(tree):
            if not isinstance(node, _ast.ClassDef) or node.name not in target_names:
                continue
            for child in node.body:
                member_name = self._class_member_name(child)
                if not member_name:
                    continue
                if member_prefix and not member_name.startswith(member_prefix):
                    continue
                if member_name in seen:
                    continue
                seen.add(member_name)
                items.append({
                    "word": member_name,
                    "kind": "member",
                    "menu": f"[class:{node.name}]",
                    "score": 1500,
                })
                if len(items) >= limit:
                    return items
        return items

    def _class_member_name(self, node: Any) -> str | None:
        """Extract a direct class member name from a class-body statement."""
        import ast as _ast

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
        if isinstance(node, (_ast.Assign, _ast.AnnAssign)):
            targets = node.targets if isinstance(node, _ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, _ast.Name):
                    return target.id
        return None

    def _find_enclosing_scope_node(self, tree: Any, line: int) -> Any | None:
        """Return the innermost function/class/module node containing the cursor."""
        if line <= 0:
            return tree

        candidate = tree
        candidate_span = float("inf")
        scope_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        for node in ast.walk(tree):
            if not isinstance(node, scope_types):
                continue
            start = getattr(node, "lineno", 0)
            end = getattr(node, "end_lineno", start)
            if start <= line <= end:
                span = end - start
                if span < candidate_span:
                    candidate = node
                    candidate_span = span
        return candidate

    def _infer_receiver_target(
        self,
        node: Any,
        parent: str,
        import_names: dict[str, str],
    ) -> str | None:
        """Infer the target type/module path assigned to a receiver name."""
        import ast as _ast

        if isinstance(node, _ast.Assign):
            targets = node.targets
            value = node.value
            annotation = None
        elif isinstance(node, _ast.AnnAssign):
            targets = [node.target]
            value = node.value
            annotation = node.annotation
        else:
            return None

        if not any(isinstance(target, _ast.Name) and target.id == parent for target in targets):
            return None

        inferred = self._qualified_name_from_expr(value, import_names)
        if inferred:
            return inferred
        return self._qualified_name_from_expr(annotation, import_names)

    def _qualified_name_from_expr(
        self,
        expr: Any | None,
        import_names: dict[str, str],
    ) -> str | None:
        """Extract a dotted name from a simple AST expression."""
        import ast as _ast

        if expr is None:
            return None
        if isinstance(expr, _ast.Call):
            return self._qualified_name_from_expr(expr.func, import_names)
        if isinstance(expr, _ast.Name):
            return import_names.get(expr.id, expr.id)
        if isinstance(expr, _ast.Attribute):
            parts: list[str] = []
            current = expr
            while isinstance(current, _ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, _ast.Name):
                root = import_names.get(current.id, current.id)
                parts.append(root)
                return ".".join(reversed(parts))
        if isinstance(expr, _ast.Subscript):
            return self._qualified_name_from_expr(expr.value, import_names)
        return None

    def complete_diagnostics(self, prefix: str, file: str = "", line: int = 0, col: int = 0) -> SearchResult:
        """Return completion candidates with detailed timing breakdown."""
        t0 = time.monotonic()
        timings: dict[str, float] = {}

        result = self.complete(prefix, file=file, line=line, col=col)
        timings["total_ms"] = round((time.monotonic() - t0) * 1000, 2)
        timings["item_count"] = len(result.items)

        # Report which signals were used
        signals_used = []
        for item in result.items:
            menu = item.get("menu", "")
            if menu not in signals_used:
                signals_used.append(menu)

        return SearchResult(
            items=[{"timings": timings, "signals": signals_used}] + result.items,
            elapsed_ms=timings["total_ms"],
            mode="complete_diagnostics",
            query=prefix,
        )

    def _extract_import_names(self, file: str) -> dict[str, str]:
        """Parse imports from a Python file or hot buffer.

        Returns ``{local_name: qualified_source}``.
        """
        import ast as _ast

        try:
            source = self._read_file_or_hot(file)
            if source is None:
                source = Path(file).read_text()
            tree = _ast.parse(source)
        except Exception:
            return {}

        names: dict[str, str] = {}
        for node in _ast.iter_child_nodes(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[-1]
                    names[local] = alias.name
            elif isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    local = alias.asname or alias.name
                    names[local] = f"{module}.{alias.name}" if module else alias.name
        return names

    def _complete_via_mapping(
        self,
        resolved_parent: str,
        member_prefix: str,
        limit: int,
        seen: set[str],
    ) -> list[dict]:
        """Resolve a symbol through module mappings and list its children.

        E.g. for ``DocumentOrderEntry.`` where DocumentOrderEntry is defined in
        a mapped external repo, resolve the selector, read that file's symbols,
        and return children matching *member_prefix*.
        """
        try:
            from emend.knowledge import MappingStore
        except Exception:
            return []

        store = MappingStore(self.project_root)
        selector = store.resolve_selector(resolved_parent)
        if not selector or "::" not in selector:
            return []

        file_part, sym_part = selector.split("::", 1)
        if not Path(file_part).is_file():
            return []

        # Read symbols from the resolved file
        try:
            from emend.ast_utils import find_nested_definitions
            content = Path(file_part).read_text()
            symbols = find_nested_definitions(content)
        except Exception:
            return []

        # Find the target symbol and return its children
        items: list[dict] = []
        target_prefix = f"{sym_part}." if sym_part else ""
        for sym in symbols:
            qn = sym.get("qualified_name", sym.get("name", ""))
            name = sym.get("name", "")
            if target_prefix and not qn.startswith(target_prefix):
                continue
            # Only direct children (one level deep)
            remainder = qn[len(target_prefix):]
            if "." in remainder:
                continue
            if member_prefix and not name.startswith(member_prefix):
                continue
            if name not in seen:
                seen.add(name)
                items.append({
                    "word": name,
                    "kind": sym.get("kind", "variable"),
                    "menu": f"[{qn}]",
                })
            if len(items) >= limit:
                break
        return items


# ---------------------------------------------------------------------------
# JSON-RPC server
# ---------------------------------------------------------------------------

def _dispatch(engine: EditorSearchEngine, method: str, params: dict) -> dict:
    """Route a JSON-RPC method to the engine."""
    if method == "search":
        return engine.search(**params).to_dict()
    elif method == "symbols":
        return engine.search_symbols(**params).to_dict()
    elif method == "pattern":
        return engine.search_pattern(**params).to_dict()
    elif method == "references":
        return engine.search_references(**params).to_dict()
    elif method == "grep":
        pat = params.pop("pattern", params.pop("query", ""))
        return engine._search_grep(pat, **params).to_dict()
    elif method == "selector":
        sel = params.pop("selector", params.pop("query", ""))
        return engine.resolve_selector(sel, **params).to_dict()
    elif method == "file_symbols":
        fp = params.pop("file", params.pop("file_path", ""))
        content = params.pop("content", None)
        return engine.file_symbols(fp, content=content, **params).to_dict()
    elif method == "status":
        return engine.status().to_dict()
    elif method == "reindex":
        return engine.reindex().to_dict()
    elif method == "query_history":
        limit = int(params.get("limit", 50))
        return engine.query_history(limit=limit).to_dict()
    elif method == "reindex_async":
        started = engine.start_background_reindex()
        return {"started": started, "indexing": engine.is_indexing}
    elif method in ("goto_definition", "goto_local"):
        file = params.pop("file", "")
        line = int(params.pop("line", 1))
        col = int(params.pop("col", 0))
        return engine.goto_definition(file, line, col).to_dict()
    elif method == "rename_preview":
        qn = params.get("qualified_name", "")
        new_name = params.get("new_name", "")
        file = params.get("file", "")
        return engine.rename_preview(qn, new_name, file=file).to_dict()
    elif method == "rename_apply":
        qn = params.get("qualified_name", "")
        new_name = params.get("new_name", "")
        file = params.get("file", "")
        return engine.rename_apply(qn, new_name, file=file).to_dict()
    elif method == "complete":
        prefix = params.get("prefix", params.get("query", ""))
        file = params.get("file", "")
        line = int(params.get("line", 0))
        col = int(params.get("col", 0))
        logger.debug(f"complete() called: prefix={prefix!r}, file={file!r}, line={line}, col={col}")
        return engine.complete(prefix, file=file, line=line, col=col).to_dict()
    elif method == "complete_diagnostics":
        prefix = params.get("prefix", params.get("query", ""))
        file = params.get("file", "")
        line = int(params.get("line", 0))
        col = int(params.get("col", 0))
        return engine.complete_diagnostics(prefix, file=file, line=line, col=col).to_dict()
    # -- Mapping methods --
    elif method == "mapping_lookup":
        return _mapping_lookup(engine, params)
    elif method == "mapping_goto":
        # First try goto_definition if file/line/col are provided
        if "file" in params and "line" in params:
            file = params.get("file", "")
            line = int(params.get("line", 1))
            col = int(params.get("col", 0))
            logger.debug(f"mapping_goto: trying goto_definition(file={file!r}, line={line}, col={col})")
            res = engine.goto_definition(file, line, col)
            logger.debug(f"mapping_goto: goto_definition returned {len(res.items)} items")
            if res.items:
                return res.to_dict()
        logger.debug(f"mapping_goto: falling back to _mapping_goto")
        return _mapping_goto(engine, params)
    elif method == "module_resolve":
        return _module_resolve(engine, params)
    elif method == "module_map_add":
        return _module_map_add(engine, params)
    elif method == "replace_preview":
        return engine.replace_preview(
            pattern=params.get("pattern", ""),
            replacement=params.get("replacement", ""),
            file=params.get("file", ""),
            inside=params.get("inside"),
            not_inside=params.get("not_inside"),
        ).to_dict()
    elif method == "replace_apply":
        return engine.replace_apply(
            pattern=params.get("pattern", ""),
            replacement=params.get("replacement", ""),
            file=params.get("file", ""),
            inside=params.get("inside"),
            not_inside=params.get("not_inside"),
        ).to_dict()
    elif method == "move_preview":
        return engine.move_preview(
            qualified_name=params.get("qualified_name", ""),
            dest_file=params.get("dest_file", ""),
            file=params.get("file", ""),
        ).to_dict()
    elif method == "move_apply":
        return engine.move_apply(
            qualified_name=params.get("qualified_name", ""),
            dest_file=params.get("dest_file", ""),
            file=params.get("file", ""),
        ).to_dict()
    elif method == "callers":
        return engine.callers(
            qualified_name=params.get("qualified_name", ""),
            file=params.get("file", ""),
            limit=int(params.get("limit", 50)),
        ).to_dict()
    elif method == "callees":
        return engine.callees(
            qualified_name=params.get("qualified_name", ""),
            file=params.get("file", ""),
            limit=int(params.get("limit", 50)),
        ).to_dict()
    elif method == "types_at_cursor":
        return engine.types_at_cursor(
            file=params.get("file", ""),
            line=int(params.get("line", 0)),
            col=int(params.get("col", 0)),
        ).to_dict()
    elif method == "impact":
        return engine.impact(
            qualified_name=params.get("qualified_name", ""),
            file=params.get("file", ""),
            limit=int(params.get("limit", 50)),
        ).to_dict()
    elif method == "check_duplicates":
        return engine.check_duplicates(
            file=params.get("file", ""),
            mode=params.get("mode", "all"),
            limit=int(params.get("limit", 10)),
            min_lines=int(params.get("min_lines", 5)),
            min_score=float(params.get("min_score", 0.0)),
        ).to_dict()
    elif method == "buffer_open":
        fp = params.pop("file", "")
        content = params.pop("content", "")
        version = int(params.pop("version", 0))
        return engine.buffer_open(fp, content, version).to_dict()
    elif method == "buffer_update":
        fp = params.pop("file", "")
        content = params.pop("content", "")
        version = int(params.pop("version", 0))
        return engine.buffer_update(fp, content, version).to_dict()
    elif method == "buffer_close":
        fp = params.pop("file", "")
        return engine.buffer_close(fp).to_dict()
    else:
        raise ValueError(f"Unknown method: {method!r}")


# -- Mapping RPC handlers --


def _get_store(engine: EditorSearchEngine):
    """Lazy-init a MappingStore on the engine."""
    from emend.knowledge import MappingStore
    if not hasattr(engine, '_kb'):
        engine._kb = MappingStore(engine.project_root)  # type: ignore[attr-defined]
    return engine._kb  # type: ignore[attr-defined]


def _mapping_lookup(engine: EditorSearchEngine, params: dict) -> dict:
    """Look up identifier mappings for a symbol."""
    from emend.knowledge import mapping_to_dict
    t0 = time.monotonic()
    store = _get_store(engine)
    identifier = params.get("identifier", params.get("query", ""))
    project = params.get("project")
    direction = params.get("direction", "both")
    results = store.find_mappings_for(identifier, project=project, direction=direction)
    items = [mapping_to_dict(m) for m in results]
    elapsed = (time.monotonic() - t0) * 1000
    return {"items": items, "elapsed_ms": round(elapsed, 2), "mode": "mapping_lookup"}



def _resolve_selector_to_goto_item(engine: EditorSearchEngine, selector: str) -> dict | None:
    """Resolve a file::Symbol selector to a goto result dict with line number.
    
    Follows re-exports (star imports and explicit imports) to find the actual
    definition location.
    """
    if "::" not in selector:
        return None
    
    file_path, symbol_path = selector.split("::", 1)
    if not Path(file_path).is_file():
        return None

    from emend.ast_utils import find_nested_definitions, find_symbol_by_path, resolve_through_reexports
    
    # We only handle top-level symbol re-exports for now (e.g. mod.Symbol).
    # Nested symbols like mod.Class.method are assumed to be defined in mod.py.
    parts = symbol_path.split(".")
    base_symbol = parts[0]
    
    from emend.knowledge import make_resolve_module_cb
    store = _get_store(engine)
    resolve_cb = make_resolve_module_cb(store)

    res = resolve_through_reexports(file_path, base_symbol, resolve_cb)

    if res:
        resolved_file, line = res
        # If it was a nested path, we need to find the actual line for the nested part
        if len(parts) > 1:
            try:
                definitions = find_nested_definitions(resolved_file)
                symbol = find_symbol_by_path(definitions, parts)
                if symbol:
                    line = symbol.line_start
            except Exception:
                pass

        return {
            "name": parts[-1],
            "qualified_name": symbol_path,
            "location": f"{resolved_file}:{line}",
            "file_path": str(resolved_file),
            "line": line,
        }

    # Fallback to original logic (line 1 of the initial file) if resolution fails
    return {
        "name": parts[-1],
        "qualified_name": symbol_path,
        "location": f"{file_path}:1",
        "file_path": str(file_path),
        "line": 1,
    }


def _mapping_goto(engine: EditorSearchEngine, params: dict) -> dict:
    """Go to definition: try local symbol lookup first, then mappings.

    1. Search the local project index for the identifier (most common case).
    2. If no local results, check the identifier mappings and resolve
       targets to local paths (cloning external repos via gh if needed).
    """
    t0 = time.monotonic()
    identifier = params.get("identifier", params.get("query", ""))

    # --- 1. Local in-repo symbol lookup (fast, common case) ---
    local_result = engine.search_symbols(identifier, limit=10)
    local_items = []
    for item in local_result.items:
        # Prefer exact qualified_name or name match over fuzzy hits.
        qn = item.get("qualified_name", "")
        name = item.get("name", "")
        if identifier in (qn, name) or qn.endswith("." + identifier):
            local_items.append(item)

    if local_items:
        elapsed = (time.monotonic() - t0) * 1000
        return {
            "items": local_items,
            "elapsed_ms": round(elapsed, 2),
            "mode": "mapping_goto",
            "source": "local",
        }

    # --- 2. Cross-service mapping lookup (fallback) ---
    from emend.knowledge import mapping_to_dict
    store = _get_store(engine)
    results = store.find_mappings_for(identifier, direction="source")
    items = []
    for m in results:
        entry = mapping_to_dict(m)
        # Try to resolve the target identifier to a local path.
        resolved = store.resolve_module_to_path(m.target_identifier)
        if resolved:
            entry["resolved_path"] = resolved
        items.append(entry)

    # --- 3. Import-aware module mapping resolution (Tier 3) ---
    found_import = None
    if not items and params.get("file"):
        from emend.ast_utils import get_imports
        file_path = params["file"]
        imports = get_imports(file_path)

        # Filter imports for the target identifier
        for imp in imports:
            if (imp["asname"] or imp["name"]) == identifier:
                found_import = imp
                break

        if found_import:
            module_path = found_import["module"] or ""
            imported_name = found_import["name"]
            # 'from common.domain_models import X' -> 'common.domain_models.X'
            fq_path = f"{module_path}.{imported_name}" if module_path else imported_name
            resolved_selector = store.resolve_selector(fq_path)
            if resolved_selector:
                item = _resolve_selector_to_goto_item(engine, resolved_selector)
                if item:
                    items.append(item)

    # --- 4. No results — include import info so the editor can offer to map ---
    elapsed = (time.monotonic() - t0) * 1000
    result: dict = {"items": items, "elapsed_ms": round(elapsed, 2), "mode": "mapping_goto", "source": "kb"}
    if not items and found_import:
        module = found_import.get("module") or found_import["name"]
        result["unmapped_import"] = {"unmapped_module": module, "identifier": identifier}
    return result


def _module_resolve(engine: EditorSearchEngine, params: dict) -> dict:
    """Resolve a module name to a local path via module mappings.

    Clones the repo via gh if needed.
    """
    from emend.knowledge import module_mapping_to_dict
    t0 = time.monotonic()
    store = _get_store(engine)
    module_name = params.get("module", params.get("query", ""))
    mm = store.resolve_module(module_name)
    if mm is None:
        elapsed = (time.monotonic() - t0) * 1000
        return {"items": [], "elapsed_ms": round(elapsed, 2), "mode": "module_resolve"}

    resolved_path = store.resolve_module_to_path(module_name)
    item = module_mapping_to_dict(mm)
    if resolved_path:
        item["resolved_path"] = resolved_path
    elapsed = (time.monotonic() - t0) * 1000
    return {"items": [item], "elapsed_ms": round(elapsed, 2), "mode": "module_resolve"}


def _module_map_add(engine: EditorSearchEngine, params: dict) -> dict:
    """Add a module mapping (module_prefix → repo or local path).

    If ``local_path`` is provided, verifies the directory exists before saving.
    """
    from emend.knowledge import ModuleMapping

    t0 = time.monotonic()
    module_prefix = params.get("module_prefix", "").strip()
    if not module_prefix:
        return {"error": {"code": -1, "message": "module_prefix is required"}}

    repo = params.get("repo", "").strip()
    local_path = params.get("local_path", "").strip()
    branch = params.get("branch", "").strip()
    subpath = params.get("subpath", "").strip()

    if not repo and not local_path:
        return {"error": {"code": -1, "message": "either repo or local_path is required"}}

    # Validate local_path exists on the filesystem.
    if local_path:
        p = Path(local_path).expanduser()
        if not p.is_dir():
            return {
                "error": {
                    "code": -1,
                    "message": f"directory does not exist: {local_path}",
                }
            }
        local_path = str(p.resolve())

    store = _get_store(engine)
    mapping = ModuleMapping(
        module_prefix=module_prefix,
        repo=repo,
        local_path=local_path,
        branch=branch,
        subpath=subpath,
        provenance="manual",
    )
    store.add_module_mapping(mapping)

    elapsed = (time.monotonic() - t0) * 1000
    return {
        "items": [{"module_prefix": module_prefix, "repo": repo, "local_path": local_path}],
        "elapsed_ms": round(elapsed, 2),
        "mode": "module_map_add",
        "message": f"Mapped {module_prefix} → {repo or local_path}",
    }


def _write_json(obj: dict, stream=None) -> None:
    """Write a JSON object as a single line to *stream* (default stdout)."""
    out = stream or sys.stdout
    out.write(json.dumps(obj, default=str) + "\n")
    out.flush()


def run_editor_server(project_path: str = ".") -> None:
    """Run a newline-delimited JSON-RPC server over stdio.

    Protocol
    --------
    Each request is a single JSON line on stdin::

        {"id": 1, "method": "search", "params": {"query": "foo"}}

    Each response is a single JSON line on stdout::

        {"id": 1, "result": {...}}

    Error responses::

        {"id": 1, "error": {"code": -32603, "message": "..."}}

    Methods
    -------
    - ``search``         — auto-detect mode (symbol/pattern/selector)
    - ``symbols``        — symbol name search
    - ``pattern``        — code pattern search (supports partial input)
    - ``references``     — find references by qualified name
    - ``selector``       — resolve a selector (``file.py::Class.method``)
    - ``file_symbols``   — file outline
    - ``status``         — index status
    - ``reindex``        — refresh stale files + rebuild FTS (blocking)
    - ``reindex_async``  — start background reindex (non-blocking)
    - ``shutdown``       — clean exit

    Notifications (server → client)
    --------------------------------
    - ``indexing_started``   — background reindex has begun
    - ``indexing_complete``  — background reindex finished, results are fresh

    The server automatically starts a background reindex on startup so
    the first search returns results from the existing (possibly stale)
    index while fresh data is being prepared.
    """
    engine = EditorSearchEngine(project_path)

    # Signal readiness
    _write_json({"jsonrpc": "2.0", "method": "ready", "params": {
        "project_root": engine.project_root,
        "db_path": str(engine.db_path),
    }})

    # Auto-start background reindex so searches return immediately
    # from the existing index while it refreshes.
    if engine.db_path.exists():
        started = engine.start_background_reindex()
        if started:
            _write_json({"jsonrpc": "2.0", "method": "indexing_started", "params": {}})

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            # Check if background reindex completed between requests.
            if engine.check_index_complete():
                engine.finalize_reindex()
                _write_json({
                    "jsonrpc": "2.0",
                    "method": "indexing_complete",
                    "params": {},
                })

            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                _write_json({
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                })
                continue

            req_id = request.get("id")
            method = request.get("method", "")
            params = request.get("params", {})

            if method == "shutdown":
                _write_json({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}})
                break

            try:
                result = _dispatch(engine, method, params)
                _write_json({"jsonrpc": "2.0", "id": req_id, "result": result})
            except Exception as exc:
                _write_json({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": str(exc)},
                })

            # Also check after dispatching (the reindex may have
            # completed while we were processing the request).
            if engine.check_index_complete():
                engine.finalize_reindex()
                _write_json({
                    "jsonrpc": "2.0",
                    "method": "indexing_complete",
                    "params": {},
                })
    finally:
        engine.close()
