# Phase 2: Python `__all__` Export Detection

## Problem

`transform.py:1253-1265` detects names exported via Python's `__all__` list
using two compiled regexes:

```python
_ALL_RE = re.compile(
    r'^__all__\s*=\s*[\[\(](.*?)[\]\)]',
    re.MULTILINE | re.DOTALL,
)
_ALL_NAME_RE = re.compile(r"""['"](\w+)['"]""")
```

`_extract_all_exports_text(source: str) -> set[str]` applies these to raw
source text.

This approach is fragile:
- Misses `__all__ += [...]` and `__all__.extend([...])`
- Misses `__all__` defined with tuple literals across multiple assignments
- Can match content inside multi-line string literals that look like `__all__`
- The inner `_ALL_NAME_RE` misses names that contain non-`\w` characters
  (unlikely in practice, but still wrong in principle)

## Goal

Replace with a `find_pattern`-based approach that uses tree-sitter to match
the assignment node and extract string literal children.

## Affected Files

- `src/emend/transform.py` — `_ALL_RE`, `_ALL_NAME_RE`, `_extract_all_exports_text()`
- Callers of `_extract_all_exports_text()` in `transform.py` (search for
  the function name to find all call sites)

## Implementation Notes

### Option A: `find_pattern` with captured metavar

Use `find_pattern` to find the assignment, then iterate the captured node's
children:

```python
from emend.transform import find_pattern

def _extract_all_exports_ts(source: str, file_path: str) -> set[str]:
    matches = find_pattern("__all__ = $NAMES", file_path,
                           source_override=source, language="python")
    names: set[str] = set()
    for m in matches:
        # $NAMES will be bound to the list/tuple node text; extract strings
        raw = m.captures.get("NAMES", "")
        for n in re.findall(r"""['"](\w+)['"]""", raw):
            names.add(n)
    return names
```

This is better than pure regex because the pattern match is tree-sitter-based
and respects syntactic boundaries (won't match inside strings or comments).
However it still uses a small regex to pull names out of the already-parsed
`$NAMES` text.  That inner regex is acceptable since it operates on a
structurally extracted sub-tree, not raw source.

### Option B: Extend `emend_core` with a dedicated `__all__` extractor

Add a Rust-side helper `collect_all_exports(source: str, ext: str) -> list[str]`
that walks the tree-sitter parse tree for Python and returns string literal
values that are direct children of an `__all__` assignment's list/tuple.  This
would be fully regex-free.

Option A is acceptable for a first pass.  Option B is the ideal end state.

### Handling `__all__ += [...]` and `__all__.extend(...)`

These patterns are currently ignored.  After the migration, consider also
matching `find_pattern("__all__ += $NAMES", ...)` and
`find_pattern("__all__.extend($NAMES)", ...)`.  This is out of scope for Phase
2 but should be noted as a follow-up.

## Tests

- Add tests in `test_dead_code.py` or a new `test_all_exports.py`:
  - `__all__ = ["foo", "bar"]` — basic case
  - `__all__ = (\n    "foo",\n    "bar",\n)` — multi-line tuple
  - `__all__ = ["foo"]  # inside a comment: __all__ = ["not_exported"]` — ensure
    comment doesn't interfere (old regex would have matched both)
  - File with `__all__` in a docstring — ensure no false positive

## Acceptance Criteria

- [ ] `_ALL_RE` and `_ALL_NAME_RE` removed from module scope of `transform.py`.
- [ ] `_extract_all_exports_text()` replaced with tree-sitter-based equivalent.
- [ ] Multi-line `__all__` tuples handled correctly.
- [ ] `__all__` inside strings/comments does not produce false positives.
- [ ] All existing dead-code and lint tests pass.
