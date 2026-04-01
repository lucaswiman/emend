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

- [ ] Add a lightweight FactGraph construction path that builds
  symbol/CFG/def-use facts from an explicit list of source files (not a
  project directory).  This may be a new `FactGraph.build_from_files()` class
  method or an extension of the existing builder.
- [ ] Wire `_run_trace_datalog()` to use this path so it always has populated
  facts, regardless of project size.
- [ ] Convert the 10 xfailed Phase 9 differential tests to passing tests.
- [ ] Run the full trace/flow/policy regression slices.

## Exit Criteria

- `_run_trace_datalog()` produces correct violations on single-file fixtures.
- All Phase 9 differential xfails are removed and green.
- No regression in existing Datalog trace tests.
