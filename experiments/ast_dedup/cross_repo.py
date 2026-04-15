"""Cross-repo duplicate analysis over the persistent subtree corpus DB."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from experiments.ast_dedup.corpus_db import connect, list_cross_repo_clusters

_KEYWORDISH = {
    "return",
    "for",
    "if",
    "elif",
    "else",
    "while",
    "try",
    "except",
    "finally",
    "with",
    "in",
    "not",
    "and",
    "or",
    "is",
    "None",
    "True",
    "False",
}


@dataclass(frozen=True)
class HeuristicConfig:
    min_repo_count: int = 2
    min_total_lines: int = 5
    min_node_count: int = 18
    min_unique_non_keyword_tokens: int = 5
    min_token_count: int = 10
    min_non_placeholder_tokens: int = 4
    max_results: int = 20


def _load_tokens(row: Any) -> list[str]:
    return list(json.loads(row["token_seq_json"]))


def _non_placeholder_token_count(tokens: list[str]) -> int:
    count = 0
    for tok in tokens:
        if tok.startswith("bound_") or tok.startswith("free_") or tok == "free_unresolved":
            continue
        if tok in _KEYWORDISH:
            continue
        if not any(ch.isalnum() for ch in tok):
            continue
        count += 1
    return count


def _looks_trivial(row: Any, cfg: HeuristicConfig) -> str | None:
    if int(row["total_lines"]) < cfg.min_total_lines:
        return "too_short_lines"
    if int(row["node_count"]) < cfg.min_node_count:
        return "too_small_nodes"
    if int(row["unique_non_keyword_tokens"]) < cfg.min_unique_non_keyword_tokens:
        return "low_unique_tokens"
    tokens = _load_tokens(row)
    if len(tokens) < cfg.min_token_count:
        return "too_few_tokens"
    if _non_placeholder_token_count(tokens) < cfg.min_non_placeholder_tokens:
        return "too_few_named_tokens"
    source = str(row["source_text"]).strip()
    stripped_lines = [line.strip() for line in source.splitlines() if line.strip()]
    if len(stripped_lines) < cfg.min_total_lines:
        return "too_few_nonblank_lines"
    root_kind = str(row["root_kind"])
    if root_kind == "class_definition":
        body_lines = [line for line in stripped_lines[1:] if line]
        if body_lines and all("=" in line and "(" not in line and "[" not in line for line in body_lines):
            return "constant_class_body"
    if root_kind == "block":
        if stripped_lines and all("=" in line and "(" not in line and "[" not in line for line in stripped_lines):
            return "constant_assignment_block"
    if all(line.startswith("self.") and "=" in line for line in stripped_lines):
        return "attribute_assignments_only"
    if all(line.startswith("clauses.append(") or line.startswith("params[") for line in stripped_lines):
        return "query_builder_fragment"
    return None


def _pick_rows_for_hash(conn, canonical_hash: str) -> list[Any]:
    return conn.execute(
        """
        SELECT *
        FROM subtree_hashes
        WHERE canonical_hash = ? AND accepted = 1
        ORDER BY repo ASC, total_lines DESC, node_count DESC, rel_file ASC, start_line ASC
        """,
        (canonical_hash,),
    ).fetchall()


def analyze_cross_repo(
    db_path: str | Path,
    *,
    config: HeuristicConfig | None = None,
) -> dict[str, Any]:
    cfg = config or HeuristicConfig()
    conn = connect(db_path)
    clusters = list_cross_repo_clusters(
        conn,
        accepted_only=True,
        min_repo_count=cfg.min_repo_count,
    )

    reviewed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for cluster in clusters:
        rows = _pick_rows_for_hash(conn, cluster.canonical_hash)
        if not rows:
            continue
        first = rows[0]
        rejection = _looks_trivial(first, cfg)
        sample_rows = []
        seen_repos: set[str] = set()
        for row in rows:
            repo = str(row["repo"])
            if repo in seen_repos and len(sample_rows) >= cfg.min_repo_count:
                continue
            seen_repos.add(repo)
            sample_rows.append(
                {
                    "repo": repo,
                    "rel_file": row["rel_file"],
                    "start_line": int(row["start_line"]),
                    "end_line": int(row["end_line"]),
                    "root_kind": row["root_kind"],
                    "source_text": row["source_text"],
                    "normalized_text": row["normalized_text"],
                }
            )
        payload = {
            "canonical_hash": cluster.canonical_hash,
            "repo_count": cluster.repo_count,
            "occurrence_count": cluster.occurrence_count,
            "total_lines": cluster.total_lines,
            "node_count": cluster.node_count,
            "unique_non_keyword_tokens": cluster.unique_non_keyword_tokens,
            "samples": sample_rows[:6],
        }
        if rejection is None:
            reviewed.append(payload)
        else:
            payload["rejection"] = rejection
            rejected.append(payload)

    reviewed.sort(
        key=lambda item: (
            -item["repo_count"],
            -item["total_lines"],
            -item["node_count"],
            -item["occurrence_count"],
            item["canonical_hash"],
        )
    )
    rejected.sort(
        key=lambda item: (
            item["rejection"],
            -item["repo_count"],
            -item["total_lines"],
            item["canonical_hash"],
        )
    )

    result = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "db_path": str(Path(db_path)),
        "config": cfg.__dict__,
        "candidate_cluster_count": len(clusters),
        "interesting_clusters": reviewed[: cfg.max_results],
        "rejected_clusters": rejected[: cfg.max_results],
    }
    conn.close()
    return result


def render_markdown(analysis: dict[str, Any]) -> str:
    date = analysis["generated_at"].split("T", 1)[0]
    lines = [f"# Cross-repo AST duplicate report ({date})", ""]
    cfg = analysis["config"]
    lines.append(
        "Heuristics: "
        f"repos>={cfg['min_repo_count']}, lines>={cfg['min_total_lines']}, "
        f"nodes>={cfg['min_node_count']}, unique_tokens>={cfg['min_unique_non_keyword_tokens']}, "
        f"tokens>={cfg['min_token_count']}, named_tokens>={cfg['min_non_placeholder_tokens']}"
    )
    lines.append("")
    lines.append(
        f"Cross-repo candidate clusters before post-filtering: {analysis['candidate_cluster_count']}"
    )
    lines.append(
        f"Interesting clusters after post-filtering: {len(analysis['interesting_clusters'])}"
    )
    lines.append("")
    lines.append("## Interesting duplicates")
    lines.append("")
    if not analysis["interesting_clusters"]:
        lines.append("_none_")
    for idx, cluster in enumerate(analysis["interesting_clusters"], 1):
        lines.append(
            f"### {idx}. {cluster['repo_count']} repos, {cluster['occurrence_count']} occurrences, "
            f"{cluster['total_lines']} lines, {cluster['node_count']} nodes"
        )
        lines.append("")
        for sample in cluster["samples"][:3]:
            lines.append(
                f"- `{sample['repo']}` — `{sample['rel_file']}:{sample['start_line']}-{sample['end_line']}` "
                f"({sample['root_kind']})"
            )
        lines.append("")
        lines.append("Normalized shape:")
        lines.append("")
        lines.append("```text")
        lines.append(cluster["samples"][0]["normalized_text"])
        lines.append("```")
        lines.append("")
        for sample in cluster["samples"][:2]:
            lines.append(
                f"`{sample['repo']}` snippet from `{sample['rel_file']}:{sample['start_line']}-{sample['end_line']}`:"
            )
            lines.append("")
            lines.append("```python")
            lines.append(str(sample["source_text"]).rstrip())
            lines.append("```")
            lines.append("")

    lines.append("## Rejected top matches")
    lines.append("")
    if not analysis["rejected_clusters"]:
        lines.append("_none_")
    for cluster in analysis["rejected_clusters"][:10]:
        sample = cluster["samples"][0]
        lines.append(
            f"- `{cluster['rejection']}`: {cluster['repo_count']} repos, "
            f"{cluster['total_lines']} lines at "
            f"`{sample['repo']}/{sample['rel_file']}:{sample['start_line']}-{sample['end_line']}`"
        )
    lines.append("")
    return "\n".join(lines)


def write_report(
    analysis: dict[str, Any],
    *,
    json_path: str | Path,
    md_path: str | Path,
) -> None:
    json_target = Path(json_path)
    md_target = Path(md_path)
    json_target.parent.mkdir(parents=True, exist_ok=True)
    md_target.parent.mkdir(parents=True, exist_ok=True)
    json_target.write_text(
        json.dumps(analysis, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    md_target.write_text(render_markdown(analysis), encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="experiments.ast_dedup.cross_repo",
        description="Analyze cross-repo exact duplicate subtrees from the corpus DB.",
    )
    parser.add_argument("--db", required=True, help="SQLite DB path written by run.py --write-db")
    parser.add_argument(
        "--out-json",
        default="experiments/ast_dedup/reports/cross-repo-analysis.json",
        help="Where to write the JSON analysis payload.",
    )
    parser.add_argument(
        "--out-md",
        default="experiments/ast_dedup/reports/cross-repo-analysis.md",
        help="Where to write the markdown report.",
    )
    parser.add_argument("--min-repos", type=int, default=2)
    parser.add_argument("--min-lines", type=int, default=5)
    parser.add_argument("--min-nodes", type=int, default=18)
    parser.add_argument("--min-unique-tokens", type=int, default=5)
    parser.add_argument("--min-token-count", type=int, default=10)
    parser.add_argument("--min-named-tokens", type=int, default=4)
    parser.add_argument("--max-results", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    analysis = analyze_cross_repo(
        args.db,
        config=HeuristicConfig(
            min_repo_count=args.min_repos,
            min_total_lines=args.min_lines,
            min_node_count=args.min_nodes,
            min_unique_non_keyword_tokens=args.min_unique_tokens,
            min_token_count=args.min_token_count,
            min_non_placeholder_tokens=args.min_named_tokens,
            max_results=args.max_results,
        ),
    )
    write_report(analysis, json_path=args.out_json, md_path=args.out_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
