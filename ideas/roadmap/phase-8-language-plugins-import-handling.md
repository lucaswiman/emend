# Phase 8: `language_plugins.py` Import Line Detection

## Problem

`language_plugins.py` uses regex in two methods of `GenericImportHandler`:

### `_is_import_line()` (line ~153)

```python
if stripped.startswith("pub ") and re.search(r'\b' + re.escape(kw) + r'\b', stripped):
    return True
if stripped.startswith("export ") and re.search(r'\b' + re.escape(kw) + r'\b', stripped):
    return True
```

This is a fallback heuristic for when the scope resolver doesn't return
imports.  It detects Rust `pub use` and TypeScript `export ... from` by
keyword scanning.

### `remove_import()` (line ~285)

```python
mod_pat = re.compile(r'\b' + re.escape(module) + r'\b')
name_pat = re.compile(r'\b' + re.escape(name) + r'\b')
for line in lines:
    if self._is_import_line(stripped) and mod_pat.search(...) and name_pat.search(...):
        continue  # drop the line
```

This removes an import by pattern-matching lines.  It will fail on:
- Multi-line imports spanning multiple lines
- Imports where `name` appears elsewhere on the same line in a comment
- Rust tree-style imports `use std::{mod::name, other::name}` where removing
  one name requires re-writing the tree

### `DocCommentHandler` regex (line ~359)

```python
_JSDOC_RE = re.compile(r'/\*\*.*?\*/', re.DOTALL)
_RUST_DOC_LINE_RE = re.compile(r'^[ \t]*(?:///|//!)', re.MULTILINE)
```

Used by `find_docstrings()` to locate doc comment regions for rename
operations.

## Goal

- **`_is_import_line()`** — eliminate as a method.  The entire fallback path
  in `extract_imports()` uses `_is_import_line()` when `PyScopeResolver`
  returns nothing.  Fix the scope resolver (or its invocation) rather than
  keeping the keyword fallback.
- **`remove_import()`** — replace with a tree-sitter-based import removal that
  locates the import node by module+name and removes the exact byte range.
- **`DocCommentHandler`** — replace regex-based doc comment detection with
  tree-sitter comment node traversal.

## Affected Files

- `src/emend/language_plugins.py` — `_is_import_line()`, `remove_import()`,
  `RegexCommentHandler`, `DocCommentHandler`

## Implementation Notes

### Eliminating `_is_import_line()` fallback

The Phase 1 path in `extract_imports()` calls `PyScopeResolver` and uses its
results to identify import lines.  The Phase 2 fallback re-scans lines using
`_is_import_line()`.  The fallback should only be needed if the scope resolver
fails or returns empty results.  Investigate why the scope resolver sometimes
returns empty for TypeScript/Rust — likely the language extension isn't
supported yet or the file extension mapping is wrong.  Fix the root cause.

If a fallback is unavoidable, replace the keyword regex with a tree-sitter
query for import node types (stored in `config.toml` as `import_node`).

### `remove_import()` via tree-sitter

A correct implementation should:
1. Parse the file with tree-sitter to find the import node that imports `name`
   from `module`.
2. Identify the exact byte range of that node (or the specific named import
   within a grouped import).
3. Delete that range (and clean up trailing commas/braces if in a named list).

This requires either extending `emend_core` with a `remove_import_from_source()`
function, or using `PyFileTransform` with a computed edit.

### Doc comment detection

Replace `_JSDOC_RE` and `_RUST_DOC_LINE_RE` with tree-sitter node type
queries.  JSDoc comments are `comment` nodes whose text starts with `/**`.
Rust doc comments are `line_comment` nodes starting with `///` or `//!`.
These node types are first-class in the tree-sitter grammars — no regex needed.

Add to `config.toml`:
```toml
[comments]
doc_comment_prefix = "///"
doc_comment_block_start = "/**"
```

And use `PyScopeResolver` or `collect_symbols_from_str` metadata to find doc
comment ranges when rename operations need to update them.

## Tests

- `test_regressions.py` — `--docs` flag tests exercise doc comment renaming.
- `test_primitives_copy_imports.py` — import extraction tests.
- Add test: remove a named import from `import { A, B } from "mod"` → result
  is `import { A } from "mod"` (not a regex line-drop).

## Acceptance Criteria

- [ ] `_is_import_line()` fallback replaced or eliminated.
- [ ] `remove_import()` uses tree-sitter byte-range editing, not line scanning.
- [ ] `DocCommentHandler` regex patterns removed; uses tree-sitter comment nodes.
- [ ] All rename-with-docs and import tests pass.
- [ ] New named-import removal test added.
