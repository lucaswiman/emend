# Phase 4: Intraprocedural Trace for TypeScript & Rust

## Goal

Run the existing Datalog intraprocedural trace engine on TypeScript and Rust
source files, producing correct source→sink violations.

## Why

This is the first user-visible analysis feature for non-Python.  The Datalog
trace rules (`tainted`, `unsanitized`, `propagated`) are already
language-agnostic — they operate on `DefUseFact`, `CfgEdgeFact`,
`CfgBlockFact`, and `MethodCallFact`.  After Phases 1–3 populate these facts
correctly for TS/Rust, the trace engine should work with minimal changes.

## Prerequisites

- Phase 1 (import extraction) — needed for cross-file symbol resolution
- Phase 2 (language-aware fact graph) — needed to populate facts for TS/Rust
- Phase 3 (parameterised helpers) — needed for keyword filtering and
  container mutation

## Scope

- `src/emend/trace.py` — `run_trace_analysis()`, `_run_trace_datalog()`,
  `_resolve_match_to_location()`, `_find_functions_in_file()`
- `src/emend/fact_graph.py` — `trace_propagation_datalog()` (should need no
  changes if facts are correct)
- `src/emend/location_resolver.py` — ensure `from_source()` works with
  non-Python extensions
- `src/emend/cli.py` — `trace` command: accept `.ts`/`.rs` files

## Remaining Hardcoded Assumptions

| Location | Issue | Fix |
|----------|-------|-----|
| `trace.py:655` | `run_trace_analysis(language="python")` default | Auto-detect from file extensions |
| `trace.py:464` | `_find_assignments_in_source(ext="py")` | Pass correct ext |
| `trace.py:_find_functions_in_file()` | May assume Python function nodes | Use config `function_nodes` |
| `location_resolver.py` | `from_source()` ext parameter | Verify it passes through |
| `trace.py` | `build_cfgs_for_source(func_source, ext="py")` | Derive ext from file path |

## Todo

### Core trace plumbing

- [ ] Update `run_trace_analysis()` to auto-detect language from file
  extensions when `language` is not specified.
- [ ] Fix `build_cfgs_for_source()` calls in `trace.py` to pass the correct
  `ext` derived from the file path instead of hardcoding `"py"`.
- [ ] Update `_find_functions_in_file()` to use language config `function_nodes`
  instead of assuming Python AST structure.
- [ ] Verify `LocationResolver.from_source()` works with `ext="ts"` and
  `ext="rs"`.
- [ ] Verify `_resolve_match_to_location()` correctly maps pattern matches
  to CFG blocks for non-Python languages.

### TypeScript trace tests

- [ ] Test: simple assignment propagation (`let x = source(); sink(x)`).
- [ ] Test: sanitizer blocking (`let x = source(); x = sanitize(x); sink(x)`).
- [ ] Test: arrow function taint flow.
- [ ] Test: method call taint (`obj.method()` as source/sink).
- [ ] Test: destructuring assignment (`const {a, b} = source()`).
- [ ] Test: conditional branches (`if (cond) { x = source() } sink(x)`).
- [ ] Test: try/catch taint flow.
- [ ] Test: container mutation (`.push()` propagation).

### Rust trace tests

- [ ] Test: simple let binding propagation (`let x = source(); sink(x)`).
- [ ] Test: sanitizer blocking.
- [ ] Test: match arm taint flow.
- [ ] Test: method call taint (`obj.method()` as source/sink).
- [ ] Test: pattern destructuring (`let (a, b) = source()`).
- [ ] Test: loop taint flow (`for x in source() { sink(x) }`).
- [ ] Test: `if let` / `while let` taint flow.
- [ ] Test: container mutation (`.push()` propagation).

### CLI integration

- [ ] Verify `emend trace src/ --language typescript` works end-to-end.
- [ ] Verify `emend trace src/ --language rust` works end-to-end.
- [ ] Verify auto-detection: `emend trace file.ts` detects TypeScript.
- [ ] JSON output includes correct file paths and line numbers.

## Exit Criteria

- `emend trace` detects source→sink violations in TypeScript files.
- `emend trace` detects source→sink violations in Rust files.
- Sanitizer blocking works correctly for both languages.
- Container mutation taint propagation works for both languages.
- All existing Python trace tests continue to pass unchanged.
- At least 8 trace test cases per language, mirroring the Python test suite
  structure in `test_trace.py`.
