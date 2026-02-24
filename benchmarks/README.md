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
```

## Benchmarks

| Name | Description |
|------|-------------|
| `search_symbol_lookup` | `search "Model"` in a single file (symbol lookup mode) |
| `search_summary_subtree` | `search --output summary django/db/models/` (symbol listing for a subtree) |
| `find_pattern` | `find "$X.objects.filter($...args)"` across the Django source |
| `find_pattern_constrained` | Same pattern with `--where "class $C(TestCase):"` constraint |
| `refs_model` | `refs django/db/models/base.py::Model` (cross-project reference finding) |
| `rename_dry_run` | `rename django/db/models/query.py::QuerySet.filter --to filter_queryset` (dry-run) |
| `lint_db_models` | `lint django/db/models/` with 5 pattern rules |
| `lint_full_django` | `lint django/` with 5 pattern rules (full project) |
| `graph_file` | `graph django/db/models/query.py` (call graph generation) |

## Output

Human-readable output (default) prints a summary table with min/mean/median/max
times, iteration count, output line count, and pass/fail status for each
benchmark.

JSON output (`--json`) produces a structured object suitable for CI tracking
and trend analysis.
