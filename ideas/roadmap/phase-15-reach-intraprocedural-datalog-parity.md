# Phase 15: Reach Intraprocedural Datalog Parity

## Goal

Prove that the Datalog intraprocedural trace engine produces results equivalent
to the Python engine on the full existing test suite, and document or fix any
remaining divergences.

## Why

Phase 14 fixes the structural problem (empty facts), but there may be semantic
differences in how the two engines handle edge cases: container mutations,
for-loop iteration variables, augmented assignments, scope sanitizers,
same-block line ordering, module-level code.  These must be enumerated and
either fixed or accepted before cutover.

## Scope

- `src/emend/trace.py` — `_run_trace_datalog()` propagation logic
- `src/emend/fact_graph.py` — `trace_propagation_datalog()` rules
- `tests/test_emend/test_phase9_differential.py` — remaining divergences
- New differential tests for edge cases

## Todo

- [ ] Add differential tests for container mutations (`.append()`, `.extend()`,
  subscript assignment) comparing both engines.
- [ ] Add differential tests for for-loop iteration variable taint.
- [ ] Add differential tests for augmented assignment (`+=`) taint propagation.
- [ ] Add differential tests for module-level (non-function) code.
- [ ] Fix Datalog propagation rules or Python post-processing until all
  differential tests pass or divergences are explicitly documented with
  rationale.
- [ ] Verify scope sanitizer and path-sensitive sanitizer behaviour matches.
- [ ] Run the full trace/flow/policy regression slices.

## Exit Criteria

- Every differential test either passes or is documented as an accepted
  divergence with rationale.
- The Datalog engine does not produce false negatives relative to the Python
  engine on any existing test.
