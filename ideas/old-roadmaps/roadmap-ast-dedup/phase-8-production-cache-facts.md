# Phase 8 — Production Cache + Facts Integration

## Purpose

Move the proven parts of the experiment into the real indexing pipeline
without introducing a third persistence layer. The production design must
reuse `parse.db` for content-addressed cache data and `facts.db` for
structured/queryable analysis facts.

## Scope

Production code only. No external-corpus fetching, no standalone experiment DB,
no ad hoc report generator.

## Required production changes

1. Add a content-hash keyed cache table to `parse.db` for per-file duplicate
   analysis payloads, for example:

   - `dup_cache(hash TEXT PRIMARY KEY, version TEXT NOT NULL, data BLOB NOT NULL)`

   `data` stores the canonical subtree payload and sibling-sequence payload for
   one file. It is cache data, not query data.

2. Extend `warm_caches()` / `emend index` to compute duplicate-analysis payloads
   for Python files after parse/QN indexing, using the existing file manifest
   invalidation rules.

3. Materialize queryable duplicate facts into `facts.db` from the cached
   per-file payloads. Minimum required relations:

   - `dup_subtree[file, symbol, root_kind, start_line, end_line, node_count, total_lines, canonical_hash, score]`
   - `dup_run[file, symbol, start_line, end_line, run_hash, stmt_count, score]`

   The exact relation names can differ, but the split must remain:
   per-subtree facts for whole-subtree duplicates, per-run facts for sibling
   sequence duplicates.

4. Hook incremental refresh into the existing indexing path:

   - changed file → recompute `dup_cache`
   - delete old duplicate facts for that file from `facts.db`
   - insert fresh duplicate facts for that file

5. Limit production v1 to Python. The data model may be language-agnostic, but
   the indexing path should only execute on `.py` files until precision is
   proven elsewhere.

6. Production canonicalization rule is intentionally narrow:

   - rename variable bindings/usages
   - keep attribute names and method names literal
   - keep literal constants in the canonical form and hash
     (strings, numbers, booleans, `None`)
   - ignore comments/trivia

   Production code must not ship the experiment's broader literal-stubbing
   behavior as the default path.

## Deliberately out of scope

- Cross-repo corpus merges
- Clone/fetch helpers under `experiments/ast_dedup/.corpora/`
- Per-strategy benchmark reports
- Multiple storage backends or a new `duplicates.db`

## Tests

Add production tests that verify:

1. `emend index` populates duplicate cache/facts on a small synthetic repo.
2. Re-indexing after editing one file updates only that file's duplicate facts.
3. Deleting a file removes its duplicate facts.
4. Re-running `emend index` with no changes reuses the cached `dup_cache`
   rows instead of recomputing all files.

## Checklist

- [x] `parse.db` gains a content-addressed duplicate payload cache
- [x] `facts.db` gains duplicate fact relations
- [x] `emend index` computes duplicate payloads for Python files
- [x] Incremental refresh works for add/edit/delete
- [x] Production tests cover populate + incremental refresh
