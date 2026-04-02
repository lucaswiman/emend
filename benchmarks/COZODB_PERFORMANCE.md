# CozoDB Performance Analysis on Django Codebase

**Date**: 2026-04-02
**Codebase**: Django 5.2 (883 Python files)
**Author**: Codex

## Local Benchmark Baseline ("Before")

These numbers were collected on 2026-04-02 in the current Docker-on-macOS
Codex environment via:

```bash
.venv/bin/python benchmarks/bench_cozodb.py --quick --json
```

This is a single-iteration run, so treat it as a local baseline for relative
comparison rather than a noise-smoothed absolute benchmark.

### Before: Build And Query Times

| Metric | Local Before |
|--------|-------------:|
| index_build | 118.78s |
| refs(QuerySet) | 2.41s |
| refs(Model) | 2.34s |
| callers(QS.filter) | 959ms |
| callees(QS.filter) | 936ms |
| graph(full) | 2.23s |
| graph(query.py) | 953ms |
| transitive_callers | 7.19s |
| transitive_callees | 6.15s |
| dead_code_simple | 5.56s |
| dead_code_unified | 11.90s |
| unreachable_blocks | 2.86s |

### Before: Diagnostic Query Shapes

| Diagnostic Query | Local Before |
|------------------|-------------:|
| refs POSITIONAL | 1ms |
| refs == FILTER | 2.39s |
| callees POSITIONAL | 1ms |
| callees == FILTER | 955ms |
| call 1st-key bind | 1ms |
| call 2nd-key bind | 1.97s |
| ref 1st-key bind | 1ms |
| ref 2nd-key bind | 5.55s |
| cfg 1st-key bind | 7ms |
| cfg 2nd-key bind | 441ms |
| scan: symbol | 354ms |
| scan: call | 2.29s |
| scan: reference | 6.64s |
| join: ref+reachable | 4.87s |

## Optimization 1: Positional Binding For Leading-Key Lookups

Changes:

- `refs_datalog()` now binds `reference[$qn, ...]`
- `callees_datalog()` now binds `call[$fqn, ...]`
- `transitive_callees()` now seeds recursion from `call[$qn, ...]`

Benchmark command:

```bash
.venv/bin/python benchmarks/bench_cozodb.py --quick --skip-index --json
```

`--skip-index` is intentional here: this optimization changes query shape only,
not the physical schema or fact population.

### Optimization 1: Before vs After

| Metric | Before | After Opt 1 | Delta |
|--------|-------:|------------:|------:|
| refs(QuerySet) | 2.41s | 1ms | 2116x faster |
| refs(Model) | 2.34s | 1ms | 3210x faster |
| callees(QS.filter) | 936ms | 1ms | 779x faster |
| transitive_callees | 6.15s | 5.90s | 1.04x faster |
| callers(QS.filter) | 959ms | 948ms | no material change |
| graph(query.py) | 953ms | 944ms | no material change |
| dead_code_unified | 11.90s | 11.86s | no material change |

## Optimization 2: Add `call_by_callee` Reverse Index

Changes:

- added stored relation `call_by_callee`
- populated it in both full-build and incremental-update paths
- switched reverse traversals to read from `call_by_callee`
  - `callers_datalog()`
  - `transitive_callers()`
  - `impact_closure()`
  - `cascade_dead()`
  - `unreferenced_symbols(exclude_qns=...)`

Benchmark commands:

```bash
.venv/bin/python benchmarks/bench_cozodb.py --quick --json
.venv/bin/python benchmarks/bench_cozodb.py --quick --skip-index --json
```

The full rebuild run measures schema cost. The follow-up `--skip-index` run
captures the final query numbers after switching the last direct caller lookup.

### Optimization 2: Build Impact

| Metric | Before | After Opt 2 | Delta |
|--------|-------:|------------:|------:|
| index_build | 118.78s | 127.76s | 8.98s slower |

### Optimization 2: Previous vs Current Query Times

| Metric | After Opt 1 | After Opt 2 | Delta |
|--------|------------:|------------:|------:|
| callers(QS.filter) | 948ms | 1ms | 1006x faster |
| transitive_callers | 7.16s | 2ms | 4473x faster |
| graph(query.py) | 944ms | 978ms | no material change |
| transitive_callees | 5.90s | 6.39s | noise/regression |
| dead_code_unified | 11.86s | 11.94s | no material change |

## Optimization 3: Add `call_by_file` Reverse Index

Changes:

- added stored relation `call_by_file`
- populated it in both full-build and incremental-update paths
- switched `graph_datalog(file_path=...)` to read `call_by_file[$fp, ...]`

Benchmark command:

```bash
.venv/bin/python benchmarks/bench_cozodb.py --quick --json
```

### Optimization 3: Build Impact

| Metric | After Opt 2 | After Opt 3 | Delta |
|--------|------------:|------------:|------:|
| index_build | 127.76s | 134.90s | 7.14s slower |

### Optimization 3: Previous vs Current Query Times

| Metric | After Opt 2 | After Opt 3 | Delta |
|--------|------------:|------------:|------:|
| graph(query.py) | 978ms | 9ms | 109x faster |
| graph(full) | 2.34s | 2.27s | no material change |
| callers(QS.filter) | 1ms | 1ms | no material change |
| dead_code_unified | 11.94s | 12.04s | no material change |

## Optimization 4: Add `module_level_ref` For Dead-Code Liveness

Changes:

- added stored relation `module_level_ref`
- populated it in both full-build and incremental-update paths
- switched the module-level `live_ref` rule in `dead_code_unified()` to use
  `module_level_ref` instead of filtering the full `reference` relation

Benchmark command:

```bash
.venv/bin/python benchmarks/bench_cozodb.py --quick --json
```

### Optimization 4: Build Impact

| Metric | After Opt 3 | After Opt 4 | Delta |
|--------|------------:|------------:|------:|
| index_build | 134.90s | 138.25s | 3.35s slower |

### Optimization 4: Previous vs Current Query Times

| Metric | After Opt 3 | After Opt 4 | Delta |
|--------|------------:|------------:|------:|
| dead_code_unified | 12.04s | 7.73s | 1.56x faster |
| dead_code_simple | 5.59s | 5.65s | no material change |
| graph(query.py) | 9ms | 9ms | no material change |
| callers(QS.filter) | 1ms | 1ms | no material change |

## Optimization 5: Reorder The Recursive `transitive_callees()` Rule

Changes:

- kept the same relation and same leading-key access path
- reordered the recursive rule body from:
  - `*call[mid, b, ...], reaches[mid]`
- to:
  - `reaches[mid], *call[mid, b, ...]`

Benchmark command:

```bash
.venv/bin/python benchmarks/bench_cozodb.py --quick --skip-index --json
```

`--skip-index` is intentional here: this is a query-planning change only.

### Optimization 5: Previous vs Current Query Times

| Metric | After Opt 4 | After Opt 5 | Delta |
|--------|------------:|------------:|------:|
| transitive_callees | 6.20s | 1ms | 6200x faster |
| callees(QS.filter) | 1ms | 2ms | no material change |
| transitive_callers | 1ms | 1ms | no material change |
| dead_code_unified | 7.73s | 7.58s | no material change |
| graph(query.py) | 9ms | 9ms | no material change |

## Local End State

After all five implemented optimizations in this environment:

| Metric | Before | Final | Delta |
|--------|-------:|------:|------:|
| index_build | 118.78s | 138.25s | 19.47s slower |
| refs(QuerySet) | 2.41s | 1ms | 2116x faster |
| callers(QS.filter) | 959ms | 1ms | 1017x faster |
| callees(QS.filter) | 936ms | 1ms | 780x faster |
| graph(query.py) | 953ms | 9ms | 106x faster |
| transitive_callers | 7.19s | 1ms | 7190x faster |
| transitive_callees | 6.15s | 1ms | 6150x faster |
| dead_code_unified | 11.90s | 7.58s | 1.57x faster |

## Executive Summary

The local benchmark results support keeping CozoDB.

The large wins came from aligning query shape with relation layout:

- positional binding on leading keys for `reference[...]` and `call[...]`
- mirrored stored relations for alternate access paths
- a dedicated `module_level_ref` relation for the dead-code module-level case
- correct body ordering in the recursive `transitive_callees()` rule

The last point matters because it invalidates the strongest earlier claim in
this memo: the remaining recursive hotspot was not proof that Cozo could not
use index lookups inside recursion. In this case the recursive clause order was
the problem.

## What The Benchmarks Now Show

### 1. Leading-key positional binding matters

The baseline measurements still clearly show that:

- `*reference[$qn, ...]` is effectively instant
- `*reference[sqn, ...], sqn == $qn` is a full scan
- `*call[$fqn, ...]` is effectively instant
- `*call[caller, $qn, ...]` is still a scan because it binds a non-leading key

That remains the main Cozo performance rule for this schema.

### 2. Recursive queries can be fast when the recursive step is shaped correctly

Measured against the same local `facts.db` and the same start symbol
`django.db.models.query.QuerySet.filter`:

- old recursive clause order: `*call[mid, b, ...], reaches[mid]` -> `6.29s`
- reordered clause: `reaches[mid], *call[mid, b, ...]` -> `~1-2ms`
- result size in both cases: 3 reachable callees

So the previous explanation, "Cozo does not use index lookups on recursive
queries", was wrong for `transitive_callees()`. The engine can evaluate this
recursively and still be fast when the already-bound recursive frontier is
introduced before the indexed relation probe.

### 3. The remaining build-time increase is real work, not planner confusion

The extra ~19.5s of build time mostly comes from writing three additional
stored relations:

- `call_by_callee`
- `call_by_file`
- `module_level_ref`

That is a reasonable trade given the query wins. The build is slower because it
is doing more work and persisting more rows, not because the query fixes
themselves made indexing worse.

## Relation Statistics (Django 5.2, Local End State)

| Relation         |     Rows | Notes                              |
|------------------|---------:|------------------------------------|
| reference        |  736,297 | Largest relation (dominant cost)    |
| ref_by_block     |  348,637 | Block-tagged references             |
| call             |  275,298 | Call edges (with position info)     |
| call_by_callee   |  275,298 | Reverse call index by callee        |
| call_by_file     |  275,298 | Call index by file                  |
| method_call      |  269,352 | Receiver.method calls               |
| module_level_ref |  154,785 | Module-level references only        |
| source_loc       |   94,888 | Source location metadata            |
| cfg_block        |   86,852 | CFG basic blocks                    |
| reachable_block  |   86,284 | Pre-computed reachable blocks       |
| cfg_edge         |   77,992 | CFG edges                           |
| def_use          |   71,930 | Def-use chains                      |
| symbol           |   40,345 | Symbol definitions                  |
| import           |   18,068 | Import facts                        |
| decorator_on     |    7,098 | Decorator associations              |

**Total**: ~2.8M facts

## Build-Time Parallelism: What Still Looks Worth Doing

The current full-build path in [`transform.py`](/Users/lucaswiman/personal/emend/src/emend/transform.py)
still leaves parallelism on the table.

The high-value serial section is the per-file extraction loop inside
[`_build_facts_db()`](/Users/lucaswiman/personal/emend/src/emend/transform.py#L637),
which currently does, per file:

- file read
- Rust symbol extraction
- scope-resolver reference extraction
- import extraction
- CFG build
- block/range assembly
- call/reference/def-use/source-loc row generation

That work is embarrassingly parallel across files once the project-level scope
resolver has been populated.

### Likely best next build optimizations

1. Parallelize the per-file extraction loop.
   Use worker processes or threads to build the row batches for each file, then
   merge into the final `all_*` lists before the `:replace` phase.

2. Keep Cozo writes batched and serialized.
   The `:replace` calls are large relation swaps. Parallelizing them is less
   attractive than reducing time spent before them.

3. Avoid duplicate row materialization where possible.
   The extra access-path relations are useful, but they also mean more Python
   list building and more Cozo writes. If build time becomes a priority, this
   is where to look for compaction or more direct bulk-write paths.

### Constraints

- The project-level `PyScopeResolver.index_file()` pass is still serialized.
- The final Cozo `:replace` sequence is still serialized.
- In this container only 2 CPUs are visible, so local speedup from additional
  parallelism will understate what developer machines can achieve.

## Recommendation

Keep CozoDB.

The current local results show that the previous pain points were mostly query
shape and access-path issues, not a fundamental mismatch between Cozo and the
workload.

If you want another pass after this, the best target is build throughput:

1. parallelize `_build_facts_db()` per-file extraction
2. re-measure build time on a wider-core developer machine
3. only then revisit whether the extra mirrored relations should be made
   optional for lighter-weight indexing modes
