# Datalog Migration Roadmap

This roadmap tracks the migration from dual Python/Datalog analysis engines to
a single Datalog/FactGraph-based engine, and the subsequent extension to
cross-language support.

## Completed Phases

- [x] [Phase 1: Remove Public Datalog Surfaces](./phase-1-remove-public-datalog-surfaces.md)
- [x] [Phase 2: Remove Fallback Execution Paths](./phase-2-remove-fallback-execution-paths.md)
- [x] [Phase 3: Make Cozo the Primary Analysis Store](./phase-3-make-cozo-primary-analysis-store.md)
- [x] [Phase 4: Shrink `parse.db` to FTS + Cache Metadata](./phase-4-shrink-parse-db.md)
- [x] [Phase 5: Repair Intraprocedural Trace Semantics](./phase-5-repair-intraprocedural-trace-semantics.md)
- [x] [Phase 6: Make Datalog Trace Paths Real](./phase-6-make-datalog-trace-paths-real.md)
- [x] [Phase 7: Resolve Pattern Matches to Exact CFG Locations](./phase-7-resolve-pattern-matches-to-exact-cfg-locations.md)
- [x] [Phase 8: Unify Flow, Policy, and Sequence Checks on One Engine](./phase-8-unify-flow-policy-and-sequence-checks.md)
- [x] [Phase 9: Add Differential CFG/Trace/Flow Regression Coverage](./phase-9-add-differential-cfg-trace-flow-regression-coverage.md)
- [x] [Phase 10: Finalize Interprocedural Trace Engine and Cleanup](./phase-10-finalize-interprocedural-trace-engine-and-cleanup.md)
- [ ] [Phase 11: Reach Interprocedural Datalog Parity](./phase-11-reach-interprocedural-datalog-parity.md)
- [x] [Phase 12: Cut Over Public Interprocedural Trace to Datalog](./phase-12-cut-over-public-interprocedural-trace-to-datalog.md)
- [x] [Phase 13: Remove Legacy Python Interprocedural Trace Path](./phase-13-remove-legacy-python-interprocedural-trace-path.md)

## Intraprocedural Datalog Migration

- [x] [Phase 14: Fix Datalog Intraprocedural Trace for Small Projects](./phase-14-fix-datalog-intraprocedural-trace-for-small-projects.md)
- [x] [Phase 14a: Incremental Fact Updates and Builder Consolidation](./phase-14a-incremental-fact-updates.md)
- [x] [Phase 15: Reach Intraprocedural Datalog Parity](./phase-15-reach-intraprocedural-datalog-parity.md)
- [x] [Phase 16: Cut Over Intraprocedural Trace to Datalog](./phase-16-cut-over-intraprocedural-trace-to-datalog.md)
- [x] [Phase 17: Remove Legacy Python Intraprocedural Trace](./phase-17-remove-legacy-python-intraprocedural-trace.md)

## Cross-Language Extension

- [ ] [Phase 18: Cross-Language Trace Analysis](./phase-18-cross-language-trace-analysis.md)

## Current CFG / Trace / Flow Status

Status of known issues in the tracing and flow stack:

- ~~The Python intraprocedural sanitizer check is sink-insensitive.~~
  Addressed: the Datalog engine uses sink-scoped propagation rules.  The legacy
  Python engine was removed in Phase 17.
- ~~Python scope sanitizers are not path-sensitive.~~
  Addressed: the Datalog engine handles this correctly.  The legacy Python engine
  was removed in Phase 17.
- ~~The Datalog trace path called undefined helpers and used stale field names.~~
  Fixed in Phases 5–6.
- ~~Datalog trace entry points were incomplete (effect sinks, sanitizer
  quantifiers, same-block ordering, exact resolution).~~
  Fixed in Phases 7–9.
- ~~Flow/policy/sequence logic resolved matches to `("", -1)`.~~
  Fixed in Phase 8.
- ~~The Datalog intraprocedural engine returns empty results on small
  file sets because FactGraph construction requires a full project build.~~
  Fixed in Phase 14: `FactGraph.build_from_files()` builds facts directly from
  an explicit file list; `_run_trace_datalog()` uses this path automatically.
- ~~Fact graph updates are not incremental.~~
  Addressed in Phase 14a: `_ensure_index_fresh()` now calls
  `FactGraph.update_files()` for changed files (incremental) instead of
  `_build_facts_db()` (full rebuild).  `_get_or_build_fact_graph()`
  simplified from 4 fallback levels to 2.  `build_from_project()` and
  `_build_facts_db()` consolidation deferred pending legacy relation
  removal.
- ~~Datalog intraprocedural engine missing container mutation, module-level,
  and scope-sanitizer support.~~
  Fixed in Phase 15: container mutation taint via `method_call` facts,
  module-level def-use synthesis from scope resolver, scope-kill same-block
  line-ordering suppression.  Three accepted divergences documented where
  the Datalog engine is more correct than the Python engine.

## Intended End State

- Users interact with `find`, `edit`, `analyze`, `check`, `map`, and `mcp`.
- CozoDB / `FactGraph` are internal infrastructure for structured analysis.
- There is one canonical execution path per feature.
- CFG, trace, and flow analyses share one block-aware location model.
- Trace and flow semantics are sink-scoped, path-sensitive, and testable across
  both direct API and CLI entry points.
- Trace analysis works for Python, TypeScript, and Rust using the same Datalog
  rules over language-agnostic facts.
- `parse.db` remains only where SQLite is still the better fit:
  - full-text/editor search
  - freshness metadata / manifests
  - possibly type cache

## Non-Goals

- Keep raw CozoScript as a product feature.
- Preserve silent fallbacks that hide fact graph breakage.
- Keep dual-write / post-copy architecture longer than necessary.
