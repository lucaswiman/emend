# Phase 11: Cross-Language Type Oracle

## Goal

Extend the `TypeOracle` system to support TypeScript (via `tsc`/`tsserver` or
a standalone type checker) and Rust (via `rust-analyzer`), enabling
`:type[X]` and `:returns[X]` pattern constraints for non-Python code.

## Why

The type oracle enables powerful pattern constraints like
`find: "def $F($X: :type[str]) -> :returns[int]"` to match functions with
specific type signatures.  Currently only Python type checkers are supported
(pyrefly, pyright, ty).  TypeScript has excellent built-in type information
via `tsc`, and Rust has `rust-analyzer` — both can provide type bindings.

This phase is independent of the trace/flow analysis work and can be done
in parallel with other phases.

## Scope

- `src/emend/type_oracle.py` — new adapters
- `src/emend/cli.py` — `types` command, `--engine` option
- `languages/typescript/config.toml` — type oracle configuration
- `languages/rust/config.toml` — type oracle configuration

## Existing Architecture

```
TypeOracle (ABC)
├── PyreflyAdapter   — runs `pyrefly check --debug-info`, parses JSON
├── PyrightAdapter   — starts `pyright-langserver` via LSP, queries hover
└── TyAdapter        — starts `ty lsp` via LSP, queries hover

create_type_oracle(engine="auto") — factory, autodetects from config files
detect_type_engine(project_root) — heuristic: config files first, then PATH
parse_type_string(raw) — parses type strings into TypeDescriptor trees
```

## Planned Adapters

### TypeScriptAdapter

- **Engine**: `tsc` or `tsserver` (TypeScript Language Server)
- **Strategy**: Start `tsserver` via LSP (like PyrightAdapter), query hover
  for type information at each symbol position.
- **Detection**: Look for `tsconfig.json` in project root, then `tsc` on PATH.
- **Type string parsing**: TypeScript type strings (`string`, `number`,
  `Array<string>`, `{ key: string }`, union types `A | B`, intersection
  types `A & B`).
- **Challenges**:
  - Generic types: `Promise<T>`, `Map<K, V>`
  - Union/intersection types need `TypeDescriptor` extensions
  - Template literal types: `` `hello ${string}` ``
  - Conditional types: `T extends U ? X : Y`

### RustAnalyzerAdapter

- **Engine**: `rust-analyzer`
- **Strategy**: Start `rust-analyzer` via LSP, query hover for type
  information.
- **Detection**: Look for `Cargo.toml` in project root, then `rust-analyzer`
  on PATH.
- **Type string parsing**: Rust type strings (`i32`, `String`, `Vec<String>`,
  `Option<T>`, `Result<T, E>`, `&str`, `&mut T`, `Box<dyn Trait>`).
- **Challenges**:
  - Lifetime annotations: `&'a str`
  - Trait bounds: `impl Trait`, `dyn Trait`
  - Associated types
  - Complex generic bounds

## Todo

### TypeScript adapter

- [x] Implement `TypeScriptAdapter` using batch TypeScript Compiler API via Node.js
  (faster than LSP-based approach — avoids per-symbol hover round-trips).
- [x] Add TypeScript type string parsing to `parse_type_string()`.
- [x] Handle union types (`A | B`) in `TypeDescriptor`.
- [x] Handle generic types (`Array<T>`, `Promise<T>`) via angle bracket parsing.
- [x] Handle array shorthand (`string[]`) in `parse_type_string()`.
- [x] Handle arrow function types (`(a: string) => boolean`).
- [x] Add `detect_type_engine()` support for TypeScript projects
  (`tsconfig.json`, `.ts`/`.tsx`/`.js`/`.jsx` extensions).
- [x] Add caching (reuse existing `_FileTypeCache` and `type_cache` table).
- [x] Tests: basic types, generic types, union types, function signatures,
  array shorthand, adapter unit tests, integration tests.

### Rust adapter

- [x] Implement `RustAnalyzerAdapter` following the `_LSPTypeOracle` pattern
  (with `_language_id = "rust"` for correct LSP `textDocument/didOpen`).
- [x] Add Rust type string parsing to `parse_type_string()`.
- [x] Handle lifetime annotations (`&'a str` → strip lifetime).
- [x] Handle generic types (`Vec<T>`, `Option<T>`, `Result<T, E>`).
- [x] Handle reference types (`&str`, `&mut T`).
- [x] Handle Rust function signatures (`fn foo(x: i32) -> String`).
- [x] Add `detect_type_engine()` support for Rust projects
  (`Cargo.toml`, `.rs` extension).
- [x] Add caching (inherits from `_LSPTypeOracle`).
- [x] Tests: basic types, generic types, references, trait objects,
  adapter unit tests, integration tests.

### Pattern constraint integration

- [x] Verify `:type[string]` constraint works in TypeScript patterns
  (via `TypeDescriptor.matches()` — same mechanism as Python).
- [x] Verify `:type[i32]` constraint works in Rust patterns.
- [x] Verify angle bracket generics (`Vec<String>`) match square bracket
  constraints (`Vec[String]`) — tested via `TestTypeDescriptorMatchesCrossLanguage`.
- [x] Verify `:returns[...]` works for arrow functions and Rust callable types.

### CLI integration

- [x] `emend types file.ts` shows inferred types for TypeScript symbols
  (auto-detects `typescript` engine from file extension).
- [x] `emend types file.rs` shows inferred types for Rust symbols
  (auto-detects `rust-analyzer` engine from file extension).
- [x] `--engine` flag supports `typescript` and `rust-analyzer` values.
- [x] Default engine changed from `pyrefly` to `auto` for seamless
  cross-language usage.

## Exit Criteria

- `create_type_oracle(engine="auto")` detects and uses `tsc`/`tsserver` for
  TypeScript projects and `rust-analyzer` for Rust projects.
- `:type[X]` and `:returns[X]` pattern constraints work for TypeScript and
  Rust patterns.
- `emend types` command shows correct inferred types for all three languages.
- All existing Python type oracle tests still pass.

## Non-Goals

- Full type system modelling (dependent types, higher-kinded types).
- Type-aware taint analysis (tracking taint through generic type parameters).
- Cross-language type unification.
