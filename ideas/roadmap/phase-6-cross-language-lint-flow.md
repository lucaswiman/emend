# Phase 6: Cross-Language Lint & Flow Rules

## Goal

Make `emend lint` work on TypeScript and Rust files, including pattern-based
rules, flow rules (`flows-from` / `flows-to` / `not-through`), and the
`deadcode` lint section.

## Why

The lint engine (`lint.py`) loads rules from `.emend/patterns.yaml` and runs
them via pattern matching + optional trace analysis.  Pattern matching already
works for all languages (tree-sitter-based).  Flow rules depend on the trace
engine, which becomes available for TS/Rust in Phase 4.  This phase wires
them together and handles language-specific rule authoring.

## Prerequisites

- Phase 4 (intraprocedural trace) — needed for flow rule checking

## Scope

- `src/emend/lint.py` — `run_lint()`, `_check_flow_rule()`, rule loading
- `src/emend/fact_graph.py` — `flow_rule_check_datalog()` (should be
  language-agnostic)
- `.emend/patterns.yaml` — example multi-language rules

## Current State

The lint engine is mostly language-agnostic already:
- Pattern rules use tree-sitter matching, which works for all languages.
- Flow rules call into `_check_flow_rule()`, which uses the trace engine.
- The `deadcode` section calls `find_dead_code()`.

Remaining issues:
- `run_lint()` may filter files by `.py` extension.
- `_check_flow_rule()` may hardcode `language="python"`.
- Example rules in `.emend/patterns.yaml` are Python-only.

## Todo

### Lint engine language support

- [ ] Update `run_lint()` to scan files of all supported languages, not just
  `.py` files.
- [ ] Update `_check_flow_rule()` to pass the correct language to the trace
  engine.
- [ ] Verify pattern matching works correctly in lint rules for TypeScript
  (e.g., `find: "console.log($X)"` matches `console.log(secret)`).
- [ ] Verify pattern matching works for Rust lint rules (e.g.,
  `find: "unwrap()"` matches `.unwrap()` calls).

### Multi-language rule files

- [ ] Support language scoping in `.emend/patterns.yaml`:
  ```yaml
  rules:
    - name: no-console-log
      language: typescript  # only applies to .ts/.js files
      find: "console.log($X)"
      message: "Remove console.log before merging"
  ```
- [ ] If no `language` key is specified, apply rule to all languages where
  the pattern parses successfully.  (A Python-syntax pattern won't parse
  as TypeScript, so it naturally filters.)
- [ ] Support `language: [python, typescript]` for rules that apply to
  multiple languages.

### TypeScript lint tests

- [ ] Test: simple pattern rule (`find: "console.log($X)"`).
- [ ] Test: pattern rule with `not-inside` constraint.
- [ ] Test: flow rule (`flows-from: "req.query.$X"`, `flows-to: "res.send($X)"`).
- [ ] Test: `--fix` with replacement pattern.

### Rust lint tests

- [ ] Test: simple pattern rule (`find: "unwrap()"`).
- [ ] Test: pattern rule with `not-inside` constraint.
- [ ] Test: flow rule for unsafe patterns.
- [ ] Test: `--fix` with replacement pattern.

### Dead code lint integration

- [ ] Verify `deadcode` section in `patterns.yaml` works when the project
  contains TypeScript or Rust files.
- [ ] Test: `deadcode` section with `entry-point-decorators` for each language.

## Exit Criteria

- `emend lint src/` runs pattern and flow rules on Python, TypeScript, and
  Rust files in the same project.
- Language-scoped rules only apply to matching file types.
- Flow rules produce correct violations for TypeScript and Rust.
- `--fix` works for all three languages.
- All existing Python lint tests still pass.
