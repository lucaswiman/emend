# Phase 8: Cross-Language Impact Analysis

## Goal

Make `emend impact` work on TypeScript and Rust projects, computing the
transitive set of symbols impacted by a code change.

## Why

Impact analysis is a high-value feature for CI pipelines and code review:
"which tests do I need to run after this change?"  It uses the call graph
(Phase 7) to compute a BFS reverse-caller closure from changed symbols.
Once callers work for TS/Rust, impact analysis follows naturally.

## Prerequisites

- Phase 7 (cross-language refs/callers/graph)

## Scope

- `src/emend/transform.py` — `find_impact()`, `_parse_diff_to_selectors()`
- `src/emend/cli.py` — `impact` command

## Current Python Assumptions

| Location | Issue |
|----------|-------|
| `_parse_diff_to_selectors()` | Parses `diff --git a/foo.py` — may filter by `.py` extension |
| `find_impact()` | Test file detection assumes `test_` prefix and `tests/` directory |
| `find_impact()` | Calls `find_callers()` which was Python-only (fixed in Phase 7) |

## Todo

### Diff parsing

- [ ] Update `_parse_diff_to_selectors()` to handle `.ts`/`.tsx`/`.js`/`.jsx`
  and `.rs` file paths in git diffs.
- [ ] Verify that hunk-to-symbol mapping works for TypeScript and Rust (relies
  on `collect_symbols_from_str()` which already handles both).

### Test detection

- [ ] Update test file/symbol detection heuristics:
  - Python: `test_` prefix, `tests/` directory, `pytest` markers
  - TypeScript: `.test.ts`, `.spec.ts`, `__tests__/` directory, `describe`/
    `it`/`test` function names
  - Rust: `#[test]` attribute, `tests/` directory, `mod tests` convention
- [ ] Make these heuristics config-driven or at least language-dispatched.

### Impact analysis tests

- [ ] TypeScript: change a utility function, verify its callers and their
  callers appear in impact set.
- [ ] TypeScript: verify impacted test files are identified.
- [ ] Rust: change a function, verify transitive impact set.
- [ ] Rust: verify impacted test functions (via `#[test]`) are identified.
- [ ] Mixed project: change a Python file, verify only Python impact is
  computed (no spurious TS/Rust results).

### CLI integration

- [ ] Verify `emend impact --diff HEAD~1 --output tests` works for TS projects.
- [ ] Verify same for Rust projects.
- [ ] JSON output includes correct language metadata.

## Exit Criteria

- `emend impact --diff HEAD~1` on a TypeScript project reports impacted symbols
  and tests.
- Same for a Rust project.
- Test detection correctly identifies language-specific test conventions.
- All existing Python impact tests still pass.
