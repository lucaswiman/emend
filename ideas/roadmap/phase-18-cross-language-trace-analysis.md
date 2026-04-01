# Phase 18: Cross-Language Trace Analysis

## Goal

Enable trace analysis (source/sink/sanitizer detection and taint propagation)
for TypeScript, Rust, and other supported languages, not just Python.

## Why

Once the Python intraprocedural engine is removed (Phase 17), the entire trace
pipeline flows through language-agnostic Datalog rules operating on
language-agnostic facts (def-use, cfg-edge, cfg-block).  The only remaining
barriers to cross-language support are a handful of hardcoded Python
assumptions in fact population and a few helper functions.

## Prerequisites

CFG construction already works for TypeScript and Rust (tested in
`test_cfg_typescript.py` and `test_cfg_rust.py`).  Tree-sitter pattern matching
already works for all supported languages.  FactGraph `build_from_project()`
already parameterises `ext` from file extensions.

## Current Hardcoded Python Assumptions

| Location | Issue |
|----------|-------|
| `fact_graph.py` `_extract_imports()` | Uses `stdlib ast.parse()` — Python only |
| `trace.py:923` | `build_cfgs_for_source(func_source, ext="py")` — ignores language param |
| `trace.py` `_find_container_mutations()` | Hardcoded `.append()`, `.extend()`, `.update()` |
| `trace.py` `_find_for_loops()` | Python-specific `for x in y:` regex |
| `trace.py` `_extract_identifiers()` | Python keyword set (`_KEYWORDS`) |
| `_get_or_build_fact_graph()` | Defaults to `language="python"` |

## Scope

- `src/emend/fact_graph.py` — import extraction, language threading
- `src/emend/trace.py` — remaining hardcoded assumptions
- `src/emend/languages/*/config.toml` — language-specific metadata tables
- New tests for TypeScript and Rust trace analysis

## Todo

### Import Extraction

- [ ] Replace `stdlib ast.parse()` import extraction with tree-sitter-based
  extraction that works for Python, TypeScript, and Rust.
- [ ] Add `ImportFact` population for TypeScript (`import { X } from "Y"`,
  `require()`) and Rust (`use X::Y`, `mod X`).

### Container / Loop / Keyword Parameterisation

- [ ] Move container mutation method names (`.append()`, `.extend()`, etc.)
  into language config files so TypeScript (`.push()`, `.splice()`) and Rust
  (`.push()`, `.insert()`) are handled automatically.
- [ ] Replace the Python for-loop regex with tree-sitter node-type queries
  per language (Python `for_statement`, TypeScript `for_in_statement` /
  `for_of_statement`, Rust `for_expression`).
- [ ] Parameterise the keyword set from language config or tree-sitter grammar
  keywords rather than a hardcoded Python frozenset.

### Plumbing

- [ ] Thread the `language` parameter through `_get_or_build_fact_graph()` so
  non-Python projects build the correct facts.
- [ ] Fix `trace.py:923` to derive `ext` from the file path or language
  parameter instead of hardcoding `"py"`.

### Tests

- [ ] Add trace analysis tests for TypeScript: source→sink detection,
  assignment propagation, sanitizer blocking.
- [ ] Add trace analysis tests for Rust: same coverage.
- [ ] Add a cross-language fixture that traces taint across file types
  (stretch goal — requires cross-language import resolution).

## Exit Criteria

- `emend trace src/ --language typescript` produces correct violations on
  TypeScript source files.
- `emend trace src/ --language rust` produces correct violations on Rust source
  files.
- Language-specific container/loop/keyword handling is driven by config, not
  hardcoded.
- All existing Python trace tests continue to pass.

## Non-Goals

- Cross-language interprocedural analysis (tracing from a Python caller into a
  TypeScript callee).  That requires cross-language import resolution and is a
  separate effort.
- Full type-aware trace analysis for non-Python languages.  Type oracle support
  for TypeScript/Rust is out of scope here.
