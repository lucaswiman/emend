# Phase 12: Cut Over Public Interprocedural Trace to Datalog

## Goal

Switch the public interprocedural trace API/CLI path from the Python fixed-point
engine to the Datalog/fact-graph engine.

## Why

Once parity is proven, keeping the Python engine as the public path only
preserves duplicate logic and keeps the migration incomplete.

## Scope

- `src/emend/trace.py`
- CLI/MCP trace entry points
- engine-reporting tests

## Todo

- [ ] Route `run_interprocedural_trace_analysis()` through the Datalog path.
- [ ] Preserve existing public output shape:
  - `TraceViolation`
  - engine metadata
  - message/sink pattern fidelity
  - trace/witness formatting expectations
- [ ] Update tests to assert `engine == "datalog"` for public interprocedural
  violations after cutover.
- [ ] Remove stale comments that still describe the Python engine as canonical.
- [ ] Ensure failures are explicit rather than hidden behind fallback behavior.

## Exit Criteria

- Public interprocedural trace uses the Datalog engine by default.
- Engine choice is observable and asserted in tests.
- No silent fallback to the old Python engine remains in the public path.
