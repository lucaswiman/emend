# Phase 10: Finalize Interprocedural Trace Engine and Cleanup

## Goal

Close the gap between "migration stabilized" and "the intended engine choice is
explicit, documented, and encoded in tests, while making the remaining
interprocedural Datalog migration an official next step rather than an implied
cleanup.

## Why

Phases 1-9 fixed correctness bugs and added regression coverage, but they left
one migration seam half-finished:

- `run_interprocedural_trace_analysis()` still claimed it tried Datalog first,
  even though the public path is Python-canonical.
- Tests mostly asserted output shape, not the intended engine choice for the
  interprocedural API.
- The codebase still carried roadmap-era comments implying the interprocedural
  engine decision was temporary, even though the Python engine is the only
  implementation with the current summary/witness behavior.

That ambiguity makes future cleanup harder, because it is unclear whether the
remaining Datalog helper is supposed to replace the public engine or remain a
lower-level/reference path. This phase resolves that ambiguity by recording the
current checkpoint and handing off to explicit migration phases.

## Checkpoint Decision

For now, **public interprocedural trace remains Python-canonical**.

Rationale:

- it has the working fixed-point summary semantics used by the CLI/API
- it produces the current user-facing `TraceViolation` traces
- it already carries the bug fixes from the roadmap regression work
- the Datalog helper is still useful as a lower-level/reference query, but it
  is not yet a drop-in replacement for public interprocedural analysis

This is a **staging decision**, not the final architecture. The project still
intends to migrate public interprocedural trace to the fact-graph/Datalog path
after parity and cutover work are complete.

## Follow-On Migration

The migration is officially part of this roadmap:

- Phase 11 reaches semantic parity between the Python public engine and the
  Datalog interprocedural path.
- Phase 12 switches the public API/CLI entry points to the Datalog engine once
  parity is proven by engine-observable tests.
- Phase 13 removes the legacy Python interprocedural path and any stale
  migration scaffolding left behind by the cutover.

## Scope

- `src/emend/trace.py`
- `tests/test_emend/test_interprocedural_trace.py`
- roadmap/docs for engine ownership
- `docs/internal/manual-testing/README.md`
- `docs/internal/manual-testing/trace-pipeline.md`

## Todo

- [x] Make the public engine choice explicit in `run_interprocedural_trace_analysis()`.
- [x] Remove stale comments/docstrings claiming the public path tries Datalog
  first or falls back silently.
- [x] Add tests that assert the interprocedural API reports the Python engine
  explicitly.
- [x] Record the engine decision in the roadmap index so this cleanup is no
  longer implicit tribal knowledge.
- [x] Split the remaining interprocedural Datalog migration into explicit
  follow-on roadmap phases.
- [x] Add internal manual-testing docs so engine migration work has a stable
  command-execution baseline outside unit tests.

## Exit Criteria

- The public interprocedural trace API has one clearly documented canonical
  engine at the Phase 10 checkpoint.
- Tests fail if that checkpoint engine choice changes silently before the
  planned cutover work.
- Roadmap/docs no longer imply a fallback path that does not exist.
- The remaining Datalog migration is explicitly scheduled, not left as an
  ambiguous future cleanup.
