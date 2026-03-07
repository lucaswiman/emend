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
    # -- Knowledge base methods --
    elif method == "kb_search":
        return _kb_search(engine, params)
    elif method == "kb_add":
        return _kb_add(engine, params)
    elif method == "mapping_lookup":
        return _mapping_lookup(engine, params)
    elif method == "mapping_goto":
        return _mapping_goto(engine, params)
    elif method == "module_resolve":
        return _module_resolve(engine, params)
    else:
        raise ValueError(f"Unknown method: {method!r}")


# -- Knowledge base RPC handlers --


def _get_kb(engine: EditorSearchEngine):
    """Lazy-init a KnowledgeBase on the engine."""
    from emend.knowledge import KnowledgeBase
    if not hasattr(engine, '_kb'):
        engine._kb = KnowledgeBase(engine.project_root)  # type: ignore[attr-defined]
    return engine._kb  # type: ignore[attr-defined]


def _kb_search(engine: EditorSearchEngine, params: dict) -> dict:
    """Search notes and return results in the standard items format."""
    import time as _time
    from emend.knowledge import note_to_dict
    t0 = _time.monotonic()
    kb = _get_kb(engine)
    query = params.get("query", "")
    results = kb.search_notes(
        query,
        category=params.get("category"),
        project=params.get("project"),
        file_path=params.get("file_path"),
        symbol=params.get("symbol"),
        limit=params.get("limit", 50),
    )
    items = [note_to_dict(n) for n in results]
    elapsed = (_time.monotonic() - t0) * 1000
    return {"items": items, "elapsed_ms": round(elapsed, 2), "mode": "kb_search"}


def _kb_add(engine: EditorSearchEngine, params: dict) -> dict:
    """Add a knowledge note via RPC."""
    from emend.knowledge import KnowledgeNote, note_to_dict
    kb = _get_kb(engine)
    note = KnowledgeNote(
        title=params.get("title", ""),
        content=params.get("content", ""),
        category=params.get("category", "note"),
        tags=params.get("tags", ""),
        source=params.get("source", "user"),
        project=params.get("project", ""),
        file_path=params.get("file_path", ""),
        symbol=params.get("symbol", ""),
    )
    nid = kb.add_note(note)
    saved = kb.get_note(nid)
    return {"item": note_to_dict(saved), "mode": "kb_add"}  # type: ignore[arg-type]


def _mapping_lookup(engine: EditorSearchEngine, params: dict) -> dict:
    """Look up identifier mappings for a symbol."""
    import time as _time
    from emend.knowledge import mapping_to_dict
    t0 = _time.monotonic()
    kb = _get_kb(engine)
    identifier = params.get("identifier", params.get("query", ""))
    project = params.get("project")
    direction = params.get("direction", "both")
    results = kb.find_mappings_for(identifier, project=project, direction=direction)
    items = [mapping_to_dict(m) for m in results]
    elapsed = (_time.monotonic() - t0) * 1000
    return {"items": items, "elapsed_ms": round(elapsed, 2), "mode": "mapping_lookup"}


def _mapping_goto(engine: EditorSearchEngine, params: dict) -> dict:
    """Go to definition: try local symbol lookup first, then KB mappings.

    1. Search the local project index for the identifier (most common case).
    2. If no local results, check the KB identifier_mapping table and resolve
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

    # --- 2. KB cross-service mapping lookup (fallback) ---
    from emend.knowledge import mapping_to_dict
    kb = _get_kb(engine)
    results = kb.find_mappings_for(identifier, direction="source")
    items = []
    for m in results:
        entry = mapping_to_dict(m)
        # Try to resolve the target identifier to a local path.
        resolved = kb.resolve_module_to_path(m.target_identifier)
        if resolved:
            entry["resolved_path"] = resolved
        items.append(entry)

    elapsed = (_time.monotonic() - t0) * 1000
    return {"items": items, "elapsed_ms": round(elapsed, 2), "mode": "mapping_goto", "source": "kb"}


def _module_resolve(engine: EditorSearchEngine, params: dict) -> dict:
    """Resolve a module name to a local path via module mappings.

    Clones the repo via gh if needed.
    """
    import time as _time
    from emend.knowledge import module_mapping_to_dict
    t0 = _time.monotonic()
    kb = _get_kb(engine)
    module_name = params.get("module", params.get("query", ""))
    mm = kb.resolve_module(module_name)
    if mm is None:
        elapsed = (_time.monotonic() - t0) * 1000
        return {"items": [], "elapsed_ms": round(elapsed, 2), "mode": "module_resolve"}

    resolved_path = kb.resolve_module_to_path(module_name)
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
