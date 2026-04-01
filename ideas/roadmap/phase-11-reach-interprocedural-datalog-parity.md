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
- [x] Enumerate every currently accepted divergence and either fix it or mark it
  as intentional in tests/docs.
- [x] Extend the Datalog path so it can reconstruct public-facing violations
  with parity on label, line, message, and sink pattern.
- [x] Prove parity for nested functions, caller/callee scoping, late
  sanitizers, and statement-order cases already covered in the Python engine.
- [x] Add CLI/API tests that make engine choice and result equivalence
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
- CLI `trace --interprocedural --engine datalog` flag added to make engine
  choice observable in both CLI output and JSON.
- `tests/test_emend/test_phase11_cli_parity.py` locks CLI-level parity:
  - default engine is Python
  - explicit `--engine python` and `--engine datalog` route correctly
  - JSON output includes `engine` field for both engines
  - result equivalence (cross-function, returned taint, late sanitizer)
- `tests/test_emend/test_phase11_divergence.py` enumerates parity:
  - 11 parity cases covering: empty config, intraprocedural, sanitizer
    ordering, multi-label, call chains, label filtering, summary contents,
    iteration counts, and trace step descriptions
  - no accepted divergences remain — cases originally expected to diverge
    (iteration count, trace descriptions) were confirmed at full parity

Still required before cutover:

- manual parity runs from `docs/internal/manual-testing/trace-pipeline.md`

## Exit Criteria

- The Datalog interprocedural path matches the Python public engine on the
  curated parity corpus.
- Any remaining divergence is explicitly documented and accepted.
- Cutover can proceed without knowingly dropping public behavior.
