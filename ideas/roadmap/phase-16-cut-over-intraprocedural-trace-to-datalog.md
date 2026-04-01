# Phase 16: Cut Over Intraprocedural Trace to Datalog

## Goal

Switch the public intraprocedural trace path (`run_trace_analysis`) from the
Python taint engine to the Datalog/FactGraph engine.

## Why

Same rationale as Phase 12 for the interprocedural path.  Once parity is
proven, keeping the Python engine as the default preserves duplicate logic.
Cutting over unblocks cross-language trace analysis (Phase 17) because the
Datalog rules are language-agnostic.

## Scope

- `src/emend/trace.py` — `run_trace_analysis()`, `_analyze_function()`
- CLI/MCP trace entry points
- `src/emend/trace.py` — engine-reporting metadata

## Todo

- [ ] Route `run_trace_analysis()` through `_run_trace_datalog()` by default.
- [ ] Preserve `TraceViolation` output shape, engine metadata, message/sink
  fidelity, and trace/witness formatting.
- [ ] Update tests to assert `engine == "datalog"` for intraprocedural
  violations after cutover.
- [ ] Remove stale comments describing the Python engine as canonical for
  intraprocedural analysis.
- [ ] Add an `--engine` escape hatch (like Phase 12 did) if a temporary
  fallback is needed during stabilisation.
- [ ] Run the full trace/flow/policy/lint regression slices.

## Exit Criteria

- Public intraprocedural trace uses the Datalog engine by default.
- Engine choice is observable and asserted in tests.
- No silent fallback to the Python engine.
