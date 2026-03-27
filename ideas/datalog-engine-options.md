# Datalog Engine Options

## Requirements

The fact graph (symbols, calls, references, taint flows, types, imports) is
currently stored in SQLite (`parse.db`) and queried via hand-written SQL.
Many analyses (dead code, transitive callers, taint propagation, slicing,
points-to, effect inference) reduce to Datalog-style fixed-point computation
over these relations.

We need:

1. **Persistence** — on-disk storage, survives process restarts, keyed by
   content hash like parse.db today.
2. **Runtime-definable rules** — users write Datalog rules in YAML/config
   without recompiling.  Powers the MCP query interface and policy engine.
3. **Embeddable** — in-process library, not a server.  No JVM.
4. **Rust and/or Python** — must integrate with emend_core (Rust/PyO3) or
   the Python layer.
5. **Performance** — competitive with current SQLite queries on ~100k-line
   projects.

## Recommended Engine: CozoDB

[CozoDB](https://github.com/cozodb/cozo) is an embedded transactional
database with a Datalog query language (CozoScript).  It was explicitly
designed as "SQLite but Datalog."

**Why CozoDB:**

- **Embeddable**: in-process, no server.  Single Rust crate (`cozo`).
- **Persistent**: multiple backends — RocksDB (default), SQLite, in-memory.
  RocksDB gives ~100K QPS mixed read/write on 1.6M-row relations.
- **Runtime rules**: queries are CozoScript strings evaluated at runtime.
  Users can define custom analyses without recompiling.
- **Rust-native**: core is Rust.  Python bindings via `cozo-embedded` +
  `pycozo` on PyPI (PyO3-based).
- **Transactional**: ACID transactions, so concurrent editor-server queries
  and background indexing don't corrupt data.
- **Datalog-native**: semi-naive evaluation, stratified negation, aggregation,
  fixed-point computation — exactly what the fact graph needs.
- **License**: MPL-2.0 (permissive, compatible with our use).

**What it replaces:**

| Current (SQLite parse.db) | CozoDB equivalent |
|---------------------------|-------------------|
| `symbol_index` table + hand-written SQL | `:symbol` relation + Datalog rules |
| `reference_index` table + `NOT EXISTS` subquery | `:reference` relation + negation-as-failure |
| `find_dead_code` 50-line SQL query | ~5-line Datalog rule set |
| `query_reference_index` exact-match lookup | Pattern query with joins |
| `FactGraph.transitive_callers()` BFS in Python | `Reaches(a,b) :- Calls(a,b). Reaches(a,c) :- Reaches(a,b), Calls(b,c).` |
| `run_interprocedural_taint_analysis()` fixed-point | Taint propagation rules with fixed-point |
| Policy engine `FlowCheck` | Flow policy as Datalog query |

**Example — dead code detection as CozoScript:**

```
dead_code[name, file, line, kind] :=
    *symbol[name, file, line, kind, depth, is_entry, is_exported],
    depth == 1,
    is_entry == false,
    is_exported == false,
    not *reference[target_qn, _, _, _],
    target_qn == name
```

Compare with the current 130-line `_find_dead_code_cached()` function.

**Maintenance status:**

Last release v0.7.6 (Dec 2023).  No new releases in 2024-2025, though minor
PR activity continued.  The codebase is mature Rust (~30k lines), well-tested,
and functionally complete for our use case.  Stalled maintenance is acceptable
because:

- The feature set we need (basic Datalog, persistence, transactions) is
  stable and unlikely to need upstream changes.
- MPL-2.0 Rust means we can fork and maintain if needed.
- We'd pin to a specific version anyway.
- The alternative (building a custom semi-naive evaluator) is more code to
  maintain than adopting a working 30k-line codebase.

## Other Options Considered

### egglog

Datalog + equality saturation.  Has Python bindings and runtime rules.  But
**no persistence**, so it can't replace the cache layer.  Remains interesting
as an optional backend for the rewrite engine (`emend saturate`), as already
noted in `backend-options.md`.

### DuckDB Recursive CTEs

Embedded analytical DB with excellent performance and a new `USING KEY`
optimization for recursive CTEs (SIGMOD 2025).  But encoding Datalog
programs as CTEs is verbose and error-prone.  Better than SQLite for
recursive workloads, but not as natural as actual Datalog.

### SQLite Recursive CTEs

Already available (we use SQLite for parse.db).  The fact graph's transitive
closures are hand-coded versions of this.  Adequate for simple cases but
poor ergonomics for complex multi-rule analyses.  No semi-naive evaluation.

### Souffle

Best-in-class Datalog performance (compiles to parallel C++).  But painful
to embed: C++ compilation step, SWIG Python bindings, no native Rust API.
Better as an external tool than an embedded library.

### Datafrog

Minimal Rust Datalog primitives (sorted-vector joins).  Used by Polonius
(Rust borrow checker).  Too low-level: no persistence, no runtime rules, no
query language.  Essentially a join library, not a database.

### Crepe

Rust Datalog proc macro, similar to Ascent but less feature-rich (no
lattices, no parallelism, no BYODS).  Ascent strictly dominates it.

### pyDatalog

Pure-Python Datalog.  Abandoned (last release 2022).  LGPL.  Do not adopt.

## Migration Path

### Phase 1: Add CozoDB alongside SQLite ✅

- ✅ Added `cozo` v0.7.6 Rust crate to `emend_core` (`Cargo.toml`).
- ✅ Created `PyCozoDb` PyO3 wrapper (`rust/src/cozo_db.rs`) exposing
  CozoDB to Python — supports `"mem"` and `"sqlite"` backends.
- ✅ Rewrote `FactGraph` (`fact_graph.py`) to use CozoDB stored relations
  as its backing store.  Six relations: `symbol`, `call`, `reference`,
  `taint_flow`, `type_binding`, `import`.
- ✅ Transitive closures use native Datalog recursive rules (no Python BFS).
- ✅ `dead_code()` method is a pure Datalog query.
- ✅ Fallback to `pycozo` Python package for standalone testing.

**Design note:** The `cozo-embedded` PyPI wheel lacks a `cp314t`
(free-threaded Python 3.14) build.  Integrating via the Rust crate
in `emend_core` sidesteps this entirely and is the more robust path.
The `compact` feature (which includes `graph-algo`) has a `rayon`
compatibility bug in `graph_builder` 0.4.1; using `minimal` + `rayon`
features avoids it while still providing Datalog + SQLite persistence.

### Phase 2: Migrate parse.db queries (IN PROGRESS)

- ✅ `FactGraph.transitive_callers()` and `transitive_callees()` are
  now Datalog recursive rules.
- ✅ `emend facts` CLI uses CozoDB-backed FactGraph.
- [ ] Dual-write: `warm_caches()` / `_index_batch()` populates CozoDB
  `facts.db` alongside SQLite `parse.db`.
- [ ] `query_symbol_index()` reads from CozoDB.
- [ ] `query_reference_index()` reads from CozoDB.
- [ ] `query_import_graph()` reads from CozoDB.
- [ ] `_find_dead_code_cached()` becomes a CozoScript query.

**Design note:** The CozoDB `facts.db` is a *separate file* from
`parse.db`.  CozoDB's SQLite backend uses its own internal KV schema,
incompatible with hand-written tables.  The two databases live in
`.emend/cache/` side by side.

### Phase 3: User-facing Datalog ✅

- ✅ `emend query` command accepts CozoScript strings.
- ✅ Policy engine `DatalogCheck` type runs CozoScript queries as
  declarative policy checks (parsed from YAML with `type: datalog`).
- [ ] MCP server `datalog_query` tool (deferred — MCP is optional).

### Phase 4: Drop SQLite for fact storage

- [ ] Remove `symbol_index`, `reference_index`, `import_graph` tables
  from `parse.db` once Phase 2 is complete.
- Keep SQLite for: `qn_index` (content-hash blob cache),
  `type_cache` (type oracle cache), `file_manifest` (indexing state),
  `index_meta` (schema version), and FTS5 trigram indexes (editor search).
- CozoDB's built-in FTS uses word-level tokenizers (Simple, Raw), not
  trigram.  Sub-string matching for the editor search still needs
  SQLite FTS5 trigram or a regex fallback.

## References

- CozoDB: https://github.com/cozodb/cozo — MPL-2.0, Rust
- CozoDB Python: https://pypi.org/project/cozo-embedded/
- CozoDB docs: https://docs.cozodb.org/
- Ascent: https://github.com/s-arash/ascent — MIT, Rust
- Ascent OOPSLA paper on BYODS
- egglog: https://github.com/egraphs-good/egglog — MIT, Rust
- DuckDB USING KEY: https://duckdb.org/2025/05/23/using-key (SIGMOD 2025)
- Datafrog: https://github.com/rust-lang/datafrog — Apache-2.0/MIT, Rust
- Souffle: https://github.com/souffle-lang/souffle — UPL-1.0
