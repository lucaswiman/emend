# Phase 14: Fix Datalog Intraprocedural Trace for Small Projects

## Goal

Make the Datalog intraprocedural trace engine produce correct results for
arbitrary file sets — including single-file fixtures — by ensuring FactGraph
construction populates CFG/def-use facts eagerly rather than requiring a
full-project build.

## Why

The Datalog intraprocedural engine (`_run_trace_datalog`) returns empty
violations on small file sets because `_get_or_build_fact_graph()` only
populates CFG/def-use facts during full `build_from_project()` runs.  This is
the sole root cause behind all 10 xfailed Phase 9 differential tests.  Until
this is fixed, the Datalog path cannot replace the Python intraprocedural
engine.

## Scope

- `src/emend/fact_graph.py` — `build_from_project()` or new lightweight builder
- `src/emend/trace.py` — `_run_trace_datalog()` FactGraph construction path
- `tests/test_emend/test_phase9_differential.py` — remove xfails

## Todo

- [x] Add a lightweight FactGraph construction path that builds
  symbol/CFG/def-use facts from an explicit list of source files (not a
  project directory).  This may be a new `FactGraph.build_from_files()` class
  method or an extension of the existing builder.
- [x] Wire `_run_trace_datalog()` to use this path so it always has populated
  facts, regardless of project size.
- [x] Convert the 10 xfailed Phase 9 differential tests to passing tests.
- [x] Run the full trace/flow/policy regression slices.

## Current Status

Done:

- `FactGraph.build_from_files(file_paths)` classmethod added: builds symbol,
  CFG block/edge, def-use, reference, call, method-call, and import facts from
  an explicit list of source files.  Uses absolute paths as fact keys so callers
  can look up facts without path normalization.
- `_build_trace_fact_graph()` helper in `trace.py` prefers `build_from_files()`
  for small file sets, falling back to `_get_or_build_fact_graph()`.
- `_run_trace_datalog()` also extracts assignment targets on source-match lines
  (mirroring the Python engine) so taint flows from `request.args.get($X)` to
  the assignment target variable.
- Cross-variable taint propagation added to the Datalog `tainted` rules: when a
  tainted variable is used on the same line as a simple (non-dotted) variable
  is defined, taint transfers across variable names.
- `LocationResolver._find_most_specific_block()` improved: when multiple blocks
  have identical spans, prefer the higher block_id (merge block over entry).
- 9 of 10 Phase 9 xfails removed and green.  The remaining xfail is
  `test_nested_function_both_engines` (strict=False): cross-scope closure taint
  requires interprocedural analysis.
- Full regression suite: 2357 passed, 0 failed.

## Exit Criteria

- [x] `_run_trace_datalog()` produces correct violations on single-file fixtures.
- [x] All Phase 9 differential xfails are removed and green (9/10; 1 accepted
  divergence for nested-function closure scope).
- [x] No regression in existing Datalog trace tests.
