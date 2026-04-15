from __future__ import annotations

import json
from hashlib import blake2b

from experiments.ast_dedup.canonicalize import CanonicalSubtree
from experiments.ast_dedup.corpora import CorpusSpec
from experiments.ast_dedup.corpus_db import connect, insert_subtrees, list_cross_repo_clusters
from experiments.ast_dedup.filter import FilterVerdict


def _sub(file_path: str, label: str) -> CanonicalSubtree:
    return CanonicalSubtree(
        file=file_path,
        start_byte=0,
        end_byte=24,
        start_line=0,
        end_line=5,
        kind_seq=("function_definition", "block", "return_statement"),
        token_seq=("return", "bound_0", "helper"),
        depth=4,
        node_count=24,
        raw_merkle=blake2b(f"raw:{label}".encode(), digest_size=16).digest(),
        canonical_hash=blake2b(label.encode(), digest_size=16).digest(),
        unique_tokens=3,
        unique_non_keyword_tokens=2,
        kind_histogram=(("block", 1), ("function_definition", 1), ("return_statement", 1)),
        child_merkle_bag=(),
    )


def test_insert_subtrees_records_metadata(tmp_path):
    db_path = tmp_path / "corpus.sqlite"
    file_path = tmp_path / "pkg" / "mod.py"
    file_path.parent.mkdir(parents=True)
    file_path.write_text("def f(x):\n    return helper(x)\n", encoding="utf-8")

    conn = connect(db_path)
    spec = CorpusSpec(
        name="repoa",
        url="https://example.com/repoa.git",
        commit=None,
        tag="v1",
        subpath="pkg",
        description="",
    )
    sub = _sub(str(file_path), "same")
    insert_subtrees(
        conn,
        spec=spec,
        corpus_root=file_path.parent,
        subtrees_with_verdicts=[(sub, FilterVerdict(accept=True))],
        source_by_file={str(file_path.resolve()): file_path.read_text(encoding="utf-8")},
    )

    row = conn.execute(
        "SELECT repo, rel_file, total_lines, accepted, normalized_text, token_seq_json FROM subtree_hashes"
    ).fetchone()
    assert row is not None
    assert row["repo"] == "repoa"
    assert row["rel_file"] == "mod.py"
    assert row["total_lines"] == 6
    assert row["accepted"] == 1
    assert "root=function_definition" in row["normalized_text"]
    assert json.loads(row["token_seq_json"]) == ["return", "bound_0", "helper"]


def test_list_cross_repo_clusters_only_returns_shared_hashes(tmp_path):
    db_path = tmp_path / "corpus.sqlite"
    conn = connect(db_path)

    a_root = tmp_path / "a"
    b_root = tmp_path / "b"
    a_root.mkdir()
    b_root.mkdir()
    a_file = a_root / "one.py"
    b_file = b_root / "two.py"
    a_file.write_text("def a():\n    return helper(1)\n", encoding="utf-8")
    b_file.write_text("def b():\n    return helper(2)\n", encoding="utf-8")

    insert_subtrees(
        conn,
        spec=CorpusSpec("repoa", None, None, None, ".", ""),
        corpus_root=a_root,
        subtrees_with_verdicts=[(_sub(str(a_file), "shared"), FilterVerdict(accept=True))],
        source_by_file={str(a_file.resolve()): a_file.read_text(encoding="utf-8")},
    )
    insert_subtrees(
        conn,
        spec=CorpusSpec("repob", None, None, None, ".", ""),
        corpus_root=b_root,
        subtrees_with_verdicts=[(_sub(str(b_file), "shared"), FilterVerdict(accept=True))],
        source_by_file={str(b_file.resolve()): b_file.read_text(encoding="utf-8")},
    )
    insert_subtrees(
        conn,
        spec=CorpusSpec("repoc", None, None, None, ".", ""),
        corpus_root=b_root,
        subtrees_with_verdicts=[(_sub(str(b_file), "unique"), FilterVerdict(accept=True))],
        source_by_file={str(b_file.resolve()): b_file.read_text(encoding="utf-8")},
    )

    clusters = list_cross_repo_clusters(conn)
    assert len(clusters) == 1
    assert clusters[0].repo_count == 2
    assert clusters[0].occurrence_count == 2
