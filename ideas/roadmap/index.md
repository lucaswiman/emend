# Datalog Migration Roadmap

This roadmap assumes the current codebase is in the middle of a migration:

- `parse.db` is still the primary cache/index
- `facts.db` / `FactGraph` / CozoDB power a growing share of analysis
- several features still use "try Datalog, then fall back"
- raw Datalog is still exposed as a user-facing surface

The goal is to finish the migration cleanly instead of carrying both models
indefinitely.

## Phases

- [x] [Phase 1: Remove Public Datalog Surfaces](./phase-1-remove-public-datalog-surfaces.md)
- [x] [Phase 2: Remove Fallback Execution Paths](./phase-2-remove-fallback-execution-paths.md)
- [x] [Phase 3: Make Cozo the Primary Analysis Store](./phase-3-make-cozo-primary-analysis-store.md)
- [x] [Phase 4: Shrink `parse.db` to FTS + Cache Metadata](./phase-4-shrink-parse-db.md)
- [x] [Phase 5: Repair Intraprocedural Trace Semantics](./phase-5-repair-intraprocedural-trace-semantics.md)
- [x] [Phase 6: Make Datalog Trace Paths Real](./phase-6-make-datalog-trace-paths-real.md)
- [x] [Phase 7: Resolve Pattern Matches to Exact CFG Locations](./phase-7-resolve-pattern-matches-to-exact-cfg-locations.md)
- [ ] [Phase 8: Unify Flow, Policy, and Sequence Checks on One Engine](./phase-8-unify-flow-policy-and-sequence-checks.md)
- [ ] [Phase 9: Add Differential CFG/Trace/Flow Regression Coverage](./phase-9-add-differential-cfg-trace-flow-regression-coverage.md)

## Current CFG / Trace / Flow Bugs

The current tracing and flow stack has correctness bugs in addition to the
architectural migration work above.

- The Python intraprocedural sanitizer check is sink-insensitive: it asks
  whether all entry-to-exit paths pass through a sanitizer, not whether all
  source-to-sink paths do. That can report false positives when every path to
  the sink is sanitized but some unrelated path to function exit is not.
- Python scope sanitizers are not path-sensitive: a single matched
  `scope_sanitizer` currently kills all taint for the label across the whole
  function, which causes false negatives when the kill occurs on only one
  branch.
- The Datalog trace path is currently hidden behind fallback, but not actually
  healthy. The current code calls an undefined helper in `_run_trace_datalog()`
  and still constructs `TraceViolation` objects using stale field names.
- The Datalog trace entry points are also incomplete: they do not yet thread
  through effect sinks, per-sink messages, sanitizer quantifiers, same-block
  line-ordering data, or exact function/block resolution.
- Flow/policy/sequence logic still resolves many matches to `("", -1)` or
  nearest-line approximations instead of exact `(file, function, block)` facts,
  which makes blocker semantics and CFG reasoning lossy.

## Intended End State

- Users interact with `find`, `edit`, `analyze`, `check`, `map`, and `mcp`.
- CozoDB / `FactGraph` are internal infrastructure for structured analysis.
- There is one canonical execution path per feature.
- CFG, trace, and flow analyses share one block-aware location model.
- Trace and flow semantics are sink-scoped, path-sensitive, and testable across
  both direct API and CLI entry points.
- `parse.db` remains only where SQLite is still the better fit:
  - full-text/editor search
  - freshness metadata / manifests
  - possibly type cache

## Non-Goals

- Keep raw CozoScript as a product feature.
- Preserve silent fallbacks that hide fact graph breakage.
- Keep dual-write / post-copy architecture longer than necessary.
