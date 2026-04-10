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

## Design Principle: Tree-Sitter AST over Regexes

The current trace module (`trace.py`) contains hardcoded regexes that assume
Python syntax for extracting assignment targets, return values, etc.  Per the
project design philosophy:

> **Do not** use hand-rolled regexes for parsing source code when a
> tree-sitter-based solution exists or can be built.  If the Rust extension
> lacks a needed capability, extend it rather than working around the gap.

The `DefUseFact` relation — populated by tree-sitter `def_use_rules` from
each language's `config.toml` — already contains write targets with line
numbers for every language.  Rather than adding per-language regexes, this
phase replaces inline regex parsing with **fact-graph queries** backed by the
tree-sitter AST.

## Scope

- `src/emend/trace.py` — `run_trace_analysis()`, `_run_trace_datalog()`
- `src/emend/fact_graph.py` — `trace_propagation_datalog()` (should need no
  changes), possibly a new lightweight query helper
- `src/emend/location_resolver.py` — `from_source()` already derives `ext`
  from `file_path`; verify it works with `.ts`/`.rs`
- Language configs: `languages/{python,typescript,rust}/config.toml`

## Hardcoded Assumptions to Fix

### 1. Assignment-target extraction via regex (lines ~814, ~894)

**Current:** `_run_trace_datalog()` uses
`re.match(r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=\s*", stmt_line)` to find the
variable being assigned on a source/sanitizer match line.  This fails for
TypeScript (`let x = source()`) and Rust (`let x = source()`) because it
captures the keyword `let`/`const` instead of the variable name.

**Fix:** Query the `DefUseFact` relation in the already-built fact graph for
`kind="write"` facts on the matched line.  The fact graph is available as the
`graph` local in `_run_trace_datalog()`.  Add a helper:

```python
def _write_targets_on_line(graph: FactGraph, file_path: str, line: int) -> set[str]:
    """Return variable names written on *line* according to DefUseFacts."""
    return {
        fact.var_name
        for fact in graph.def_uses(file_path=file_path, kind="write")
        if fact.def_line == line
    }
```

This is fully language-agnostic because `DefUseFact` is populated by
tree-sitter `def_use_rules` defined in each language's `config.toml`.

### 2. `_extract_identifiers()` keyword filtering (line ~459)

**Current:** Already loads keywords from `config.toml` via `_get_keywords()`.
The identifier tokenisation regex itself (`[A-Za-z_][A-Za-z_0-9]*`) is
language-agnostic and acceptable — it's tokenising capture text, not parsing
source structure.

**Fix:** Thread `language` to all ~15 call sites in `_run_trace_datalog()`
that currently rely on the `language="python"` default.  No new regexes
needed.

### 3. `language="python"` defaults throughout

**Current:** `run_trace_analysis()`, `_compute_function_summary()`,
`_run_interprocedural_trace_datalog()`, `run_interprocedural_trace()` all
default to `language="python"`.

**Fix:** Add auto-detection from file extensions using
`language_registry.detect_language()`.  Keep `"python"` as the fallback for
backward compatibility, but when a list of files is provided, infer the
language from the first file's extension.

### 4. `_find_assignments_in_source()` (line ~487) — Phase 9 scope

This function uses three hardcoded regexes to parse assignments from statement
text.  It is only called from interprocedural analysis functions
(`_compute_function_summary`, `_compute_return_reachable_vars`,
`_collect_param_to_return_dependencies`).  Phase 9 should replace it with
fact-graph `DefUseFact` queries.  Phase 4 only needs to ensure the `ext`
parameter is threaded through (not the default `"py"`), so the underlying
`get_statement_ranges()` call parses the correct language.

## Todo

### Replace regex assignment extraction with fact-graph queries

- [x] Add a `_write_targets_on_line(graph, file_path, line)` helper to
  `trace.py` that queries `DefUseFact` for write targets on a given line.
- [x] Replace the inline regex at ~line 814 (source match assignment target)
  with a call to `_write_targets_on_line()`.
- [x] Replace the inline regex at ~line 894 (sanitizer match assignment
  target) with a call to `_write_targets_on_line()`.
- [x] Verify the replacement produces identical results for existing Python
  tests (the fact graph should return the same targets the regex found).
- [x] Fix CozoSQL `def_use` schema: move `def_line` and `use_line` into the
  primary key to prevent deduplication of same-variable defs in one block.

### Thread `language` through `_extract_identifiers()` calls

- [x] Thread `language` to the `_extract_identifiers()` call at ~line 807
  (source capture variables).
- [x] Thread `language` to the `_extract_identifiers()` call at ~line 851
  (sink capture variables).
- [x] Thread `language` to the `_extract_identifiers()` call at ~line 889
  (sanitizer capture variables).

### Auto-detection and parameter plumbing

- [x] Update `run_trace_analysis()` to auto-detect language from file
  extensions via `detect_language()` when the caller doesn't specify.
- [x] Thread `ext` through `_find_assignments_in_source()` callers (so
  `get_statement_ranges()` parses the correct language for Phase 9 prep).
- [x] Verify `LocationResolver.from_source()` works with `.ts` and `.rs`
  file paths (it derives `ext` from the path already).

### Fix subscript pattern support for non-Python languages

- [x] Fix Rust `node_to_ir()` subscript handling to try `object_field`
  when `value_field` is not found (TypeScript uses `"object"` field).
- [x] Fix Rust `matches_node()` subscript matching to use same fallback.
- [x] Fix subscript index field lookup: try `"index"` when `"subscript"`
  field is not found (TypeScript uses `"index"`, Python uses `"subscript"`).
- [x] Add test for `req.body[$X]` subscript source pattern in TypeScript.

### TypeScript trace tests

- [x] Test: simple assignment propagation (`let x = source(); sink(x)`).
- [x] Test: sanitizer blocking (`let x = source(); x = sanitize(x); sink(x)`).
- [x] Test: arrow function taint flow.
- [x] Test: method call taint (`obj.method()` as source/sink).
- [x] Test: destructuring assignment (`const {a, b} = source()`).
- [x] Test: conditional branches (`if (cond) { x = source() } sink(x)`).
- [x] Test: try/catch taint flow.
- [x] Test: container mutation (`.push()` propagation).
- [x] Test: subscript source pattern (`req.body[$X]`).

### Rust trace tests

- [x] Test: simple let binding propagation (`let x = source(); sink(x)`).
- [x] Test: sanitizer blocking.
- [x] Test: match arm taint flow.
- [x] Test: method call taint (`obj.method()` as source/sink).
- [x] Test: variable reassignment (multi-hop).
- [x] Test: loop taint flow.
- [x] Test: conditional taint flow (`if true { sink(data) }`).
- [x] Test: container mutation (`.push()` propagation).

### CLI integration

- [x] Verify `emend trace src/ --language typescript` works end-to-end.
- [x] Verify `emend trace src/ --language rust` works end-to-end.
- [x] Verify auto-detection: `emend trace file.ts` detects TypeScript.
- [x] JSON output includes correct file paths and line numbers.

## Exit Criteria

- `emend trace` detects source→sink violations in TypeScript files.
- `emend trace` detects source→sink violations in Rust files.
- Sanitizer blocking works correctly for both languages.
- Container mutation taint propagation works for both languages.
- **No hardcoded language-specific regexes in `trace.py`.**  Assignment
  target extraction uses tree-sitter–backed `DefUseFact` queries from the
  fact graph, not regex.
- All existing Python trace tests continue to pass unchanged.
- At least 8 trace test cases per language, mirroring the Python test suite
  structure in `test_trace.py`.
