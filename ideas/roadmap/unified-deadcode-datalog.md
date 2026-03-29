# Datalog-First Analysis: Replacing Python Graph Traversals

## Motivation

Many emend commands implement graph traversals, transitive closures, and
relational joins in Python that Datalog expresses naturally. The fact
graph (CozoDB) already stores most of the needed relations — but most
commands bypass it, re-deriving facts from the Rust scope resolver or
SQLite indexes on every invocation. A few commands (`impact`,
`safe_delete`, `policy --datalog`) already use Datalog. The rest should
follow.

This proposal covers **all** commands that should migrate, not just dead
code.

---

## Position model: block containment, not line numbers

The current schema uses absolute line numbers everywhere
(`reference.line`, `cfg_edge.from_line`, `symbol.line`). This is
problematic:

- **Brittle joins.** Associating a reference with a CFG block requires
  an inequality join on line ranges — expensive in Datalog and
  error-prone.
- **Redundant re-indexing.** Adding a line to a file invalidates every
  fact below the edit. While the graph is currently rebuilt from scratch,
  this makes incremental updates impossible.
- **Wrong abstraction.** For reachability analysis, what matters is
  *which block contains this reference*, not *what line is it on*. Lines
  are a presentation concern.

**Solution: tag references with their containing block ID at extraction
time.**

The Rust scope resolver already emits byte offsets for each reference.
The Rust CFG builder already emits byte ranges for each block. During
`build_from_project()`, intersect these to assign each reference a
`(func_qn, block_id)` pair. The join then becomes an exact match on
block ID — fast and correct.

Line numbers are stored in a separate `source_loc` relation for display
(error messages, editor navigation) but are not part of the relational
core. When stored, they are **relative to the containing symbol's start
line** so edits outside a symbol don't invalidate its internal facts.

---

## Revised schema

```
# Symbols — key is qualified name; lines moved to source_loc
{:create symbol {
    qualified_name: String
    =>
    file_path: String,
    name: String,
    kind: String,
    parent: String default ""
}}

# Decorators on symbols
{:create decorator_on {
    symbol_qn: String,
    decorator: String
}}

# CFG blocks — first-class relation with entry/exit flags
{:create cfg_block {
    file_path: String,
    func_qn: String,
    block_id: Int
    =>
    is_entry: Bool default false,
    is_exit: Bool default false
}}

# CFG edges — block IDs only, no line numbers
{:create cfg_edge {
    file_path: String,
    func_qn: String,
    from_block: Int,
    to_block: Int
    =>
    edge_kind: String
}}

# References — tagged with containing function and block
{:create reference {
    symbol_qn: String,
    file_path: String,
    ref_id: Int
    =>
    ref_kind: String,
    func_qn: String default "",
    block_id: Int default -1
}}

# Calls — tagged with containing function and block
{:create call {
    caller_qn: String,
    callee_qn: String,
    file_path: String,
    ref_id: Int
    =>
    func_qn: String default "",
    block_id: Int default -1
}}

# Imports
{:create import {
    importing_file: String,
    imported_module: String,
    imported_name: String default ""
    =>
    alias: String default ""
}}

# Def-use chains — block-tagged
{:create def_use {
    file_path: String,
    func_qn: String,
    var_name: String,
    def_block: Int,
    use_block: Int
}}

# Taint flow
{:create taint_flow {
    source_var: String,
    sink_var: String,
    label: String,
    file_path: String,
    func_qn: String,
    source_block: Int,
    sink_block: Int
}}

# Function taint summaries (for interprocedural analysis)
{:create func_summary {
    func_qn: String,
    param_name: String
    =>
    flows_to_return: Bool default false,
    flows_to_sink: Bool default false,
    sink_label: String default ""
}}

# Type bindings
{:create type_binding {
    symbol_qn: String,
    file_path: String,
    binding_kind: String
    =>
    type_str: String
}}

# Source locations — display only, never joined in analysis
{:create source_loc {
    file_path: String,
    loc_kind: String,     # "symbol", "reference", "call", ...
    loc_id: String        # qualified_name or ref_id
    =>
    line: Int,
    col: Int default 0,
    end_line: Int default 0,
    rel_line: Int default 0
}}

# Entry point configuration
{:create entry_point_decorator { decorator: String }}
{:create entry_point_name { name: String }}
```

Key changes from current schema:

- **`cfg_block`** is new. Explicit `is_entry`/`is_exit` flags replace
  the implicit "block 0 is entry" convention.
- **`reference`** and **`call`** carry `func_qn` + `block_id` so we
  know where in the control flow each reference lives.
- **`def_use`** uses block IDs instead of line/col pairs.
- **`taint_flow`** uses block IDs instead of line numbers.
- **`func_summary`** is new — stores interprocedural taint summaries as
  facts so the fixed-point loop can be Datalog recursion.
- **`string_ref`** is new — pre-computed string literal occurrences of
  symbol names for dead code detection.
- **`decorator_on`** is new — enables entry point filtering in Datalog.
- **`source_loc`** separates positional display data from the relational
  core. Analysis queries never join on it.
- **`import`** drops its line number (display concern).

---

## Commands to migrate

### Already Datalog

These commands already use the fact graph. No migration needed, but they
benefit from the schema improvements above.

| Command | Implementation |
|---------|---------------|
| `impact` | `_find_impact_via_fact_graph()` — transitive reverse-caller closure |
| `delete --cascade` | Phase 1 cascade uses inline Datalog |
| `policy --datalog` | Direct CozoScript execution |

### Tier 1: Direct relation queries (no schema changes needed)

These commands re-derive facts from the Rust scope resolver on every
call. With a populated fact graph, each becomes a single Datalog query.

#### `refs` / `find_references()`

**Current**: `visit_project_ts()` iterates all files, calls
`references_in_file()` per file, filters by QN and kind.

**Replacement**:
```datalog
?[fp, ref_id, ref_kind] :=
    *reference[$target_qn, fp, ref_id, ref_kind, _, _]
```

Add `source_loc` join for display. Filter `ref_kind` for
`--writes-only`, `--reads-only`, `--calls-only`.

#### `callers` / `find_callers()`

**Current**: Iterates candidate files (filtered by import graph), finds
all references with `kind == "call"`.

**Replacement**:
```datalog
?[caller_qn, fp, ref_id] :=
    *call[caller_qn, $target_qn, fp, ref_id, _, _]
```

#### `graph` / `generate_graph()`

**Current**: Rust `collect_callees()` for a single file, formats as
adjacency list.

**Replacement**:
```datalog
?[caller_qn, callee_qn] :=
    *call[caller_qn, callee_qn, $target_file, _, _, _]
```

Formatting (plain/JSON/DOT) stays in Python.

### Tier 2: Transitive queries (need block tagging)

These require the `cfg_block` relation and block-tagged references.

#### `deadcode` — unified dead code detection

**Current**: Two independent systems. `find_dead_code()` in
`transform.py` does reference counting via SQLite. `cfg --unreachable`
does BFS over `PyCfg` objects. Neither knows about the other.

**Replacement** — a single Datalog program:

```datalog
# Reachable blocks (transitive closure from entry)
reachable[fp, fq, bid] :=
    *cfg_block[fp, fq, bid, is_entry, _],
    is_entry == true

reachable[fp, fq, tb] :=
    reachable[fp, fq, fb],
    *cfg_edge[fp, fq, fb, tb, _]

# Live references: from reachable code or module level
live_ref[sq] :=
    *reference[sq, _, _, _, fq, bid],
    fq == "", bid == -1

live_ref[sq] :=
    *reference[sq, fp, _, _, fq, bid],
    reachable[fp, fq, bid]

# String literal filtering is a post-processing step in Python
# (FTS or substring match against symbol names) — not worth
# encoding in the schema. See "Hybrid boundaries" below.

# Entry points are always live
live_ref[qn] :=
    *symbol[qn, _, name, _, _],
    starts_with(name, "__"), ends_with(name, "__")

live_ref[qn] :=
    *symbol[qn, _, name, _, _],
    starts_with(name, "test_")

live_ref[qn] :=
    *decorator_on[qn, dec],
    *entry_point_decorator[dec]

live_ref[qn] :=
    *symbol[qn, _, name, _, _],
    *entry_point_name[name]

# Dead symbols
?[qn, fp, name, kind] :=
    *symbol[qn, fp, name, kind, _],
    not live_ref[qn]
```

This catches cases neither system can today:

| Case | `deadcode` today | `cfg --unreachable` today | Unified |
|------|------------------|--------------------------|---------|
| Function never called anywhere | Yes | No | Yes |
| Code after unconditional return | No | Yes (blocks) | Yes |
| Function called only from unreachable code | No | No | Yes |
| Transitively dead call chains | No | No | Yes |

#### `callees` / `find_callees()`

**Current**: Finds target symbol's line range, scans all call references
in the file, filters by line range.

**Replacement**: With block-tagged calls, use `func_qn` directly:
```datalog
?[callee_qn, ref_id] :=
    *call[_, callee_qn, _, ref_id, $target_func_qn, _]
```

No line-range filtering needed — the `func_qn` tag on `call` already
scopes it to the right function.

### Tier 3: Taint analysis (need def-use + summary facts)

#### Intraprocedural taint (`_check_flow_rule()` in lint, `_analyze_function()` in taint)

**Current**: Per-function analysis in Python. Finds source/sink pattern
matches, builds assignment graph via regex, simulates taint propagation
through assignments, checks for taint reaching sinks.

Pattern matching (identifying sources/sinks/sanitizers) must stay in
Python/Rust — it requires AST pattern matching. But **propagation** can
be Datalog over `def_use` facts:

```datalog
# Taint sources (pre-computed by pattern matching, inserted as facts)
tainted[fp, fq, var, block, label] :=
    *taint_source_match[fp, fq, var, block, label]

# Propagation through def-use chains
tainted[fp, fq, target, use_block, label] :=
    tainted[fp, fq, source, def_block, label],
    *def_use[fp, fq, source, def_block, use_block],
    not *taint_sanitizer_match[fp, fq, source, def_block, label]

# Violations: taint reaches sink
?[fp, fq, var, block, label] :=
    tainted[fp, fq, var, block, label],
    *taint_sink_match[fp, fq, var, block, label]
```

**Hybrid approach**: Python finds pattern matches → inserts as
temporary facts → Datalog does propagation → Python formats output.

#### Interprocedural taint (`run_interprocedural_taint_analysis()`)

**Current**: Fixed-point iteration in Python. Computes
`FunctionSummary` per function (param → return, param → sink), then
propagates across call graph until convergence.

**Replacement**: Fixed-point computation is exactly what Datalog
recursive rules do. With `func_summary` and `call` facts:

```datalog
# Direct summaries (from intraprocedural analysis)
param_flows_to_return[fq, param] :=
    *func_summary[fq, param, true, _, _]

# Transitive: if callee's param flows to return, and caller passes
# tainted value, taint propagates through the call
param_flows_to_return[caller_fq, caller_param] :=
    *call[caller_fq, callee_fq, _, _, _, _],
    param_flows_to_return[callee_fq, callee_param],
    *def_use[_, caller_fq, caller_param, _, _]

# Violations: tainted param flows to sink through call chain
?[caller_fq, callee_fq, param, label] :=
    *call[caller_fq, callee_fq, _, _, _, _],
    *func_summary[callee_fq, param, _, true, label],
    tainted[_, caller_fq, param, _, label]
```

This replaces the Python fixed-point loop with native Datalog
semi-naive evaluation.

### Tier 4: Flow-based lint rules

#### `lint` flow rules (`flows-from` / `flows-to` / `not-through`)

**Current**: `_check_flow_rule()` in `lint.py` does per-function
taint simulation using regex-based assignment graphs.

**Replacement**: Same approach as intraprocedural taint above. Pattern
matching identifies source/sink/sanitizer locations, Datalog handles
propagation. The lint engine becomes:

1. Parse rules from `.emend/patterns.yaml`
2. For each flow rule, run pattern matches to find source/sink/sanitizer
   locations
3. Insert as temporary facts
4. Run the propagation query
5. Format violations

---

## Hybrid boundaries

The goal is Datalog for **reasoning and inference** — not to force
everything into relations. Some things are better as post-processing
or pre-processing in Python/Rust:

**Pre-processing** (feeds facts into Datalog):
- Pattern matching for taint sources/sinks/sanitizers (AST patterns)
- Type oracle queries (external tool integration)
- CFG construction (Rust tree-sitter)
- Symbol/reference extraction (Rust scope resolver)

**Post-processing** (filters/enriches Datalog results):
- String literal matching for dead code (FTS or substring match against
  symbol names — not worth a schema relation)
- Git `log -S` for last-reference annotation
- Output formatting (plain/JSON/DOT)
- Display-layer source locations (line numbers, editor navigation)

**Stays in Python/Rust entirely**:
- Code transformation (`edit`, `add`, `move`, `rename`, `replace`)
- `cfg` visualization (needs full block structure: statements, defs, uses)
- Pattern compilation (Lark grammar → Rust IR)

The boundary is: **inference** (what references what, what's reachable,
where does data flow) → Datalog. **Transformation** (rewriting code) and
**rendering** → Python/Rust. **Heuristic filtering** (string literals,
git history) → Python post-filter on Datalog results.

---

## Populating the facts

In `build_from_project()`:

1. **Symbols + decorators**: Already populated. Add `decorator_on` facts
   from the symbol collector's decorator metadata.

2. **CFG blocks**: Call `build_cfgs_for_file()` per file. Emit
   `cfg_block` per block, tag `is_entry`/`is_exit` from `cfg.entry` /
   `cfg.exit`.

3. **CFG edges**: Call `cfg.get_edges()`, emit facts.

4. **Block-tagged references and calls**: The scope resolver emits byte
   offsets per reference. The CFG builder emits byte ranges per block.
   Sort blocks by byte range, binary-search each reference into its
   containing block. Emit `(func_qn, block_id)` on each reference/call.
   Module-level code uses sentinel `func_qn=""`, `block_id=-1`.

5. **Def-use chains**: Already available from the Rust CFG builder
   (`block["defs"]`, `block["uses"]`). Map to `def_use` facts using
   block IDs.

6. **Taint summaries**: After intraprocedural taint analysis, persist
   `func_summary` facts so interprocedural analysis uses them.

Step 4 is the main new work — the rest is wiring up existing Rust
infrastructure.

---

## Migration path

### Phase 1: Schema and CFG population

- Add `cfg_block` and `decorator_on` relations
- Populate `cfg_block`, `cfg_edge`, and `decorator_on` in
  `build_from_project()`
- String literal matching for dead code stays as Python post-filter

### Phase 2: Block-tagged references

- Extend reference extraction to assign `(func_qn, block_id)` per
  reference via byte-offset intersection with CFG blocks
- Same for `call` facts
- Add `source_loc` relation, move all display positions there
- Populate `def_use` from CFG builder's block defs/uses

### Phase 3: Tier 1 command migration

- `refs` → single Datalog query on `reference`
- `callers` → single Datalog query on `call`
- `callees` → single Datalog query on `call` (scoped by `func_qn`)
- `graph` → single Datalog query on `call` + Python formatting
- Remove Python traversal code from `transform.py`

### Phase 4: Unified dead code

- Implement the reachable-block + live-reference Datalog query
- Port entry point heuristics to Datalog rules
- Wire into `emend deadcode` as the default backend
- Switch `cfg --unreachable` to query the fact graph
- Remove `find_dead_code()` and `find_unreachable_blocks()`

### Phase 5: Taint migration

- Populate `def_use` facts (block-based) in `build_from_project()`
- Add `func_summary` relation and populate after intraprocedural analysis
- Rewrite intraprocedural taint propagation as Datalog over `def_use`
  (pattern matching stays in Python)
- Rewrite interprocedural fixed-point as recursive Datalog rules
- Migrate flow-based lint rules to use the same Datalog propagation
- Remove Python taint simulation and fixed-point loop

### Phase 6: Cleanup

- Remove all Python graph traversal code replaced by Datalog
- Enforce fact-graph-only path for `impact` (remove fallback)
- Consolidate `parse.db` SQLite indexes and `facts.db` CozoDB into
  a single storage layer where possible
- Update all tests

---

## Open questions

- **Incremental updates.** The fact graph is currently rebuilt from
  scratch. With block IDs instead of line numbers, incremental
  per-function updates become feasible (only re-extract facts for
  changed functions). Worth designing but not blocking.
- **`cfg` visualization.** The `cfg` command needs full block structure
  (statements, defs, uses). Keep using Rust `PyCfg` directly for
  rendering, but delegate `--unreachable` to the fact graph.
- **Two databases.** `parse.db` (SQLite, symbol/reference indexes) and
  `facts.db` (CozoDB, Datalog). Long-term, can `parse.db` be replaced
  by CozoDB? Or is SQLite better for the incremental indexing workload?
- **Pattern matching as facts.** Source/sink matches could be persisted
  as facts to avoid re-running patterns on each taint invocation. Trade-off:
  stale facts vs. re-computation cost.
- **Performance.** CozoDB's semi-naive evaluation handles transitive
  closures efficiently, but the query planner may struggle with large
  inequality joins. Block tagging eliminates the worst case (line-range
  joins), but benchmarking is needed for projects with 100K+ facts.
