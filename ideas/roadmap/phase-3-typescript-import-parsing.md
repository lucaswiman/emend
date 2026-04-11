# Phase 3: TypeScript Import Parsing in `fact_graph.py`

## Problem

`fact_graph.py:3660-3747` implements TypeScript/JavaScript import extraction
with five hand-rolled regex patterns plus manual string-splitting logic:

```python
_TS_IMPORT_FROM_RE      # import ... from "module"
_TS_SIDE_EFFECT_RE      # import "module"
_TS_REQUIRE_RE          # const X = require("module")
_TS_EXPORT_FROM_RE      # export { X } from "module"
_TS_FULL_IMPORT_RE      # fallback multiline import
```

Together with `_ts_parse_import_clause()` (line 3696), these constitute ~100
lines of fragile regex + string manipulation that needs to handle TypeScript's
complex import syntax (default, named, namespace, dynamic, `import type`, etc.).
Known gaps include:
- `import()` dynamic imports
- `/// <reference>` directives
- Multi-line `import { a,\n  b }` where the brace spans lines
- CommonJS `module.exports` and `exports.X` patterns

## Goal

Replace all TypeScript/JavaScript import extraction in `fact_graph.py` with
`PyScopeResolver.imports_in_file()` (Rust-backed, tree-sitter based), which
already handles all the above forms correctly.

## Affected Files

- `src/emend/fact_graph.py` — `_TS_IMPORT_FROM_RE`, `_TS_SIDE_EFFECT_RE`,
  `_TS_REQUIRE_RE`, `_TS_EXPORT_FROM_RE`, `_TS_FULL_IMPORT_RE`,
  `_ts_parse_import_clause()`, and `_extract_ts_imports()` (search for the
  function that calls these patterns)

## Implementation Notes

### Existing Rust API

`PyScopeResolver.imports_in_file(path)` returns a list of
`(local_name, module_path, imported_name, is_star)` tuples.  It already works
for TypeScript (the scope resolver supports `.ts`, `.tsx`, `.js`, `.jsx`).

The `_extract_imports()` dispatcher in `fact_graph.py` already calls a
TypeScript-specific helper — that helper is what contains the regex patterns.
Replace the body of the TS branch in `_extract_imports()` with:

```python
from emend import emend_core

def _extract_ts_imports(file_path: str, content: str) -> list[ImportFact]:
    ext = Path(file_path).suffix.lstrip(".")
    resolver = emend_core.PyScopeResolver(".", ext)
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
                line=0,  # scope resolver doesn't return line numbers here
            ))
    except Exception:
        pass
    return facts
```

If line numbers are needed, check whether `structured_imports_in_file()` returns
them (it may return richer data).

### Preserving `_ts_parse_import_clause()`

`_ts_parse_import_clause()` is only called from the regex-based path; once
the regex patterns are gone it can be deleted.

### `_TS_FULL_IMPORT_RE` guard in `_normalize_qn()` (line ~3494)

Check whether `_TS_FULL_IMPORT_RE` or other TS regexes are referenced outside
of the import extraction path.  If so, migrate those uses separately.

## Tests

- `test_fact_graph.py` — existing TS import fact tests should all pass.
- Add new test cases for:
  - `import type { Foo } from "./foo"` — type-only import
  - `export { X } from "./bar"` — re-export
  - `const { a, b } = require("./c")` — CommonJS destructured require
  - Multi-line `import {\n  foo,\n  bar\n} from "baz"` — line-spanning import

## Acceptance Criteria

- [ ] All five regex constants removed from `fact_graph.py`.
- [ ] `_ts_parse_import_clause()` removed.
- [ ] TypeScript imports resolved via `PyScopeResolver`.
- [ ] All existing TS import tests pass.
- [ ] New edge-case tests added and passing.
