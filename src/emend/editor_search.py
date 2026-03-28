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

import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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

    def to_dict(self) -> dict:
        """Lightweight serialization (avoids ``dataclasses.asdict`` deep-copy)."""
        return {
            "items": self.items,
            "elapsed_ms": self.elapsed_ms,
            "mode": self.mode,
            "truncated": self.truncated,
            "query": self.query,
        }


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

    # -- connection ---------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
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
            sym_count = conn.execute(
                "SELECT COUNT(*) FROM symbol_index"
            ).fetchone()[0]
            if sym_count > 0:
                rebuild_fts(conn)

        self._fts_ready = True
        return True

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
        elif "::" in query:
            result = self._search_selector(query, limit=limit)
        elif "$" in query:
            result = self._search_pattern(
                query, limit=limit, file_scope=file_scope
            )
        elif re.match(r'\s*(?:async\s+)?(?:def|class)\s+\w*[*?]', query):
            result = self._search_pattern(
                query, limit=limit, file_scope=file_scope
            )
        elif "/" in query or any(query.endswith(ext) for ext in (".py", ".ts", ".js", ".rs", ".go", ".c", ".cpp", ".h")):
            # Prioritize file search for path-like queries
            result = self._search_files(query, limit=limit)
            if not result.items:
                result = self._search_symbols(
                    query, limit=limit, file_scope=file_scope, kind=kind
                )
        else:
            result = self._search_symbols(
                query, limit=limit, file_scope=file_scope, kind=kind
            )

        result.elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
        result.query = query
        return result

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
        )

    def _search_files(self, query: str, limit: int = 50) -> SearchResult:
        """Search for files matching the query."""
        conn = self._get_conn()
        q_lower = query.lower()
        
        candidates: set[str] = set()
        
        # Strategy 1: exact basename or substring
        # Using DISTINCT file_path from symbol_index
        base_sql = "SELECT DISTINCT file_path FROM symbol_index"
        
        # FTS5 pre-filter
        fts_ok = len(query) >= 3 and self._ensure_fts()
        if fts_ok:
            fts_q = '"' + query.replace('"', '""') + '"'
            sql = "SELECT file_path FROM file_fts WHERE file_path MATCH ? LIMIT 200"
            candidates.update(r[0] for r in conn.execute(sql, (fts_q,)))
        else:
            # Fallback for short queries
            sql = f"{base_sql} WHERE lower(file_path) LIKE ? LIMIT 200"
            candidates.update(r[0] for r in conn.execute(sql, ("%" + q_lower + "%",)))

        # Fuzzy subsequence fallback if candidates are few
        if len(candidates) < limit:
            # This is expensive on large indexes, but DISTINCT file_path is usually small enough
            all_files = [r[0] for r in conn.execute(base_sql)]
            for fp in all_files:
                if is_fuzzy_subsequence(query, fp):
                    candidates.add(fp)
                    if len(candidates) >= 200:
                        break

        items = []
        for fp in candidates:
            score = _score_file(fp, query)
            if score > 0:
                items.append({
                    "kind": "file",
                    "name": Path(fp).name,
                    "file_path": fp,
                    "line": 1,
                    "score": score,
                })
        
        items.sort(key=lambda x: -x["score"])
        return SearchResult(items=items[:limit], elapsed_ms=0, mode="symbol")

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
        """Fallback for goto_local: resolve DSL symbols at the cursor line."""
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

    def file_symbols(
        self, file_path: str, *, limit: int = 500
    ) -> SearchResult:
        t0 = time.monotonic()
        conn = self._get_conn()

        # Resolve to absolute path for matching
        resolved = str(Path(file_path).resolve())

        items: list[dict] = []
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

    def goto_local(self, file: str, line: int, col: int) -> SearchResult:
        """Find the definition of the symbol at the given position.

        Uses the scope resolver to trace the reference back to its binding site.
        Works for local variables, parameters, loop variables, etc.
        """
        from emend.transform import _rust
        import time as _time
        t0 = _time.monotonic()

        logger.debug(f"goto_local: file={file}, line={line}, col={col}")

        file_path = Path(file).resolve()
        if not file_path.exists():
            logger.debug(f"goto_local: file not found: {file_path}")
            return SearchResult(items=[], elapsed_ms=0, mode="symbol")

        # Parse with scope resolver
        try:
            ext = file_path.suffix.lstrip('.')
            resolver = _rust.PyScopeResolver(str(self.project_root), extension=ext)
            with open(file_path, "r") as f:
                content = f.read()
            resolver.index_file(str(file_path), content)
            refs = resolver.references_in_file(str(file_path))
            logger.debug(f"goto_local: found {len(refs)} references in file")

            # Also get bindings (for parameters and other definitions)
            bindings = []
            try:
                scopes = resolver.scopes_in_file(str(file_path))
                for scope_kind, scope_start, scope_end, scope_bindings in scopes:
                    for b_name, b_kind, b_line, b_col in scope_bindings:
                        # scopes_in_file returns 0-based line numbers, convert to 1-based
                        binding_line_1based = b_line + 1
                        bindings.append((f"{b_name}", binding_line_1based, b_col, b_kind))
                logger.debug(f"goto_local: found {len(bindings)} bindings in scopes")
            except Exception as e:
                logger.debug(f"goto_local: error getting bindings: {e}")
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
                    logger.debug(f"goto_local: empty line or cursor at start, skipping identifier extraction")
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
                            logger.debug(f"goto_local: cursor not on identifier, skipping reference search")

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
                        logger.debug(f"goto_local: extracted identifier='{identifier}' from cursor at col={col}")

                        # Find the reference with matching identifier (last component of QN)
                        for qn, r_line, r_col, r_offset, r_end_offset, r_kind in refs:
                            if r_line == line:
                                qn_parts = qn.split(qn_sep)
                                qn_last = qn_parts[-1]

                                if qn_last == identifier:
                                    logger.debug(f"goto_local: MATCH found target_qn={qn}")
                                    target_qn = qn
                                    break

                        # If no reference found, check bindings (for parameters, etc.)
                        if not target_qn:
                            # First try exact line match
                            for b_name, b_line, b_col, b_kind in bindings:
                                if b_line == line and b_name == identifier:
                                    logger.debug(f"goto_local: MATCH found binding {b_name} at line {b_line}")
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
                                    logger.debug(f"goto_local: MATCH found binding {b_name} in parent scope at line {b_line}")
                                    target_qn = b_name

        if not target_qn:
            logger.debug(f"goto_local: no target_qn found at line={line}, col={col}, trying DSL fallback")
            # DSL fallback: if cursor is inside an embedded DSL region (e.g. SQL
            # string), resolve table/column names to host-language definitions.
            dsl_result = self._goto_dsl_fallback(str(file_path), line)
            if dsl_result.items:
                dsl_result.elapsed_ms = round((_time.monotonic() - t0) * 1000, 2)
                return dsl_result
            return SearchResult(items=[], elapsed_ms=0, mode="symbol")

        # 1. Local definition in the same file
        local_defs = []
        all_refs = []
        resolved_qn = target_qn
        for qn, r_line, r_col, r_offset, r_end_offset, r_kind in refs:
            # Match by exact QN, or by suffix when target_qn is a bare name from bindings
            if qn == target_qn or (qn_sep not in target_qn and qn.endswith(qn_sep + target_qn)):
                resolved_qn = qn  # upgrade to fully-qualified name
                all_refs.append((r_line, r_col))
                if r_kind in ("definition", "write"):
                    local_defs.append((r_line, r_col))

        if not local_defs and all_refs:
            # Fallback: use the first occurrence in the file
            all_refs.sort()
            local_defs = [all_refs[0]]

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
            res.elapsed_ms = round((_time.monotonic() - t0) * 1000, 2)
            return res

        # 2. Cross-file definition: resolve target_qn via symbol index
        # This handles module-level symbols and imported names.
        return self._search_symbols(target_qn, limit=1)

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

    def rename_preview(self, qualified_name: str, new_name: str, file: str = "") -> SearchResult:
        """Dry-run rename, return list of changes."""
        from emend.transform import rename_symbol
        from emend.component_selector import ExtendedSelector
        import time as _time
        t0 = _time.monotonic()

        selector = ExtendedSelector(file_path=file, symbol_path=qualified_name.split("."))
        diffs = rename_symbol(selector, new_name, project_path=str(self.project_root), apply=False)

        items = []
        for fp, diff in diffs.items():
            items.append({
                "file_path": fp,
                "diff": diff,
            })

        elapsed = round((_time.monotonic() - t0) * 1000, 2)
        return SearchResult(items=items, elapsed_ms=elapsed, mode="symbol", query=f"rename {qualified_name}")

    def rename_apply(self, qualified_name: str, new_name: str, file: str = "") -> SearchResult:
        """Apply rename across the project."""
        from emend.transform import rename_symbol
        from emend.component_selector import ExtendedSelector
        import time as _time
        t0 = _time.monotonic()

        selector = ExtendedSelector(file_path=file, symbol_path=qualified_name.split("."))
        diffs = rename_symbol(selector, new_name, project_path=str(self.project_root), apply=True)

        items = []
        for fp, diff in diffs.items():
            items.append({
                "file_path": fp,
                "diff": diff,
            })

        elapsed = round((_time.monotonic() - t0) * 1000, 2)
        return SearchResult(items=items, elapsed_ms=elapsed, mode="symbol", query=f"rename {qualified_name}")

    def replace_preview(self, pattern: str, replacement: str, file: str = "", inside: str | None = None, not_inside: str | None = None) -> SearchResult:
        """Dry-run pattern replace, return diffs."""
        from emend.transform import replace_pattern
        from emend.cli import resolve_files
        import time as _time
        t0 = _time.monotonic()

        items: list[dict] = []
        search_path = file if file else str(self.project_root)
        files, is_multi = resolve_files(search_path)
        file_strs = [str(f) for f in files]

        total_count = 0
        for fp in file_strs:
            try:
                diff, count = replace_pattern(
                    pattern, replacement, fp,
                    inside=inside, not_inside=not_inside,
                    apply=False,
                )
                if count > 0:
                    items.append({"file_path": fp, "diff": diff, "count": count})
                    total_count += count
            except Exception:
                continue

        elapsed = round((_time.monotonic() - t0) * 1000, 2)
        return SearchResult(items=items, elapsed_ms=elapsed, mode="replace_preview", query=f"{pattern} -> {replacement}")

    def replace_apply(self, pattern: str, replacement: str, file: str = "", inside: str | None = None, not_inside: str | None = None) -> SearchResult:
        """Apply pattern replace, return diffs of applied changes."""
        from emend.transform import replace_pattern
        from emend.cli import resolve_files
        import time as _time
        t0 = _time.monotonic()

        items: list[dict] = []
        search_path = file if file else str(self.project_root)
        files, is_multi = resolve_files(search_path)
        file_strs = [str(f) for f in files]

        total_count = 0
        for fp in file_strs:
            try:
                diff, count = replace_pattern(
                    pattern, replacement, fp,
                    inside=inside, not_inside=not_inside,
                    apply=True,
                )
                if count > 0:
                    items.append({"file_path": fp, "diff": diff, "count": count})
                    total_count += count
            except Exception:
                continue

        elapsed = round((_time.monotonic() - t0) * 1000, 2)
        return SearchResult(items=items, elapsed_ms=elapsed, mode="replace_apply", query=f"{pattern} -> {replacement}")

    def move_preview(self, qualified_name: str, dest_file: str, file: str = "") -> SearchResult:
        """Dry-run move, return diffs."""
        from emend.transform import move_symbol
        from emend.component_selector import ExtendedSelector
        import time as _time
        t0 = _time.monotonic()

        selector = ExtendedSelector(file_path=file, symbol_path=qualified_name.split("."))
        diffs = move_symbol(selector, dest_file, project_path=str(self.project_root), apply=False)

        items: list[dict] = []
        for fp, diff in diffs.items():
            items.append({"file_path": fp, "diff": diff})

        elapsed = round((_time.monotonic() - t0) * 1000, 2)
        return SearchResult(items=items, elapsed_ms=elapsed, mode="move_preview", query=f"move {qualified_name}")

    def move_apply(self, qualified_name: str, dest_file: str, file: str = "") -> SearchResult:
        """Apply move across the project."""
        from emend.transform import move_symbol
        from emend.component_selector import ExtendedSelector
        import time as _time
        t0 = _time.monotonic()

        selector = ExtendedSelector(file_path=file, symbol_path=qualified_name.split("."))
        diffs = move_symbol(selector, dest_file, project_path=str(self.project_root), apply=True)

        items: list[dict] = []
        for fp, diff in diffs.items():
            items.append({"file_path": fp, "diff": diff})

        elapsed = round((_time.monotonic() - t0) * 1000, 2)
        return SearchResult(items=items, elapsed_ms=elapsed, mode="move_apply", query=f"move {qualified_name}")

    def callers(self, qualified_name: str, file: str = "", limit: int = 50) -> SearchResult:
        """Find call sites for a symbol."""
        from emend.transform import find_callers
        from emend.component_selector import ExtendedSelector
        import time as _time
        t0 = _time.monotonic()

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

        elapsed = round((_time.monotonic() - t0) * 1000, 2)
        return SearchResult(items=items, elapsed_ms=elapsed, mode="callers", query=f"callers of {qualified_name}")

    def callees(self, qualified_name: str, file: str = "", limit: int = 50) -> SearchResult:
        """Find functions called by a symbol."""
        from emend.transform import find_callees
        from emend.component_selector import ExtendedSelector
        import time as _time
        t0 = _time.monotonic()

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

        elapsed = round((_time.monotonic() - t0) * 1000, 2)
        return SearchResult(items=items, elapsed_ms=elapsed, mode="callees", query=f"callees of {qualified_name}")

    def types_at_cursor(self, file: str, line: int = 0, col: int = 0) -> SearchResult:
        """Return type information for the symbol at cursor position."""
        import time as _time
        t0 = _time.monotonic()

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

        elapsed = round((_time.monotonic() - t0) * 1000, 2)
        return SearchResult(items=items, elapsed_ms=elapsed, mode="types", query=f"types at {file}:{line}")

    def complete(self, prefix: str, limit: int = 20, file: str = "", line: int = 0, col: int = 0) -> SearchResult:
        """Return completion candidates for the given prefix.

        When *file* is provided, imported names in that file are included
        as candidates (union with the project symbol index).

        When *line* and *col* are provided, local variables from the
        enclosing scopes are ranked first.
        """
        conn = self._get_conn()
        seen: set[str] = set()
        items: list[dict] = []

        # 1. Local variables from PyScopeResolver
        local_names: set[str] = set()
        if file and line > 0:
            try:
                from emend.transform import _rust
                resolver = _rust.PyScopeResolver(str(self.project_root))
                source = Path(file).read_text()
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
                
                enclosing_scopes.sort(key=lambda x: x[0]) # innermost first
                
                for _, bindings in enclosing_scopes:
                    for b_name, b_kind, b_line, b_col in bindings:
                        # Only include if it matches prefix
                        if not b_name.startswith(prefix.split(".")[-1]):
                            continue
                        if b_name not in seen:
                            seen.add(b_name)
                            local_names.add(b_name)
                            # Rank local variables higher than parameters
                            score = 2000 if b_kind.lower() != "parameter" else 1800
                            items.append({
                                "word": b_name,
                                "kind": b_kind.lower(),
                                "menu": "[local]",
                                "score": score,
                            })
            except Exception as exc:
                logger.debug("Local completion failed: %s", exc)

        # Collect imported names from the current file for local completions.
        import_names: dict[str, str] = {}  # local_name -> qualified source
        if file:
            import_names = self._extract_import_names(file)

        if "." in prefix:
            # Dotted prefix: e.g. "Foo.bar" -> search qualified names
            parts = prefix.rsplit(".", 1)
            parent = parts[0]
            member_prefix = parts[1] if len(parts) > 1 else ""

            # Resolve parent through imports (e.g. DocumentFilter -> document_api...DocumentFilter)
            resolved_parent = import_names.get(parent, parent)

            # Search local project symbol index
            parents_to_check = [resolved_parent, parent]
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

        return SearchResult(items=items[:limit], elapsed_ms=0, mode="complete", query=prefix)

    @staticmethod
    def _extract_import_names(file: str) -> dict[str, str]:
        """Parse imports from a Python file, returning {local_name: qualified_source}."""
        import ast as _ast

        try:
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
        return engine.file_symbols(fp, **params).to_dict()
    elif method == "status":
        return engine.status().to_dict()
    elif method == "reindex":
        return engine.reindex().to_dict()
    elif method == "goto_local":
        file = params.pop("file", "")
        line = int(params.pop("line", 1))
        col = int(params.pop("col", 0))
        return engine.goto_local(file, line, col).to_dict()
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
    # -- Mapping methods --
    elif method == "mapping_lookup":
        return _mapping_lookup(engine, params)
    elif method == "mapping_goto":
        # First try goto_local if file/line/col are provided
        if "file" in params and "line" in params:
            file = params.get("file", "")
            line = int(params.get("line", 1))
            col = int(params.get("col", 0))
            logger.debug(f"mapping_goto: trying goto_local(file={file!r}, line={line}, col={col})")
            res = engine.goto_local(file, line, col)
            logger.debug(f"mapping_goto: goto_local returned {len(res.items)} items")
            if res.items:
                return res.to_dict()
        logger.debug(f"mapping_goto: falling back to _mapping_goto")
        return _mapping_goto(engine, params)
    elif method == "module_resolve":
        return _module_resolve(engine, params)
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
    import time as _time
    from emend.knowledge import mapping_to_dict
    t0 = _time.monotonic()
    store = _get_store(engine)
    identifier = params.get("identifier", params.get("query", ""))
    project = params.get("project")
    direction = params.get("direction", "both")
    results = store.find_mappings_for(identifier, project=project, direction=direction)
    items = [mapping_to_dict(m) for m in results]
    elapsed = (_time.monotonic() - t0) * 1000
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
    import time as _time
    t0 = _time.monotonic()
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
        elapsed = (_time.monotonic() - t0) * 1000
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
    if not items and params.get("file"):
        from emend.ast_utils import get_imports
        file_path = params["file"]
        imports = get_imports(file_path)

        # Filter imports for the target identifier
        found_import = None
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

    elapsed = (_time.monotonic() - t0) * 1000
    return {"items": items, "elapsed_ms": round(elapsed, 2), "mode": "mapping_goto", "source": "kb"}


def _module_resolve(engine: EditorSearchEngine, params: dict) -> dict:
    """Resolve a module name to a local path via module mappings.

    Clones the repo via gh if needed.
    """
    import time as _time
    from emend.knowledge import module_mapping_to_dict
    t0 = _time.monotonic()
    store = _get_store(engine)
    module_name = params.get("module", params.get("query", ""))
    mm = store.resolve_module(module_name)
    if mm is None:
        elapsed = (_time.monotonic() - t0) * 1000
        return {"items": [], "elapsed_ms": round(elapsed, 2), "mode": "module_resolve"}

    resolved_path = store.resolve_module_to_path(module_name)
    item = module_mapping_to_dict(mm)
    if resolved_path:
        item["resolved_path"] = resolved_path
    elapsed = (_time.monotonic() - t0) * 1000
    return {"items": [item], "elapsed_ms": round(elapsed, 2), "mode": "module_resolve"}


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
    - ``reindex``        — refresh stale files + rebuild FTS
    - ``shutdown``       — clean exit
    """
    engine = EditorSearchEngine(project_path)

    # Signal readiness
    _write_json({"jsonrpc": "2.0", "method": "ready", "params": {
        "project_root": engine.project_root,
        "db_path": str(engine.db_path),
    }})

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

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
    finally:
        engine.close()
