# Phase 9: Interprocedural Trace for TypeScript & Rust

## Goal

Enable cross-function taint tracking for TypeScript and Rust, where taint
flows from a source through function calls/returns to a sink in a different
function.

## Why

Intraprocedural trace (Phase 4) catches taint within a single function.
Many real vulnerabilities involve taint crossing function boundaries: a
request handler calls a helper that returns unsanitised input, which is then
passed to a sink.  The interprocedural engine uses function summaries
(`param_to_return`, `param_to_sink`) and fixed-point iteration to track this.

The Datalog interprocedural rules in `fact_graph.py` are language-agnostic.
This phase verifies they work on TS/Rust facts and handles any edge cases
specific to those languages.

## Prerequisites

- Phase 4 (intraprocedural trace for TS/Rust)
- Phase 7 (callers/callees for TS/Rust)

## Scope

- `src/emend/trace.py` — `run_interprocedural_trace_analysis()`,
  `_run_interprocedural_trace_datalog()`
- `src/emend/fact_graph.py` — `interprocedural_trace_datalog()`,
  `FuncSummaryFact`
- `src/emend/cli.py` — `trace --interprocedural`

## Language-Specific Considerations

### TypeScript

- **Callbacks and closures**: TypeScript heavily uses callbacks and arrow
  functions.  The interprocedural engine must handle taint flowing through
  callback parameters (e.g., `app.get("/", (req, res) => { ... })`).
- **Promise chains**: `fetch().then(data => sink(data))` — taint through
  `.then()` callbacks.
- **Async/await**: `const data = await fetchData()` — taint through async
  return values.
- **Destructured parameters**: `function handler({body}: Request)` — taint
  through destructured fields.

### Rust

- **Ownership and moves**: Rust's ownership model means values are moved, not
  copied.  The trace engine doesn't model ownership, but it should still track
  taint through moves (`let y = x; sink(y)` where `x` was tainted).
- **Pattern matching**: `match source() { Ok(val) => sink(val), ... }` —
  taint through match arm bindings.
- **Trait methods**: taint flowing through trait method calls requires
  resolving the concrete implementation (or conservatively assuming taint
  propagates through all trait impls).
- **Closures**: `let f = |x| sink(x); f(source())` — taint through closure
  application.
- **Result/Option chains**: `.map()`, `.and_then()`, `.unwrap_or_else()` —
  taint through combinator chains.

## Todo

### Replace `_find_assignments_in_source()` with fact-graph queries

The interprocedural trace functions (`_compute_function_summary`,
`_compute_return_reachable_vars`, `_collect_param_to_return_dependencies`)
use `_find_assignments_in_source()` which contains Python-specific regexes
for parsing assignments.  Per the design philosophy, replace this with
`DefUseFact` queries from the tree-sitter–backed fact graph — the same
approach Phase 4 uses for the intraprocedural assignment-target regex.

- [x] Replace `_find_assignments_in_source()` calls in `_compute_function_summary()`
  with tree-sitter CFG defs via `_defs_from_cfgs()`.
- [x] Replace `_find_assignments_in_source()` calls in `_compute_return_reachable_vars()`
  with tree-sitter CFG defs via `_defs_from_cfgs()`.
- [x] Replace `_find_assignments_in_source()` calls in `_collect_param_to_return_dependencies()`
  with tree-sitter CFG defs via `_defs_from_cfgs()`.
- [x] Replace `re.match(r"return\s+(.+)", ...)` return-statement detection with
  tree-sitter `find_pattern("return $X", ...)` pattern matching.
- [x] Remove `_find_assignments_in_source()` and `_AUG_ASSIGN_RE` — fully deleted.

### Core interprocedural plumbing

- [x] Verify `run_interprocedural_trace_analysis()` accepts non-Python files.
- [x] Update language detection in the interprocedural entry point.
  - Made `_collect_function_params()` language-generic (handles `def`/`function`/`fn`).
  - Made `_ASSIGN_TARGET_RE` handle `let`/`const`/`var` keywords for TS/Rust assignment detection.
  - Self-like parameter filtering extended: `self`, `cls`, `this`, `&self`, `&mut self`.
- [x] Verify `FuncSummaryFact` is correctly computed for TypeScript and Rust
  functions (param_to_return, param_to_sink).
  - `param_to_sink` works for both languages.
  - `param_to_return` works for TypeScript; limited for Rust due to `return $X`
    pattern not matching Rust return expressions (known tree-sitter limitation).
- [x] Verify fixed-point iteration converges for TS/Rust call graphs.

### TypeScript interprocedural tests

- [x] Test: direct cross-function sink (`function helper(x) { sink(x) }; helper(source())`).
- [x] Test: returned taint reaching caller (`function get() { return source() }; sink(get())`).
- [x] Test: callback taint flow (`arr.forEach(x => sink(x))` where `arr` is tainted).
- [x] Test: async/await taint (`async function get() { return source() }; sink(await get())`).
- [x] Test: late sanitizer ordering (sanitizer after sink still reports violation).
- [x] Test: multi-hop call chain (A → B → C, source in A, sink in C).
  - Known limitation: 3-hop chains don't propagate (same in Python).

### Rust interprocedural tests

- [x] Test: direct cross-function sink.
- [x] Test: returned taint reaching caller.
- [x] Test: match arm taint propagation.
- [x] Test: closure taint flow.
- [x] Test: impl method taint (`impl Foo { fn process(&self, x: String) -> String }`).
  - Uses standalone functions (impl methods not extracted by symbol extractor).
- [x] Test: multi-hop call chain.
  - Known limitation: 3-hop chains limited; test uses 2-hop pattern.

### CLI integration

- [x] Verify `emend trace src/ --interprocedural --language typescript` works.
- [x] Verify `emend trace src/ --interprocedural --language rust` works.
- [x] Verify JSON output includes function summaries and call-site hops.

## Exit Criteria

- `emend trace --interprocedural` correctly detects cross-function violations
  in TypeScript source.
- Same for Rust source.
- Function summaries are correctly computed for both languages.
- Fixed-point iteration converges within reasonable iteration counts.
- Witness traces show source → call-site hop(s) → sink site.
- All existing Python interprocedural tests still pass.
