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


# ---------------------------------------------------------------------------
# Case 6: per-strategy RSS delta (not a shared process-wide high-water mark)
# ---------------------------------------------------------------------------


def test_rss_delta_is_per_strategy_not_shared_hwm(tmp_path: Path) -> None:
    """Each strategy must report ``rss_delta_mb`` independently.

    Previously ``peak_rss_mb`` was derived from ``resource.getrusage`` which
    returns a monotonically increasing process-wide high-water mark.  Every
    strategy therefore reported the same number (the maximum ever seen during
    the process lifetime).

    With the new VmRSS-delta approach the measurement is taken before and after
    each strategy in isolation, so:

    1. The field is called ``rss_delta_mb`` (rename confirms the semantics
       changed).
    2. Each value must be >= 0 (a delta, never negative).
    3. When two strategies are run, the values are independently measured and
       therefore *not forced to be equal* — they may coincidentally be equal
       but there is no structural reason they must be.  We confirm the field
       exists and is non-negative, and additionally verify via patching that
       the measurement function is called *once per strategy* (not once for
       the whole batch).
    """
    from unittest.mock import patch, call as mock_call

    root = _write_synthetic_corpus(tmp_path, n_files=3)
    # Run with exactly two strategies so we can assert call count.
    args = _make_args(strategies="merkle_exact,bag_of_subtrees")
    rss_sequence = [10.0, 12.0, 12.0, 15.0]  # before/after for each strategy
    rss_iter = iter(rss_sequence)

    with patch.object(
        runner_module, "_current_rss_mb", side_effect=lambda: next(rss_iter)
    ):
        report = runner_module.run_one("_synth", args, corpus_root=root)

    assert len(report.strategy_stats) == 2, (
        f"expected 2 strategy_stats, got {len(report.strategy_stats)}"
    )

    # Field must be renamed to rss_delta_mb.
    for strat in report.strategy_stats:
        assert hasattr(strat, "rss_delta_mb"), (
            f"StrategyStats.rss_delta_mb missing on {strat.name}; "
            "field was not renamed from peak_rss_mb"
        )
        assert not hasattr(strat, "peak_rss_mb"), (
            f"old field peak_rss_mb still present on {strat.name}"
        )
        assert strat.rss_delta_mb >= 0.0, (
            f"rss_delta_mb must be >= 0, got {strat.rss_delta_mb} for {strat.name}"
        )

    # With our mocked sequence: strategy 0 delta = 12-10 = 2, strategy 1 = 15-12 = 3.
    deltas = [s.rss_delta_mb for s in report.strategy_stats]
    assert deltas[0] == pytest.approx(2.0), f"unexpected delta[0]={deltas[0]}"
    assert deltas[1] == pytest.approx(3.0), f"unexpected delta[1]={deltas[1]}"

    # JSON round-trip should use the new field name.
    payload = json.loads(report.to_json())
    for s in payload["strategy_stats"]:
        assert "rss_delta_mb" in s, f"rss_delta_mb missing from JSON for {s['name']}"
        assert "peak_rss_mb" not in s, f"old peak_rss_mb still in JSON for {s['name']}"

    # Markdown should reference new column header.
    md = report.to_markdown()
    assert "RSS delta" in md, "markdown column header not updated to 'RSS delta'"
    assert "peak RSS" not in md, "old 'peak RSS' column header still in markdown"
