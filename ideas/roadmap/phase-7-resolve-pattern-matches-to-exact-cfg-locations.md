# Phase 7: Resolve Pattern Matches to Exact CFG Locations

## Goal

Introduce one exact location-resolution layer that maps pattern matches to
`(file_path, func_qn, block_id, line, captures)` and use it everywhere.

## Why

The current CFG/flow integration is too lossy:

- Trace and lint Datalog entry points frequently pass `("", -1)` for function
  and block IDs.
- Sequence compilation reconstructs block locations with nearest-line
  heuristics and leaves an unused `block_ranges_result`, which suggests the
  intended exact source-loc path was never finished.
- Module-level matches and nested-function matches are handled inconsistently
  across trace, flow, and sequence checks.

Without exact block resolution, path blockers and CFG reachability rules cannot
be trusted.

## Scope

- trace source/sink/sanitizer resolution
- flow rule resolution
- sequence/path-constraint resolution
- source location and CFG block facts

## Todo

- [x] Add a single resolver from `PatternMatch` to exact CFG-backed locations.
- [x] Use `source_loc` / `cfg_block` facts as the source of truth rather than
  nearest-line guesses from `def_use` and `cfg_edge`.
- [x] Resolve matches to the innermost containing function consistently.
- [x] Decide on a first-class representation for module-level code instead of
  mixing empty function names and sentinel block IDs.
- [x] Thread exact block IDs through trace, lint flow rules, policy checks, and
  sequence/path constraints.
- [x] Remove the remaining `("", -1)` sentinel plumbing once the resolver is in
  place.
- [x] Add regressions for nested functions, same-line multiple blocks, and
  module-level matches.

## Exit Criteria

- Pattern-driven analyses all consume the same exact location tuples.
- Sentinel block/function placeholders are gone from analysis entry points.
- Blocker and CFG semantics no longer rely on nearest-line approximations.
