# Phase 8: Unify Flow, Policy, and Sequence Checks on One Engine

## Goal

Remove the current split between lint flow rules, policy flow checks, and
sequence checks by giving them one block-aware execution model.

## Why

The current flow stack is fragmented:

- `policy._run_flow_check()` still delegates to lint's `_check_flow_rule()`.
- Lint flow rules have their own Python simulation plus a partial Datalog path.
- Sequence checks compile separately and use their own blocker-resolution logic.
- Blocker semantics in `flow_rule_check_datalog()` are too weak: they check the
  current/from block but not the destination block, so a blocked sink block can
  still be entered.

This duplication guarantees semantic drift.

## Scope

- `src/emend/lint.py`
- `src/emend/policy.py`
- `src/emend/fact_graph.py`
- sequence/path-constraint compilation

## Todo

- [x] Define one canonical intermediate representation for flow/path checks.
  — `FlowSpec`, `WitnessStep`, `FlowViolation` in `src/emend/flow_ir.py`.
- [x] Make lint and policy flow rules compile into that shared IR.
  — `from_lint_rule()` and `from_flow_check()` converters in `flow_ir.py`.
- [x] Decide whether sequence checks are a specialized form of the same IR or a
  separate IR layered on top of exact CFG locations.
  — Sequence checks remain separate but share the `WitnessStep`/`format_witness`
  witness model via `flow_ir.py`.
- [x] Fix blocker semantics so both source and destination blocks are handled
  correctly.
  — `flow_rule_check_datalog()` now checks `not blocked[..., use_block]`;
  `_compile_sequence_query()` checks `not blocker[..., to_block]` and
  `not scope_kill[..., to_block]`.
- [x] Add same-block ordering semantics for blockers where needed.
  — `flow_rule_check_datalog()` accepts `source_lines`/`sink_lines`/`blocker_lines`
  and post-filters in Python for same-block line ordering.
- [x] Stop routing policy flow checks through lint-specific formatting code.
  — `policy._run_flow_check()` now uses `execute_flow_spec()` directly.
- [x] Normalize witness generation so lint/policy/check output share one path
  explanation model.
  — `WitnessStep` + `format_witness()` used by lint, policy, and sequence checks.

## Exit Criteria

- Flow-based lint and policy checks use one engine.
- Sequence/path constraints use the same block/location model.
- Blocker semantics are consistent and test-covered.
