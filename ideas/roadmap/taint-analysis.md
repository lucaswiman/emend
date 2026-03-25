# Taint Analysis

## Goal

Track value flow from sources to sinks, with sanitizers and path traces.

## Recommended Scope Split

### v1: Intraprocedural

Start with single-function analysis:

- source pattern introduces labels
- assignments and expression use propagate labels
- sanitizer pattern removes labels
- sink pattern reports violations
- every violation includes a path trace

This delivers useful security and correctness checks without the complexity of
whole-program summaries.

### v2: Interprocedural

Add function summaries only after v1 has:

- stable trace output
- acceptable false-positive rates
- representative benchmark projects

## Core Representation

The critical artifact is not "egglog facts" but a typed flow graph with
provenance:

- value node
- binding/use edge
- call edge
- sanitizer edge
- source and sink annotations
- file/range metadata for trace reporting

If this graph is well designed, it can later be consumed by a native solver,
egglog, or another relational engine.

## Configuration Surface

Extend `.emend/patterns.yaml` with:

- `taint.labels`
- `taint.sources`
- `taint.sinks`
- `taint.sanitizers`
- `taint.propagation`

Keep the initial model close to Semgrep terminology so the feature feels
familiar.

## Commands

```bash
emend taint
emend taint --label user_input
emend taint --trace
emend taint --json
```

## Soundness / Precision Strategy

Default toward sound but explainable over-approximation:

- union at branch joins
- no path sensitivity in v1
- field-insensitive by default
- conservative handling of containers and spread arguments

That is acceptable only if results are explainable. Trace quality matters as
much as raw recall.

## Implementation Notes

1. Build per-function local transfer analysis.
2. Emit path-ready facts, not just booleans.
3. Add suppression support consistent with linting.
4. Benchmark on representative Python projects before adding summaries.

## Deferred

- field sensitivity
- object-sensitive dispatch
- high-precision container modeling
- aggressive framework-specific modeling
