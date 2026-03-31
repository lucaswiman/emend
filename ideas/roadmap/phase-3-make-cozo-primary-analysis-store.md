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

- [ ] Define the canonical set of analysis relations that Cozo owns.
- [ ] Split indexing outputs into:
  - Cozo-owned facts
  - SQLite-owned search/cache data
- [ ] Write Cozo-owned facts directly during indexing.
- [ ] Remove the "copy from parse.db into facts.db" population step.
- [ ] Rework deletion / stale-file cleanup so it updates Cozo directly.
- [ ] Rework first-run bootstrap so `facts.db` is created by normal indexing, not by repair logic.
- [ ] Make `FactGraph.build_from_project()` a deliberate rebuild path, not normal steady-state behavior.
- [ ] Revisit locking/concurrency so direct Cozo writes are robust in parallel indexing.
- [ ] Update tests to validate direct population rather than derived-copy behavior.

## Exit Criteria

- Cozo-owned analysis facts are written directly during indexing.
- There is no post-index copy step from `parse.db` into `facts.db`.
- First-use bootstrapping produces a valid `facts.db` through the normal indexing path.
