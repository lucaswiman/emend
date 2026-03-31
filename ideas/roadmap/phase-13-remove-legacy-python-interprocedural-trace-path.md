# Phase 13: Remove Legacy Python Interprocedural Trace Path

## Goal

Delete the superseded Python interprocedural trace implementation once the
public Datalog cutover is complete and stable.

## Why

The migration is not finished until there is one canonical public path.
Keeping the old Python implementation around after cutover would reintroduce
the ambiguity that the roadmap has been trying to remove.

## Scope

- `src/emend/trace.py`
- dead helper cleanup
- docs/comments/tests referring to the old engine

## Todo

- [ ] Remove the legacy Python fixed-point implementation from
  `run_interprocedural_trace_analysis()`.
- [ ] Delete helper code that only existed to support the old public engine.
- [ ] Keep or relocate any utility that is still useful for tests, fixtures, or
  lower-level experiments, but remove it from the public execution path.
- [ ] Update roadmap/docs so the end state is unambiguous.
- [ ] Run the full trace/flow/policy regression slices to confirm the cleanup
  did not reopen old bugs.

## Exit Criteria

- There is one canonical public interprocedural trace implementation.
- Legacy migration scaffolding for the old Python public path is removed.
- The roadmap’s intended end state matches the codebase.
