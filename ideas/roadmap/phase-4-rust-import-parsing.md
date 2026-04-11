# Phase 4: Rust Module/Import Parsing in `fact_graph.py`

## Problem

`fact_graph.py:3855-3974` implements Rust import extraction with:

```python
_RUST_MOD_RE = re.compile(
    r'^\s*(?:pub\s*(?:\([^)]*\)\s*)?)?\bmod\s+(\w+)\s*;', re.MULTILINE
)
_RUST_USE_START_RE = re.compile(r'\buse\b', re.MULTILINE)
```

Plus the supporting helpers `_rust_find_use_body()`, `_rust_matching_brace()`,
`_rust_split_top_level()`, and `_parse_rust_use_tree()` (~120 lines total).

These implement a hand-rolled parser for Rust's `use` tree syntax
(`use std::{io, fmt::{self, Display}}`), which is notoriously complex:
nested braces, `self` references, `*` glob imports, `pub(crate)` visibility
modifiers, re-exports via `pub use`.

Known gaps:
- `use X as Y` aliasing in nested trees
- Attribute macros on `use` items (`#[cfg(...)] use ...`)
- `extern crate` declarations
- Inline `mod { ... }` blocks vs. external `mod name;` declarations

## Goal

Replace the Rust-specific regex + manual brace-matching parser with
`PyScopeResolver.imports_in_file()` for `.rs` files, which uses the
tree-sitter Rust grammar to correctly parse all use-tree forms.

## Affected Files

- `src/emend/fact_graph.py` — `_RUST_MOD_RE`, `_RUST_USE_START_RE`,
  `_rust_find_use_body()`, `_rust_matching_brace()`, `_rust_split_top_level()`,
  `_parse_rust_use_tree()`, and `_extract_rust_imports()` (the function that
  calls all of the above)

## Implementation Notes

### Existing Rust API

`PyScopeResolver` for `.rs` files already extracts imports via the tree-sitter
Rust grammar (the Rust scope resolver handles `use` and `mod` statements).

Replace `_extract_rust_imports()` with:

```python
def _extract_rust_imports(file_path: str, content: str) -> list[ImportFact]:
    from emend import emend_core
    resolver = emend_core.PyScopeResolver(".", "rs")
    resolver.index_file(file_path, content)
    facts = []
    try:
        for local_name, module_path, imported_name, is_star in \
                resolver.imports_in_file(file_path):
            facts.append(ImportFact(
                importing_file=file_path,
                imported_module=module_path,
                imported_name="*" if is_star else (imported_name or None),
                alias=local_name if local_name != imported_name else None,
                line=0,
            ))
    except Exception:
        pass
    return facts
```

### `mod` declarations

`mod name;` declares a sub-module; it is not an import in the traditional sense
but creates a module-level reference.  Verify that `PyScopeResolver` returns
these as imports (with `module_path = "name"`) or as symbol definitions.  If
not returned as imports, add a tree-sitter pattern match:

```python
matches = find_pattern("mod $NAME;", file_path, source_override=content, language="rust")
for m in matches:
    facts.append(ImportFact(
        importing_file=file_path,
        imported_module=m.captures["NAME"],
        imported_name=None,
        alias=None,
        line=m.line,
    ))
```

### Capability gap

If `PyScopeResolver.imports_in_file()` for Rust does not yet return correct
results (check `test_fact_graph.py`'s Rust import tests), the Rust extension
may need to be updated.  Do **not** fall back to the regex approach; instead
extend `emend_core` to handle the missing cases.

## Tests

- `test_fact_graph.py` — Rust import-related tests must pass.
- Add test cases:
  - `use std::{io, fmt::{self, Display}}` — nested use tree
  - `pub use crate::foo::Bar` — re-export
  - `use super::baz as b` — aliased relative import
  - `mod sub_module;` — external module declaration

## Acceptance Criteria

- [ ] `_RUST_MOD_RE` and `_RUST_USE_START_RE` removed.
- [ ] `_rust_find_use_body()`, `_rust_matching_brace()`, `_rust_split_top_level()`,
      `_parse_rust_use_tree()` removed.
- [ ] Rust imports resolved via `PyScopeResolver`.
- [ ] All existing Rust import tests pass.
- [ ] New edge-case use-tree tests added and passing.
