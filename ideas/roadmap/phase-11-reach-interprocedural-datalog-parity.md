# Phase 11: Reach Interprocedural Datalog Parity

## Goal

Make the Datalog interprocedural trace path a semantic replacement candidate
for the current public Python engine.

## Why

Today the lower-level Datalog path exists, but it is not yet a safe substitute
for `run_interprocedural_trace_analysis()`. The project should not cut over
until parity is explicit and regression-tested.

The implementation checkpoint for this phase is a private adapter in
`trace.py` that computes transitive interprocedural return summaries via
Datalog while preserving the existing public-style witness reconstruction.
That keeps parity work observable without changing the public engine yet.

## Scope

- `src/emend/trace.py`
- `src/emend/fact_graph.py`
- interprocedural trace API tests
- CLI/MCP engine-observable tests where interprocedural trace is surfaced
- `docs/internal/manual-testing/trace-pipeline.md`

## Required Parity Areas

- summary semantics:
  - `param_to_return`
  - `param_to_sink`
  - nested-function scoping
  - same-line / same-block ordering where relevant
- sink timing and ordering semantics:
  - no retroactive sanitization
  - no retroactive assignment taint
- witness fidelity:
  - source site
  - call-site hop(s)
  - sink site
  - stable message/sink-pattern reporting
- config fidelity:
  - label filtering
  - sanitizer behavior
  - configured sources/sinks only

## Todo

- [x] Add differential tests that compare the public Python engine and the
  Phase 11 Datalog adapter on the same fixtures.
- [ ] Enumerate every currently accepted divergence and either fix it or mark it
  as intentional in tests/docs.
- [x] Extend the Datalog path so it can reconstruct public-facing violations
  with parity on label, line, message, and sink pattern.
- [x] Prove parity for nested functions, caller/callee scoping, late
  sanitizers, and statement-order cases already covered in the Python engine.
- [ ] Add CLI/API tests that make engine choice and result equivalence
  observable.
- [ ] Run the manual commands in `docs/internal/manual-testing/trace-pipeline.md`
  on both `emend` itself and at least one external comparison target during
  parity validation.

## Current Status

Done in this phase:

- `trace.py` now has a private `_run_interprocedural_trace_datalog()` adapter.
- the adapter uses Datalog for transitive `param_to_return` closure and reuses
  public-style trace/witness reconstruction so output parity is testable.
- `tests/test_emend/test_phase11_differential.py` locks parity on:
  - direct cross-function sink reporting
  - returned-taint reaching a caller-local sink
  - late sanitizer ordering
  - nested same-named helper scoping

Still required before cutover:

- public CLI/API/MCP equivalence assertions for the Phase 11 adapter
- manual parity runs from `docs/internal/manual-testing/trace-pipeline.md`
- explicit documentation of any remaining accepted divergence

## Exit Criteria

- The Datalog interprocedural path matches the Python public engine on the
  curated parity corpus.
- Any remaining divergence is explicitly documented and accepted.
- Cutover can proceed without knowingly dropping public behavior.
