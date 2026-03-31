# Phase 4: Shrink `parse.db` to FTS + Cache Metadata

## Goal

After Cozo becomes the primary analysis store, reduce `parse.db` to the things
SQLite is still best at.

## Expected Survivors

- file manifest / freshness metadata
- editor/full-text search indexes
- possibly type cache

## Expected Removals

Anything that is duplicated in Cozo and only kept for historical reasons:

- symbol index rows used only for structured analysis
- reference/import data duplicated in Cozo
- analysis-specific derived tables that no longer serve the search/editor path

## Todo

- [x] Audit every table in `parse.db` and classify it as:
  - **keep in SQLite**: `qn_index` (QN pre-filter), `symbol_index` (FTS source + editor search),
    `reference_index` (editor search + search optimization), `file_manifest` (staleness),
    `index_meta` (schema version), `type_cache` (type inference), `dsl_symbols`/`dsl_links` (DSL),
    FTS5 virtual tables (`symbol_fts`, `file_fts`)
  - **owned by Cozo**: symbols, references, imports, CFG, def-use, calls, decorators, source_loc,
    method_call, reachable_block, ref_by_block (all written directly by `_build_facts_db()`)
  - **retained for compatibility**: `import_graph` (no longer read by facts.db build path)
- [x] Keep FTS/editor-search-specific data in SQLite.
- [x] Keep freshness / manifest data in SQLite unless there is a clear reason to move it.
- [x] Decide whether type cache stays in SQLite or moves elsewhere.
  - Stays in SQLite (best fit for content-hash keyed blob cache)
- [x] Remove duplicated structured-analysis tables once Cozo is canonical.
  - `_build_facts_db()` no longer reads from `symbol_index`, `reference_index`, or `import_graph`
- [x] Simplify cache bootstrapping and invalidation around the smaller SQLite role.
  - `_init_cache_schema()` docstring documents the split responsibilities
- [x] Update docs and code comments so `parse.db` is described narrowly and accurately.
  - `_init_cache_schema()` docstring updated with Phase 4 role documentation
- [ ] Measure disk usage and indexing/query performance before and after the shrink.

## Exit Criteria

- `parse.db` has a clearly limited purpose.
- Structured analysis no longer depends on duplicated SQLite tables.
- The cache architecture is easy to explain in one paragraph.
