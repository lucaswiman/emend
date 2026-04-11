# Regex Migration Roadmap

Eliminate source-code-parsing regexes from the Python codebase and replace them
with tree-sitter-based analysis, in accordance with the design philosophy in
CLAUDE.md: *"Regexes in analysis code are a big code smell"*.

## Motivation

The codebase contains several clusters of `re.compile` / `re.finditer` calls
that parse source code structure (imports, exports, `__all__`, comments, noqa
suppressions).  These are fragile — they break on multi-line statements, string
literals, comments, and other edge cases that a real parser handles
transparently.  The Rust `emend_core` extension already exposes the right
primitives (`PyScopeResolver`, `find_pattern`, `collect_symbols_from_str`) and
language differences should be expressed as `config.toml` data, not Python
code.

## Inventory of Violations

| Phase | Location | What the regex does |
|-------|----------|---------------------|
| 1 | `transform.py:677-680, 1471-1480` | Python import extraction (`fact_imp`) |
| 2 | `transform.py:1253-1265` | Python `__all__` list extraction |
| 3 | `fact_graph.py:3660-3692` | TypeScript import parsing (5 patterns) |
| 4 | `fact_graph.py:3858-3974` | Rust `mod` / `use` parsing |
| 5 | `language_registry.py:302-332` | Config-driven export detection (TS/Rust) |
| 6 | `transform.py:1268`, `language_plugins.py:305-342`, `python_plugin.py:22` | noqa comment prefix hardcoded in Python (regex itself is fine; prefix belongs in `config.toml`) |
| 7 | `python_plugin.py:9-41` | Python AST (`ast.parse`) for import extraction |
| 8 | `language_plugins.py:153-169, 285-302` | Import line detection / removal |
| 9 | `dsl.py:95-155, 306-365` | DSL region detection (SQL/Jinja2/GraphQL keywords) |

## Phases

### High Priority (Source Code Parsing Regressions Most Likely)

- [ ] [Phase 1: Python `fact_imp` Import Regex](./phase-1-python-fact-imp-import-regex.md)
- [ ] [Phase 2: Python `__all__` Export Detection](./phase-2-python-all-export-detection.md)
- [x] [Phase 3: TypeScript Import Parsing](./phase-3-typescript-import-parsing.md)
- [x] [Phase 4: Rust Module/Import Parsing](./phase-4-rust-import-parsing.md)

### Medium Priority (Structural Improvements)

- [x] [Phase 5: Config-Driven Export Detection](./phase-5-config-driven-export-detection.md)
- [x] [Phase 6: noqa Comment Detection](./phase-6-noqa-comment-detection.md)
- [x] [Phase 7: Python Plugin `ast.parse` Migration](./phase-7-python-plugin-ast-migration.md)
- [ ] [Phase 8: `language_plugins.py` Import Handling](./phase-8-language-plugins-import-handling.md)

### Lower Priority (Complex / Needs Rust Extension Work)

- [x] [Phase 9: DSL Region Detection](./phase-9-dsl-region-detection.md)

## Design Principles

1. **Use `PyScopeResolver` for import/export extraction.** It already works for
   Python, TypeScript, and Rust.  Functions `imports_in_file()` and
   `structured_imports_in_file()` return structured data; no regex needed.
2. **Use `find_pattern` with `$METAVAR` captures for structural matching.**
   Patterns like `__all__ = [$NAMES]` or `export { $NAMES }` are parsed by
   tree-sitter, not regex.
3. **Encode language differences in `config.toml`, not Python `if` chains.**
   Export visibility rules, import keywords, comment prefixes all belong in
   `languages/<lang>/config.toml`.
4. **Tree-sitter comment nodes for noqa detection.**  Comment nodes are first-
   class tree-sitter nodes — traverse them rather than scanning raw text.
5. **Never add a new regex for source code structure.**  If the Rust extension
   lacks a needed capability, extend it.
