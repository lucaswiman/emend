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
import subprocess
import sys
import textwrap
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DJANGO_REPO = "https://github.com/django/django.git"
DJANGO_COMMIT = "9e7cc2b628fe8fd3895986af9b7fc9525034c1b0"
DJANGO_TAG = "5.2"  # Tag that resolves to DJANGO_COMMIT (annotated tag)
CACHE_DIR = Path(__file__).resolve().parent / ".django-checkout"

LINT_RULES_YAML = textwrap.dedent("""\
    macros:
      print_call: "print($...ARGS)"
      isinstance_str: "isinstance($X, str)"

    rules:
      no-print:
        find: "{print_call}"
        message: "Avoid bare print() calls in production code."

      isinstance-str:
        find: "{isinstance_str}"
        message: "Consider using type($X) is str or more specific checks."

      no-hasattr:
        find: "hasattr($X, $Y)"
        message: "hasattr() swallows exceptions; use try/except or check __dict__."

      no-open-without-encoding:
        find: "open($PATH)"
        message: "Specify encoding when calling open()."

      no-mutable-default:
        find: "def $F($...A, $P=[], $...B):"
        message: "Mutable default argument; use None and initialize inside."
""")

# Each benchmark entry: (name, description, args_list, ok_codes)
# args_list is a list of CLI arguments to pass after 'emend'.
# ok_codes is a set of return codes considered successful (default: {0}).
# The working directory will be set to the Django checkout.
BENCHMARKS: list[tuple[str, str, list[str], set[int]]] = [
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


TIMEOUT = 600  # seconds -- generous limit for slow operations on large codebases

# Whether to suppress progress output (for JSON mode).
_quiet = False


def _log(msg: str) -> None:
    """Print a progress message to stderr (so JSON output stays clean)."""
    if not _quiet:
        print(msg, file=sys.stderr)


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, raising on failure with combined output."""
    kwargs.setdefault("timeout", TIMEOUT)
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def ensure_django_checkout() -> Path:
    """Clone Django into the cache directory if not already present.

    Uses a shallow clone at the exact commit (via its tag) to minimize
    download size.  Falls back to a full clone if needed.

    Returns the path to the Django checkout.
    """
    if CACHE_DIR.exists() and (CACHE_DIR / ".git").is_dir():
        # Verify we have the right commit checked out.
        result = _run(["git", "rev-parse", "HEAD"], cwd=CACHE_DIR)
        if result.returncode == 0 and result.stdout.strip() == DJANGO_COMMIT:
            _log(f"  Django checkout already present at {CACHE_DIR}")
            return CACHE_DIR
        else:
            _log("  Django checkout exists but wrong commit, resetting...")
            _run(
                ["git", "fetch", "--depth", "1", "origin", f"tag {DJANGO_TAG}"],
                cwd=CACHE_DIR,
            )
            result = _run(
                ["git", "checkout", f"tags/{DJANGO_TAG}"], cwd=CACHE_DIR
            )
            if result.returncode != 0:
                _log(f"  Failed to checkout tag {DJANGO_TAG}, re-cloning...")
                shutil.rmtree(CACHE_DIR)

    if not CACHE_DIR.exists():
        _log(f"  Cloning Django (tag {DJANGO_TAG}) into {CACHE_DIR}...")
        # Shallow clone at the exact tag to minimize download size.
        result = _run([
            "git", "clone",
            "--branch", DJANGO_TAG,
            "--depth", "1",
            DJANGO_REPO,
            str(CACHE_DIR),
        ])
        if result.returncode != 0:
            print(
                f"ERROR: Failed to clone Django at tag {DJANGO_TAG}:\n"
                f"{result.stderr}",
                file=sys.stderr,
            )
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
            sys.exit(1)

        # Verify the commit matches what we expect.
        result = _run(["git", "rev-parse", "HEAD"], cwd=CACHE_DIR)
        actual_commit = (
            result.stdout.strip() if result.returncode == 0 else "<unknown>"
        )
        if actual_commit != DJANGO_COMMIT:
            print(
                f"  WARNING: Tag {DJANGO_TAG} resolved to {actual_commit}, "
                f"expected {DJANGO_COMMIT}. Proceeding anyway.",
                file=sys.stderr,
            )

    _log(f"  Django checkout ready at {CACHE_DIR}")
    return CACHE_DIR


def setup_lint_rules(django_dir: Path) -> None:
    """Create .emend/patterns.yaml in the Django checkout for lint benchmarks."""
    emend_dir = django_dir / ".emend"
    emend_dir.mkdir(exist_ok=True)
    rules_file = emend_dir / "patterns.yaml"
    rules_file.write_text(LINT_RULES_YAML)
    _log(f"  Lint rules written to {rules_file}")


def check_emend_available() -> list[str]:
    """Check that emend CLI is available. Returns the command to use."""
    # Try 'emend' on PATH first, then look for it next to the current Python.
    venv_emend = str(Path(sys.executable).parent / "emend")
    candidates = [
        ["emend", "--help"],
        [venv_emend, "--help"],
        [sys.executable, "-m", "emend", "--help"],
    ]
    for cmd in candidates:
        try:
            result = _run(cmd)
        except (FileNotFoundError, OSError):
            continue
        if result.returncode == 0:
            # Return the base command (without --help).
            return cmd[:-1]

    print(
        "ERROR: emend is not installed or not on PATH.\n"
        "Install it with: pip install -e . (from the emend repo root)",
        file=sys.stderr,
    )
    sys.exit(1)


def run_benchmark(
    emend_cmd: list[str],
    django_dir: Path,
    args: list[str],
    iterations: int,
    ok_codes: set[int] | None = None,
) -> dict:
    """Run a single benchmark for the specified number of iterations.

    Args:
        emend_cmd: Base command to invoke emend (e.g. ["emend"]).
        django_dir: Working directory for the command.
        args: CLI arguments to append after emend_cmd.
        iterations: Number of times to run the benchmark.
        ok_codes: Set of return codes treated as success (default: {0}).

    Returns a dict with timing results and metadata.
    """
    if ok_codes is None:
        ok_codes = {0}

    full_cmd = emend_cmd + args
    times: list[float] = []
    last_returncode = 0
    last_stdout = ""
    last_stderr = ""

    for _i in range(iterations):
        start = time.perf_counter()
        try:
            result = _run(full_cmd, cwd=django_dir)
        except subprocess.TimeoutExpired:
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

    # Count output lines as a rough measure of result volume.
    output_lines = (
        len(last_stdout.strip().splitlines()) if last_stdout else 0
    )

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
    """Format a duration in human-friendly form."""
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.2f}s"


def print_table(results: dict[str, dict]) -> None:
    """Print a human-readable summary table."""
    # Header
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

    # Print errors for failed benchmarks.
    failures = {
        name: data for name, data in results.items() if not data["ok"]
    }
    if failures:
        print("Failures:")
        for name, data in failures.items():
            print(f"  {name}: exit code {data['returncode']}")
            if data.get("error"):
                for line in data["error"].splitlines()[:5]:
                    print(f"    {line}")
        print()


def _get_emend_commit() -> str:
    """Get the current emend git commit hash."""
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
    """Print machine-readable JSON output, optionally saving to a file."""
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
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run only 1 iteration of each benchmark.",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Output results as JSON.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="Override number of iterations (default: 3, or 1 with --quick).",
    )
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help="Run only benchmarks whose name contains this substring.",
    )
    parser.add_argument(
        "--label",
        type=str,
        default=None,
        help="Prose description of what's being tested (required with --save).",
    )
    parser.add_argument(
        "--save",
        type=str,
        default=None,
        help="Save JSON results to this file path.",
    )
    args = parser.parse_args()

    if args.save and not args.label:
        print("ERROR: --label is required when using --save.", file=sys.stderr)
        sys.exit(1)

    global _quiet
    iterations = args.iterations or (1 if args.quick else 3)

    if args.json_output:
        _quiet = True

    _log("emend Django Benchmark Suite")
    _log("=" * 40)
    _log("")

    # Step 1: Check emend is available.
    _log("[1/3] Checking emend installation...")
    emend_cmd = check_emend_available()
    _log(f"  Using: {' '.join(str(c) for c in emend_cmd)}")
    _log("")

    # Step 2: Ensure Django checkout.
    _log("[2/3] Ensuring Django checkout...")
    django_dir = ensure_django_checkout()
    setup_lint_rules(django_dir)
    _log("")

    # Step 3: Run benchmarks.
    _log(f"[3/3] Running benchmarks ({iterations} iteration(s) each)...")
    _log("")

    selected_benchmarks = BENCHMARKS
    if args.only:
        selected_benchmarks = [
            b for b in BENCHMARKS if args.only in b[0]
        ]
        if not selected_benchmarks:
            print(
                f"ERROR: No benchmarks match --only={args.only!r}. "
                f"Available: {', '.join(b[0] for b in BENCHMARKS)}",
                file=sys.stderr,
            )
            sys.exit(1)

    results: dict[str, dict] = {}
    total_start = time.perf_counter()

    for bench_name, bench_desc, bench_args, bench_ok_codes in selected_benchmarks:
        _log(f"  Running: {bench_desc}...")

        data = run_benchmark(
            emend_cmd, django_dir, bench_args, iterations,
            ok_codes=bench_ok_codes,
        )
        results[bench_name] = data

        if data["ok"]:
            status = "OK"
        else:
            status = f"FAIL (exit {data['returncode']})"
        _log(
            f"    -> {status}  median={_fmt_time(data['median'])}  "
            f"({data['output_lines']} output lines)"
        )

    total_elapsed = time.perf_counter() - total_start

    # Step 4: Output results.
    benchmarks_meta = [(b[0], b[1]) for b in selected_benchmarks]
    if args.save:
        # Save JSON to file; print human-readable table to stderr
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
