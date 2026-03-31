# Phase 10: Finalize Interprocedural Trace Engine and Cleanup

## Goal

Close the gap between "migration stabilized" and "the intended engine choice is
explicit, documented, and encoded in tests."

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
lower-level/reference path.

## Decision

For now, **public interprocedural trace remains Python-canonical**.

Rationale:

- it has the working fixed-point summary semantics used by the CLI/API
- it produces the current user-facing `TraceViolation` traces
- it already carries the bug fixes from the roadmap regression work
- the Datalog helper is still useful as a lower-level/reference query, but it
  is not yet a drop-in replacement for public interprocedural analysis

## Scope

- `src/emend/trace.py`
- `tests/test_emend/test_interprocedural_trace.py`
- roadmap/docs for engine ownership

## Todo

- [x] Make the public engine choice explicit in `run_interprocedural_trace_analysis()`.
- [x] Remove stale comments/docstrings claiming the public path tries Datalog
  first or falls back silently.
- [x] Add tests that assert the interprocedural API reports the Python engine
  explicitly.
- [x] Record the engine decision in the roadmap index so this cleanup is no
  longer implicit tribal knowledge.
- [ ] If the project later wants Datalog-canonical interprocedural trace,
  create a follow-up phase that first reaches full parity on:
  - summary semantics
  - witness generation
  - engine-observable tests at API/CLI boundaries

## Exit Criteria

- The public interprocedural trace API has one clearly documented canonical
  engine.
- Tests fail if that engine choice changes silently.
- Roadmap/docs no longer imply a fallback path that does not exist.
