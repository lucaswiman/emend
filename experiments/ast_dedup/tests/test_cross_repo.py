from __future__ import annotations

from hashlib import blake2b

from experiments.ast_dedup.canonicalize import CanonicalSubtree
from experiments.ast_dedup.corpora import CorpusSpec
from experiments.ast_dedup.corpus_db import connect, insert_subtrees
from experiments.ast_dedup.cross_repo import HeuristicConfig, analyze_cross_repo
from experiments.ast_dedup.filter import FilterVerdict


def _sub(file_path: str, label: str, *, token_seq: tuple[str, ...], total_lines: int) -> CanonicalSubtree:
    return CanonicalSubtree(
        file=file_path,
        start_byte=0,
        end_byte=1000,
        start_line=0,
        end_line=total_lines - 1,
        kind_seq=("function_definition", "block", "call", "return_statement"),
        token_seq=token_seq,
        depth=4,
        node_count=30,
        raw_merkle=blake2b(f"raw:{label}".encode(), digest_size=16).digest(),
        canonical_hash=blake2b(label.encode(), digest_size=16).digest(),
        unique_tokens=len(set(token_seq)),
        unique_non_keyword_tokens=len({t for t in token_seq if t not in {"return"}}),
        kind_histogram=(("block", 1), ("call", 1), ("function_definition", 1), ("return_statement", 1)),
        child_merkle_bag=(),
    )


def test_analyze_cross_repo_filters_trivial_clusters(tmp_path):
    db_path = tmp_path / "corpus.sqlite"
    conn = connect(db_path)

    a_root = tmp_path / "fastapi"
    b_root = tmp_path / "flask"
    a_root.mkdir()
    b_root.mkdir()
    a_file = a_root / "a.py"
    b_file = b_root / "b.py"
    a_file.write_text(
        "def f(request, response, handler, exc):\n"
        "    response = handler(request)\n"
        "    response.headers['x'] = exc.name\n"
        "    response.headers['y'] = request.url.path\n"
        "    return response\n",
        encoding="utf-8",
    )
    b_file.write_text(
        "def g(req, resp, callback, err):\n"
        "    resp = callback(req)\n"
        "    resp.headers['x'] = err.name\n"
        "    resp.headers['y'] = req.url.path\n"
        "    return resp\n",
        encoding="utf-8",
    )

    insert_subtrees(
        conn,
        spec=CorpusSpec("fastapi", None, None, None, ".", ""),
        corpus_root=a_root,
        subtrees_with_verdicts=[
            (
                _sub(
                    str(a_file),
                    "interesting",
                    token_seq=("bound_0", "handler", "headers", "x", "name", "headers", "y", "path", "return", "bound_0"),
                    total_lines=5,
                ),
                FilterVerdict(accept=True),
            )
        ],
        source_by_file={str(a_file.resolve()): a_file.read_text(encoding="utf-8")},
    )
    insert_subtrees(
        conn,
        spec=CorpusSpec("flask", None, None, None, ".", ""),
        corpus_root=b_root,
        subtrees_with_verdicts=[
            (
                _sub(
                    str(b_file),
                    "interesting",
                    token_seq=("bound_0", "callback", "headers", "x", "name", "headers", "y", "path", "return", "bound_0"),
                    total_lines=5,
                ),
                FilterVerdict(accept=True),
            )
        ],
        source_by_file={str(b_file.resolve()): b_file.read_text(encoding="utf-8")},
    )

    trivial_root = tmp_path / "django"
    trivial_root.mkdir()
    trivial_file = trivial_root / "c.py"
    trivial_file.write_text(
        "def trivial(x):\n"
        "    return x\n"
        "    \n"
        "    \n"
        "    \n",
        encoding="utf-8",
    )
    insert_subtrees(
        conn,
        spec=CorpusSpec("django", None, None, None, ".", ""),
        corpus_root=trivial_root,
        subtrees_with_verdicts=[
            (
                _sub(
                    str(trivial_file),
                    "trivial",
                    token_seq=("return", "bound_0", "bound_1", "bound_2", "bound_3", "bound_4", "bound_5", "bound_6", "bound_7", "bound_8"),
                    total_lines=5,
                ),
                FilterVerdict(accept=True),
            )
        ],
        source_by_file={str(trivial_file.resolve()): trivial_file.read_text(encoding="utf-8")},
    )
    insert_subtrees(
        conn,
        spec=CorpusSpec("sympy", None, None, None, ".", ""),
        corpus_root=trivial_root,
        subtrees_with_verdicts=[
            (
                _sub(
                    str(trivial_file),
                    "trivial",
                    token_seq=("return", "bound_0", "bound_1", "bound_2", "bound_3", "bound_4", "bound_5", "bound_6", "bound_7", "bound_8"),
                    total_lines=5,
                ),
                FilterVerdict(accept=True),
            )
        ],
        source_by_file={str(trivial_file.resolve()): trivial_file.read_text(encoding="utf-8")},
    )

    analysis = analyze_cross_repo(
        db_path,
        config=HeuristicConfig(max_results=10),
    )
    assert len(analysis["interesting_clusters"]) == 1
    assert analysis["interesting_clusters"][0]["repo_count"] == 2
    assert analysis["rejected_clusters"][0]["rejection"] == "too_few_named_tokens"
