# Phase 9: Add Differential CFG / Trace / Flow Regression Coverage

## Goal

Add a regression harness that catches semantic drift between CFG facts, trace,
and flow checks.

## Why

The code currently contains multiple implementations with partially overlapping
semantics. The recent bugs were easy to hide because tests mostly asserted
"some result" rather than "the canonical engine executed with these exact
semantics."

## Scope

- trace tests
- flow-rule tests
- policy/sequence tests
- CLI and MCP smoke tests

## Todo

- [x] Add direct regression tests for the currently known bugs:
  - sanitizer on all sink-reaching paths but not all exit paths
  - scope sanitizer on one branch only
  - Datalog trace runtime breakage
  - effect sinks end-to-end
  - blocked sink/destination block semantics
- [x] Add parity tests that run the same scenario through:
  - engine API
  - CLI command
  - MCP entry point where applicable
- [x] During migration, add differential tests comparing the reference engine
  and the canonical fact-graph engine on the same fixtures.
- [x] Add explicit engine-used reporting in debug/JSON output.
- [x] Add a small curated corpus with nested functions, module-level code,
  same-block ordering cases, and branch-sensitive sanitization.

## Exit Criteria

- Every bug listed in this roadmap has a stable regression test.
- Engine selection is observable in tests.
- CFG/trace/flow behavior is verified at the API and CLI boundaries.
