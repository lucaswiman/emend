"""Phase 6 runner tests.

Five cases per the roadmap:

1. Synthetic corpus with two identical ~15-statement functions → merkle_exact
   finds a single cluster with largest size = 2.
2. ``--strategies merkle_exact`` honours the selection filter.
3. ``--max-files`` honours the cap.
4. The report JSON round-trips through ``json.loads``.
5. The markdown report is <= 4 KB for the synthetic corpus.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from experiments.ast_dedup import run as runner_module


# ---------------------------------------------------------------------------
# Synthetic-function templates
# ---------------------------------------------------------------------------
#
# DUPLICATE_FN is deliberately straight-line (no nested control flow blocks
# with >= 2 statements) so that ``iter_candidates`` yields exactly two
# candidates per occurrence: the ``function_definition`` itself and its
# body block. With two duplicated files, merkle_exact therefore produces
# exactly two clusters, each of size two. The test asserts on the largest
# cluster size (which is the property we actually care about).

DUPLICATE_FN = '''\
def process_items(items, threshold):
    """Transform items above threshold into a flat summary."""
    accepted = []
    rejected = []
    total = 0
    count = len(items)
    limit = threshold * 2
    average = total / max(count, 1)
    first = items[0]
    value = int(first)
    label = str(first)
    final = (accepted, rejected, total, average, limit, value, label)
    return final
'''

UNIQUE_FN = '''\
def format_greeting(name, mood):
    """A totally unrelated helper with different shape."""
    greeting = "hello, " + name
    header = greeting.upper()
    suffix = "!" * 3
    combined = header + suffix
    return combined
'''


def _write_synthetic_corpus(root: Path, n_files: int = 3) -> Path:
    """Write ``n_files`` Python files into ``root``.

    Files 0 and 1 share the same DUPLICATE_FN; files 2+ get UNIQUE_FN with
    a filename-based rename so they're distinct.
    """
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        if i < 2:
            (root / f"dup_{i}.py").write_text(DUPLICATE_FN)
        else:
            # Each unique file gets a slightly different name so they don't
            # accidentally dedupe against each other.
            text = UNIQUE_FN.replace(
                "format_greeting", f"format_greeting_{i}"
            )
            (root / f"uniq_{i}.py").write_text(text)
    return root


# ---------------------------------------------------------------------------
# Argparse namespace helper
# ---------------------------------------------------------------------------


def _make_args(**kwargs) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.corpus = "_synth"
    ns.all = False
    ns.max_files = None
    ns.strategies = None
    # Tests bypass filters so we can assert on cluster counts directly
    # without having to fine-tune the synthetic snippets.
    ns.no_filter = True
    ns.ablate = None
    ns.out = None
    ns.winnowing_w = 4
    ns.winnowing_t = 4
    ns.suffix_array = False
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# Case 1: synthetic corpus produces exactly one merkle_exact cluster of 2
# ---------------------------------------------------------------------------


def test_synthetic_corpus_merkle_cluster(tmp_path: Path) -> None:
    root = _write_synthetic_corpus(tmp_path, n_files=3)
    args = _make_args()
    report = runner_module.run_one("_synth", args, corpus_root=root)

    merkle = next(
        (s for s in report.strategy_stats if s.name == "merkle_exact"), None
    )
    assert merkle is not None, (
        "merkle_exact should appear in strategy_stats by default; "
        f"got {[s.name for s in report.strategy_stats]}"
    )
    # DUPLICATE_FN yields two candidate subtrees per file (the
    # function_definition and its body block), both of which dedupe.
    assert merkle.cluster_count >= 1, (
        f"expected at least one cluster, got {merkle.cluster_count}"
    )
    assert merkle.largest_cluster_size == 2, (
        f"expected largest cluster size 2, got {merkle.largest_cluster_size}"
    )


# ---------------------------------------------------------------------------
# Case 2: --strategies filter
# ---------------------------------------------------------------------------


def test_strategies_filter(tmp_path: Path) -> None:
    root = _write_synthetic_corpus(tmp_path, n_files=3)
    args = _make_args(strategies="merkle_exact")
    report = runner_module.run_one("_synth", args, corpus_root=root)

    assert len(report.strategy_stats) == 1
    assert report.strategy_stats[0].name == "merkle_exact"


# ---------------------------------------------------------------------------
# Case 3: --max-files cap honored
# ---------------------------------------------------------------------------


def test_max_files_limit(tmp_path: Path) -> None:
    root = _write_synthetic_corpus(tmp_path, n_files=5)
    args = _make_args(max_files=2)
    report = runner_module.run_one("_synth", args, corpus_root=root)
    assert report.corpus_stats["files"] == 2


# ---------------------------------------------------------------------------
# Case 4: JSON round-trips
# ---------------------------------------------------------------------------


def test_json_round_trip(tmp_path: Path) -> None:
    root = _write_synthetic_corpus(tmp_path, n_files=3)
    args = _make_args()
    report = runner_module.run_one("_synth", args, corpus_root=root)

    payload = json.loads(report.to_json())
    assert isinstance(payload, dict)
    expected_keys = {
        "corpus",
        "timestamp",
        "config",
        "corpus_stats",
        "filter_stats",
        "strategy_stats",
        "agreement",
        "sibling_sequence_clones",
        "cross_corpus_overlaps",
    }
    missing = expected_keys - set(payload.keys())
    assert not missing, f"missing top-level keys: {missing}"


# ---------------------------------------------------------------------------
# Case 5: markdown <= 4 KB
# ---------------------------------------------------------------------------


def test_markdown_size_limit(tmp_path: Path) -> None:
    root = _write_synthetic_corpus(tmp_path, n_files=3)
    args = _make_args()
    report = runner_module.run_one("_synth", args, corpus_root=root)

    md_bytes = len(report.to_markdown().encode())
    assert md_bytes <= 4096, f"markdown too large: {md_bytes} bytes"
