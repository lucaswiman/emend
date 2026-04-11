# Phase 7: Python Plugin `ast.parse` Migration

## Problem

`python_plugin.py` uses Python's standard `ast` module in several places:

1. **`PythonImportHandler.extract_imports()`** (line ~28) — calls `ast.parse(source)`
   to find `ast.Import` and `ast.ImportFrom` nodes.
2. **`PythonImportHandler.add_import_text()`** (line ~50) — calls `ast.parse(source_code)`
   to find existing import positions.
3. **`PythonImportHandler.remove_import()`** (line ~100, if present) — may also use `ast`.

CLAUDE.md explicitly states: *"Do not use Python's `ast` module…when a
tree-sitter-based solution exists or can be built."*

The design principle is that language analysis code should be language-agnostic
and driven by the Rust tree-sitter extension, so that the same code paths work
for Python, TypeScript, and Rust.  Python-specific `ast.parse()` calls
undermine this goal and create a maintenance split.

## Goal

Replace `ast.parse()` calls in `python_plugin.py` with `PyScopeResolver`-based
or `find_pattern`-based equivalents.

## Affected Files

- `src/emend/python_plugin.py` — `PythonImportHandler`
- `src/emend/language_plugins.py` — `GenericImportHandler.extract_imports()` is
  the tree-sitter path; confirm it is equivalent

## Implementation Notes

### `extract_imports()`

`PyScopeResolver.imports_in_file()` already exists and is used by
`GenericImportHandler.extract_imports()` (the TS/Rust path) as Phase 1 of its
implementation (see `language_plugins.py:186-220`).  The Python plugin should
use the same approach:

```python
def extract_imports(self, source: str) -> str:
    from emend import emend_core
    resolver = emend_core.PyScopeResolver(".", "py")
    fake_path = "__temp__.py"
    resolver.index_file(fake_path, source)
    try:
        imports = resolver.imports_in_file(fake_path)
    except Exception:
        return ""
    # imports: list of (local_name, module_path, imported_name, is_star)
    # Reconstruct import lines from source using scope resolver line numbers,
    # OR use find_pattern to locate import nodes and extract their source text
    ...
```

The tricky part is reconstructing the exact import text (to preserve formatting
and multi-line imports).  If `imports_in_file()` returns line numbers, use
those to slice the original source.  If not, use `find_pattern` with patterns
`"import $MODULE"` and `"from $MODULE import $NAMES"` to locate import
statement spans, then slice.

### `add_import_text()`

This needs to find the last existing import line to insert after it.  Currently
it uses `ast.parse()` to find `stmt.lineno`.  Replace with:

```python
matches = find_pattern("import $X", file_path, source_override=source, language="python")
matches += find_pattern("from $X import $Y", file_path, source_override=source, language="python")
last_import_line = max((m.line for m in matches), default=0)
```

### Deprecating `PythonImportHandler` entirely

Once `GenericImportHandler` (the tree-sitter path) handles Python correctly,
`PythonImportHandler` may be unnecessary.  Confirm that `load_plugin("python")`
returns a plugin with a `GenericImportHandler` (or equivalent) and delete
`PythonImportHandler` if so.  Check `src/emend/python_plugin.py` and the
plugin registry in `language_plugins.py` to see how plugins are registered.

### `ast.tokenize` usage

`python_plugin.py` also imports `tokenize` — check whether it is used for
anything that can be replaced with tree-sitter.

## Tests

- Existing `test_copy_to.py`, `test_primitives_copy_imports.py` exercise import
  extraction and insertion — all must pass.
- Add a test with a Python source that has syntax errors; verify graceful
  degradation (the old path raised `SyntaxError`, the new path should return
  empty without crashing).

## Acceptance Criteria

- [x] `import ast` removed from `python_plugin.py`.
- [x] `PythonImportHandler.extract_imports()` uses `PyScopeResolver` or `find_pattern`.
- [x] `PythonImportHandler.add_import_text()` uses tree-sitter line numbers.
- [x] All import-related tests pass (copy, add, primitives).
- [x] Syntax-error graceful-degradation test added.
