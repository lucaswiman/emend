# Phase 6: Make Datalog Trace Paths Real

## Goal

Turn Datalog trace and interprocedural trace from partially wired code paths
into real, testable implementations.

## Why

The current Datalog trace entry points are not merely incomplete; they are
broken and masked by fallback:

- `_run_trace_datalog()` calls `_extract_names_from_text`, which is not defined
  in `trace.py`.
- `_run_trace_datalog()` and the interprocedural Datalog path still construct
  `TraceViolation` using stale field names that do not match the current
  dataclass.
- The intraprocedural Datalog path does not yet thread through:
  - effect sinks
  - sink messages
  - sanitizer quantifiers
  - same-block line-ordering metadata
  - scalar/type-conditioned filtering inputs
- The interprocedural Datalog path currently calls the graph query with no
  config-derived source/sink setup, so it is not equivalent to the configured
  trace analysis.

Until this phase is done, "remove fallback" would just expose broken code.

## Scope

- `src/emend/trace.py`
- `src/emend/fact_graph.py`
- trace CLI and MCP entry points
- intraprocedural and interprocedural Datalog trace

## Todo

- [ ] Fix the immediate runtime breakages in `_run_trace_datalog()`.
- [ ] Align `TraceViolation` construction with the current dataclass.
- [ ] Pass full trace configuration into the Datalog path:
  - sources
  - sinks
  - effect sinks
  - labels
  - per-sink messages
  - sanitizer quantifier
  - same-block line-ordering inputs
  - type/scalar filters
- [ ] Decide whether interprocedural Datalog should consume precomputed
  summaries, raw facts, or both, and make that explicit.
- [ ] Make interprocedural Datalog config-driven rather than a graph-global
  query with no trace config inputs.
- [ ] Add tests that fail if the Datalog path throws and silently falls back.
- [ ] Add end-to-end tests for effect sinks and interprocedural configured flow.

## Exit Criteria

- Datalog intraprocedural trace runs successfully without fallback.
- Datalog interprocedural trace is config-driven and test-covered.
- Trace CLI can report which engine executed, and tests assert it.
