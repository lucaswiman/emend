# Phase 3: Make Cozo the Primary Analysis Store

## Goal

Stop populating CozoDB by copying from `parse.db` after indexing. Instead, write
analysis facts directly into CozoDB during indexing.

## Why

The current architecture pays for two stores:

- SQLite `parse.db`
- CozoDB `facts.db`

But Cozo is currently downstream of SQLite for many facts. That creates:

- duplicate storage
- extra synchronization logic
- bootstrap complexity
- deletion / invalidation complexity

If Cozo is the structured analysis engine, it should be the primary write
target for those facts.

## Scope

Move direct indexing for:

- symbols
- references
- imports
- calls
- CFG edges / blocks
- def-use facts
- decorators / entry-point facts
- trace-supporting facts

## Todo

- [x] Define the canonical set of analysis relations that Cozo owns.
- [x] Split indexing outputs into:
  - Cozo-owned facts (symbols, refs, imports, CFG, def-use, calls, decorators, source_loc, method_call, reachable_block, ref_by_block)
  - SQLite-owned search/cache data (qn_index, symbol_index for FTS, file_manifest, type_cache, dsl_symbols)
- [x] Write Cozo-owned facts directly during indexing.
- [x] Remove the "copy from parse.db into facts.db" population step.
  - `_populate_facts_db()` replaced by `_build_facts_db()` which extracts directly from source files via Rust
- [x] Rework deletion / stale-file cleanup so it updates Cozo directly.
  - `_build_facts_db()` uses `:replace` for atomic swap of all relations
- [x] Rework first-run bootstrap so `facts.db` is created by normal indexing, not by repair logic.
  - `_build_facts_db()` called from `warm_caches()` creates facts.db from scratch
- [x] Make `FactGraph.build_from_project()` a deliberate rebuild path, not normal steady-state behavior.
  - Docstring updated to clarify this is a test/one-off rebuild path
- [x] Revisit locking/concurrency so direct Cozo writes are robust in parallel indexing.
  - `_build_facts_db()` runs single-threaded in main process after workers complete
- [x] Update tests to validate direct population rather than derived-copy behavior.
  - All 2221 tests pass with direct extraction

## Exit Criteria

- Cozo-owned analysis facts are written directly during indexing.
- There is no post-index copy step from `parse.db` into `facts.db`.
- First-use bootstrapping produces a valid `facts.db` through the normal indexing path.
