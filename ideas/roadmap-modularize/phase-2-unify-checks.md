# Phase 2 — Unify lint / policy / checks / flow_ir into a `checks/` package

## Why

The three rule-running engines have diverged but cover overlapping ground:

| File | LOC | Responsibility |
|---|---|---|
| `lint.py` | 1,108 | Pattern-based `find` + `not-inside` rules; flow rules; deadcode rule wrapper |
| `policy.py` | 1,064 | `FlowCheck`, `StructuralCheck`, `TypeCheck`, `DeadCodeCheck`, `DatalogCheck`, `CustomCheck`, `SequenceCheck` |
| `checks.py` | 195 | Unified runner that just dispatches to lint + policy and normalises violations |
| `flow_ir.py` | 422 | The Datalog ↔ Python flow bridge that both lint and policy call into |
| `rules_config.py` | 192 | Shared `DeadCodeConfig` dataclass and YAML loader |

Symptoms of the split:

- `DeadCodeConfig` lives in `rules_config.py`; `policy.DeadCodeCheck` is
  literally a type alias for it (Phase C3 of the simplify roadmap).
- Both `lint.run_lint` and `policy.run_policy_checks` parse YAML, validate
  rule schemas, resolve config paths with the same `LEGACY_*_PATH`
  fallback chain.
- `cli_checks.py` has three commands (`lint`, `policy`, `check`) that all
  end up calling the same Datalog / FactGraph code via different surface.
- `mcp_server.py` exposes the same dispatch a third time.
- Every new rule kind lands in either `lint.py` or `policy.py` based on
  historical accident, not domain logic.

`checks.py` (195 lines) is already the unified entry point CLI dispatch
should target. This phase finishes the consolidation that file started.

## Target layout

```
src/emend/checks/
├── __init__.py             # Public surface: run_checks, CheckViolation, load_rules
├── engine.py               # run_checks() dispatcher; handles YAML loading; normalises violations
├── rules_config.py         # Move from src/emend/rules_config.py
├── pattern_rules.py        # find/not-inside/replace rules (was lint.py main body)
├── flow.py                 # FlowCheck + flow_ir.py merged; Datalog/Python execution
├── structural.py           # StructuralCheck (must-have-decorator, etc.)
├── types.py                # TypeCheck (oracle-driven type assertions)
├── deadcode.py             # DeadCodeCheck wrapper (delegates to transform/deadcode.py)
├── datalog.py              # DatalogCheck (raw query)
├── custom.py               # CustomCheck (Python callable)
├── sequence.py             # SequenceCheck
└── duplicates.py           # DuplicateCodeConfig wrapper (delegates to duplicate.py)
```

## Execution plan

This is bigger than Phase 1 because it actually merges code, not just
moves it. Do it in stages so each stage is testable.

### Stage 2a — extract shared loader

- Move `rules_config.py` into `checks/rules_config.py`.
- Move `flow_ir.py` into `checks/flow.py` (just relocation; leave the
  Python flow tracker in `lint.py` for now).
- Run tests; ship as one commit.

### Stage 2b — split lint into rule-kind files

- Inside `checks/`, create `pattern_rules.py`, `flow.py` (extends 2a),
  `deadcode.py`, etc.
- For each rule kind, move the matching code from `lint.py` and `policy.py`
  into a single file. Pick the cleaner of the two implementations; the
  other becomes a thin shim.
- The cross-cutting `_check_flow_rule` (lint) and `_run_flow_check`
  (policy) merge into `checks/flow.execute_flow_spec` (already exists in
  `flow_ir.py` — promote it).

### Stage 2c — single engine

- `checks/engine.py` exposes `run_checks(rules, files) -> list[CheckViolation]`.
- Internally: parse YAML once → group rules by kind → dispatch to the
  appropriate `checks/<kind>.py` module → collect violations.
- Replace `lint.run_lint` and `policy.run_policy_checks` with thin
  back-compat wrappers (one-liners) so existing imports don't break.

### Stage 2d — CLI consolidation

- `cli_checks.py` keeps three commands (`lint`, `policy`, `check`) for
  user-facing back-compat, but all three now call `checks.engine.run_checks`
  with different default rule-kind filters:
  - `lint` → only `pattern`/`flow`/`deadcode` kinds
  - `policy` → only `flow`/`structural`/`type`/`deadcode`/`datalog`/`custom`/`sequence` kinds
  - `check` → all kinds
- `mcp_server.py` collapses three nearly-identical wrappers into one.

### Stage 2e — delete the wrappers

- Once Stage 2d is in production for one release, delete `lint.run_lint`
  and `policy.run_policy_checks` shims. Callers that didn't migrate get
  a clear ImportError pointing at `checks.engine.run_checks`.

## Public API to preserve

These imports are used outside the `checks/` package (CLI, MCP, tests).
Whatever happens internally, these names must keep resolving from somewhere:

- `from emend.lint import LintRule, LintViolation, run_lint, load_rules`
- `from emend.policy import Policy, FlowCheck, StructuralCheck, TypeCheck, DeadCodeCheck, CustomCheck, SequenceCheck, run_policy_checks, load_policies, validate_policies`
- `from emend.checks import CheckViolation, run_checks`
- `from emend.rules_config import DeadCodeConfig, load_rules_document`
- `from emend.flow_ir import FlowSpec, FlowViolation, WitnessStep, execute_flow_spec`

Easiest: leave `src/emend/lint.py`, `policy.py`, `flow_ir.py`,
`rules_config.py` as one-line back-compat shims that re-export from the
new locations. (Same pattern as Phase 1's `transform/__init__.py`.)

## Acceptance criteria

- [ ] `make test` passes at every stage.
- [ ] `test_lint.py`, `test_policy.py`, `test_flow_ir.py`,
      `test_flow_rules.py`, `test_lint_*.py` (multilang variants), and
      `test_phase8_dup_cache.py` all stay green.
- [ ] `lint.py` shrinks to <100 lines (back-compat re-exports).
- [ ] `policy.py` shrinks to <100 lines (back-compat re-exports).
- [ ] One CLI command (`emend check`) covers everything; `lint` and `policy`
      become aliases with default kind filters.
- [ ] No more "patterns.yaml vs policies.yaml" branching in dispatch — only
      one document type, validated once.

## Risks

- **Hidden coupling**: lint's `--fix` flag patches source via `replace`.
  Verify the fix path still works after dispatch consolidation
  (`test_lint.py::test_lint_fix*`).
- **`flow_ir.execute_flow_spec` is the hot path**: the 188-line bridge has
  a fallback to the Python tracker. Keep both code paths and the fallback
  trigger logic intact during the move.
- **Datalog rule loading order**: lint loads rules + macros + deadcode +
  duplicate-code config from one document; policy loads policies. They
  use different schema validation. Stage 2c needs a unified schema OR a
  per-kind schema with shared dispatch — pick one explicitly before
  starting 2c, document the choice, and validate against existing
  `.emend/rules.yaml` files in user repos (check `experiments/` and any
  fixtures under `tests/test_emend/data/`).

## Estimated diff size

- ~2,800 lines moved or merged.
- ~300 lines of unification (shared dispatch, schema validator).
- ~−400 lines of duplicated YAML loaders / schema validators / violation
  formatters going away.
- Net: roughly −400 lines and one concept instead of three.
