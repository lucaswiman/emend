# TypeScript & Rust Parity Roadmap

Bring TypeScript and Rust analysis to feature parity with Python across
control flow, taint/trace, dead code, lint, references, impact, and
interprocedural analysis.

## Current State

Python has full analysis support: CFG construction, intraprocedural and
interprocedural trace/taint analysis, dead code detection, lint rules with
flow checks, impact analysis, references/callers/callees/graph, DSL
integration, and type oracle integration.

TypeScript and Rust have:
- Tree-sitter parsing and pattern matching (working)
- Scope resolution and qualified names (working)
- CFG construction with def-use facts (working, tested)
- Language config files with scoping/binding/CFG rules (working)
- Source root detection (`_find_source_root`) (working)

TypeScript and Rust are **missing**:
- Trace/taint analysis (hardcoded Python assumptions block it)
- Import fact extraction (uses `stdlib ast.parse`, Python only)
- Dead code detection (Python-specific entry point heuristics, `__init__.py`)
- Lint flow rules (trace engine is Python-only)
- Project-level references/callers/callees/graph (hardcoded `language="python"`)
- Impact analysis (depends on callers, which is Python-only)
- Container mutation tracking (hardcoded `.append()`, `.extend()`, `.update()`)
- Keyword filtering (hardcoded `_PYTHON_KEYWORDS` frozenset)
- Module naming (`_file_to_module` assumes Python dotted paths)
- Type oracle (Python-only adapters: pyrefly, pyright, ty)
- Interprocedural trace (depends on all of the above)

## Phases

### Foundation: Language-Agnostic Fact Population

- [x] [Phase 1: Tree-Sitter Import Extraction](./phase-1-treesitter-import-extraction.md)
- [x] [Phase 2: Language-Aware Fact Graph Building](./phase-2-language-aware-fact-graph.md)
- [x] [Phase 3: Language-Parameterised Helpers](./phase-3-language-parameterised-helpers.md)

### Core Analysis

- [x] [Phase 4: Intraprocedural Trace for TS & Rust](./phase-4-intraprocedural-trace.md)
- [x] [Phase 5: Cross-Language Dead Code Detection](./phase-5-cross-language-dead-code.md)
- [ ] [Phase 6: Cross-Language Lint & Flow Rules](./phase-6-cross-language-lint-flow.md)

### Project-Level Features

- [ ] [Phase 7: Cross-Language References, Callers, Callees, Graph](./phase-7-cross-language-refs-callers-graph.md)
- [ ] [Phase 8: Cross-Language Impact Analysis](./phase-8-cross-language-impact.md)

### Advanced Analysis

- [ ] [Phase 9: Interprocedural Trace for TS & Rust](./phase-9-interprocedural-trace.md)
- [ ] [Phase 10: Language-Specific Trace Presets](./phase-10-language-trace-presets.md)
- [ ] [Phase 11: Cross-Language Type Oracle](./phase-11-cross-language-type-oracle.md)

## Dependency Graph

```
Phase 1 ──┐
           ├── Phase 2 ──┬── Phase 4 ──┬── Phase 6
Phase 3 ──┘              │             │
                          ├── Phase 5   ├── Phase 9 ── Phase 10
                          │             │
                          └── Phase 7 ── Phase 8
                                                  Phase 11 (independent)
```

## Design Principles

1. **Config-driven, not hardcoded.** Language-specific behaviour belongs in
   `languages/<lang>/config.toml`, not in Python `if` chains or frozensets.
2. **Same Datalog rules, different facts.** The Datalog trace/flow/dead-code
   rules are language-agnostic; only fact population changes per language.
3. **Incremental testing.** Each phase adds tests for TS and Rust that mirror
   existing Python test coverage for the same feature.
4. **No cross-language analysis yet.** Tracing taint from a Python caller into
   a TypeScript callee (or vice versa) is out of scope. Each language is
   analysed independently using the same engine.
