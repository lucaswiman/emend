#!/usr/bin/env python3
"""Benchmark suite for emend operations against the Django codebase.

Clones Django (pinned to tag 5.2, commit 9e7cc2b628fe8fd3895986af9b7fc9525034c1b0)
and times various emend commands against it.

Usage:
    python benchmarks/bench_django.py            # full run (3 iterations)
    python benchmarks/bench_django.py --quick     # quick run (1 iteration)
    python benchmarks/bench_django.py --json      # JSON output
    python benchmarks/bench_django.py --json --quick
"""
from __future__ import annotations

import argparse
import datetime
import json
import shutil
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path

# ---------------------------------------------------------------------------
# Shared utilities (path setup for script-mode execution)
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_utils import _log, _run, check_emend_available, TIMEOUT  # noqa: E402
from django_checkout import (  # noqa: E402
    ensure_django_checkout,
    ensure_scaled_checkout,
    setup_lint_rules,
    SCALED_COPIES,
)

# ---------------------------------------------------------------------------
# Benchmark definitions
# ---------------------------------------------------------------------------

LINT_RULES_YAML_SUMMARY = "5 pattern rules (print, isinstance, hasattr, open, mutable-default)"

# Each benchmark entry: (name, description, args_list, ok_codes)
# args_list is a list of CLI arguments to pass after 'emend'.
# ok_codes is a set of return codes considered successful (default: {0}).
# The working directory will be set to the Django checkout.
BENCHMARKS: list[tuple[str, str, list[str], set[int]]] = [
    (
        "index_full",
        "index . --type-engine none (full index build)",
        ["index", ".", "--type-engine", "none"],
        {0},
    ),
    (
        "search_symbol_lookup",
        "search django/db/models/base.py::Model (symbol lookup)",
        ["search", "django/db/models/base.py::Model"],
        {0},
    ),
    (
        "search_summary_subtree",
        "search --output summary django/db/models/ (symbol listing)",
        ["search", "django/db/models/", "--output", "summary"],
        {0},
    ),
    (
        "find_pattern",
        'find "$X.objects.filter($...ARGS)" (pattern matching)',
        ["search", "$X.objects.filter($...ARGS)", "django/"],
        {0},
    ),
    (
        "find_pattern_constrained",
        'find "$X.objects.filter($...ARGS)" --where "class $C(TestCase):"',
        [
            "search",
            "$X.objects.filter($...ARGS)",
            "django/",
            "--where",
            "class $C(TestCase):",
        ],
        {0},
    ),
    (
        "refs_queryset",
        "refs django/db/models/query.py::QuerySet --project django/db/",
        ["refs", "django/db/models/query.py::QuerySet", "--project", "django/db/"],
        {0},
    ),
    (
        "rename_dry_run",
        "rename QuerySet.filter -> filter_queryset --project django/db/ (dry-run)",
        [
            "rename",
            "django/db/models/query.py::QuerySet.filter",
            "--to",
            "filter_queryset",
            "--project", "django/db/",
        ],
        {0},
    ),
    (
        "lint_db_models",
        "lint django/db/models/ with 5 pattern rules",
        ["lint", "django/db/models/"],
        {0, 1},  # exit 1 = violations found (expected for Django)
    ),
    (
        "lint_full_django",
        "lint django/ with 5 pattern rules (full project)",
        ["lint", "django/"],
        {0, 1},  # exit 1 = violations found (expected for Django)
    ),
    (
        "graph_file",
        "graph django/db/models/query.py (call graph)",
        ["graph", "django/db/models/query.py", "--project", "."],
        {0},
    ),
]

# Scaled benchmarks run against the 50x-duplicated codebase.
SCALED_BENCHMARKS: list[tuple[str, str, list[str], set[int]]] = [
    (
        "scaled_find_optional",
        f'search "Optional[$X]" on {SCALED_COPIES}x django (~38K py files)',
        ["search", "Optional[$X]", "."],
        {0},
    ),
    (
        "scaled_find_filter",
        f'search "$X.objects.filter($...ARGS)" on {SCALED_COPIES}x django',
        ["search", "$X.objects.filter($...ARGS)", "."],
        {0},
    ),
    (
        "scaled_find_isinstance",
        f'search "isinstance($X, str)" on {SCALED_COPIES}x django',
        ["search", "isinstance($X, str)", "."],
        {0},
    ),
    (
        "scaled_find_print",
        f'search "print($...ARGS)" on {SCALED_COPIES}x django',
        ["search", "print($...ARGS)", "."],
        {0},
    ),
    (
        "scaled_find_assign",
        f'search "$X = None" on {SCALED_COPIES}x django',
        ["search", "$X = None", "."],
        {0},
    ),
    (
        "scaled_summary",
        f'search --output summary django1/django/db/models/ on {SCALED_COPIES}x django',
        ["search", "django1/django/db/models/", "--output", "summary"],
        {0},
    ),
]


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------


def run_benchmark(
    emend_cmd: list[str],
    django_dir: Path,
    args: list[str],
    iterations: int,
    ok_codes: set[int] | None = None,
    setup: Callable | None = None,
) -> dict:
    """Run a single benchmark for the specified number of iterations.

    Args:
        setup: Optional callable invoked before each iteration (e.g. to clear
            caches for cold-start benchmarks like ``index``).
    """
    if ok_codes is None:
        ok_codes = {0}

    full_cmd = emend_cmd + args
    times: list[float] = []
    last_returncode = 0
    last_stdout = ""
    last_stderr = ""

    for _i in range(iterations):
        if setup is not None:
            setup()
        start = time.perf_counter()
        try:
            result = _run(full_cmd, cwd=django_dir)
        except __import__("subprocess").TimeoutExpired:
            elapsed = time.perf_counter() - start
            times.append(elapsed)
            last_returncode = -1
            last_stderr = f"TIMEOUT after {TIMEOUT}s"
            continue
        elapsed = time.perf_counter() - start

        times.append(elapsed)
        last_returncode = result.returncode
        last_stdout = result.stdout
        last_stderr = result.stderr

    output_lines = len(last_stdout.strip().splitlines()) if last_stdout else 0
    is_ok = last_returncode in ok_codes

    return {
        "times": times,
        "min": min(times),
        "max": max(times),
        "mean": statistics.mean(times),
        "median": statistics.median(times),
        "iterations": iterations,
        "returncode": last_returncode,
        "ok": is_ok,
        "output_lines": output_lines,
        "error": last_stderr.strip()[:500] if not is_ok else None,
    }


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------


def _fmt_time(seconds: float) -> str:
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.2f}s"


def print_table(results: dict[str, dict]) -> None:
    name_width = max(len(name) for name in results) + 2
    print()
    print("=" * 80)
    print("  emend Django Benchmark Results")
    print("=" * 80)
    print()
    header = (
        f"  {'Benchmark':<{name_width}}"
        f"  {'Min':>8}"
        f"  {'Mean':>8}"
        f"  {'Median':>8}"
        f"  {'Max':>8}"
        f"  {'Runs':>5}"
        f"  {'Lines':>7}"
        f"  {'Status':>7}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    for name, data in results.items():
        status = "OK" if data["ok"] else "FAIL"
        print(
            f"  {name:<{name_width}}"
            f"  {_fmt_time(data['min']):>8}"
            f"  {_fmt_time(data['mean']):>8}"
            f"  {_fmt_time(data['median']):>8}"
            f"  {_fmt_time(data['max']):>8}"
            f"  {data['iterations']:>5}"
            f"  {data['output_lines']:>7}"
            f"  {status:>7}"
        )
    print()

    failures = {name: data for name, data in results.items() if not data["ok"]}
    if failures:
        print("Failures:")
        for name, data in failures.items():
            print(f"  {name}: exit code {data['returncode']}")
            if data.get("error"):
                for line in data["error"].splitlines()[:5]:
                    print(f"    {line}")
        print()


def _get_emend_commit() -> str:
    try:
        result = _run(["git", "rev-parse", "HEAD"])
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def print_json(
    results: dict[str, dict],
    benchmarks_meta: list[tuple[str, str]],
    label: str | None = None,
    save_path: str | None = None,
) -> None:
    from django_checkout import DJANGO_COMMIT
    output: dict = {
        "django_commit": DJANGO_COMMIT,
        "emend_commit": _get_emend_commit(),
        "label": label or "",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "benchmarks": {},
    }
    for name, desc in benchmarks_meta:
        if name in results:
            data = results[name]
            output["benchmarks"][name] = {
                "description": desc,
                "min_seconds": round(data["min"], 4),
                "max_seconds": round(data["max"], 4),
                "mean_seconds": round(data["mean"], 4),
                "median_seconds": round(data["median"], 4),
                "iterations": data["iterations"],
                "output_lines": data["output_lines"],
                "ok": data["ok"],
                "returncode": data["returncode"],
                "error": data.get("error"),
            }
    json_str = json.dumps(output, indent=2)
    if save_path:
        Path(save_path).write_text(json_str)
        _log(f"  Results saved to {save_path}")
    else:
        print(json_str)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark emend operations against the Django codebase.",
    )
    parser.add_argument("--quick", action="store_true",
                        help="Run only 1 iteration of each benchmark.")
    parser.add_argument("--json", dest="json_output", action="store_true",
                        help="Output results as JSON.")
    parser.add_argument("--iterations", type=int, default=None,
                        help="Override number of iterations (default: 3, or 1 with --quick).")
    parser.add_argument("--only", type=str, default=None,
                        help="Run only benchmarks whose name contains this substring.")
    parser.add_argument("--label", type=str, default=None,
                        help="Prose description of what's being tested (required with --save).")
    parser.add_argument("--save", type=str, default=None,
                        help="Save JSON results to this file path.")
    parser.add_argument("--scaled", action="store_true",
                        help=f"Run scaled benchmarks against {SCALED_COPIES}x-duplicated Django codebase.")
    parser.add_argument("--scaled-only", action="store_true",
                        help="Run ONLY the scaled benchmarks (skip standard benchmarks).")
    args = parser.parse_args()

    if args.save and not args.label:
        print("ERROR: --label is required when using --save.", file=sys.stderr)
        sys.exit(1)

    global _quiet  # noqa: PLW0603
    from bench_utils import _quiet as _q  # read current value
    iterations = args.iterations or (1 if args.quick else 3)

    if args.json_output:
        import bench_utils
        bench_utils._quiet = True

    _log("emend Django Benchmark Suite")
    _log("=" * 40)
    _log("")

    _log("[1/4] Checking emend installation...")
    emend_cmd = check_emend_available()
    _log(f"  Using: {' '.join(str(c) for c in emend_cmd)}")
    _log("")

    _log("[2/4] Ensuring Django checkout...")
    django_dir = ensure_django_checkout()
    setup_lint_rules(django_dir)
    _log("")

    scaled_dir: Path | None = None
    if args.scaled or args.scaled_only:
        _log("[2b/4] Ensuring scaled Django checkout...")
        scaled_dir = ensure_scaled_checkout(django_dir)
        _log("")

    all_benchmarks: list[tuple[str, str, list[str], set[int]]] = []
    all_dirs: list[tuple[str, Path]] = []

    if not args.scaled_only:
        selected_benchmarks = BENCHMARKS
        if args.only:
            selected_benchmarks = [b for b in BENCHMARKS if args.only in b[0]]
        all_benchmarks.extend(selected_benchmarks)
        all_dirs.extend((b[0], django_dir) for b in selected_benchmarks)

    if (args.scaled or args.scaled_only) and scaled_dir is not None:
        selected_scaled = SCALED_BENCHMARKS
        if args.only:
            selected_scaled = [b for b in SCALED_BENCHMARKS if args.only in b[0]]
        all_benchmarks.extend(selected_scaled)
        all_dirs.extend((b[0], scaled_dir) for b in selected_scaled)

    if not all_benchmarks:
        print(
            f"ERROR: No benchmarks match --only={args.only!r}. "
            f"Available: {', '.join(b[0] for b in BENCHMARKS + SCALED_BENCHMARKS)}",
            file=sys.stderr,
        )
        sys.exit(1)

    _log(f"[3/4] Running benchmarks ({iterations} iteration(s) each)...")
    _log("")

    results: dict[str, dict] = {}
    total_start = time.perf_counter()
    dir_map = {name: cwd for name, cwd in all_dirs}

    # Setup functions for benchmarks that need pre-iteration cleanup.
    def _clear_cache(cwd: Path) -> Callable:
        """Return a callable that clears the .emend/cache directory."""
        def _setup():
            cache = cwd / ".emend" / "cache"
            if cache.exists():
                shutil.rmtree(cache)
        return _setup

    _SETUP_MAP: dict[str, Callable] = {
        "index_full": _clear_cache(django_dir),
    }

    for bench_name, bench_desc, bench_args, bench_ok_codes in all_benchmarks:
        cwd = dir_map[bench_name]
        _log(f"  Running: {bench_desc}...")

        data = run_benchmark(
            emend_cmd, cwd, bench_args, iterations,
            ok_codes=bench_ok_codes,
            setup=_SETUP_MAP.get(bench_name),
        )
        results[bench_name] = data

        status = "OK" if data["ok"] else f"FAIL (exit {data['returncode']})"
        _log(
            f"    -> {status}  median={_fmt_time(data['median'])}  "
            f"({data['output_lines']} output lines)"
        )

    total_elapsed = time.perf_counter() - total_start

    benchmarks_meta = [(b[0], b[1]) for b in all_benchmarks]
    if args.save:
        print_json(results, benchmarks_meta, label=args.label, save_path=args.save)
        print_table(results)
        print(f"  Total wall time: {_fmt_time(total_elapsed)}")
        print()
    elif args.json_output:
        print_json(results, benchmarks_meta, label=args.label)
    else:
        print_table(results)
        print(f"  Total wall time: {_fmt_time(total_elapsed)}")
        print()


if __name__ == "__main__":
    main()
