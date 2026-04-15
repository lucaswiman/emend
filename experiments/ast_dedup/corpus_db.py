"""Persistent subtree corpus storage for AST dedup experiments.

Stores canonical subtree hashes plus enough metadata to support cross-repo
analysis without re-running canonicalization for every iteration on the
heuristics. The table intentionally keeps both accepted and rejected
subtrees so we can audit filter behaviour after the fact.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from experiments.ast_dedup.canonicalize import CanonicalSubtree
from experiments.ast_dedup.corpora import CorpusSpec
from experiments.ast_dedup.filter import FilterVerdict

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StoredCluster:
    canonical_hash: str
    repo_count: int
    occurrence_count: int
    total_lines: int
    node_count: int
    unique_non_keyword_tokens: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = NORMAL;

        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subtree_hashes (
            id INTEGER PRIMARY KEY,
            corpus TEXT NOT NULL,
            repo TEXT NOT NULL,
            repo_url TEXT,
            repo_ref TEXT,
            corpus_root TEXT NOT NULL,
            file TEXT NOT NULL,
            rel_file TEXT NOT NULL,
            start_byte INTEGER NOT NULL,
            end_byte INTEGER NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            total_lines INTEGER NOT NULL,
            node_count INTEGER NOT NULL,
            depth INTEGER NOT NULL,
            unique_tokens INTEGER NOT NULL,
            unique_non_keyword_tokens INTEGER NOT NULL,
            root_kind TEXT NOT NULL,
            canonical_hash TEXT NOT NULL,
            raw_merkle TEXT NOT NULL,
            kind_seq_json TEXT NOT NULL,
            token_seq_json TEXT NOT NULL,
            kind_histogram_json TEXT NOT NULL,
            child_merkle_bag_json TEXT NOT NULL,
            normalized_text TEXT NOT NULL,
            source_text TEXT NOT NULL,
            accepted INTEGER NOT NULL,
            rejection_filter TEXT,
            rejection_reason TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(repo, file, start_byte, end_byte)
        );

        CREATE INDEX IF NOT EXISTS idx_subtree_hashes_canonical_hash
            ON subtree_hashes(canonical_hash);
        CREATE INDEX IF NOT EXISTS idx_subtree_hashes_repo_hash
            ON subtree_hashes(repo, canonical_hash);
        CREATE INDEX IF NOT EXISTS idx_subtree_hashes_repo_accept
            ON subtree_hashes(repo, accepted);
        CREATE INDEX IF NOT EXISTS idx_subtree_hashes_repo_lines
            ON subtree_hashes(repo, total_lines, node_count);
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    conn.commit()


def delete_repo_rows(conn: sqlite3.Connection, repo: str) -> None:
    conn.execute("DELETE FROM subtree_hashes WHERE repo = ?", (repo,))
    conn.commit()


def merge_from_paths(
    destination: str | Path,
    sources: Iterable[str | Path],
) -> None:
    dest_conn = connect(destination)
    for src in sources:
        src_conn = connect(src)
        rows = src_conn.execute("SELECT * FROM subtree_hashes").fetchall()
        if not rows:
            src_conn.close()
            continue
        columns = [desc[0] for desc in src_conn.execute("SELECT * FROM subtree_hashes LIMIT 1").description]
        placeholders = ",".join("?" for _ in columns)
        dest_conn.executemany(
            f"INSERT OR REPLACE INTO subtree_hashes ({', '.join(columns)}) VALUES ({placeholders})",
            [tuple(row[col] for col in columns) for row in rows],
        )
        src_conn.close()
    dest_conn.commit()
    dest_conn.close()


def render_normalized_text(sub: CanonicalSubtree, max_items: int = 80) -> str:
    kinds = " ".join(sub.kind_seq[:max_items])
    tokens = " ".join(sub.token_seq[:max_items])
    if len(sub.kind_seq) > max_items:
        kinds += " ..."
    if len(sub.token_seq) > max_items:
        tokens += " ..."
    return (
        f"root={sub.kind_seq[0] if sub.kind_seq else '?'}\n"
        f"kinds: {kinds}\n"
        f"tokens: {tokens}"
    )


def _extract_source_text(source: str, sub: CanonicalSubtree) -> str:
    encoded = source.encode("utf-8", errors="replace")
    chunk = encoded[sub.start_byte : sub.end_byte]
    return chunk.decode("utf-8", errors="replace")


def insert_subtrees(
    conn: sqlite3.Connection,
    *,
    spec: CorpusSpec,
    corpus_root: str | Path,
    subtrees_with_verdicts: Iterable[tuple[CanonicalSubtree, FilterVerdict]],
    source_by_file: dict[str, str],
) -> int:
    corpus_root_str = str(Path(corpus_root).resolve())
    rows = []
    created_at = _utc_now()

    for sub, verdict in subtrees_with_verdicts:
        file_path = str(Path(sub.file).resolve())
        rel_file = str(Path(file_path).relative_to(Path(corpus_root_str)))
        source = source_by_file[file_path]
        rows.append(
            (
                spec.name,
                spec.name,
                spec.url,
                spec.commit or spec.tag or "",
                corpus_root_str,
                file_path,
                rel_file,
                sub.start_byte,
                sub.end_byte,
                sub.start_line + 1,
                sub.end_line + 1,
                (sub.end_line - sub.start_line) + 1,
                sub.node_count,
                sub.depth,
                sub.unique_tokens,
                sub.unique_non_keyword_tokens,
                sub.kind_seq[0] if sub.kind_seq else "",
                sub.canonical_hash.hex(),
                sub.raw_merkle.hex(),
                json.dumps(sub.kind_seq),
                json.dumps(sub.token_seq),
                json.dumps(sub.kind_histogram),
                json.dumps([h.hex() for h in sub.child_merkle_bag]),
                render_normalized_text(sub),
                _extract_source_text(source, sub),
                1 if verdict.accept else 0,
                None if verdict.accept else _reason_prefix(verdict.reason),
                verdict.reason,
                created_at,
            )
        )

    conn.executemany(
        """
        INSERT OR REPLACE INTO subtree_hashes(
            corpus, repo, repo_url, repo_ref, corpus_root, file, rel_file,
            start_byte, end_byte, start_line, end_line, total_lines,
            node_count, depth, unique_tokens, unique_non_keyword_tokens,
            root_kind, canonical_hash, raw_merkle, kind_seq_json, token_seq_json,
            kind_histogram_json, child_merkle_bag_json, normalized_text,
            source_text, accepted, rejection_filter, rejection_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def _reason_prefix(reason: str | None) -> str | None:
    if not reason:
        return None
    return reason.split(":", 1)[0]


def list_cross_repo_clusters(
    conn: sqlite3.Connection,
    *,
    accepted_only: bool = True,
    min_repo_count: int = 2,
) -> list[StoredCluster]:
    where = ["COUNT(DISTINCT repo) >= ?"]
    params: list[object] = [min_repo_count]
    accepted_predicate = ""
    if accepted_only:
        accepted_predicate = "WHERE accepted = 1"
    rows = conn.execute(
        f"""
        SELECT canonical_hash,
               COUNT(DISTINCT repo) AS repo_count,
               COUNT(*) AS occurrence_count,
               MAX(total_lines) AS total_lines,
               MAX(node_count) AS node_count,
               MAX(unique_non_keyword_tokens) AS unique_non_keyword_tokens
        FROM subtree_hashes
        {accepted_predicate}
        GROUP BY canonical_hash
        HAVING {' AND '.join(where)}
        ORDER BY repo_count DESC, total_lines DESC, node_count DESC, occurrence_count DESC, canonical_hash ASC
        """,
        params,
    ).fetchall()
    return [
        StoredCluster(
            canonical_hash=row["canonical_hash"],
            repo_count=int(row["repo_count"]),
            occurrence_count=int(row["occurrence_count"]),
            total_lines=int(row["total_lines"]),
            node_count=int(row["node_count"]),
            unique_non_keyword_tokens=int(row["unique_non_keyword_tokens"]),
        )
        for row in rows
    ]


__all__ = [
    "SCHEMA_VERSION",
    "StoredCluster",
    "connect",
    "delete_repo_rows",
    "insert_subtrees",
    "list_cross_repo_clusters",
    "merge_from_paths",
    "render_normalized_text",
]
