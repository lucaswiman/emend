# Phase 7: Cross-Language References, Callers, Callees, Graph

## Goal

Make `emend refs`, `emend graph`, and the underlying `find_references()`,
`find_callers()`, `find_callees()`, and `generate_graph()` functions work on
TypeScript and Rust projects.

## Why

These features are the foundation for project-level navigation and impact
analysis.  They all query the FactGraph (Datalog) for `ReferenceFact`,
`CallFact`, and `SymbolFact` data.  Once the fact graph is correctly populated
for TS/Rust (Phase 2), the Datalog queries themselves are language-agnostic.
The remaining work is in the Python orchestration layer.

## Prerequisites

- Phase 2 (language-aware fact graph) — facts must exist for TS/Rust symbols

## Scope

- `src/emend/transform.py` — `find_references()`, `find_callers()`,
  `find_callees()`, `generate_graph()`
- `src/emend/fact_graph.py` — `refs_datalog()`, `callers_datalog()`,
  `callees_datalog()`, `graph_datalog()` (should need no changes)
- `src/emend/cli.py` — `refs`, `graph` commands

## Current Issues

| Function | Issue |
|----------|-------|
| `find_references()` | Calls `_get_or_build_fact_graph()` without language; uses `_file_to_module()` which assumes Python dotted names |
| `find_callers()` | Same `_file_to_module()` issue; `_find_project_root()` looks for `pyproject.toml` |
| `find_callees()` | Same issues |
| `generate_graph()` | Same issues; output format is language-agnostic |
| `_find_project_root()` | Marker files include `pyproject.toml`, `setup.py` — needs `package.json`, `Cargo.toml` |

## Todo

### Project root detection

- [ ] Update `_find_project_root()` marker files to include:
  - TypeScript: `package.json`, `tsconfig.json`
  - Rust: `Cargo.toml`
  - (Python markers already present)
- [ ] Verify that `_find_source_root()` returns correct paths for TS/Rust
  projects (already partially implemented).

### Module path computation

- [ ] Update `_file_to_module()` to use the language-specific module separator
  from config (`"."` for Python, `"/"` for TypeScript, `"::"` for Rust).
- [ ] Handle TypeScript module paths: `src/utils/helper.ts` → `utils/helper`
  (strip extension, strip source root).
- [ ] Handle Rust module paths: `src/foo/mod.rs` → `foo`, `src/foo/bar.rs` →
  `foo::bar`, `src/lib.rs` → `crate`.

### References

- [ ] Verify `find_references()` works end-to-end for a TypeScript symbol.
- [ ] Verify `find_references()` works end-to-end for a Rust symbol.
- [ ] Test `--writes-only` and `--reads-only` filters for both languages.

### Callers / Callees

- [ ] Verify `find_callers()` works for TypeScript function calls.
- [ ] Verify `find_callers()` works for Rust function calls.
- [ ] Verify `find_callees()` works for both languages.
- [ ] Test method calls (`obj.method()` in TS, `self.method()` in Rust).

### Call graph

- [ ] Verify `generate_graph()` produces correct plain/JSON/DOT output for
  TypeScript projects.
- [ ] Verify same for Rust projects.
- [ ] Test that a mixed Python+TypeScript project graph includes nodes from
  both languages (without cross-language edges).

### Tests

- [ ] `test_refs_typescript.py`: find references to a TypeScript function,
  class, variable; writes-only; reads-only.
- [ ] `test_refs_rust.py`: find references to a Rust function, struct, variable;
  writes-only; reads-only.
- [ ] `test_callers_typescript.py`: callers of exported function, method.
- [ ] `test_callers_rust.py`: callers of pub function, impl method.
- [ ] `test_graph_typescript.py`: call graph generation.
- [ ] `test_graph_rust.py`: call graph generation.

## Exit Criteria

- `emend refs file.ts::functionName` lists all references across a TypeScript
  project.
- `emend refs file.rs::function_name` lists all references across a Rust
  project.
- `emend graph file.ts --format dot` produces a valid call graph.
- `emend graph file.rs --format dot` produces a valid call graph.
- All existing Python reference/caller/callee/graph tests still pass.
