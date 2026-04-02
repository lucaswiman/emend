# Phase 2: Remove Fallback Execution Paths

## Goal

Eliminate "try Datalog, then fall back to Python" behavior for features that
are supposed to be on the fact graph.

## Why

These fallbacks create the worst possible migration behavior:

- they hide bugs in the fact graph path
- they make correctness hard to reason about
- they force two implementations to evolve in parallel
- they make test coverage ambiguous

If a feature is fact-graph-backed, failures should be explicit.

## Scope

Primary candidates:

- trace
- interprocedural trace
- CFG reachability / unreachable blocks
- deadcode
- cascade delete

## Todo

- [x] Inventory every "try Datalog, then fall back" path.
- [x] Classify each feature as one of:
  - fact-graph canonical
  - Python canonical
  - pending migration
- [x] For fact-graph-canonical features, remove the silent fallback.
- [x] Replace silent fallback with explicit failure or a clear bootstrap step.
- [ ] Tighten tests so they assert the intended engine rather than just output shape.
- [x] Add debug logging that reports which engine a feature used.
- [ ] Remove dead code left behind from fallback helpers after each feature is migrated.

## Implementation Notes

### Classification

| Feature | Canonical Engine | Status |
|---------|-----------------|--------|
| Intraprocedural trace | Python | Silent fallback removed |
| Lint flow rules | Python | Silent fallback already removed |
| CFG/unreachable blocks | Fact graph | No fallback existed |
| Dead code | Fact graph | No silent fallback |
| Cascade delete | Fact graph | No silent fallback |
| FactGraph loading | Multi-stage bootstrap | Kept (intentional) |
| Policy datalog checks | Fact graph | Returns explicit error on failure (not silent) |

### Changes Made

- `trace.py`: Removed `if project_path: try: _run_trace_datalog(…)` fallback.
  Python is now the canonical intraprocedural engine. `_run_trace_datalog()`
  retained as dead code for Phase 6 reference.
- `lint.py`: Datalog flow rule fallback was already removed. `fact_graph`
  parameter kept in signature for API compatibility.
- Debug logging added: `logger.debug("Using Python intraprocedural trace engine for %d files", len(paths))`

### Remaining Work

- Tests could be tightened to assert engine choice (low priority)
- `_run_trace_datalog()` dead code can be removed after Phase 6

## Exit Criteria

- Each major feature has one canonical engine.
- There are no silent fallback paths masking fact graph failures.
- Tests encode engine expectations where appropriate.
