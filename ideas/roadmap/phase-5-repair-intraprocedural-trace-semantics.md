# Phase 5: Repair Intraprocedural Trace Semantics

## Goal

Make the intraprocedural trace engine correct before making it canonical.

## Why

The current Python trace path has semantic bugs even before the Datalog
migration questions:

- Sanitizer coverage is checked against entry-to-exit reachability instead of
  source-to-sink reachability.
- If CFG construction fails, `_all_paths_sanitized()` currently returns
  `True`, which suppresses violations instead of failing closed.
- Scope sanitizers kill all taint for a label globally once matched, even when
  they appear on only one branch.
- Sanitizer variable extraction is regex-based (`name = ...`) and misses
  destructuring, attributes, subscripts, walrus expressions, and other
  non-trivial assignments.

These bugs mean the current "fallback" path is not a trustworthy oracle.

## Scope

- `src/emend/trace.py`
- intraprocedural CLI / API behavior
- path-sensitive sanitizer semantics
- scope sanitizer semantics

## Todo

- [ ] Define the intended suppression semantics explicitly:
  - source-to-sink path coverage
  - same-block line ordering
  - branch-sensitive scope sanitizers
- [ ] Replace the current entry-to-exit `_all_paths_sanitized()` check with a
  sink-specific reachability test.
- [ ] Change CFG-construction failure from "assume sanitized" to an explicit
  degraded-mode policy that does not silently suppress findings.
- [ ] Make scope sanitizers path-sensitive and order-sensitive, not global
  function-wide label deletion.
- [ ] Replace regex assignment-target discovery with AST/block-aware resolution.
- [ ] Decide how module-level code should participate in CFG-backed tracing and
  encode that consistently.
- [ ] Add focused regressions for:
  - sanitizer only on sink-reaching branch
  - scope kill on one branch only
  - same-block sanitizer after sink
  - CFG build failure behavior

## Exit Criteria

- The Python intraprocedural engine has explicitly documented semantics.
- Sanitizer and scope-sanitizer behavior is sink-scoped and path-sensitive.
- There are regression tests for the known false-positive and false-negative
  cases.
