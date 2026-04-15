"""Phase 6 corpus runner.

Ties together canonicalization (Phase 2), hashing strategies (Phase 3),
triviality filters (Phase 4), and sibling-sequence detection (Phase 5)
into a single script that walks a corpus and produces a ``CorpusReport``
via the helpers in :mod:`experiments.ast_dedup.stats`.

Usage:
    python -m experiments.ast_dedup.run --corpus emend
    python -m experiments.ast_dedup.run --corpus django --max-files 500
    python -m experiments.ast_dedup.run --all
    python -m experiments.ast_dedup.run --corpus emend --strategies merkle_exact
    python -m experiments.ast_dedup.run --corpus emend --no-filter
    python -m experiments.ast_dedup.run --corpus emend --ablate rename_attrs
"""

from __future__ import annotations

import argparse
import resource
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from emend import emend_core

from experiments.ast_dedup import corpora
from experiments.ast_dedup.canonicalize import (
    CanonicalSubtree,
    CanonicalizerConfig,
    canonicalize_file,
)
from experiments.ast_dedup.filter import FilterConfig, default_pipeline
from experiments.ast_dedup.hashers import (
    REGISTRY,
    StrategyResult,
    compare_strategies,
)
from experiments.ast_dedup.sequence import (
    build_statement_seqs,
    filter_sequence_clones,
    find_clones_suffix_array,
    find_clones_winnowing,
)
from experiments.ast_dedup.stats import (
    CorpusReport,
    build_filter_stats,
    build_sequence_stats,
    build_strategy_stats,
    compute_agreement,
    write_report,
)

# ---------------------------------------------------------------------------
# Ablation support
# ---------------------------------------------------------------------------

_ABLATIONS: frozenset[str] = frozenset(
    {
        "rename_attrs",
        "rename_string_literals",
        "rename_numeric_literals",
        "keep_literal_equality",
    }
)


def _build_canonicalizer_config(ablate: Optional[str]) -> CanonicalizerConfig:
    """Return a CanonicalizerConfig adjusted for an ablation flag.

    ``rename_attrs`` / ``keep_literal_equality`` flip their defaults from
    False → True. ``rename_string_literals`` / ``rename_numeric_literals``
    flip from True → False.
    """
    cfg = CanonicalizerConfig()
    if ablate is None:
        return cfg
    if ablate not in _ABLATIONS:
        raise ValueError(
            f"--ablate must be one of {sorted(_ABLATIONS)}, got {ablate!r}"
        )
    if ablate == "rename_attrs":
        cfg.rename_attrs = True
    elif ablate == "rename_string_literals":
        cfg.rename_string_literals = False
    elif ablate == "rename_numeric_literals":
        cfg.rename_numeric_literals = False
    elif ablate == "keep_literal_equality":
        cfg.keep_literal_equality = True
    return cfg


# ---------------------------------------------------------------------------
# Resource helpers
# ---------------------------------------------------------------------------


def _peak_rss_mb() -> float:
    """Return the peak RSS of the current process in megabytes.

    ``ru_maxrss`` is kilobytes on Linux and bytes on macOS. We convert to
    MB using platform detection.
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        # macOS reports bytes
        return raw / (1024 * 1024)
    # Linux and most others report kilobytes
    return raw / 1024


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------


def _select_registry(
    strategies_arg: Optional[str],
) -> dict[str, tuple[type, type, float]]:
    """Subset the global REGISTRY based on the comma-separated --strategies flag."""
    if strategies_arg is None:
        return dict(REGISTRY)
    names = [s.strip() for s in strategies_arg.split(",") if s.strip()]
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise ValueError(
            f"Unknown strategies: {unknown}. Available: {sorted(REGISTRY)}"
        )
    return {n: REGISTRY[n] for n in names}


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------


def run_one(
    corpus_name: str,
    args,
    corpus_root: Optional[Path] = None,
) -> CorpusReport:
    """Run the full per-corpus pipeline and return a ``CorpusReport``.

    Parameters
    ----------
    corpus_name:
        The name of the corpus (used for the report metadata).
    args:
        An ``argparse.Namespace``-like object with the runner flags.
    corpus_root:
        Optional override for the corpus source directory. When ``None``
        (the CLI path), ``corpora.ensure(corpus_name)`` is used.
    """
    if corpus_root is None:
        corpus_root = corpora.ensure(corpus_name)
    corpus_root = Path(corpus_root)

    cfg = _build_canonicalizer_config(getattr(args, "ablate", None))
    selected_registry = _select_registry(getattr(args, "strategies", None))

    resolver = emend_core.PyScopeResolver(str(corpus_root), "py")

    all_subtrees: list[CanonicalSubtree] = []
    seqs = []
    n_files = 0
    total_loc = 0

    max_files = getattr(args, "max_files", None)
    for path in corpora.iter_py_files(corpus_root, max_files):
        n_files += 1
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                total_loc += len(fh.read().splitlines())
        except OSError:
            pass

        try:
            subs = canonicalize_file(str(path), resolver, cfg)
        except (OSError, ValueError):
            subs = []
        all_subtrees.extend(subs)

        try:
            file_seqs = build_statement_seqs(str(path), resolver)
        except (OSError, ValueError):
            file_seqs = []
        seqs.extend(file_seqs)

    # Filter pipeline
    filter_cfg = FilterConfig()
    pipeline = default_pipeline(filter_cfg)
    no_filter = getattr(args, "no_filter", False)
    accepted: list[CanonicalSubtree] = []
    if no_filter:
        accepted = list(all_subtrees)
    else:
        for sub in all_subtrees:
            verdict = pipeline.run(sub)
            if verdict.accept:
                accepted.append(sub)

    # Keys → subtrees lookup for stats helpers
    subtree_lookup: dict[tuple[str, int, int], CanonicalSubtree] = {
        (s.file, s.start_byte, s.end_byte): s for s in accepted
    }

    # Peak RSS before strategy work
    rss_before = _peak_rss_mb()

    # Run strategies
    strategy_results: list[StrategyResult] = compare_strategies(
        accepted, registry=selected_registry
    )

    rss_after = _peak_rss_mb()
    peak_rss = max(rss_before, rss_after)

    # Sibling-sequence clones (winnowing + optional suffix-array)
    w = getattr(args, "winnowing_w", 4)
    t = getattr(args, "winnowing_t", 4)
    use_sa = getattr(args, "suffix_array", False)
    try:
        winnowing_clones = find_clones_winnowing(seqs, w=w, min_run=t)
    except Exception:
        winnowing_clones = []
    sa_clones = []
    if use_sa:
        try:
            sa_clones = find_clones_suffix_array(seqs, min_run=t)
        except Exception:
            sa_clones = []
    all_clones = list(winnowing_clones) + list(sa_clones)
    filtered_clones = filter_sequence_clones(all_clones, min_run=t)

    # Build stats
    filter_stats = build_filter_stats(pipeline)
    strategy_stats = [
        build_strategy_stats(result, peak_rss, subtree_lookup)
        for result in strategy_results
    ]
    sequence_stats = build_sequence_stats(filtered_clones)

    # Agreement: merkle_exact vs every other strategy
    agreement: dict[str, object] = {}
    merkle_result = next(
        (r for r in strategy_results if r.name == "merkle_exact"), None
    )
    if merkle_result is not None:
        for other in strategy_results:
            if other.name == "merkle_exact":
                continue
            key = f"merkle_exact_vs_{other.name}"
            agreement[key] = compute_agreement(
                merkle_result, other, subtree_lookup
            )

    # Assemble the config payload
    config = {
        "canonicalizer": asdict(cfg),
        "filter": asdict(filter_cfg),
        "strategies": [r.name for r in strategy_results],
        "sequence": {
            "winnowing_w": w,
            "winnowing_t": t,
            "suffix_array": use_sa,
        },
    }

    corpus_stats = {
        "files": n_files,
        "total_loc": total_loc,
        "candidate_subtrees": len(all_subtrees),
        "after_filters": len(accepted),
    }

    report = CorpusReport(
        corpus=corpus_name,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        config=config,
        corpus_stats=corpus_stats,
        filter_stats=filter_stats,
        strategy_stats=strategy_stats,
        agreement=agreement,
        sibling_sequence_clones=sequence_stats,
        cross_corpus_overlaps={"note": "", "entries": []},
    )
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="experiments.ast_dedup.run",
        description="Run the AST near-duplicate detection experiment on a corpus.",
    )
    parser.add_argument(
        "--corpus",
        type=str,
        default=None,
        help="Corpus name (must exist in corpora.CORPORA).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run every corpus registered in corpora.CORPORA.",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=None,
        help="Optional cap on the number of .py files processed per corpus.",
    )
    parser.add_argument(
        "--strategies",
        type=str,
        default=None,
        help="Comma-separated subset of hashers.REGISTRY keys (default: all).",
    )
    parser.add_argument(
        "--no-filter",
        action="store_true",
        help="Skip the Phase 4 filter pipeline. Rejection counters stay at 0.",
    )
    parser.add_argument(
        "--ablate",
        type=str,
        default=None,
        choices=sorted(_ABLATIONS),
        help="Flip a CanonicalizerConfig flag for ablation experiments.",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="experiments/ast_dedup/reports",
        help="Directory into which to write the JSON and markdown reports.",
    )
    parser.add_argument(
        "--winnowing-w",
        type=int,
        default=4,
        help="Winnowing window width (default: 4).",
    )
    parser.add_argument(
        "--winnowing-t",
        type=int,
        default=4,
        help="Winnowing minimum run length (default: 4).",
    )
    parser.add_argument(
        "--suffix-array",
        action="store_true",
        help="Also run suffix-array based sibling-sequence detection.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.all and args.corpus is None:
        parser.error("either --corpus NAME or --all must be provided")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.all:
        corpora_to_run = list(corpora.CORPORA)
    else:
        corpora_to_run = [args.corpus]

    for name in corpora_to_run:
        report = run_one(name, args)
        json_path, md_path = write_report(report, out_dir)
        print(f"[run] wrote {json_path} and {md_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
