"""Mapping knowledge base for cross-service identifier mappings and notes.

Provides two capabilities:
1. **Identifier mappings** — records that an identifier in one project
   maps to an identifier in another (e.g. ``users.UserService.create``
   → ``POST /api/v1/users`` in the gateway repo).
2. **Knowledge notes** — a free-form, FTS-searchable scratchpad where
   humans or LLMs can record architectural decisions, conventions,
   patterns, or any other information relevant to the codebase.

Both are stored in ``<project>/.emend/cache/knowledge.db`` (SQLite,
WAL mode) and indexed with FTS5 trigram for instant substring search.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .transform import _cache_db_dir

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class IdentifierMapping:
    """A mapping between identifiers across services/repos."""

    source_project: str
    source_identifier: str
    source_kind: str  # function, class, endpoint, model, field, ...
    target_project: str
    target_identifier: str
    target_kind: str
    relationship: str = "equivalent"  # equivalent, calls, implements, produces, consumes
    confidence: float = 1.0  # 0–1, useful for heuristic/LLM-generated
    provenance: str = "manual"  # manual, heuristic, llm
    evidence: str = ""  # human-readable explanation
    metadata: dict[str, Any] = field(default_factory=dict)
    # set by DB
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class KnowledgeNote:
    """A free-form knowledge entry with full-text search support."""

    title: str
    content: str
    category: str = "note"  # note, architecture, convention, mapping, decision, pattern
    tags: str = ""  # comma-separated
    source: str = "user"  # user, llm, heuristic
    project: str = ""  # repo/project scope
    file_path: str = ""  # optional related file
    symbol: str = ""  # optional related symbol
    metadata: dict[str, Any] = field(default_factory=dict)
    # set by DB
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_KNOWLEDGE_SCHEMA_VERSION = "1"

_DDL = """\
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS kb_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Identifier mappings -------------------------------------------------------

CREATE TABLE IF NOT EXISTS identifier_mapping (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_project    TEXT NOT NULL,
    source_identifier TEXT NOT NULL,
    source_kind       TEXT NOT NULL DEFAULT '',
    target_project    TEXT NOT NULL,
    target_identifier TEXT NOT NULL,
    target_kind       TEXT NOT NULL DEFAULT '',
    relationship      TEXT NOT NULL DEFAULT 'equivalent',
    confidence        REAL NOT NULL DEFAULT 1.0,
    provenance        TEXT NOT NULL DEFAULT 'manual',
    evidence          TEXT NOT NULL DEFAULT '',
    metadata_json     TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_map_src
    ON identifier_mapping(source_project, source_identifier);
CREATE INDEX IF NOT EXISTS idx_map_tgt
    ON identifier_mapping(target_project, target_identifier);
CREATE INDEX IF NOT EXISTS idx_map_rel
    ON identifier_mapping(relationship);

-- Knowledge notes -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS knowledge_note (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    content       TEXT NOT NULL,
    category      TEXT NOT NULL DEFAULT 'note',
    tags          TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT 'user',
    project       TEXT NOT NULL DEFAULT '',
    file_path     TEXT NOT NULL DEFAULT '',
    symbol        TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_note_cat ON knowledge_note(category);
CREATE INDEX IF NOT EXISTS idx_note_proj ON knowledge_note(project);
CREATE INDEX IF NOT EXISTS idx_note_file ON knowledge_note(file_path);
CREATE INDEX IF NOT EXISTS idx_note_symbol ON knowledge_note(symbol);
"""

# FTS5 tables live separately so we can rebuild without touching data.
_FTS_DDL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS mapping_fts USING fts5(
    source_identifier, target_identifier, evidence,
    content=identifier_mapping,
    content_rowid=id,
    tokenize='trigram'
);

CREATE VIRTUAL TABLE IF NOT EXISTS note_fts USING fts5(
    title, content, tags, symbol,
    content=knowledge_note,
    content_rowid=id,
    tokenize='trigram'
);
"""

# Triggers keep the FTS indexes in sync with the base tables.
_TRIGGER_DDL = """\
-- mapping triggers
CREATE TRIGGER IF NOT EXISTS mapping_ai AFTER INSERT ON identifier_mapping BEGIN
    INSERT INTO mapping_fts(rowid, source_identifier, target_identifier, evidence)
    VALUES (new.id, new.source_identifier, new.target_identifier, new.evidence);
END;

CREATE TRIGGER IF NOT EXISTS mapping_ad AFTER DELETE ON identifier_mapping BEGIN
    INSERT INTO mapping_fts(mapping_fts, rowid, source_identifier, target_identifier, evidence)
    VALUES ('delete', old.id, old.source_identifier, old.target_identifier, old.evidence);
END;

CREATE TRIGGER IF NOT EXISTS mapping_au AFTER UPDATE ON identifier_mapping BEGIN
    INSERT INTO mapping_fts(mapping_fts, rowid, source_identifier, target_identifier, evidence)
    VALUES ('delete', old.id, old.source_identifier, old.target_identifier, old.evidence);
    INSERT INTO mapping_fts(rowid, source_identifier, target_identifier, evidence)
    VALUES (new.id, new.source_identifier, new.target_identifier, new.evidence);
END;

-- note triggers
CREATE TRIGGER IF NOT EXISTS note_ai AFTER INSERT ON knowledge_note BEGIN
    INSERT INTO note_fts(rowid, title, content, tags, symbol)
    VALUES (new.id, new.title, new.content, new.tags, new.symbol);
END;

CREATE TRIGGER IF NOT EXISTS note_ad AFTER DELETE ON knowledge_note BEGIN
    INSERT INTO note_fts(note_fts, rowid, title, content, tags, symbol)
    VALUES ('delete', old.id, old.title, old.content, old.tags, old.symbol);
END;

CREATE TRIGGER IF NOT EXISTS note_au AFTER UPDATE ON knowledge_note BEGIN
    INSERT INTO note_fts(note_fts, rowid, title, content, tags, symbol)
    VALUES ('delete', old.id, old.title, old.content, old.tags, old.symbol);
    INSERT INTO note_fts(rowid, title, content, tags, symbol)
    VALUES (new.id, new.title, new.content, new.tags, new.symbol);
END;
"""


# ---------------------------------------------------------------------------
# KnowledgeBase
# ---------------------------------------------------------------------------


def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe "
            "USING fts5(x, tokenize='trigram')"
        )
        conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        return True
    except Exception:
        return False


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class KnowledgeBase:
    """Interface to the knowledge DB.

    Usage::

        kb = KnowledgeBase(".")          # project root
        kb.add_note(KnowledgeNote(...))
        results = kb.search_notes("auth")
        kb.close()
    """

    def __init__(self, project_root: str = ".") -> None:
        db_dir = _cache_db_dir(project_root)
        db_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = db_dir / "knowledge.db"
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        conn = self._conn
        conn.executescript(_DDL)

        if _fts5_available(conn):
            conn.executescript(_FTS_DDL)
            conn.executescript(_TRIGGER_DDL)
            self._has_fts = True
        else:
            self._has_fts = False

        # Store schema version.
        conn.execute(
            "INSERT OR REPLACE INTO kb_meta(key, value) VALUES (?, ?)",
            ("schema_version", _KNOWLEDGE_SCHEMA_VERSION),
        )
        conn.commit()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        self._conn.close()

    # -- Identifier mappings -------------------------------------------------

    def add_mapping(self, m: IdentifierMapping) -> int:
        """Insert a mapping, returning its row id."""
        now = _now_iso()
        cur = self._conn.execute(
            "INSERT INTO identifier_mapping "
            "(source_project, source_identifier, source_kind, "
            " target_project, target_identifier, target_kind, "
            " relationship, confidence, provenance, evidence, metadata_json, "
            " created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                m.source_project,
                m.source_identifier,
                m.source_kind,
                m.target_project,
                m.target_identifier,
                m.target_kind,
                m.relationship,
                m.confidence,
                m.provenance,
                m.evidence,
                json.dumps(m.metadata),
                now,
                now,
            ),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_mapping(self, mapping_id: int) -> IdentifierMapping | None:
        row = self._conn.execute(
            "SELECT * FROM identifier_mapping WHERE id = ?", (mapping_id,)
        ).fetchone()
        return self._row_to_mapping(row) if row else None

    def update_mapping(self, mapping_id: int, **kwargs: Any) -> bool:
        """Update fields on an existing mapping. Returns True if found."""
        row = self._conn.execute(
            "SELECT id FROM identifier_mapping WHERE id = ?", (mapping_id,)
        ).fetchone()
        if not row:
            return False
        sets = []
        vals: list[Any] = []
        for col in (
            "source_project", "source_identifier", "source_kind",
            "target_project", "target_identifier", "target_kind",
            "relationship", "confidence", "provenance", "evidence",
        ):
            if col in kwargs:
                sets.append(f"{col} = ?")
                vals.append(kwargs[col])
        if "metadata" in kwargs:
            sets.append("metadata_json = ?")
            vals.append(json.dumps(kwargs["metadata"]))
        if not sets:
            return True
        sets.append("updated_at = ?")
        vals.append(_now_iso())
        vals.append(mapping_id)
        self._conn.execute(
            f"UPDATE identifier_mapping SET {', '.join(sets)} WHERE id = ?", vals
        )
        self._conn.commit()
        return True

    def delete_mapping(self, mapping_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM identifier_mapping WHERE id = ?", (mapping_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def search_mappings(
        self,
        query: str,
        *,
        source_project: str | None = None,
        target_project: str | None = None,
        relationship: str | None = None,
        limit: int = 50,
    ) -> list[IdentifierMapping]:
        """Full-text search over mappings.

        Falls back to LIKE if FTS5 is unavailable.
        """
        if self._has_fts and len(query) >= 3:
            sql = (
                "SELECT m.* FROM identifier_mapping m "
                "JOIN mapping_fts f ON m.id = f.rowid "
                "WHERE mapping_fts MATCH ?"
            )
            params: list[Any] = [_fts_escape(query)]
        else:
            like = f"%{query}%"
            sql = (
                "SELECT m.* FROM identifier_mapping m WHERE "
                "(m.source_identifier LIKE ? OR m.target_identifier LIKE ? OR m.evidence LIKE ?)"
            )
            params = [like, like, like]

        if source_project:
            sql += " AND m.source_project = ?"
            params.append(source_project)
        if target_project:
            sql += " AND m.target_project = ?"
            params.append(target_project)
        if relationship:
            sql += " AND m.relationship = ?"
            params.append(relationship)

        sql += " ORDER BY m.confidence DESC, m.updated_at DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_mapping(r) for r in rows]

    def list_mappings(
        self,
        *,
        source_project: str | None = None,
        target_project: str | None = None,
        relationship: str | None = None,
        limit: int = 100,
    ) -> list[IdentifierMapping]:
        """List mappings with optional filters (no full-text search)."""
        sql = "SELECT * FROM identifier_mapping WHERE 1=1"
        params: list[Any] = []
        if source_project:
            sql += " AND source_project = ?"
            params.append(source_project)
        if target_project:
            sql += " AND target_project = ?"
            params.append(target_project)
        if relationship:
            sql += " AND relationship = ?"
            params.append(relationship)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_mapping(r) for r in rows]

    def find_mappings_for(
        self,
        identifier: str,
        *,
        project: str | None = None,
        direction: str = "both",  # source, target, both
    ) -> list[IdentifierMapping]:
        """Find all mappings where *identifier* appears as source or target."""
        clauses = []
        params: list[Any] = []
        if direction in ("source", "both"):
            if project:
                clauses.append("(source_identifier = ? AND source_project = ?)")
                params.extend([identifier, project])
            else:
                clauses.append("source_identifier = ?")
                params.append(identifier)
        if direction in ("target", "both"):
            if project:
                clauses.append("(target_identifier = ? AND target_project = ?)")
                params.extend([identifier, project])
            else:
                clauses.append("target_identifier = ?")
                params.append(identifier)

        sql = f"SELECT * FROM identifier_mapping WHERE {' OR '.join(clauses)} ORDER BY confidence DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_mapping(r) for r in rows]

    # -- Knowledge notes -----------------------------------------------------

    def add_note(self, n: KnowledgeNote) -> int:
        """Insert a knowledge note, returning its row id."""
        now = _now_iso()
        cur = self._conn.execute(
            "INSERT INTO knowledge_note "
            "(title, content, category, tags, source, project, "
            " file_path, symbol, metadata_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                n.title,
                n.content,
                n.category,
                n.tags,
                n.source,
                n.project,
                n.file_path,
                n.symbol,
                json.dumps(n.metadata),
                now,
                now,
            ),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_note(self, note_id: int) -> KnowledgeNote | None:
        row = self._conn.execute(
            "SELECT * FROM knowledge_note WHERE id = ?", (note_id,)
        ).fetchone()
        return self._row_to_note(row) if row else None

    def update_note(self, note_id: int, **kwargs: Any) -> bool:
        """Update fields on an existing note. Returns True if found."""
        row = self._conn.execute(
            "SELECT id FROM knowledge_note WHERE id = ?", (note_id,)
        ).fetchone()
        if not row:
            return False
        sets = []
        vals: list[Any] = []
        for col in (
            "title", "content", "category", "tags", "source",
            "project", "file_path", "symbol",
        ):
            if col in kwargs:
                sets.append(f"{col} = ?")
                vals.append(kwargs[col])
        if "metadata" in kwargs:
            sets.append("metadata_json = ?")
            vals.append(json.dumps(kwargs["metadata"]))
        if not sets:
            return True
        sets.append("updated_at = ?")
        vals.append(_now_iso())
        vals.append(note_id)
        self._conn.execute(
            f"UPDATE knowledge_note SET {', '.join(sets)} WHERE id = ?", vals
        )
        self._conn.commit()
        return True

    def delete_note(self, note_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM knowledge_note WHERE id = ?", (note_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def search_notes(
        self,
        query: str,
        *,
        category: str | None = None,
        project: str | None = None,
        file_path: str | None = None,
        symbol: str | None = None,
        source: str | None = None,
        limit: int = 50,
    ) -> list[KnowledgeNote]:
        """Full-text search over notes. Falls back to LIKE without FTS5."""
        # Always alias the base table as "n" so filter clauses work
        # regardless of whether the FTS join is present.
        if self._has_fts and len(query) >= 3:
            sql = (
                "SELECT n.* FROM knowledge_note n "
                "JOIN note_fts f ON n.id = f.rowid "
                "WHERE note_fts MATCH ?"
            )
            params: list[Any] = [_fts_escape(query)]
        else:
            like = f"%{query}%"
            sql = (
                "SELECT n.* FROM knowledge_note n WHERE "
                "(n.title LIKE ? OR n.content LIKE ? OR n.tags LIKE ? OR n.symbol LIKE ?)"
            )
            params = [like, like, like, like]

        if category:
            sql += " AND n.category = ?"
            params.append(category)
        if project:
            sql += " AND n.project = ?"
            params.append(project)
        if file_path:
            sql += " AND n.file_path = ?"
            params.append(file_path)
        if symbol:
            sql += " AND n.symbol = ?"
            params.append(symbol)
        if source:
            sql += " AND n.source = ?"
            params.append(source)

        sql += " ORDER BY n.updated_at DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_note(r) for r in rows]

    def list_notes(
        self,
        *,
        category: str | None = None,
        project: str | None = None,
        limit: int = 100,
    ) -> list[KnowledgeNote]:
        """List notes with optional filters."""
        sql = "SELECT * FROM knowledge_note WHERE 1=1"
        params: list[Any] = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        if project:
            sql += " AND project = ?"
            params.append(project)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_note(r) for r in rows]

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _row_to_mapping(row: sqlite3.Row) -> IdentifierMapping:
        return IdentifierMapping(
            id=row["id"],
            source_project=row["source_project"],
            source_identifier=row["source_identifier"],
            source_kind=row["source_kind"],
            target_project=row["target_project"],
            target_identifier=row["target_identifier"],
            target_kind=row["target_kind"],
            relationship=row["relationship"],
            confidence=row["confidence"],
            provenance=row["provenance"],
            evidence=row["evidence"],
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_note(row: sqlite3.Row) -> KnowledgeNote:
        return KnowledgeNote(
            id=row["id"],
            title=row["title"],
            content=row["content"],
            category=row["category"],
            tags=row["tags"],
            source=row["source"],
            project=row["project"],
            file_path=row["file_path"],
            symbol=row["symbol"],
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _fts_escape(query: str) -> str:
    """Escape a query for FTS5 trigram matching.

    FTS5 trigram tokenizer uses literal substring matching,
    so we just need to quote the string.
    """
    escaped = query.replace('"', '""')
    return f'"{escaped}"'


# ---------------------------------------------------------------------------
# Serialization helpers (for CLI / MCP JSON output)
# ---------------------------------------------------------------------------


def mapping_to_dict(m: IdentifierMapping) -> dict[str, Any]:
    d = asdict(m)
    # Remove None id for unsaved objects
    if d["id"] is None:
        del d["id"]
    return d


def note_to_dict(n: KnowledgeNote) -> dict[str, Any]:
    d = asdict(n)
    if d["id"] is None:
        del d["id"]
    return d
