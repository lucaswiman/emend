# emend Benchmarks

Performance benchmarks for emend operations against the Django codebase.

## Setup

The benchmark script automatically clones Django (pinned to tag `5.2`,
commit `9e7cc2b628fe8fd3895986af9b7fc9525034c1b0`) into
`benchmarks/.django-checkout/`. This is cached between runs.

Requirements:
- `emend` must be installed (`pip install -e .` from the repo root)
- `git` must be available (for cloning Django)

## Running

```bash
# Full run (3 iterations per benchmark)
make benchmark

# Or directly:
python benchmarks/bench_django.py

# Quick run (1 iteration)
python benchmarks/bench_django.py --quick

# JSON output (machine-readable)
python benchmarks/bench_django.py --json

# Custom iteration count
python benchmarks/bench_django.py --iterations 5

# Run only specific benchmarks (substring match)
python benchmarks/bench_django.py --only refs
python benchmarks/bench_django.py --only lint

# Scaled benchmarks (50 copies of Django, ~44K py files)
python benchmarks/bench_django.py --scaled --quick
python benchmarks/bench_django.py --scaled-only --quick
```

## Benchmarks

### Standard (single Django checkout, ~883 py files)

| Name | Description |
|------|-------------|
| `search_symbol_lookup` | `search django/db/models/base.py::Model` (symbol lookup) |
| `search_summary_subtree` | `search --output summary django/db/models/` (symbol listing for a subtree) |
| `find_pattern` | `search "$X.objects.filter($...ARGS)" django/` (pattern matching) |
| `find_pattern_constrained` | Same pattern with `--where "class $C(TestCase):"` constraint |
| `refs_queryset` | `refs django/db/models/query.py::QuerySet --project django/db/` (reference finding) |
| `rename_dry_run` | `rename QuerySet.filter --to filter_queryset --project django/db/` (dry-run) |
| `lint_db_models` | `lint django/db/models/` with 5 pattern rules |
| `lint_full_django` | `lint django/` with 5 pattern rules (full project) |
| `graph_file` | `graph django/db/models/query.py` (call graph generation) |

### Scaled (50x Django, ~44K py files)

| Name | Description |
|------|-------------|
| `scaled_find_optional` | `search "Optional[$X]"` on 50x Django |
| `scaled_find_filter` | `search "$X.objects.filter($...ARGS)"` on 50x Django |
| `scaled_find_isinstance` | `search "isinstance($X, str)"` on 50x Django |
| `scaled_find_print` | `search "print($...ARGS)"` on 50x Django |
| `scaled_find_assign` | `search "$X = None"` on 50x Django |
| `scaled_summary` | `search --output summary django1/django/db/models/` |

## Output

Human-readable output (default) prints a summary table with min/mean/median/max
times, iteration count, output line count, and pass/fail status for each
benchmark.

JSON output (`--json`) produces a structured object suitable for CI tracking
and trend analysis.
