"""Statistics + report emitters for the AST dedup experiment runner.

Consolidates Phase 2-5 output into a deterministic JSON + markdown
``CorpusReport``. No runtime dependency on ``emend_core``: all inputs are
plain dataclasses already populated by upstream phases.

See ``ideas/roadmap/phase-6-runner-and-stats.md`` for the target schema.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Histogram helpers
# ---------------------------------------------------------------------------


def cluster_size_histogram(sizes: Iterable[int]) -> dict[str, int]:
    """Bucket cluster sizes into the phase-6 schema labels.

    Buckets: ``"2"``, ``"3"``, ``"4"``, ``"5-8"`` (sizes 5..8), ``"9-16"``
    (sizes 9..16), ``"17+"`` (sizes >= 17). Missing buckets are omitted so
    the resulting JSON stays compact.
    """
    out: dict[str, int] = {}
    for s in sizes:
        if s < 2:
            continue
        if s == 2:
            label = "2"
        elif s == 3:
            label = "3"
        elif s == 4:
            label = "4"
        elif s <= 8:
            label = "5-8"
        elif s <= 16:
            label = "9-16"
        else:
            label = "17+"
        out[label] = out.get(label, 0) + 1
    return out


def length_histogram(lengths: Iterable[int]) -> dict[str, int]:
    """Bucket sequence clone lengths.

    Buckets: ``"4"``, ``"5"``, ``"6-10"`` (lengths 6..10), ``"11+"``
    (lengths >= 11). Missing buckets are omitted.
    """
    out: dict[str, int] = {}
    for length in lengths:
        if length < 4:
            continue
        if length == 4:
            label = "4"
        elif length == 5:
            label = "5"
        elif length <= 10:
            label = "6-10"
        else:
            label = "11+"
        out[label] = out.get(label, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FilterStats:
    removal_counts: dict[str, int]
    accepted: int
    rejected_samples: dict[str, list[dict[str, Any]]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "removal_counts": dict(self.removal_counts),
            "accepted": self.accepted,
            "rejected_samples": {
                name: list(samples)
                for name, samples in self.rejected_samples.items()
            },
        }


@dataclass
class StrategyStats:
    name: str
    wall_time_sec: float
    peak_rss_mb: float
    cluster_count: int
    largest_cluster_size: int
    cluster_size_histogram: dict[str, int]
    top_clusters: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "wall_time_sec": self.wall_time_sec,
            "peak_rss_mb": self.peak_rss_mb,
            "cluster_count": self.cluster_count,
            "largest_cluster_size": self.largest_cluster_size,
            "cluster_size_histogram": dict(self.cluster_size_histogram),
            "top_clusters": [dict(c) for c in self.top_clusters],
        }


@dataclass
class SequenceCloneStats:
    count: int
    length_histogram: dict[str, int]
    top_runs: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "length_histogram": dict(self.length_histogram),
            "top_runs": [dict(r) for r in self.top_runs],
        }


@dataclass
class AgreementStats:
    merkle_cluster_coverage: float
    lsh_only_pairs: int
    lsh_only_samples: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "merkle_cluster_coverage": self.merkle_cluster_coverage,
            "lsh_only_pairs": self.lsh_only_pairs,
            "lsh_only_samples": [dict(s) for s in self.lsh_only_samples],
        }


@dataclass
class CorpusReport:
    corpus: str
    timestamp: str
    config: dict[str, Any]
    corpus_stats: dict[str, int]
    filter_stats: FilterStats
    strategy_stats: list[StrategyStats]
    agreement: dict[str, AgreementStats]
    sibling_sequence_clones: SequenceCloneStats
    cross_corpus_overlaps: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_json(self) -> str:
        """Deterministic JSON: 2-space indent, sorted keys."""
        return json.dumps(
            _serialize(self),
            indent=2,
            sort_keys=True,
            default=str,
        )

    def to_markdown(self) -> str:
        """Human-readable markdown report, kept under 4 KB."""
        cs = self.corpus_stats
        files = cs.get("files", 0)
        total_loc = cs.get("total_loc", 0)
        candidates = cs.get("candidate_subtrees", 0)
        accepted = cs.get("after_filters", 0)

        # Header / date (strip time-of-day to just the calendar day).
        day = self.timestamp.split("T", 1)[0] if "T" in self.timestamp else self.timestamp

        lines: list[str] = []
        lines.append(f"# {self.corpus} @ {day}")
        lines.append("")
        lines.append(
            f"Files: {files:,} \u00b7 LOC: {total_loc:,} "
            f"\u00b7 candidates: {candidates:,} "
            f"\u00b7 after filters: {accepted:,}"
        )
        lines.append("")

        # Top exact-duplicate clusters: find the merkle_exact strategy, if any.
        merkle = next(
            (s for s in self.strategy_stats if s.name == "merkle_exact"),
            None,
        )
        lines.append("## Top exact-duplicate clusters")
        lines.append("")
        if merkle is None or not merkle.top_clusters:
            lines.append("_none_")
        else:
            for i, cluster in enumerate(merkle.top_clusters[:5], 1):
                locs = cluster.get("locations", [])
                line_ranges = cluster.get("line_ranges", [])
                size = cluster.get("size", len(locs))
                node_count = cluster.get("node_count", 0)
                if locs:
                    first_file = locs[0][0]
                    if line_ranges and line_ranges[0][0] is not None:
                        first_range = f"{line_ranges[0][0]}-{line_ranges[0][1]}"
                    else:
                        first_range = f"{locs[0][1]}-{locs[0][2]}"
                    more = f" and {size - 1} more" if size > 1 else ""
                    lines.append(
                        f"{i}. cluster {size}x \u2014 {first_file}:{first_range}"
                        f"{more} (node_count={node_count})"
                    )
                else:
                    lines.append(f"{i}. cluster {size}x (node_count={node_count})")
        lines.append("")

        # Top sibling-sequence clones.
        lines.append("## Top sibling-sequence clones")
        lines.append("")
        seq = self.sibling_sequence_clones
        if seq.count == 0 or not seq.top_runs:
            lines.append("_none_")
        else:
            for i, run in enumerate(seq.top_runs[:5], 1):
                length = run.get("length", 0)
                rlocs = run.get("locations", [])
                if len(rlocs) >= 2:
                    a = rlocs[0]
                    b = rlocs[1]
                    lines.append(
                        f"{i}. {length}-statement run: "
                        f"{a.get('file','?')}:{a.get('lines','?')} "
                        f"\u2194 {b.get('file','?')}:{b.get('lines','?')}"
                    )
                elif rlocs:
                    a = rlocs[0]
                    lines.append(
                        f"{i}. {length}-statement run: "
                        f"{a.get('file','?')}:{a.get('lines','?')}"
                    )
                else:
                    lines.append(f"{i}. {length}-statement run")
        lines.append("")

        # Strategies table.
        lines.append("## Strategies")
        lines.append("")
        lines.append(
            "| strategy | clusters | wall | peak RSS | LSH-only pairs |"
        )
        lines.append(
            "|----------|---------:|-----:|---------:|---------------:|"
        )

        # LSH-only pairs come from the agreement section, keyed on
        # "merkle_exact_vs_<strategy>".
        lsh_only_by_strategy: dict[str, int] = {}
        for key, agr in self.agreement.items():
            if key.startswith("merkle_exact_vs_"):
                other = key[len("merkle_exact_vs_") :]
                lsh_only_by_strategy[other] = agr.lsh_only_pairs

        # Sort strategies alphabetically for deterministic output.
        sorted_strategies = sorted(self.strategy_stats, key=lambda s: s.name)
        for strat in sorted_strategies:
            if strat.name == "merkle_exact":
                lsh_cell = "\u2014"
            else:
                lsh_cell = str(lsh_only_by_strategy.get(strat.name, 0))
            lines.append(
                f"| {strat.name} | {strat.cluster_count} "
                f"| {strat.wall_time_sec:.1f}s "
                f"| {strat.peak_rss_mb:.0f} MB | {lsh_cell} |"
            )
        lines.append("")

        # Filters table (sorted by filter name).
        lines.append("## Filters")
        lines.append("")
        lines.append("| filter | dropped |")
        lines.append("|--------|--------:|")
        for name in sorted(self.filter_stats.removal_counts):
            count = self.filter_stats.removal_counts[name]
            lines.append(f"| {name} | {count:,} |")
        lines.append(
            f"| __accepted__ | {self.filter_stats.accepted:,} |"
        )
        lines.append("")

        out = "\n".join(lines)
        # Safety clamp: phase-6 target is <= 4 KB.
        if len(out.encode("utf-8")) > 4000:
            # Aggressively truncate the table bodies if we overshoot.
            truncated = out.encode("utf-8")[:3900].decode("utf-8", errors="ignore")
            out = truncated + "\n\n_(truncated)_\n"
        return out


# ---------------------------------------------------------------------------
# Recursive serializer
# ---------------------------------------------------------------------------


def _serialize(obj: Any) -> Any:
    """Recursively convert dataclasses/collections to JSON-ready values."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    if is_dataclass(obj) and not isinstance(obj, type):
        return {
            f.name: _serialize(getattr(obj, f.name))
            for f in dataclasses.fields(obj)
        }
    if isinstance(obj, dict):
        return {str(k): _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_serialize(v) for v in obj]
    # Fallback: string repr.
    return str(obj)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _subtree_to_sample(sub: Any, reason: str | None = None) -> dict[str, Any]:
    """Convert a ``CanonicalSubtree`` to a compact sample dict for reports."""
    return {
        "file": getattr(sub, "file", "?"),
        "start_line": getattr(sub, "start_line", -1),
        "end_line": getattr(sub, "end_line", -1),
        "reason": reason or "",
        "node_count": getattr(sub, "node_count", 0),
    }


def build_filter_stats(pipeline: Any) -> FilterStats:
    """Build ``FilterStats`` from a ``FilterPipeline``.

    Extracts ``__accepted__`` from ``removal_counts`` into its own field.
    The ``pipeline.samples`` mapping (filter name -> list of rejected
    ``CanonicalSubtree``) is projected into the compact JSON sample shape.
    """
    removal = dict(pipeline.removal_counts)
    accepted = removal.pop("__accepted__", 0)

    samples_map = getattr(pipeline, "samples", {}) or {}
    rejected_samples: dict[str, list[dict[str, Any]]] = {}
    for filter_name, subs in samples_map.items():
        entries: list[dict[str, Any]] = []
        for sub in list(subs)[:5]:
            # We don't have the FilterVerdict.reason stored alongside the
            # sample, but the filter name itself is the primary reason
            # classifier. Leave an empty string otherwise.
            entries.append(_subtree_to_sample(sub, reason=filter_name))
        rejected_samples[filter_name] = entries

    return FilterStats(
        removal_counts=removal,
        accepted=accepted,
        rejected_samples=rejected_samples,
    )


def _cluster_sort_key(
    cluster: list[tuple[str, int, int]],
    subtree_lookup: dict[tuple[str, int, int], Any],
) -> tuple[int, int, tuple[str, int, int]]:
    """Sort key: (size DESC, node_count DESC, first_location ASC).

    Python sorts ascending, so we negate the DESC fields.
    """
    size = len(cluster)
    # Pull node_count from the first member we can look up.
    node_count = 0
    for key in cluster:
        sub = subtree_lookup.get(tuple(key))
        if sub is not None:
            node_count = getattr(sub, "node_count", 0)
            break
    first_loc = tuple(sorted(cluster)[0]) if cluster else ("", 0, 0)
    return (-size, -node_count, first_loc)


def build_strategy_stats(
    result: Any,
    peak_rss_mb: float,
    subtree_lookup: dict[tuple[str, int, int], Any],
) -> StrategyStats:
    """Build a :class:`StrategyStats` from a ``hashers.StrategyResult``."""
    clusters = list(result.duplicate_clusters)
    sizes = [len(c) for c in clusters]
    hist = cluster_size_histogram(sizes)
    largest = max(sizes, default=0)

    ranked = sorted(
        clusters,
        key=lambda c: _cluster_sort_key(c, subtree_lookup),
    )

    top_clusters: list[dict[str, Any]] = []
    # Per-cluster cap on serialized members: a pathological strategy (e.g.
    # simhash at a too-loose threshold) can emit a cluster covering half the
    # corpus. We still record the true ``size`` / ``node_count`` but only
    # serialize a few representative locations so the JSON stays compact.
    MAX_LOCATIONS_PER_CLUSTER = 20
    for cluster in ranked[:20]:
        node_count = 0
        for key in cluster:
            sub = subtree_lookup.get(tuple(key))
            if sub is not None:
                node_count = getattr(sub, "node_count", 0)
                break
        kept = list(cluster)[:MAX_LOCATIONS_PER_CLUSTER]
        # Byte-offset key used by the hasher (authoritative identity).
        locations = [[k[0], int(k[1]), int(k[2])] for k in kept]
        # Parallel list of human-readable 1-indexed line ranges, pulled
        # from the CanonicalSubtree when we can find it. Missing entries
        # fall back to ``None`` so programmatic consumers can tell.
        line_ranges: list[list[Any]] = []
        for key in kept:
            sub = subtree_lookup.get(tuple(key))
            if sub is None:
                line_ranges.append([None, None])
            else:
                line_ranges.append(
                    [int(sub.start_line) + 1, int(sub.end_line) + 1]
                )
        entry: dict[str, Any] = {
            "size": len(cluster),
            "node_count": node_count,
            "locations": locations,
            "line_ranges": line_ranges,
        }
        if len(cluster) > MAX_LOCATIONS_PER_CLUSTER:
            entry["truncated_members"] = len(cluster) - MAX_LOCATIONS_PER_CLUSTER
        top_clusters.append(entry)

    wall = float(result.index_insert_secs) + float(result.query_secs)

    return StrategyStats(
        name=result.name,
        wall_time_sec=wall,
        peak_rss_mb=peak_rss_mb,
        cluster_count=len(clusters),
        largest_cluster_size=largest,
        cluster_size_histogram=hist,
        top_clusters=top_clusters,
    )


def _format_lines(start: int, end: int) -> str:
    """Format a 1-indexed inclusive line range."""
    return f"L{start}-{end}"


def build_sequence_stats(clones: Iterable[Any]) -> SequenceCloneStats:
    """Build :class:`SequenceCloneStats` from a list of ``SequenceClone``."""
    clones_list = list(clones)
    lengths = [c.length for c in clones_list]
    hist = length_histogram(lengths)

    ranked = sorted(clones_list, key=lambda c: -c.length)
    top_runs: list[dict[str, Any]] = []
    for clone in ranked[:20]:
        left = clone.left
        right = clone.right
        l_start = left.line_ranges[clone.left_range[0]][0]
        l_end = left.line_ranges[clone.left_range[1] - 1][1]
        r_start = right.line_ranges[clone.right_range[0]][0]
        r_end = right.line_ranges[clone.right_range[1] - 1][1]
        top_runs.append(
            {
                "length": clone.length,
                "locations": [
                    {
                        "file": left.file,
                        "function": left.function_qn,
                        "lines": _format_lines(l_start, l_end),
                    },
                    {
                        "file": right.file,
                        "function": right.function_qn,
                        "lines": _format_lines(r_start, r_end),
                    },
                ],
            }
        )

    return SequenceCloneStats(
        count=len(clones_list),
        length_histogram=hist,
        top_runs=top_runs,
    )


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------


def compute_agreement(
    merkle_result: Any,
    other_result: Any,
    subtree_lookup: dict[tuple[str, int, int], Any],
) -> AgreementStats:
    """Compare merkle_exact clusters against another strategy's clusters.

    ``merkle_cluster_coverage``: fraction of merkle-exact clusters whose
    members all appear together in *some* cluster produced by
    ``other_result``. A merkle cluster is "covered" if there exists an
    other-strategy cluster containing every member.

    ``lsh_only_pairs``: count of near-duplicate pairs from ``other_result``
    that are not a subset of any merkle-exact cluster (i.e. the other
    strategy "saw" a relationship that merkle did not). Samples up to 10.
    """
    merkle_clusters = [
        [tuple(k) for k in c] for c in merkle_result.duplicate_clusters
    ]
    other_clusters = [
        [tuple(k) for k in c] for c in other_result.duplicate_clusters
    ]

    # Coverage: for each merkle cluster, does SOME other cluster contain
    # all its members?
    if merkle_clusters:
        other_sets = [frozenset(c) for c in other_clusters]
        covered = 0
        for mc in merkle_clusters:
            mset = frozenset(mc)
            if any(mset <= os for os in other_sets):
                covered += 1
        coverage = covered / len(merkle_clusters)
    else:
        coverage = 1.0

    # LSH-only pairs: pairs (a,b,sim) from other_result.near_duplicate_pairs
    # whose endpoints are NOT both in any merkle exact cluster.
    merkle_pair_set: set[frozenset] = set()
    for mc in merkle_clusters:
        for i in range(len(mc)):
            for j in range(i + 1, len(mc)):
                merkle_pair_set.add(frozenset([mc[i], mc[j]]))

    lsh_only_pairs_list: list[tuple[tuple, tuple, float]] = []
    for a, b, sim in getattr(other_result, "near_duplicate_pairs", []) or []:
        ta, tb = tuple(a), tuple(b)
        if frozenset([ta, tb]) in merkle_pair_set:
            continue
        lsh_only_pairs_list.append((ta, tb, float(sim)))

    samples: list[dict[str, Any]] = []
    for ta, tb, sim in lsh_only_pairs_list[:10]:
        samples.append(
            {
                "a": [ta[0], int(ta[1]), int(ta[2])],
                "b": [tb[0], int(tb[1]), int(tb[2])],
                "sim": sim,
            }
        )

    return AgreementStats(
        merkle_cluster_coverage=coverage,
        lsh_only_pairs=len(lsh_only_pairs_list),
        lsh_only_samples=samples,
    )


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def write_report(report: CorpusReport, out_dir: Path) -> tuple[Path, Path]:
    """Write ``reports/{corpus}-{timestamp}.json`` and ``.md``.

    Creates ``out_dir`` if missing. Returns ``(json_path, md_path)``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    # Sanitize timestamp for a filename (colons aren't friendly on Windows).
    ts_safe = report.timestamp.replace(":", "").replace("-", "")
    stem = f"{report.corpus}-{ts_safe}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(report.to_json(), encoding="utf-8")
    md_path.write_text(report.to_markdown(), encoding="utf-8")
    return json_path, md_path


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def utc_timestamp() -> str:
    """Return an ISO 8601 UTC timestamp with second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "AgreementStats",
    "CorpusReport",
    "FilterStats",
    "SequenceCloneStats",
    "StrategyStats",
    "build_filter_stats",
    "build_sequence_stats",
    "build_strategy_stats",
    "cluster_size_histogram",
    "compute_agreement",
    "length_histogram",
    "utc_timestamp",
    "write_report",
]
