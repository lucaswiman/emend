"""Fast search interface for VIN (editor) plugins.

Provides a ``VinSearchEngine`` backed by the emend SQLite index
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
4. **Long-running server** (``emend vin-server``) keeps the DB
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
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SymbolHit:
    """A symbol search result."""

    name: str
    qualified_name: str
    kind: str
    file_path: str
    line: int
    end_line: int
    signature: str | None = None
    returns: str | None = None
    depth: int = 1
    parent: str | None = None
    score: float = 0.0


@dataclass
class PatternHit:
    """A pattern / code search result."""

    file_path: str
    line: int
    end_line: int
    col: int
    end_col: int
    matched_text: str
    score: float = 0.0


@dataclass
class ReferenceHit:
    """A reference search result."""

    file_path: str
    line: int
    col: int
    ref_kind: str
    target_qn: str = ""
    score: float = 0.0


@dataclass
class SearchResult:
    """Wrapper returned by all search methods."""

    items: list[dict]
    elapsed_ms: float
    mode: str  # "symbol", "pattern", "selector", "reference", "file_symbols"
    truncated: bool = False
    query: str = ""


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

_IDENT_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])|_")


def _split_identifier(name: str) -> list[str]:
    """Split ``name`` at snake_case and camelCase boundaries."""
    return [s for s in _IDENT_BOUNDARY.split(name) if s]


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
    """(Re)build the ``symbol_fts`` FTS5 index from ``symbol_index``.

    Returns the number of rows indexed (0 if FTS5 is unavailable).
    """
    if not _fts5_available(conn):
        logger.debug("FTS5 trigram not available — skipping FTS build")
        return 0

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
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM symbol_fts").fetchone()[0]
    logger.debug("FTS index rebuilt: %d rows", count)
    return count


# ---------------------------------------------------------------------------
# VinSearchEngine
# ---------------------------------------------------------------------------


class VinSearchEngine:
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
        from emend.transform import _find_project_root

        self.project_root = _find_project_root(project_path)
        self.db_path = Path(self.project_root) / ".emend" / "cache" / "parse.db"
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

        # Check if FTS table exists and has rows.
        try:
            fts_count = conn.execute(
                "SELECT COUNT(*) FROM symbol_fts"
            ).fetchone()[0]
        except Exception:
            fts_count = 0

        if fts_count == 0:
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
    ) -> SearchResult:
        """Auto-detect mode and dispatch."""
        t0 = time.monotonic()

        if "::" in query:
            result = self._search_selector(query, limit=limit)
        elif "$" in query:
            result = self._search_pattern(
                query, limit=limit, file_scope=file_scope
            )
        else:
            result = self._search_symbols(
                query, limit=limit, file_scope=file_scope, kind=kind
            )

        result.elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
        result.query = query
        return result

    # -- symbol search ------------------------------------------------------

    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 50,
        file_scope: str | None = None,
        kind: str | None = None,
    ) -> SearchResult:
        t0 = time.monotonic()
        result = self._search_symbols(
            query, limit=limit, file_scope=file_scope, kind=kind
        )
        result.elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
        result.query = query
        return result

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

        base = (
            "SELECT rowid, name, qualified_name, kind, file_path, "
            "line, end_line, signature, returns, depth, parent "
            "FROM symbol_index"
        )

        def _add(sql: str, params: tuple) -> None:
            try:
                for row in conn.execute(sql, params):
                    rid = row[0]
                    if rid not in candidates:
                        candidates[rid] = {
                            "name": row[1],
                            "qualified_name": row[2],
                            "kind": row[3],
                            "file_path": row[4],
                            "line": row[5],
                            "end_line": row[6],
                            "signature": row[7],
                            "returns": row[8],
                            "depth": row[9],
                            "parent": row[10],
                        }
            except Exception:
                pass

        # Strategy 1: exact name (uses idx_sym_name)
        _add(f"{base} WHERE name = ?", (query,))

        # Strategy 2: case-insensitive exact
        if query != query_lower:
            _add(f"{base} WHERE lower(name) = ? AND name != ?", (query_lower, query))

        # Strategy 3: prefix (uses idx_sym_name B-tree range scan)
        _add(
            f"{base} WHERE name >= ? AND name < ? AND name != ?",
            (query, query[:-1] + chr(ord(query[-1]) + 1) if query else "~", query),
        )

        # Strategy 4: case-insensitive prefix
        _add(
            f"{base} WHERE lower(name) LIKE ? AND lower(name) != ?",
            (query_lower + "%", query_lower),
        )

        # Strategy 5: FTS5 trigram substring (needs ≥ 3 chars)
        if len(query) >= 3 and self._ensure_fts():
            fts_q = '"' + query.replace('"', '""') + '"'
            _add(
                "SELECT s.rowid, s.name, s.qualified_name, s.kind, s.file_path, "
                "s.line, s.end_line, s.signature, s.returns, s.depth, s.parent "
                "FROM symbol_fts f JOIN symbol_index s ON f.rowid = s.rowid "
                "WHERE f.name MATCH ?",
                (fts_q,),
            )

        # Strategy 6: qualified-name search for dotted queries
        if "." in query:
            _add(
                f"{base} WHERE qualified_name LIKE ?",
                ("%" + query + "%",),
            )
            if len(query) >= 3 and self._ensure_fts():
                fts_q = '"' + query.replace('"', '""') + '"'
                _add(
                    "SELECT s.rowid, s.name, s.qualified_name, s.kind, "
                    "s.file_path, s.line, s.end_line, s.signature, s.returns, "
                    "s.depth, s.parent "
                    "FROM symbol_fts f JOIN symbol_index s ON f.rowid = s.rowid "
                    "WHERE f.qualified_name MATCH ?",
                    (fts_q,),
                )

        # Strategy 7: signature substring (for parameter-based search)
        if len(query) >= 3 and self._ensure_fts():
            fts_q = '"' + query.replace('"', '""') + '"'
            _add(
                "SELECT s.rowid, s.name, s.qualified_name, s.kind, "
                "s.file_path, s.line, s.end_line, s.signature, s.returns, "
                "s.depth, s.parent "
                "FROM symbol_fts f JOIN symbol_index s ON f.rowid = s.rowid "
                "WHERE f.signature MATCH ?",
                (fts_q,),
            )

        # Apply post-filters
        items = list(candidates.values())
        if file_scope:
            items = [c for c in items if file_scope in c["file_path"]]
        if kind:
            items = [c for c in items if c["kind"] == kind]

        # Score and rank
        scored = [
            (round(_score_symbol(c["name"], c["qualified_name"], query), 1), c)
            for c in items
        ]
        scored.sort(key=lambda x: (-x[0], len(x[1]["name"])))

        truncated = len(scored) > limit
        top = scored[:limit]

        return SearchResult(
            items=[{**c, "score": s} for s, c in top],
            elapsed_ms=0,
            mode="symbol",
            truncated=truncated,
        )

    # -- pattern search -----------------------------------------------------

    def search_pattern(
        self,
        pattern: str,
        *,
        limit: int = 50,
        file_scope: str | None = None,
    ) -> SearchResult:
        t0 = time.monotonic()
        result = self._search_pattern(pattern, limit=limit, file_scope=file_scope)
        result.elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
        result.query = pattern
        return result

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
        """Run a full pattern search using emend's transform engine."""
        from emend.transform import find_pattern, extract_pattern_literals

        path = file_scope or "."
        items: list[dict] = []
        try:
            for match in find_pattern(pattern, path):
                items.append({
                    "file_path": match.file_path,
                    "line": match.line,
                    "end_line": match.end_line,
                    "col": match.col,
                    "end_col": match.end_col,
                    "matched_text": match.matched_text,
                })
                if len(items) >= limit:
                    break
        except Exception as exc:
            logger.debug("Pattern search failed: %s", exc)

        return SearchResult(
            items=items,
            elapsed_ms=0,
            mode="pattern",
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

    # -- selector resolution ------------------------------------------------

    def resolve_selector(
        self, selector: str, *, limit: int = 50
    ) -> SearchResult:
        t0 = time.monotonic()
        result = self._search_selector(selector, limit=limit)
        result.elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
        result.query = selector
        return result

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
                items.append({
                    "name": row[1],
                    "qualified_name": row[2],
                    "kind": row[3],
                    "file_path": row[4],
                    "line": row[5],
                    "end_line": row[6],
                    "signature": row[7],
                    "returns": row[8],
                    "depth": row[9],
                    "parent": row[10],
                    "score": 1000.0,
                })
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
                items.append({
                    "name": row[0],
                    "qualified_name": row[1],
                    "kind": row[2],
                    "file_path": row[3],
                    "line": row[4],
                    "end_line": row[5],
                    "signature": row[6],
                    "returns": row[7],
                    "depth": row[8],
                    "parent": row[9],
                })
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


# ---------------------------------------------------------------------------
# JSON-RPC server
# ---------------------------------------------------------------------------

_METHODS = {
    "search",
    "symbols",
    "pattern",
    "references",
    "selector",
    "file_symbols",
    "status",
    "reindex",
    "shutdown",
}


def _dispatch(engine: VinSearchEngine, method: str, params: dict) -> dict:
    """Route a JSON-RPC method to the engine."""
    if method == "search":
        return asdict(engine.search(**params))
    elif method == "symbols":
        return asdict(engine.search_symbols(**params))
    elif method == "pattern":
        return asdict(engine.search_pattern(**params))
    elif method == "references":
        return asdict(engine.search_references(**params))
    elif method == "selector":
        sel = params.pop("selector", params.pop("query", ""))
        return asdict(engine.resolve_selector(sel, **params))
    elif method == "file_symbols":
        fp = params.pop("file", params.pop("file_path", ""))
        return asdict(engine.file_symbols(fp, **params))
    elif method == "status":
        return asdict(engine.status())
    elif method == "reindex":
        return asdict(engine.reindex())
    elif method == "shutdown":
        return {"ok": True}
    else:
        raise ValueError(f"Unknown method: {method!r}")


def _write_json(obj: dict, stream=None) -> None:
    """Write a JSON object as a single line to *stream* (default stdout)."""
    out = stream or sys.stdout
    out.write(json.dumps(obj, default=str) + "\n")
    out.flush()


def run_vin_server(project_path: str = ".") -> None:
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
    engine = VinSearchEngine(project_path)

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
