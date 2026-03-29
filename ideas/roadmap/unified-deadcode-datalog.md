# Unified Dead Code Detection via Datalog

## Problem

Dead code detection is split across two independent systems:

1. **`emend deadcode`** (`find_dead_code()` in `transform.py`) — finds symbols
   with no references anywhere in the project. Uses SQLite `reference_index`
   in `parse.db`, not the fact graph at all.

2. **`emend cfg --unreachable`** (`find_unreachable_blocks()` in `cfg.py`) —
   finds basic blocks unreachable from a function's entry. Uses BFS over
   Rust `PyCfg` objects in Python.

Neither knows about the other. A symbol referenced only from unreachable
code is invisible to `deadcode` (it sees references) and invisible to
`cfg --unreachable` (it sees blocks, not symbols). The fact graph has
schema for both (`reference`, `cfg_edge`) but `cfg_edge` is never
populated, and `deadcode` doesn't use the fact graph.

## Goal

A single Datalog query over the fact graph that answers: *which symbols
have no references from reachable code?* This subsumes both analyses and
catches cases neither can alone (e.g. a function called only from dead
code after a `return`).

## Design

### Position model: block containment, not line numbers

The current schema uses absolute line numbers everywhere (`reference.line`,
`cfg_edge.from_line`, `symbol.line`). This is problematic:

- **Brittle joins.** Associating a reference with a block requires a
  line-range comparison (`ref.line >= block.start AND ref.line <= block.end`),
  which is a inequality join — expensive in Datalog and error-prone with
  off-by-one issues.
- **Redundant re-indexing.** Every fact in a file carries absolute line
  numbers. Any insertion shifts all subsequent lines, invalidating facts
  below the edit. While the graph is currently rebuilt from scratch, this
  makes incremental updates impossible.
- **Wrong abstraction.** For reachability analysis, what matters is *which
  block contains this reference*, not *what line is it on*. Lines are a
  presentation concern.

**Solution: tag references with their containing block ID at extraction
time.**

The Rust scope resolver already emits byte offsets for each reference.
The Rust CFG builder already emits byte ranges for each block. During
`build_from_project()`, we can intersect these to assign each reference
a `(func_qn, block_id)` pair. The relational join then becomes an exact
match on block ID — fast and correct.

Line numbers remain available for *display* (error messages, editor
navigation) but are not part of the relational core used for analysis.
When line numbers are stored, they should be **relative to the containing
symbol's start line**, so that edits outside the symbol don't invalidate
its internal facts.

### New schema

```
# Symbols (unchanged key, drop absolute lines from analysis path)
{:create symbol {
    qualified_name: String
    =>
    file_path: String,
    name: String,
    kind: String,
    parent: String default ""
}}

# CFG blocks — new relation
{:create cfg_block {
    file_path: String,
    func_qn: String,
    block_id: Int
    =>
    is_entry: Bool default false,
    is_exit: Bool default false
}}

# CFG edges (drop from_line/to_line — use block_id)
{:create cfg_edge {
    file_path: String,
    func_qn: String,
    from_block: Int,
    to_block: Int
    =>
    edge_kind: String
}}

# References — add containing block info
{:create reference {
    symbol_qn: String,
    file_path: String,
    ref_id: Int           # unique within file, avoids line/col as key
    =>
    ref_kind: String,
    func_qn: String default "",    # "" for module-level
    block_id: Int default -1       # -1 for module-level
}}

# Source locations — separate relation for display
{:create source_loc {
    file_path: String,
    ref_id: Int
    =>
    line: Int,                     # absolute, for display only
    col: Int,
    rel_line: Int default 0        # relative to containing symbol start
}}
```

Key changes:

- **`cfg_block`** is a first-class relation with `is_entry` / `is_exit`
  flags. No implicit "block 0 is entry" convention.
- **`reference`** carries `func_qn` and `block_id` so we know *where in
  the control flow* each reference lives.
- **`source_loc`** separates positional display data from the relational
  core. Analysis queries never join on it.
- **`cfg_edge`** drops `from_line` / `to_line` — the block ID is the
  edge endpoint, not a line range.

### The query

With this schema, unified dead code detection is a single Datalog program:

```datalog
# Step 1: Reachable blocks (transitive closure from entry)
reachable[fp, fq, bid] :=
    *cfg_block[fp, fq, bid, is_entry, _],
    is_entry == true

reachable[fp, fq, tb] :=
    reachable[fp, fq, fb],
    *cfg_edge[fp, fq, fb, tb, _]

# Step 2: Live references — referenced from reachable code or module level
live_ref[sq] :=
    *reference[sq, fp, _, _, fq, bid],
    fq == "",               # module-level reference (always reachable)
    bid == -1

live_ref[sq] :=
    *reference[sq, fp, _, _, fq, bid],
    reachable[fp, fq, bid]

# Step 3: Dead symbols — defined but never live-referenced
?[qn, fp, name, kind] :=
    *symbol[qn, fp, name, kind, _],
    not live_ref[qn]
```

This catches:

| Case | `deadcode` today | `cfg --unreachable` today | Unified query |
|------|------------------|--------------------------|---------------|
| Function never called anywhere | Yes | No | Yes |
| Code after unconditional return | No | Yes (as blocks) | Yes |
| Function called only from unreachable code | No | No | Yes |
| Transitively dead call chains from unreachable code | No | No | Yes |

### Populating the facts

In `build_from_project()`:

1. **Symbols**: Already populated. Drop `line`/`end_line` from the
   relation (keep in `source_loc`).

2. **CFG blocks**: For each file, call `build_cfgs_for_file()`. For each
   CFG, emit a `cfg_block` fact per block from `cfg.get_blocks()`. Tag
   entry/exit from `cfg.entry` / `cfg.exit`.

3. **CFG edges**: Already have the schema and API. Just call
   `cfg.get_edges()` and emit facts.

4. **References with block assignment**: The scope resolver emits byte
   offsets per reference. The CFG builder emits byte ranges per block.
   Sort blocks by byte range, binary-search each reference into its
   containing block. Emit the `(func_qn, block_id)` pair on the
   reference fact.

   For module-level code (outside any function), use sentinel values
   `func_qn=""`, `block_id=-1`.

Step 4 is the only new work — the rest is wiring up existing Rust
infrastructure.

### Entry point filtering

The current `deadcode` has heuristics: skip `__dunder__` methods, skip
decorated symbols, skip `__all__` members, skip test functions. These
should become Datalog rules too:

```datalog
# Entry points are always considered live
live_ref[qn] :=
    *symbol[qn, _, name, kind, _],
    starts_with(name, "__"),
    ends_with(name, "__")

live_ref[qn] :=
    *symbol[qn, _, name, _, _],
    starts_with(name, "test_")

# Decorator-based entry points via a small stored relation
{:create entry_point_decorator { decorator: String }}

live_ref[qn] :=
    *symbol[qn, fp, _, _, _],
    *decorator_on[qn, dec],
    *entry_point_decorator[dec]
```

This requires a `decorator_on` relation (symbol → decorator name), which
the Rust symbol collector can already provide.

### Migration path

1. **Phase 1**: Add `cfg_block` relation and populate `cfg_block` +
   `cfg_edge` in `build_from_project()`.
2. **Phase 2**: Extend reference extraction to tag `func_qn` + `block_id`
   on each reference. Add `source_loc` relation.
3. **Phase 3**: Implement the unified dead code query. Wire into
   `emend deadcode` as an alternative backend (flag or default).
4. **Phase 4**: Remove the Python `find_dead_code()` implementation and
   the BFS `find_unreachable_blocks()`. The `cfg` command keeps its
   visualization role but delegates reachability to the fact graph.

### Impact on `cfg` command

The `cfg` command remains a visualization tool. It continues to use the
Rust `PyCfg` directly for text/dot/JSON rendering (which needs the full
block structure including statements, defs, uses — more than the Datalog
relations store). The `--unreachable` flag can switch to querying the
fact graph instead of running its own BFS.

### Open questions

- **Granularity of "unreachable"**: Should we report unreachable blocks
  as dead code alongside unreferenced symbols? Or keep them as a
  separate category in the output? Probably separate — they need
  different fix actions (delete symbol vs delete statements).
- **Cross-file CFG**: Module-level code is currently treated as always
  reachable. A more precise analysis could model `if __name__ == "__main__"`
  guards, but this adds complexity for marginal benefit.
- **Decorator relation**: Need to decide whether `decorator_on` is a
  first-class fact type or derived during symbol extraction.
- **Performance**: `build_cfgs_for_file()` on every file adds cost to
  `build_from_project()`. Can be parallelized (Rust CFG builder is
  per-file) and cached.
