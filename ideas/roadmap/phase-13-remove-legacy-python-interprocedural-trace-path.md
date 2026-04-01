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

- [x] Remove the legacy Python fixed-point implementation from
  `run_interprocedural_trace_analysis()`.
- [x] Delete helper code that only existed to support the old public engine.
- [x] Keep or relocate any utility that is still useful for tests, fixtures, or
  lower-level experiments, but remove it from the public execution path.
- [x] Update roadmap/docs so the end state is unambiguous.
- [x] Run the full trace/flow/policy regression slices to confirm the cleanup
  did not reopen old bugs.

## Exit Criteria

- There is one canonical public interprocedural trace implementation.
- Legacy migration scaffolding for the old Python public path is removed.
- The roadmap's intended end state matches the codebase.

## Completion Notes

Completed in Phase 13.  Changes made:

- **Deleted** `run_interprocedural_trace_analysis()` (~590 lines) and
  `_snapshot_summary()` from `trace.py`.
- **Simplified** `run_interprocedural_trace()` — removed the `engine` parameter
  and `INTERPROCEDURAL_ENGINES` constant; it now directly calls the Datalog
  engine.
- **Updated MCP server** (`mcp_server.py`) to use the public
  `run_interprocedural_trace()` API instead of the deleted legacy import.
- **Removed `--engine` CLI option** from the `trace` command.
- **Deleted** `test_phase11_differential.py` and `test_phase11_divergence.py`
  (parity tests comparing the two engines).
- **Converted** `test_interprocedural_trace.py` to use `run_interprocedural_trace`.
- **Updated** `test_phase12_cutover.py`, `test_phase11_cli_parity.py`, and
  `test_mcp_server.py` to remove Python engine references.
- **Removed** `run_both_interprocedural_engines()` from `conftest.py`.
