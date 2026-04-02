# Phase 14a: Incremental Fact Updates and Builder Consolidation

## Goal

Make fact graph updates incremental (per-file) and collapse the multiple
build/load/fallback paths into a single pipeline backed by
`build_from_files()`.

## Why

Today there are too many ways to populate a FactGraph:

1. `warm_caches()` — full project index into `parse.db` (SQLite), then
   `_build_facts_db()` rebuilds `facts.db` (CozoDB) from scratch.
2. `_ensure_index_fresh()` — detects stale files, re-indexes them into
   `parse.db`, then calls `_build_facts_db()` for a **full CozoDB rebuild**
   even when one file changed (transform.py:1653).
3. `_get_or_build_fact_graph()` — 4-level fallback cascade: load facts.db →
   `warm_caches()` → `build_from_project()` persisted → `build_from_project()`
   in-memory.
4. `FactGraph.build_from_project()` — standalone builder that re-extracts
   everything from source files into CozoDB.

These paths duplicate logic, and none of them support incremental updates.
When a single file changes, either nothing happens to facts.db or the entire
thing is rebuilt.  This blocks both interactive use (editor-server) and the
Phase 16 cutover (Datalog must be fast enough to be the default).

## Design

### Core primitive: `FactGraph.update_files()`

A single method that takes a list of `(file_path, source_content)` pairs and:

1. **Deletes** all existing facts for those files:
   ```
   ?[...] := *symbol[qn, file, ...], file = $path :rm symbol {...}
   ?[...] := *reference[qn, file, ...], file = $path :rm reference {...}
   # ... same for cfg_block, cfg_edge, def_use, call, import, etc.
   ```
2. **Extracts** new facts from the provided source content using the Rust
   `emend_core` extractors (same logic currently in `_build_facts_db()` and
   `build_from_project()`).
3. **Inserts** the new facts via `:put`.

CozoDB supports `:rm` followed by `:put` within a session, so this is
straightforward.  Derived facts (transitive closures, taint propagation) are
computed at query time by Datalog rules, not materialized, so they're
automatically correct after base fact updates.

### Consolidation

Build everything on top of `update_files()`:

| Current path | Replacement |
|---|---|
| `FactGraph.build_from_project(path)` | `graph = FactGraph(); graph.update_files(all_project_files)` |
| `_build_facts_db(project_root)` | Open/create facts.db, then `graph.update_files(all_project_files)` |
| `_ensure_index_fresh()` facts.db rebuild | `graph.update_files(changed_files_only)` — no full rebuild |
| `_get_or_build_fact_graph()` | Load facts.db. If missing/empty, build via `update_files(all)`. Two paths, not four. |
| Phase 14's `build_from_files()` | Is `update_files()` — same method covers both "build from scratch" and "update subset" |

### Freshness tracking

`parse.db`'s `file_manifest` table already tracks per-file mtime and content
hash via `_scan_manifest()`.  This stays in SQLite — it's a natural fit.  The
incremental flow becomes:

1. `_scan_manifest()` → list of changed/new/deleted file paths
2. For changed/new: `fact_graph.update_files([(path, content), ...])`
3. For deleted: `fact_graph.remove_files([path, ...])`
4. Update `file_manifest` with new hashes

### Editor-server integration

The `reindex` RPC method currently calls `_ensure_index_fresh()` which does a
full facts.db rebuild.  After this phase:

1. `reindex` calls `_scan_manifest()` to find changed files
2. Incrementally updates parse.db (SQLite) for FTS — same as today
3. Incrementally updates facts.db (CozoDB) via `update_files()` — new
4. Returns in milliseconds for single-file changes instead of seconds

## Scope

- `src/emend/fact_graph.py` — add `update_files()`, `remove_files()`;
  refactor `build_from_project()` to delegate to `update_files()`
- `src/emend/transform.py` — replace `_build_facts_db()` body with
  `update_files()` call; fix `_ensure_index_fresh()` to do incremental
  CozoDB updates; simplify `_get_or_build_fact_graph()` fallback cascade
- `src/emend/editor_search.py` — wire `reindex` to incremental path
- Tests for incremental update correctness (add file, modify file,
  delete file — verify facts are correct after each)

## Todo

- [x] Implement `FactGraph.update_files(file_list)`: delete-then-insert
  for a list of files, using the same extraction logic as
  `_build_facts_db()` / `build_from_project()`.
- [x] Implement `FactGraph.remove_files(file_list)`: delete all facts
  for the given files.
- [ ] Refactor `build_from_project()` to call `update_files()`.
  Deferred: `build_from_project()` uses relative paths and project-level
  scope resolver (cross-file references), while `update_files()` uses
  absolute paths and per-file resolvers.  Unifying the path/module
  semantics is a separate concern.
- [ ] Refactor `_build_facts_db()` to call `update_files()` on the
  existing facts.db rather than rebuilding from scratch.
  Deferred: `_build_facts_db()` also populates legacy `fact_symbol`,
  `fact_reference`, `fact_import` relations and computes reachable
  blocks.  Full consolidation requires removing those legacy relations.
- [x] Fix `_ensure_index_fresh()` to call `update_files()` with only
  the changed files instead of `_build_facts_db()` (full rebuild).
- [x] Simplify `_get_or_build_fact_graph()` to two paths: load existing
  facts.db, or create and populate via `update_files(all_files)`.
- [x] Wire editor-server `reindex` to the incremental CozoDB path.
  (Already wired: `reindex` calls `_ensure_index_fresh` which now uses
  `update_files()` for changed files.)
- [x] Add tests: modify one file in a multi-file project, verify only
  that file's facts change, verify derived queries (callers, trace)
  reflect the update.
- [x] Add tests: delete a file, verify its facts are removed and
  derived queries no longer reference it.

## Current Status

Done in this phase:

- `FactGraph.update_files()` — per-file delete-then-insert using CozoDB
  `:rm` queries followed by batch `:put` inserts.  Extracts symbols, CFG,
  references, calls, def-use, method calls, imports, source locations, and
  decorator facts using the same Rust extractors as `build_from_files()`.
- `FactGraph.remove_files()` — deletes all 15 stored relations for given
  file paths, including join-based removal for `decorator_on` and
  `func_summary` (which key on symbol QN, not file path).
- `build_from_files()` refactored to delegate to `update_files()`.
- `_ensure_index_fresh()` now calls `FactGraph.update_files()` for changed
  files instead of `_build_facts_db()` (full CozoDB rebuild).  Deleted
  files are handled by `FactGraph.remove_files()`.
- `_get_or_build_fact_graph()` simplified from 4 fallback levels to 2:
  load existing facts.db, or build via `warm_caches()` + load.
- Editor-server `reindex` benefits automatically via `_ensure_index_fresh()`.
- `tests/test_emend/test_incremental_facts.py` — 12 tests covering:
  initial population, stale fact replacement, file isolation, CFG/source-loc
  updates, file removal, parity with `build_from_files()`, and derived
  query correctness after updates.

Still deferred:

- `build_from_project()` consolidation: uses different path/module semantics
  (relative paths, project-level scope resolver).
- `_build_facts_db()` consolidation: populates legacy `fact_symbol` /
  `fact_reference` / `fact_import` relations and computes reachable blocks.

## Relationship to Other Phases

- **Phase 14** needs `build_from_files()` for small file sets — that's
  `update_files()` on an empty graph.  This phase subsumes Phase 14's
  builder work.
- **Phases 15-17** benefit because the Datalog engine becomes fast enough
  for interactive use, removing the last argument for keeping the Python
  fallback.
- **Phase 18** (cross-language) benefits because `update_files()` can
  accept files of any language — the extraction logic is already
  parameterized by file extension.

## Exit Criteria

- Single-file updates to facts.db complete in <100ms for typical files.
- `_ensure_index_fresh()` does not rebuild facts.db from scratch.
- `_get_or_build_fact_graph()` has at most two code paths (load or build).
- `build_from_project()` and `_build_facts_db()` share one extraction
  pipeline via `update_files()`.
- All existing Datalog tests pass.
- New incremental-update tests pass.
