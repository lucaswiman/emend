#!/usr/bin/env python3
"""Benchmark CozoDB index building and query performance on the Django codebase.

Measures:
1. Index building time (full _build_facts_db pipeline)
2. Individual query times for common Datalog operations
3. Relation sizes and statistics

Usage:
    .venv/bin/python benchmarks/bench_cozodb.py
    .venv/bin/python benchmarks/bench_cozodb.py --quick       # fewer iterations
    .venv/bin/python benchmarks/bench_cozodb.py --skip-index  # reuse existing facts.db
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Ensure emend is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

DJANGO_REPO = "https://github.com/django/django.git"
DJANGO_TAG = "5.2"
CACHE_DIR = Path(__file__).resolve().parent / ".django-checkout"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ensure_django_checkout() -> Path:
    if CACHE_DIR.exists() and (CACHE_DIR / ".git").is_dir():
        print(f"  Django checkout present at {CACHE_DIR}", file=sys.stderr)
        return CACHE_DIR
    print(f"  Cloning Django (tag {DJANGO_TAG})...", file=sys.stderr)
    subprocess.run(
        ["git", "clone", "--branch", DJANGO_TAG, "--depth", "1", DJANGO_REPO, str(CACHE_DIR)],
        capture_output=True, text=True, check=True, timeout=300,
    )
    return CACHE_DIR


def _timeit(func, iterations=3, label=""):
    """Run func N times, return (median_seconds, all_times, last_result)."""
    times = []
    result = None
    for i in range(iterations):
        gc.collect()
        t0 = time.perf_counter()
        result = func()
        elapsed = time.perf_counter() - t0
        times.append(elapsed)
        print(f"    {label} iter {i+1}/{iterations}: {elapsed:.3f}s", file=sys.stderr)
    times.sort()
    median = times[len(times) // 2]
    return median, times, result


def _fmt(seconds: float) -> str:
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.2f}s"


# ---------------------------------------------------------------------------
# Relation statistics
# ---------------------------------------------------------------------------

def collect_relation_stats(client) -> dict[str, int]:
    """Count rows in each CozoDB relation."""
    # Map relation name -> positional column patterns for scanning
    relations = {
        "symbol": "a, _, _, _, _, _, _",
        "call": "a, b, c, d, e, f, g",
        "call_by_callee": "a, b, c, d, e, f, g",
        "call_by_file": "a, b, c, d, e, f, g",
        "reference": "a, b, c, d, e, f, g",
        "cfg_block": "a, b, c, d, e",
        "cfg_edge": "a, b, c, d, e, f, g",
        "def_use": "a, b, c, d, e, f, g, h, i, j",
        "method_call": "a, b, c, d, e, f",
        "source_loc": "a, b, c, d, e, f, g",
        "import": "a, b, c, d, e",
        "ref_by_block": "a, b, c, d",
        "reachable_block": "a, b, c",
        "module_level_ref": "a, b, c",
        "decorator_on": "a, b",
        "trace_flow": "a, b, c, d, e, f, g",
        "type_binding": "a, b, c, d, e",
        "func_summary": "a, b, c, d, e",
    }
    stats = {}
    for rel, cols in relations.items():
        try:
            result = client.run(f"?[{cols}] := *{rel}[{cols}]")
            stats[rel] = len(result["rows"])
        except Exception:
            stats[rel] = -1
    return stats


# ---------------------------------------------------------------------------
# Index building benchmark
# ---------------------------------------------------------------------------

def benchmark_index_build(django_dir: Path, iterations: int) -> dict:
    """Benchmark full _build_facts_db pipeline."""
    from emend.transform import _build_facts_db, _find_project_root

    cache_dir = Path(django_dir) / ".emend" / "cache"

    def _build():
        # Remove existing facts.db so we measure a fresh build
        facts_db = cache_dir / "facts.db"
        if facts_db.exists():
            facts_db.unlink()
        _build_facts_db(str(django_dir))
        return str(facts_db)

    median, times, facts_path = _timeit(_build, iterations=iterations, label="index_build")

    return {
        "median": median,
        "times": times,
        "facts_path": facts_path,
    }


# ---------------------------------------------------------------------------
# Query benchmarks
# ---------------------------------------------------------------------------

def benchmark_queries(facts_path: str, iterations: int) -> dict[str, dict]:
    """Benchmark individual CozoDB queries on a populated facts.db."""
    from emend.fact_graph import FactGraph

    graph = FactGraph(db_path=facts_path)
    client = graph._client

    results = {}

    # Use correct Django qualified names
    QS = "django.db.models.query.QuerySet"
    QS_FILTER = "django.db.models.query.QuerySet.filter"
    MODEL = "django.db.models.base.Model"
    QUERY_PY = "django/db/models/query.py"

    # --- High-level API queries (current implementation) ---

    def _refs_queryset():
        return graph.refs_datalog(QS)

    def _refs_model():
        return graph.refs_datalog(MODEL)

    def _callers():
        return graph.callers_datalog(QS_FILTER)

    def _callees():
        return graph.callees_datalog(QS_FILTER)

    def _graph_full():
        return graph.graph_datalog()

    def _graph_file():
        return graph.graph_datalog(QUERY_PY)

    def _transitive_callers():
        return graph.transitive_callers(QS_FILTER)

    def _transitive_callees():
        return graph.transitive_callees(QS_FILTER)

    def _dead_code_simple():
        return graph.dead_code()

    def _dead_code_unified():
        return graph.dead_code_unified()

    def _unreachable_blocks():
        return graph.unreachable_blocks_datalog()

    # --- Positional binding vs == filter (optimization comparison) ---

    def _refs_positional():
        """refs with positional $qn binding (indexed)."""
        return client.run(
            "?[fp, line, col, kind, fq, bid] := *reference[$qn, fp, line, col, kind, fq, bid]",
            {"qn": MODEL},
        )

    def _refs_eq_filter():
        """refs with == filter (table scan)."""
        return client.run(
            "?[fp, line, col, kind, fq, bid] := "
            "*reference[sqn, fp, line, col, kind, fq, bid], sqn == $qn",
            {"qn": MODEL},
        )

    def _callees_positional():
        """callees with positional $fqn binding (indexed)."""
        return client.run(
            "?[callee, fp, line, col, fq, bid] := *call[$fqn, callee, fp, line, col, fq, bid]",
            {"fqn": QS_FILTER},
        )

    def _callees_eq_filter():
        """callees with == filter (table scan)."""
        return client.run(
            "?[caller, callee, fp, line, col, fq, bid] := "
            "*call[caller, callee, fp, line, col, fq, bid], caller == $fqn",
            {"fqn": QS_FILTER},
        )

    # --- Key ordering tests ---

    def _call_filter_1st_key():
        """call: positional binding on caller_qn (1st key) — indexed."""
        return client.run(
            '?[callee, fp, line] := *call[$qn, callee, fp, line, _, _, _]',
            {"qn": QS_FILTER},
        )

    def _call_filter_2nd_key():
        """call: positional binding on callee_qn (2nd key) — scan."""
        return client.run(
            '?[caller, fp, line] := *call[caller, $qn, fp, line, _, _, _]',
            {"qn": QS_FILTER},
        )

    def _ref_filter_1st_key():
        """reference: positional binding on symbol_qn (1st key) — indexed."""
        return client.run(
            '?[fp, line, col, kind] := *reference[$qn, fp, line, col, kind, _, _]',
            {"qn": MODEL},
        )

    def _ref_filter_2nd_key():
        """reference: positional binding on file_path (2nd key) — scan."""
        return client.run(
            '?[sqn, line, col, kind] := *reference[sqn, $fp, line, col, kind, _, _]',
            {"fp": QUERY_PY},
        )

    def _cfg_filter_1st():
        return client.run(
            '?[fq, fb, tb, ek] := *cfg_edge[$fp, fq, fb, tb, ek, _, _]',
            {"fp": QUERY_PY},
        )

    def _cfg_filter_2nd():
        return client.run(
            '?[fp, fb, tb, ek] := *cfg_edge[fp, $fq, fb, tb, ek, _, _]',
            {"fq": QS_FILTER},
        )

    # --- Full scan benchmarks ---

    def _scan_symbol():
        return client.run("?[qn, fp, name, kind] := *symbol[qn, fp, name, kind, _, _, _]")

    def _scan_call():
        return client.run("?[caller, callee, fp] := *call[caller, callee, fp, _, _, _, _]")

    def _scan_reference():
        return client.run("?[sq, fp, line] := *reference[sq, fp, line, _, _, _, _]")

    # --- Join benchmarks ---

    def _join_ref_reachable():
        """The critical join in dead_code_unified."""
        return client.run(
            "live[sq] := *ref_by_block[fp, fq, bid, sq], "
            "*reachable_block[fp, fq, bid], sq != fq\n"
            "?[sq] := live[sq]"
        )

    benchmarks = [
        # High-level API (current implementation)
        ("refs(QuerySet)", "refs_datalog — positional key lookup", _refs_queryset),
        ("refs(Model)", "refs_datalog — positional key lookup", _refs_model),
        ("callers(QS.filter)", "callers_datalog — reverse-index lookup", _callers),
        ("callees(QS.filter)", "callees_datalog — positional key lookup", _callees),
        ("graph(full)", "graph_datalog — full scan", _graph_full),
        ("graph(query.py)", "graph_datalog — file-key lookup", _graph_file),
        ("transitive_callers", "recursive Datalog via reverse index", _transitive_callers),
        ("transitive_callees", "recursive Datalog via leading key", _transitive_callees),
        ("dead_code_simple", "no-ref check", _dead_code_simple),
        ("dead_code_unified", "reachable+entry points+module_level_ref", _dead_code_unified),
        ("unreachable_blocks", "CFG reachability", _unreachable_blocks),
        # Optimization comparison: positional vs == filter
        ("refs POSITIONAL", "positional $qn (OPTIMIZED)", _refs_positional),
        ("refs == FILTER", "sqn == $qn (CURRENT)", _refs_eq_filter),
        ("callees POSITIONAL", "positional $fqn (OPTIMIZED)", _callees_positional),
        ("callees == FILTER", "caller == $fqn (CURRENT)", _callees_eq_filter),
        # Key ordering comparison
        ("call 1st-key bind", "caller_qn positional (indexed)", _call_filter_1st_key),
        ("call 2nd-key bind", "callee_qn positional (scan!)", _call_filter_2nd_key),
        ("ref 1st-key bind", "symbol_qn positional (indexed)", _ref_filter_1st_key),
        ("ref 2nd-key bind", "file_path positional (scan!)", _ref_filter_2nd_key),
        ("cfg 1st-key bind", "file_path positional (indexed)", _cfg_filter_1st),
        ("cfg 2nd-key bind", "func_qn positional (scan!)", _cfg_filter_2nd),
        # Full scans for reference
        ("scan: symbol", "full table scan (40K)", _scan_symbol),
        ("scan: call", "full table scan (275K)", _scan_call),
        ("scan: reference", "full table scan (736K)", _scan_reference),
        # Join
        ("join: ref+reachable", "dead_code key join (349K×86K)", _join_ref_reachable),
    ]

    for name, desc, func in benchmarks:
        print(f"\n  [{name}] {desc}", file=sys.stderr)
        try:
            median, times, result = _timeit(func, iterations=iterations, label=name)
            row_count = 0
            if isinstance(result, list):
                row_count = len(result)
            elif isinstance(result, tuple):
                row_count = sum(len(x) for x in result if isinstance(x, list))
            elif isinstance(result, set):
                row_count = len(result)
            elif isinstance(result, dict) and "rows" in result:
                row_count = len(result["rows"])
            results[name] = {
                "description": desc,
                "median": median,
                "times": times,
                "rows": row_count,
            }
        except Exception as e:
            print(f"    ERROR: {e}", file=sys.stderr)
            results[name] = {
                "description": desc,
                "median": -1,
                "times": [],
                "rows": 0,
                "error": str(e),
            }

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Benchmark CozoDB performance on Django")
    parser.add_argument("--quick", action="store_true", help="1 iteration per benchmark")
    parser.add_argument("--skip-index", action="store_true", help="Reuse existing facts.db")
    parser.add_argument("--iterations", type=int, default=None)
    parser.add_argument("--json", dest="json_output", action="store_true")
    args = parser.parse_args()

    iterations = args.iterations or (1 if args.quick else 3)

    print("=" * 70, file=sys.stderr)
    print("  CozoDB Performance Benchmark (Django codebase)", file=sys.stderr)
    print("=" * 70, file=sys.stderr)

    # 1. Ensure Django checkout
    print("\n[1] Ensuring Django checkout...", file=sys.stderr)
    django_dir = ensure_django_checkout()

    # Count Python files
    py_files = list(Path(django_dir / "django").rglob("*.py"))
    print(f"  {len(py_files)} Python files in django/", file=sys.stderr)

    cache_dir = django_dir / ".emend" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    facts_path = str(cache_dir / "facts.db")

    # 2. Index building
    index_result = None
    if not args.skip_index:
        print(f"\n[2] Benchmarking index build ({iterations} iterations)...", file=sys.stderr)
        index_result = benchmark_index_build(django_dir, iterations=iterations)
        print(f"\n  Index build median: {_fmt(index_result['median'])}", file=sys.stderr)
        facts_path = index_result["facts_path"]
    else:
        print("\n[2] Skipping index build (--skip-index)", file=sys.stderr)
        if not Path(facts_path).exists():
            print("  ERROR: No facts.db found. Run without --skip-index first.", file=sys.stderr)
            sys.exit(1)

    # 3. Relation statistics
    print("\n[3] Collecting relation statistics...", file=sys.stderr)
    from emend.fact_graph import FactGraph
    graph = FactGraph(db_path=facts_path)
    stats = collect_relation_stats(graph._client)
    print(f"\n  {'Relation':<20} {'Rows':>10}", file=sys.stderr)
    print(f"  {'-'*20} {'-'*10}", file=sys.stderr)
    total_rows = 0
    for rel, count in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {rel:<20} {count:>10,}", file=sys.stderr)
        if count > 0:
            total_rows += count
    print(f"  {'TOTAL':<20} {total_rows:>10,}", file=sys.stderr)
    del graph

    # 4. Query benchmarks
    print(f"\n[4] Benchmarking queries ({iterations} iterations each)...", file=sys.stderr)
    query_results = benchmark_queries(facts_path, iterations=iterations)

    # 5. Summary
    print("\n" + "=" * 70, file=sys.stderr)
    print("  RESULTS SUMMARY", file=sys.stderr)
    print("=" * 70, file=sys.stderr)

    if index_result:
        print(f"\n  Index build:  {_fmt(index_result['median'])} (median of {iterations})", file=sys.stderr)

    print(f"\n  {'Query':<28} {'Median':>10} {'Rows':>8}", file=sys.stderr)
    print(f"  {'-'*28} {'-'*10} {'-'*8}", file=sys.stderr)
    for name, data in query_results.items():
        if data["median"] >= 0:
            print(
                f"  {name:<28} {_fmt(data['median']):>10} {data['rows']:>8}",
                file=sys.stderr,
            )

    # JSON output
    if args.json_output:
        output = {
            "index_build": index_result,
            "relation_stats": stats,
            "queries": {
                k: {
                    "description": v["description"],
                    "median_seconds": round(v["median"], 6),
                    "times": [round(t, 6) for t in v["times"]],
                    "rows": v["rows"],
                }
                for k, v in query_results.items()
            },
        }
        print(json.dumps(output, indent=2))

    print("\nDone.", file=sys.stderr)


if __name__ == "__main__":
    main()
