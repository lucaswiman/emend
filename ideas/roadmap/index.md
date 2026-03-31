# Datalog Migration Roadmap

This roadmap assumes the current codebase is in the middle of a migration:

- `parse.db` is still the primary cache/index
- `facts.db` / `FactGraph` / CozoDB power a growing share of analysis
- several features still use "try Datalog, then fall back"
- raw Datalog is still exposed as a user-facing surface

The goal is to finish the migration cleanly instead of carrying both models
indefinitely.

## Phases

- [ ] [Phase 1: Remove Public Datalog Surfaces](./phase-1-remove-public-datalog-surfaces.md)
- [ ] [Phase 2: Remove Fallback Execution Paths](./phase-2-remove-fallback-execution-paths.md)
- [ ] [Phase 3: Make Cozo the Primary Analysis Store](./phase-3-make-cozo-primary-analysis-store.md)
- [ ] [Phase 4: Shrink `parse.db` to FTS + Cache Metadata](./phase-4-shrink-parse-db.md)

## Intended End State

- Users interact with `find`, `edit`, `analyze`, `check`, `map`, and `mcp`.
- CozoDB / `FactGraph` are internal infrastructure for structured analysis.
- There is one canonical execution path per feature.
- `parse.db` remains only where SQLite is still the better fit:
  - full-text/editor search
  - freshness metadata / manifests
  - possibly type cache

## Non-Goals

- Keep raw CozoScript as a product feature.
- Preserve silent fallbacks that hide fact graph breakage.
- Keep dual-write / post-copy architecture longer than necessary.
