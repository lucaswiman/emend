# CozoDB Performance Analysis on Django Codebase

**Date**: 2026-04-02
**Codebase**: Django 5.2 (883 Python files)
**Author**: Claude Code (benchmark analysis)

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

## Executive Summary

CozoDB's query planner has a critical behavior: **B-tree index lookups only occur
when key values are bound directly in the relation pattern position** (e.g.,
`*call[$qn, callee, ...]`), not when filtered via a separate `==` clause (e.g.,
`*call[caller, callee, ...], callee == $qn`). The latter always performs a **full
table scan** regardless of which key column is filtered.

This single insight explains most performance issues and yields **1000x speedups**
for point queries by switching from `==` filters to positional parameter binding.

## Relation Statistics (Django 5.2)

| Relation         |     Rows | Notes                              |
|------------------|---------:|------------------------------------|
| reference        |  736,297 | Largest relation (dominant cost)    |
| ref_by_block     |  348,637 | Block-tagged references             |
| call             |  275,298 | Call edges (with position info)     |
| method_call      |  269,352 | Receiver.method calls               |
| source_loc       |   94,888 | Source location metadata            |
| cfg_block        |   86,852 | CFG basic blocks                    |
| reachable_block  |   86,284 | Pre-computed reachable blocks       |
| cfg_edge         |   77,992 | CFG edges                           |
| def_use          |   71,930 | Def-use chains                      |
| symbol           |   40,345 | Symbol definitions                  |
| import           |   18,068 | Import facts                        |
| decorator_on     |    7,098 | Decorator associations              |

**Total**: ~2.1M facts

## Key Finding: Positional Binding vs `==` Filter

CozoDB stores relations in a B-tree keyed on the declared key columns. However,
the query planner **only uses this index when the key value is a constant or
parameter placed directly in the relation pattern position**.

### Benchmark Evidence

| Query Pattern                                    | Time      | Speedup |
|--------------------------------------------------|-----------|---------|
| `*symbol[$qn, fp, ...]` (positional)             | **1.3ms** | —       |
| `*symbol[qn, fp, ...], qn == $qn` (== filter)   | 263ms     | 200x    |
| `*reference[$qn, fp, ...]` (positional)          | **1.5ms** | —       |
| `*reference[sqn, fp, ...], sqn == $qn` (filter)  | 3,853ms   | 2,500x  |
| `*call[$fqn, callee, ...]` (positional)           | **1.5ms** | —       |
| `*call[caller, callee, ...], caller == $fqn`     | 1,533ms   | 1,000x  |
| `*cfg_edge["path", fq, ...]` (positional)        | **12ms**  | —       |
| `*cfg_edge[fp, fq, ...], fp == "path"` (filter)  | 323ms     | 27x     |
| `*def_use["path", "func", ...]` (positional 1+2) | **2ms**   | —       |
| `*def_use[fp, fq, ...], fp == ..., fq == ...`    | 487ms     | 244x    |

### Leading vs Non-Leading Key (Positional Binding)

| Query                                            | Time      |
|--------------------------------------------------|-----------|
| `*call[bound_1st, callee, ...]` (1st key)        | **1.6ms** |
| `*call[caller, bound_2nd, ...]` (2nd key)        | 3,296ms   |
| `*call[caller, callee, bound_3rd, ...]` (3rd key)| 3,580ms   |
| `*cfg_edge[bound_1st, fq, ...]` (1st key)        | **12ms**  |
| `*cfg_edge[fp, bound_2nd, ...]` (2nd key)        | 668ms     |
| `*cfg_edge[bound_1st, bound_2nd, ...]` (1+2)     | **1.6ms** |

**Conclusion**: Positional binding only helps on **leading key prefix** — binding
the 2nd key without binding the 1st is equivalent to a full table scan (as
expected for a B-tree).

## Index Building Performance

**Full index build**: ~191 seconds (Django, 883 files)

### Build Phase Breakdown

| Phase                                   | Time     | % of total |
|-----------------------------------------|----------|------------|
| File I/O + Rust extraction              | ~130s    | 68%        |
| `reference :replace` (736K rows)        | 33s      | 17%        |
| `ref_by_block :replace` (349K rows)     | 12s      | 6%         |
| `call :replace` (275K rows)             | 11s      | 6%         |
| `symbol :replace` (40K rows)            | 1.5s     | <1%        |
| Other relations                         | ~3s      | 2%         |

The CozoDB insert phase accounts for ~32% of total build time (~60s for
`:replace` operations). The dominant cost is `reference` at 33s.

## Current Query Performance

| Query                          | Time     | Notes                          |
|--------------------------------|----------|--------------------------------|
| `refs_datalog()`               | 3,853ms  | Uses `==` filter (table scan!) |
| `callers_datalog()`            | 1,533ms  | Filters on 2nd key (scan)      |
| `callees_datalog()`            | 1,533ms  | Uses `==` filter (scan!)       |
| `graph_datalog()` (full)       | 3,746ms  | Full scan of call relation     |
| `graph_datalog(file=...)`      | 1,604ms  | Filter on 3rd key (scan)       |
| `dead_code_simple()`           | 8,218ms  | Join reference + symbol        |
| `dead_code_unified()`          | 19,539ms | Dominant: ref_by_block join    |
| `transitive_callers()`         | 16,041ms | Recursive with == filters      |
| `transitive_callees()`         | 14,155ms | Recursive with == filters      |
| `unreachable_blocks()`         | 5,220ms  | CFG reachability               |

## Recommended Optimizations

### 1. Switch `==` Filters to Positional `$param` Binding (HIGH IMPACT)

**Estimated improvement: 200-2500x for point queries**

Every query method that currently uses `sqn == $qn` or `callee_qn == $qn` should
instead use positional parameter binding:

```python
# BEFORE (table scan — 3.8s on reference):
"?[fp, line, col, kind, fq, bid] := "
"*reference[sqn, fp, line, col, kind, fq, bid], sqn == $qn"

# AFTER (index lookup — 1.5ms on reference):
"?[fp, line, col, kind, fq, bid] := "
"*reference[$qn, fp, line, col, kind, fq, bid]"
```

**Affected methods** (all in `fact_graph.py`):
- `refs_datalog()` — filter on `symbol_qn` (1st key of reference) → positional `$qn`
- `callees_datalog()` — filter on `caller_qn` (1st key of call) → positional `$fqn`
- `callers_datalog()` — filter on `callee_qn` (**2nd key** of call — see #2)
- `graph_datalog(file=...)` — filter on `file_path` (3rd key of call — see #2)
- `transitive_callers()` — base case uses `b == $qn` on 2nd key
- `transitive_callees()` — base case uses `a == $qn` on 1st key → positional `$qn`
- `dead_code()` — join pattern (needs structural change)

### 2. Add Reverse-Index Relations (HIGH IMPACT for callers)

**Estimated improvement: 1000x for `callers_datalog()`**

The `call` relation is keyed `(caller_qn, callee_qn, ...)`. Finding callers
requires filtering on `callee_qn` (2nd key), which cannot use B-tree prefix scan.

**Solution**: Add a `call_by_callee` stored relation with reversed key order:

```
{:create call_by_callee {
    callee_qn: String,
    caller_qn: String,
    file_path: String,
    line: Int,
    col: Int
    =>
    func_qn: String default "",
    block_id: Int default -1
}}
```

Populate during index build alongside the `call` relation. Cost: ~10s extra for
275K rows (negligible vs. 191s total build time).

Similarly, consider `ref_by_file` for queries that filter references by file path.

### 3. Re-Order Tuple Keys for Common Access Patterns (MEDIUM IMPACT)

Some relations have key orderings that don't match the most common query patterns:

**`call` relation** — current: `(caller_qn, callee_qn, file_path, line, col)`
- `callers_datalog()` filters on `callee_qn` (2nd) — very common
- `callees_datalog()` filters on `caller_qn` (1st) — common
- `graph_datalog(file=...)` filters on `file_path` (3rd) — occasional

Since both caller and callee queries are equally common, keeping the current order
plus adding a reverse index (recommendation #2) is the best approach.

**`cfg_edge` relation** — current: `(file_path, func_qn, from_block, to_block, ...)`
- Trace queries scope by `(file_path, func_qn)` — aligned ✓
- Good key ordering for current access patterns

**`def_use` relation** — current: `(file_path, func_qn, var_name, kind, def_block, use_block)`
- Trace queries scope by `(file_path, func_qn)` — aligned ✓
- Good key ordering for current access patterns

### 4. Optimize Join Patterns in Complex Queries (MEDIUM IMPACT)

**`dead_code_unified`**: The dominant cost is the `ref_by_block JOIN reachable_block`
(~8.6s). Both relations share the key prefix `(file_path, func_qn, block_id)`, so
the join should be efficient in principle. The cost comes from materializing 206K
result rows. Consider:

- Pre-computing `live_ref` during index build as a stored relation
- Using `:limit` or early termination if only checking existence

**`live_ref` from module-level references**: The second `live_ref` rule scans the
full `reference` relation (736K rows) with `fq == "", bid == -1` filter — this is
a table scan. Consider adding a `module_level_ref` stored relation.

### 5. Batch Index Insertion Optimization (LOW-MEDIUM IMPACT)

The `:replace` operations for large relations are slow (33s for 736K reference rows).
Possible improvements:

- **Chunked inserts**: Break large `:replace` into smaller batches to reduce peak
  memory usage
- **Parallel extraction**: Use Rust-level parallelism for per-file fact extraction
  (currently single-threaded Python loop)
- **Incremental updates**: Use `update_files()` for changed files only instead of
  full rebuild (already implemented but not always used)

### 6. Avoid Full Scans in Recursive Queries (LOW IMPACT, HARD)

Recursive Datalog queries (transitive callers/callees) currently scan the full
`call` relation on each iteration. CozoDB doesn't appear to push index lookups
into recursive rule bodies when the bound variable comes from the recursive
relation. Using the reverse index (#2) helps the base case but not recursive steps.

**Possible mitigations**:
- Depth-bounded iteration (already used in `impact_closure_datalog`)
- Pre-compute transitive closure during index build for common entry points
- Use Python-side BFS with indexed point queries instead of Datalog recursion

## Quick Win Summary

| Change | Effort | Impact | Queries Affected |
|--------|--------|--------|------------------|
| Positional `$param` binding | Low | **1000x** for point queries | refs, callees, transitive_callees |
| `call_by_callee` reverse index | Low | **1000x** for callers | callers, transitive_callers |
| Pre-compute `live_ref` | Medium | **10x** for dead_code | dead_code_unified |
| `ref_by_file` reverse index | Low | **2500x** for file-scoped refs | graph(file=...) |
| Python-side BFS for transitive | Medium | **~2x** for recursive | transitive_callers/callees |
