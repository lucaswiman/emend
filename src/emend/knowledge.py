"""Mapping knowledge base for cross-service identifier mappings and notes.

Provides two capabilities:
1. **Identifier mappings** — records that an identifier in one project
   maps to an identifier in another (e.g. ``users.UserService.create``
   → ``POST /api/v1/users`` in the gateway repo).
2. **Knowledge notes** — a free-form, FTS-searchable scratchpad where
   humans or LLMs can record architectural decisions, conventions,
   patterns, or any other information relevant to the codebase.

Both are stored in ``<project>/.emend/knowledge.db`` (SQLite,
WAL mode) and indexed with FTS5 trigram for instant substring search.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field, asdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from .transform import _cache_db_dir, _knowledge_db_dir

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


@dataclass
class ModuleMapping:
    """A coarse mapping from a Python module prefix to an external repo/directory.

    Examples::

        # "anything under payments.* lives in the payments repo"
        ModuleMapping(module_prefix="payments", repo="org/payments-service")

        # "utils.* lives in this local directory"
        ModuleMapping(module_prefix="utils", local_path="/home/user/shared-utils")
    """

    module_prefix: str  # e.g. "payments", "users.models"
    repo: str = ""  # GitHub repo (org/name), cloned on demand via gh
    local_path: str = ""  # alternative: a local directory
    branch: str = ""  # optional branch/tag for gh clone
    subpath: str = ""  # subdirectory within the repo (e.g. "src/payments")
    provenance: str = "manual"  # manual, llm, heuristic
    metadata: dict[str, Any] = field(default_factory=dict)
    # set by DB
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_KNOWLEDGE_SCHEMA_VERSION = "2"

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
    deleted           INTEGER NOT NULL DEFAULT 0,
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
    deleted       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_note_cat ON knowledge_note(category);
CREATE INDEX IF NOT EXISTS idx_note_proj ON knowledge_note(project);
CREATE INDEX IF NOT EXISTS idx_note_file ON knowledge_note(file_path);
CREATE INDEX IF NOT EXISTS idx_note_symbol ON knowledge_note(symbol);

-- Module mappings (coarse: module prefix -> repo/directory) -----------------

CREATE TABLE IF NOT EXISTS module_mapping (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    module_prefix TEXT NOT NULL UNIQUE,
    repo          TEXT NOT NULL DEFAULT '',
    local_path    TEXT NOT NULL DEFAULT '',
    branch        TEXT NOT NULL DEFAULT '',
    subpath       TEXT NOT NULL DEFAULT '',
    provenance    TEXT NOT NULL DEFAULT 'manual',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    deleted       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_modmap_prefix ON module_mapping(module_prefix);
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
        db_dir = _knowledge_db_dir(project_root)
        db_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = db_dir / "knowledge.db"

        # Migrate from old location (.emend/cache/knowledge.db) if needed.
        if not self._db_path.exists():
            old_path = _cache_db_dir(project_root) / "knowledge.db"
            if old_path.exists():
                import shutil
                shutil.move(str(old_path), str(self._db_path))
                # Also move WAL/SHM sidecar files if present.
                for suffix in ("-wal", "-shm"):
                    old_sidecar = old_path.with_name(old_path.name + suffix)
                    if old_sidecar.exists():
                        shutil.move(
                            str(old_sidecar),
                            str(self._db_path.with_name(self._db_path.name + suffix)),
                        )

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
            "SELECT * FROM identifier_mapping WHERE id = ? AND deleted = 0", (mapping_id,)
        ).fetchone()
        return self._row_to_mapping(row) if row else None

    def update_mapping(self, mapping_id: int, **kwargs: Any) -> bool:
        """Update fields on an existing mapping. Returns True if found."""
        row = self._conn.execute(
            "SELECT id FROM identifier_mapping WHERE id = ? AND deleted = 0", (mapping_id,)
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
            "UPDATE identifier_mapping SET deleted = 1, updated_at = ? WHERE id = ? AND deleted = 0",
            (_now_iso(), mapping_id),
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
                "WHERE mapping_fts MATCH ? AND m.deleted = 0"
            )
            params: list[Any] = [_fts_escape(query)]
        else:
            like = f"%{query}%"
            sql = (
                "SELECT m.* FROM identifier_mapping m WHERE m.deleted = 0 AND "
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
        sql = "SELECT * FROM identifier_mapping WHERE deleted = 0"
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

        sql = f"SELECT * FROM identifier_mapping WHERE deleted = 0 AND ({' OR '.join(clauses)}) ORDER BY confidence DESC"
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
            "SELECT * FROM knowledge_note WHERE id = ? AND deleted = 0", (note_id,)
        ).fetchone()
        return self._row_to_note(row) if row else None

    def update_note(self, note_id: int, **kwargs: Any) -> bool:
        """Update fields on an existing note. Returns True if found."""
        row = self._conn.execute(
            "SELECT id FROM knowledge_note WHERE id = ? AND deleted = 0", (note_id,)
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
            "UPDATE knowledge_note SET deleted = 1, updated_at = ? WHERE id = ? AND deleted = 0",
            (_now_iso(), note_id),
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
                "WHERE note_fts MATCH ? AND n.deleted = 0"
            )
            params: list[Any] = [_fts_escape(query)]
        else:
            like = f"%{query}%"
            sql = (
                "SELECT n.* FROM knowledge_note n WHERE n.deleted = 0 AND "
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
        sql = "SELECT * FROM knowledge_note WHERE deleted = 0"
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

    def list_tags(self) -> list[str]:
        """Return all distinct tags across non-deleted notes."""
        rows = self._conn.execute(
            "SELECT DISTINCT tags FROM knowledge_note WHERE deleted = 0 AND tags != ''"
        ).fetchall()
        seen: set[str] = set()
        for row in rows:
            for tag in row["tags"].split(","):
                tag = tag.strip()
                if tag:
                    seen.add(tag)
        return sorted(seen)

    # -- Module mappings -----------------------------------------------------

    def add_module_mapping(self, m: ModuleMapping) -> int:
        """Insert a module mapping, returning its row id.

        If a soft-deleted row with the same ``module_prefix`` exists, it is
        undeleted and updated with the new values instead of inserting a new
        row (avoids UNIQUE constraint failure).
        """
        now = _now_iso()

        # Check for a soft-deleted row with the same prefix.
        existing = self._conn.execute(
            "SELECT id FROM module_mapping WHERE module_prefix = ? AND deleted = 1",
            (m.module_prefix,),
        ).fetchone()
        if existing:
            row_id = existing["id"]
            self._conn.execute(
                "UPDATE module_mapping SET "
                "repo = ?, local_path = ?, branch = ?, subpath = ?, "
                "provenance = ?, metadata_json = ?, deleted = 0, updated_at = ? "
                "WHERE id = ?",
                (
                    m.repo, m.local_path, m.branch, m.subpath,
                    m.provenance, json.dumps(m.metadata), now, row_id,
                ),
            )
            self._conn.commit()
            return row_id

        cur = self._conn.execute(
            "INSERT INTO module_mapping "
            "(module_prefix, repo, local_path, branch, subpath, "
            " provenance, metadata_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                m.module_prefix,
                m.repo,
                m.local_path,
                m.branch,
                m.subpath,
                m.provenance,
                json.dumps(m.metadata),
                now,
                now,
            ),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_module_mapping(self, mapping_id: int) -> ModuleMapping | None:
        row = self._conn.execute(
            "SELECT * FROM module_mapping WHERE id = ? AND deleted = 0", (mapping_id,)
        ).fetchone()
        return self._row_to_module_mapping(row) if row else None

    def get_module_mapping_by_prefix(self, prefix: str) -> ModuleMapping | None:
        """Look up a module mapping by its exact prefix string."""
        row = self._conn.execute(
            "SELECT * FROM module_mapping WHERE module_prefix = ? AND deleted = 0",
            (prefix,),
        ).fetchone()
        return self._row_to_module_mapping(row) if row else None

    def delete_module_mapping_by_prefix(self, prefix: str) -> bool:
        """Soft-delete a module mapping identified by its prefix string."""
        cur = self._conn.execute(
            "UPDATE module_mapping SET deleted = 1, updated_at = ? "
            "WHERE module_prefix = ? AND deleted = 0",
            (_now_iso(), prefix),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def update_module_mapping(self, mapping_id: int, **kwargs: Any) -> bool:
        """Update fields on an existing module mapping."""
        row = self._conn.execute(
            "SELECT id FROM module_mapping WHERE id = ? AND deleted = 0", (mapping_id,)
        ).fetchone()
        if not row:
            return False
        sets = []
        vals: list[Any] = []
        for col in (
            "module_prefix", "repo", "local_path", "branch",
            "subpath", "provenance",
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
            f"UPDATE module_mapping SET {', '.join(sets)} WHERE id = ?", vals
        )
        self._conn.commit()
        return True

    def delete_module_mapping(self, mapping_id: int) -> bool:
        cur = self._conn.execute(
            "UPDATE module_mapping SET deleted = 1, updated_at = ? WHERE id = ? AND deleted = 0",
            (_now_iso(), mapping_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_module_mappings(self) -> list[ModuleMapping]:
        """List all module mappings ordered by prefix length (longest first)."""
        rows = self._conn.execute(
            "SELECT * FROM module_mapping WHERE deleted = 0 ORDER BY length(module_prefix) DESC"
        ).fetchall()
        return [self._row_to_module_mapping(r) for r in rows]

    def resolve_module(self, module_name: str) -> ModuleMapping | None:
        """Find the best (longest-prefix) module mapping for *module_name*.

        For example, if there are mappings for ``payments`` and
        ``payments.models``, and *module_name* is ``payments.models.User``,
        the ``payments.models`` mapping wins.
        """
        rows = self._conn.execute(
            "SELECT * FROM module_mapping WHERE deleted = 0 ORDER BY length(module_prefix) DESC"
        ).fetchall()
        for row in rows:
            prefix = row["module_prefix"]
            if module_name == prefix or module_name.startswith(prefix + "."):
                return self._row_to_module_mapping(row)
        return None

    def resolve_module_to_path(
        self, module_name: str, *, cache_dir: str | None = None
    ) -> str | None:
        """Resolve a module name to a local file path.

        If the mapping points to a GitHub repo and it hasn't been cloned yet,
        clones it via ``gh repo clone`` into *cache_dir* (defaults to
        ``~/.cache/emend/repo-checkouts/{repo_id}/``).

        Returns the resolved directory/file path, or None if no mapping found.
        """
        mm = self.resolve_module(module_name)
        if mm is None:
            return None

        # Determine the local root for this mapping.
        if mm.local_path:
            local_root = mm.local_path
        elif mm.repo:
            local_root = _ensure_repo_cloned(
                mm.repo, branch=mm.branch, cache_dir=cache_dir,
            )
        else:
            return None

        # Convert the remaining module suffix to a path.
        suffix = module_name
        if suffix == mm.module_prefix:
            suffix = ""
        elif suffix.startswith(mm.module_prefix + "."):
            suffix = suffix[len(mm.module_prefix) + 1:]

        base = Path(local_root)
        if mm.subpath:
            base = base / mm.subpath

        if suffix:
            parts = suffix.split(".")
            # Try as a package (directory with __init__.py) first, then module.
            candidate_dir = base / "/".join(parts)
            if candidate_dir.is_dir():
                return str(candidate_dir)
            
            # Try original name and snake_case variant for the file
            names_to_try = [parts[-1]]
            snake = re.sub(r'(?<!^)(?=[A-Z])', '_', parts[-1]).lower()
            if snake != parts[-1]:
                names_to_try.append(snake)

            parent_dir = base / "/".join(parts[:-1])
            if parent_dir.is_dir():
                for name in names_to_try:
                    candidate_file = parent_dir / (name + ".py")
                    if candidate_file.is_file():
                        return str(candidate_file)

            # Fall back to the directory for the dotted prefix.
            return str(candidate_dir)
        else:
            return str(base)

    def resolve_selector(self, selector: str) -> str | None:
        """Resolve a dotted selector using module mappings.

        If the selector is already an explicit selector (contains ::) or
        a file path, it is returned as-is.

        If it's a dotted selector like 'a.b.C', it tries to find a module
        mapping for 'a.b' or 'a', and returns an explicit selector like
        'path/to/a/b.py::C'.

        Returns the resolved selector string, or None if no mapping found
        and it wasn't already an explicit selector.
        """
        from emend.component_selector import parse_extended_selector

        if "::" in selector:
            return selector

        try:
            sel = parse_extended_selector(selector)
            if sel.file_path:
                return selector
            
            if not sel.symbol_path:
                return None

            parts = sel.symbol_path
            # Find the best (longest-prefix) module mapping for the whole path
            mm = self.resolve_module(selector)
            if not mm:
                return None

            # Resolve the mapped part to a path
            resolved_base = self.resolve_module_to_path(mm.module_prefix)
            if not resolved_base:
                return None

            # Determine remaining parts after the mapped prefix
            prefix_parts = mm.module_prefix.split('.')
            rem_parts = parts[len(prefix_parts):]

            # If the mapped part is a file, the rest are symbols
            if os.path.isfile(resolved_base):
                return f"{resolved_base}::{'.'.join(rem_parts)}"
            
            # If it was a directory, walk the remaining parts recursively.
            if os.path.isdir(resolved_base):
                current_path = Path(resolved_base)
                
                if not rem_parts:
                    return str(current_path)

                for j, part in enumerate(rem_parts):
                    names = [part]
                    # Robust snake_case translation
                    snake = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', re.sub(r'(.)([A-Z][a-z]+)', r'\1_\2', part)).lower()
                    if snake != part:
                        names.append(snake)
                    
                    found = False
                    # 1. Try as directory first
                    for name in names:
                        if (current_path / name).is_dir():
                            current_path = current_path / name
                            found = True
                            break
                    if found:
                        if j == len(rem_parts) - 1:
                            # Consumed all parts as directories
                            return str(current_path)
                        continue
                    
                    # 2. Try as file
                    for name in names:
                        candidate_file = current_path / (name + ".py")
                        if candidate_file.is_file():
                            # Found the file! 
                            symbol_parts = rem_parts[j+1:]
                            # Heuristic: if we matched a snake_case name and there are no remaining parts,
                            # the original part might be a symbol in this file.
                            if name != part and not symbol_parts:
                                symbol_parts = [part]
                            
                            symbol_suffix = ".".join(symbol_parts)
                            return f"{candidate_file}::{symbol_suffix}"
                    
                    # 3. Neither file nor dir found - check for re-export in __init__.py
                    init_file = current_path / "__init__.py"
                    if init_file.is_file():
                        resolved = self._follow_reexport(init_file, part, rem_parts[j+1:])
                        if resolved:
                            return resolved
                        # Fallback: just point to __init__.py with symbols
                        return f"{init_file}::{'.'.join(rem_parts[j:])}"
                    
                    # Completely stuck
                    break
            
            return None
        except Exception:
            return None

    def _follow_reexport(self, init_file: Path, symbol: str, rem_parts: list[str]) -> str | None:
        """Scan __init__.py for a re-export of symbol and return a resolved selector."""
        try:
            import ast
            with open(init_file) as f:
                tree = ast.parse(f.read())
            
            for node in tree.body:
                if isinstance(node, ast.ImportFrom):
                    # Check for 'from .module import Symbol [as Alias]'
                    for alias in node.names:
                        if (alias.asname or alias.name) == symbol:
                            # Found it!
                            if not node.module:
                                continue
                            
                            # Resolve module path relative to init_file
                            module_parts = node.module.split('.')
                            level = node.level # 1 for '.', 2 for '..', etc.
                            
                            target_dir = init_file.parent
                            for _ in range(level - 1):
                                target_dir = target_dir.parent
                            
                            # Walk module_parts to find the actual file/dir
                            current = target_dir
                            for part in module_parts:
                                if not part: continue
                                # Try both original and snake_case
                                names = [part]
                                snake = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', re.sub(r'(.)([A-Z][a-z]+)', r'\1_\2', part)).lower()
                                if snake != part: names.append(snake)
                                
                                found_next = False
                                for name in names:
                                    if (current / name).is_dir():
                                        current = current / name
                                        found_next = True
                                        break
                                    elif (current / (name + ".py")).is_file():
                                        current = current / (name + ".py")
                                        found_next = True
                                        break
                                if not found_next:
                                    break
                            
                            actual_symbol = alias.name
                            final_symbols = [actual_symbol] + rem_parts
                            symbol_suffix = ".".join(final_symbols)

                            # If we found a file, return it
                            if current.is_file():
                                return f"{current}::{symbol_suffix}"
                            
                            # If it's a directory, return __init__.py in that dir if it exists
                            if current.is_dir() and (current / "__init__.py").is_file():
                                return f"{current / '__init__.py'}::{symbol_suffix}"
                            
                            # Else just return the path we reached
                            return f"{current}::{symbol_suffix}"
        except Exception:
            pass
        return None

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _row_to_module_mapping(row: sqlite3.Row) -> ModuleMapping:
        return ModuleMapping(
            id=row["id"],
            module_prefix=row["module_prefix"],
            repo=row["repo"],
            local_path=row["local_path"],
            branch=row["branch"],
            subpath=row["subpath"],
            provenance=row["provenance"],
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

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


@lru_cache(maxsize=1)
def _global_cache_dir() -> Path:
    """Return the global emend cache directory.

    Checks ``EMEND_CACHE_DIR`` environment variable first, then falls back
    to ``~/.cache/emend``.
    """
    env = os.environ.get("EMEND_CACHE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "emend"


def _repo_checkouts_root(cache_dir: str | None = None) -> Path:
    """Return the global repo-checkouts directory.

    Layout::

        {EMEND_CACHE_DIR}/repo-checkouts/{repo_id}/contents   — bare clone
        {EMEND_CACHE_DIR}/repo-checkouts/{repo_id}/checkouts/{ref} — worktrees
    """
    if cache_dir:
        return Path(cache_dir)
    return _global_cache_dir() / "repo-checkouts"


def _repo_id(repo: str) -> str:
    """Normalize a repo identifier for use as a directory name.

    ``org/name`` → ``org--name`` (avoids nested directories).
    """
    return repo.replace("/", "--")


def _ensure_repo_cloned(
    repo: str,
    *,
    branch: str = "",
    cache_dir: str | None = None,
) -> str:
    """Clone a GitHub repo and check out a worktree for the requested ref.

    Layout::

        {EMEND_CACHE_DIR}/repo-checkouts/{repo_id}/contents        — bare clone
        {EMEND_CACHE_DIR}/repo-checkouts/{repo_id}/checkouts/{ref}  — worktree

    Returns the path to the worktree.
    """
    root = _repo_checkouts_root(cache_dir)
    rid = _repo_id(repo)
    contents_dir = root / rid / "contents"

    # --- Step 1: bare clone into contents/ if not already present ---
    if not (contents_dir / "HEAD").exists():
        contents_dir.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["gh", "repo", "clone", repo, str(contents_dir), "--", "--bare"]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            raise RuntimeError(
                "'gh' CLI not found. Install GitHub CLI to clone external repos: "
                "https://cli.github.com/"
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to clone {repo}: {e.stderr.strip()}")

    # Fetch tags — bare clones and gh may not include them by default.
    try:
        subprocess.run(
            ["git", "fetch", "origin", "--tags"],
            cwd=str(contents_dir),
            check=True, capture_output=True, text=True, timeout=60,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass  # best-effort; tags may already be present

    # --- Step 2: determine the ref to check out ---
    ref = branch or _default_branch(contents_dir)
    if not ref:
        ref = "main"

    # --- Step 3: create or reuse a worktree for this ref ---
    checkouts_dir = root / rid / "checkouts"
    # Sanitize ref for directory name (e.g. "v1.2.3", "feature/foo" → safe name).
    safe_ref = ref.replace("/", "--")
    worktree_dir = checkouts_dir / safe_ref

    if worktree_dir.is_dir():
        return str(worktree_dir)

    checkouts_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "worktree", "add", str(worktree_dir), ref]
    try:
        subprocess.run(
            cmd, cwd=str(contents_dir),
            check=True, capture_output=True, text=True, timeout=60,
        )
    except subprocess.CalledProcessError as e:
        # ref might be a remote branch — try origin/{ref}.
        cmd2 = ["git", "worktree", "add", str(worktree_dir), f"origin/{ref}"]
        try:
            subprocess.run(
                cmd2, cwd=str(contents_dir),
                check=True, capture_output=True, text=True, timeout=60,
            )
        except subprocess.CalledProcessError as e2:
            raise RuntimeError(
                f"Failed to create worktree for {repo}@{ref}: {e2.stderr.strip()}"
            )

    return str(worktree_dir)


def _default_branch(bare_dir: Path) -> str:
    """Read the default branch from a bare repo's HEAD."""
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(bare_dir),
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


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


def module_mapping_to_dict(m: ModuleMapping) -> dict[str, Any]:
    d = asdict(m)
    if d["id"] is None:
        del d["id"]
    return d
