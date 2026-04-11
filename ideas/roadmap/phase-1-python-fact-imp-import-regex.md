# Phase 1: Python `fact_imp` Import Regex

## Problem

`transform.py` uses a hand-rolled regex to extract Python import relationships
for the `fact_imp` SQL table.  The same regex appears twice:

- **`transform.py:677-680`** — inside `_process_file_for_facts()`, triggered on
  every `ext == "py"` file during fact population.
- **`transform.py:1471-1480`** — inside `_index_batch()` (the subprocess worker
  for parallel indexing), with the comment *"Use a lightweight regex-based
  import extraction (avoids importing the Rust module in subprocesses)"*.

```python
import_re = re.compile(
    r'^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))',
    re.MULTILINE,
)
```

The regex is fragile: it misses multi-line `from x import (a, b)` statements,
misses `import x.y.z as z`, and incorrectly matches import-like strings inside
f-strings or comments.

## Goal

Replace both occurrences with `PyScopeResolver.imports_in_file()` (or
`collect_structured_imports_from_source()` where a file path is unavailable),
which is already used elsewhere for the `imports` table and works correctly for
all Python import forms.

## Affected Files

- `src/emend/transform.py` — two sites: `_process_file_for_facts()` and
  `_index_batch()`

## Implementation Notes

### `_process_file_for_facts()` (line ~677)

The `fact_imp` rows are a subset of the `imports` rows — they only need
`(file, module)` pairs (no alias or `imported_name`).  The `imports` table is
already populated a few lines later via `_extract_imports()`, so the `fact_imp`
rows can be derived from that result rather than running a separate pass:

```python
# Replace the regex block entirely:
for imp in result["imports"]:
    importing_file, imported_module, *_ = imp
    if ext == "py":
        result["fact_imp"].append([importing_file, imported_module])
```

Verify that `result["imports"]` is populated before `fact_imp` at the call
site.  If ordering is a problem, collect `fact_imp` in a second pass after
`_extract_imports` runs.

### `_index_batch()` (line ~1471)

The comment says this avoids importing the Rust module in subprocesses, but
`PyScopeResolver` is already used in the same `_index_batch` function for
references (line ~1484).  The comment is stale.

Replace with:

```python
if need_import:
    try:
        resolver = scope_resolver  # already constructed above
        for local_name, module_path, _imported_name, _is_star in \
                resolver.imports_in_file(py_file):
            if module_path:
                import_rows.append((content_hash, py_file, module_path))
    except Exception:
        pass
```

Or use `_extract_imports(py_file, content)` (already imported in the module)
which dispatches by language and returns `ImportFact` objects:

```python
from emend.fact_graph import _extract_imports
for imp in _extract_imports(py_file, content):
    import_rows.append((content_hash, py_file, imp.imported_module))
```

## Tests

- `test_fact_graph.py` — existing import-related fact tests should pass
  unchanged.
- `test_incremental_facts.py` — `fact_imp` updates tested here; no regression
  expected.
- Add a test case with a multi-line import `from foo import (\n    bar,\n    baz\n)`
  and verify both `bar` and `baz` appear in `fact_imp`.  The old regex would
  miss these entirely.

## Acceptance Criteria

- [ ] `import_re` regex removed from `_process_file_for_facts()`.
- [ ] `import_re` regex removed from `_index_batch()`.
- [ ] `fact_imp` rows still populated correctly for all Python import forms.
- [ ] All existing tests pass.
- [ ] New multi-line import test added and passing.
