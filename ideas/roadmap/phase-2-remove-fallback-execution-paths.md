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

- [ ] Inventory every "try Datalog, then fall back" path.
- [ ] Classify each feature as one of:
  - fact-graph canonical
  - Python canonical
  - pending migration
- [ ] For fact-graph-canonical features, remove the silent fallback.
- [ ] Replace silent fallback with explicit failure or a clear bootstrap step.
- [ ] Tighten tests so they assert the intended engine rather than just output shape.
- [ ] Add debug logging that reports which engine a feature used.
- [ ] Remove dead code left behind from fallback helpers after each feature is migrated.

## Exit Criteria

- Each major feature has one canonical engine.
- There are no silent fallback paths masking fact graph failures.
- Tests encode engine expectations where appropriate.
