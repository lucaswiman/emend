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

- [ ] Audit every table in `parse.db` and classify it as:
  - keep in SQLite
  - move to Cozo
  - delete entirely
- [ ] Keep FTS/editor-search-specific data in SQLite.
- [ ] Keep freshness / manifest data in SQLite unless there is a clear reason to move it.
- [ ] Decide whether type cache stays in SQLite or moves elsewhere.
- [ ] Remove duplicated structured-analysis tables once Cozo is canonical.
- [ ] Simplify cache bootstrapping and invalidation around the smaller SQLite role.
- [ ] Update docs and code comments so `parse.db` is described narrowly and accurately.
- [ ] Measure disk usage and indexing/query performance before and after the shrink.

## Exit Criteria

- `parse.db` has a clearly limited purpose.
- Structured analysis no longer depends on duplicated SQLite tables.
- The cache architecture is easy to explain in one paragraph.
