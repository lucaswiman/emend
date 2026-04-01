# Phase 17: Remove Legacy Python Intraprocedural Trace

## Goal

Delete the superseded Python intraprocedural trace implementation once the
Datalog cutover is stable.

## Why

Same rationale as Phase 13.  The migration is not finished until there is one
canonical path.

## Scope

- `src/emend/trace.py` — `_analyze_function()`, `_find_assignments_in_source()`,
  `_extract_identifiers()`, `_find_container_mutations()`, `_find_for_loops()`,
  `_extract_qualified_identifiers()`, `_AUG_ASSIGN_RE`, related helpers
- Dead test cleanup
- Comments/docs referring to the Python intraprocedural engine

## Todo

- [ ] Remove `_analyze_function()` and all helpers it exclusively depends on.
- [ ] Remove Python-specific keyword set (`_KEYWORDS`) if no longer referenced.
- [ ] Keep or relocate any utility still needed by other subsystems (e.g.
  `_find_assignments_in_source()` if used outside trace).
- [ ] Update CLAUDE.md and roadmap docs.
- [ ] Run the full test suite.

## Exit Criteria

- There is one canonical intraprocedural trace implementation.
- No dual-engine scaffolding remains for intraprocedural analysis.
