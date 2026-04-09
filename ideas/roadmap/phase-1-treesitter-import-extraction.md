# Phase 1: Tree-Sitter Import Extraction

## Goal

Replace the Python-only `_extract_imports()` in `fact_graph.py` with a
tree-sitter-based implementation that works for Python, TypeScript, and Rust.

## Why

`_extract_imports()` currently uses `stdlib ast.parse()`, which only handles
Python.  Every downstream feature that depends on `ImportFact` — dead code
detection, module-level reference resolution, unused import detection — is
blocked for non-Python languages.

The tree-sitter grammars for all three languages are already loaded by
`emend_core`, and the language config files already define import node types
(`imports` section).  The information is there; it just isn't being used for
import extraction.

## Scope

- `src/emend/fact_graph.py` — `_extract_imports()` function
- `languages/python/config.toml` — `[imports]` section (already complete)
- `languages/typescript/config.toml` — `[imports]` section (needs enrichment)
- `languages/rust/config.toml` — `[imports]` section (needs enrichment)
- Possibly `rust/src/scope.rs` if extraction is better done in Rust

## Current State

```python
# fact_graph.py:3324
def _extract_imports(file_path: str, content: str) -> list[ImportFact]:
    import ast as stdlib_ast
    tree = stdlib_ast.parse(content, filename=file_path)
    # walks ast.Import and ast.ImportFrom nodes
```

## Todo

### Python import extraction via tree-sitter

- [ ] Implement tree-sitter-based import extraction for Python that produces
  identical `ImportFact` output to the current `ast.parse()` implementation.
- [ ] Add a differential test comparing old (ast) and new (tree-sitter) results
  on a corpus of Python files.
- [ ] Replace `_extract_imports()` with the tree-sitter version.

### TypeScript import extraction

- [ ] Handle `import { X } from "module"` → `ImportFact(imported_module="module", imported_name="X")`.
- [ ] Handle `import X from "module"` (default import).
- [ ] Handle `import * as X from "module"` (namespace import).
- [ ] Handle `const X = require("module")` (CommonJS).
- [ ] Handle `import type { X } from "module"` (type-only imports).
- [ ] Handle re-exports: `export { X } from "module"`.
- [ ] Enrich `[imports]` in `languages/typescript/config.toml` with fields
  needed for tree-sitter extraction (named_imports, default_import, etc.).
- [ ] Add tests covering all TypeScript/JS import forms.

### Rust import extraction

- [ ] Handle `use std::collections::HashMap` → `ImportFact(imported_module="std::collections", imported_name="HashMap")`.
- [ ] Handle `use crate::module::Symbol` (crate-relative).
- [ ] Handle `use super::Symbol` (parent-relative).
- [ ] Handle `use module::*` (glob imports).
- [ ] Handle `use module::Symbol as Alias`.
- [ ] Handle multi-path `use std::{io, fs}` (use_list / scoped_use_list).
- [ ] Handle `mod declarations` as implicit imports.
- [ ] Enrich `[imports]` in `languages/rust/config.toml`.
- [ ] Add tests covering all Rust import forms.

### Integration

- [ ] Make `_extract_imports()` dispatch by file extension (detect language from
  path, call appropriate tree-sitter extraction).
- [ ] Verify `FactGraph.build_from_project()` and `update_files()` correctly
  populate `ImportFact` for `.ts`/`.tsx`/`.js`/`.jsx`/`.rs` files.

## Exit Criteria

- `_extract_imports("foo.py", src)` produces the same `ImportFact` list as before.
- `_extract_imports("foo.ts", src)` produces correct `ImportFact` for all
  TypeScript/JS import forms.
- `_extract_imports("foo.rs", src)` produces correct `ImportFact` for all Rust
  import/use forms.
- No stdlib `ast` dependency remains in `_extract_imports()`.
- All existing Python tests still pass.
