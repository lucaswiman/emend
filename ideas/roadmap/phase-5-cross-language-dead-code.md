# Phase 5: Cross-Language Dead Code Detection

## Goal

Make `emend deadcode` work on TypeScript and Rust projects, detecting
unreferenced symbols and unreachable code blocks.

## Why

Dead code detection depends on three things: (1) a populated FactGraph with
symbols, references, and CFG edges, (2) entry point heuristics to know what's
alive, and (3) language-specific module structure to resolve imports.  After
Phases 1–3, (1) and (2) are available.  This phase focuses on (3) and wiring
everything together.

## Scope

- `src/emend/transform.py` — `find_dead_code()`, `_is_likely_entry_point()`,
  `_is_dunder()`
- `src/emend/fact_graph.py` — `dead_code_unified()` (Datalog, should be
  language-agnostic)
- `src/emend/cli.py` — `deadcode` command language support

## Current Python-Specific Assumptions in Dead Code

| Assumption | Location | Fix |
|-----------|----------|-----|
| `_is_dunder()` check | `transform.py:5207` | Skip for non-Python |
| `__init__.py` / `__all__` handling | `transform.py` | Language-specific module structure |
| `_ENTRY_POINT_DECORATORS` (Python-specific) | `transform.py:5175` | Config-driven (Phase 3) |
| `.py` extension in `_collect_source_files()` | `transform.py` | Multi-extension (Phase 2) |
| `# noqa: emend:deadcode` suppression | `transform.py:1211` | Add `// noqa` for TS, no Rust equivalent |
| String literal scanning for dynamic refs | `transform.py` | Language-specific string nodes |
| `git log -S` last reference | `transform.py` | Language-agnostic (already works) |
| Test file detection heuristics | `transform.py` | `test_*` (Python), `*.test.ts` / `*.spec.ts` (TS), `#[test]` (Rust) |

## Todo

### Language-aware dead code core

- [ ] Update `find_dead_code()` to accept and auto-detect language.
- [ ] Update `_is_likely_entry_point()` to use config-driven entry point lists
  (from Phase 3).  For Python, keep existing behaviour; for TS/Rust, use
  language-specific heuristics.
- [ ] Skip `_is_dunder()` for non-Python languages.
- [ ] Update test file detection: Python uses `test_` prefix and `tests/`
  directory; TypeScript uses `.test.ts`, `.spec.ts`, `__tests__/` directory;
  Rust uses `#[test]` attribute and `tests/` directory.

### TypeScript dead code

- [ ] Handle `export` visibility: exported symbols are entry points.
- [ ] Handle `module.exports` / `exports.X` as entry points.
- [ ] Handle framework-specific entry points (React components, Express
  handlers) via config.
- [ ] Handle `// noqa: emend:deadcode` comment suppression.
- [ ] String literal scanning for TypeScript string nodes.
- [ ] Tests: unreferenced function, unreferenced class, exported function
  (not dead), test function (not dead), framework handler (not dead).

### Rust dead code

- [ ] Handle `pub` visibility: `pub` items are entry points.
- [ ] Handle `#[test]` functions as entry points.
- [ ] Handle `fn main` as entry point.
- [ ] Handle `#[no_mangle]`, `#[export_name]` as entry points.
- [ ] Handle `pub use` re-exports.
- [ ] Handle trait implementations: methods implementing a trait are not dead.
- [ ] Inline suppression: `#[allow(dead_code)]` attribute (or `// noqa`).
- [ ] Tests: unreferenced function, unreferenced struct, pub function (not dead),
  test function (not dead), trait impl (not dead), `fn main` (not dead).

### Unreachable block detection

- [ ] Verify `dead_code_unified()` Datalog query handles TypeScript CFGs
  (unreachable blocks after `return`/`throw`).
- [ ] Verify same for Rust CFGs (unreachable after `return`/`break`/`continue`,
  diverging expressions like `panic!()`).
- [ ] Tests for unreachable blocks in both languages.

## Exit Criteria

- `emend deadcode /path/to/ts-project` reports unreferenced TypeScript symbols.
- `emend deadcode /path/to/rust-project` reports unreferenced Rust symbols.
- Entry point heuristics correctly exclude exported/pub/test/main symbols.
- Unreachable block detection works for both languages.
- All existing Python dead code tests still pass.
