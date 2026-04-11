# Phase 6: noqa Comment Detection

## Problem

Noqa suppression comments (`# noqa: emend:deadcode`, `// noqa: tag`) are
detected by raw-text regex scanning in three places:

1. **`transform.py:1268`** — module-level `_NOQA_RE` for Python:
   ```python
   _NOQA_RE = re.compile(r'(?:#|//)\s*noqa\b(?:\s*:\s*(.*))?', re.IGNORECASE)
   ```
   Used by `_extract_noqa_lines(source)` which scans line-by-line.

2. **`python_plugin.py:22`** — duplicate of the above for Python-only use:
   ```python
   _NOQA_RE = re.compile(r'#\s*noqa\b(?:\s*:\s*(.*))?', re.IGNORECASE)
   ```

3. **`language_plugins.py:305-342`** — `RegexCommentHandler` with
   per-language-prefix patterns, used for TypeScript and Rust:
   ```python
   self._noqa_tagged_pat = re.compile(escaped + r"\s*noqa:\s*(\S+)")
   self._noqa_bare_pat   = re.compile(escaped + r"\s*noqa\s*$")
   ```

All three scan raw source text line-by-line.  They will mis-fire on noqa-like
strings inside string literals (e.g. a test asserting `"# noqa"` is in source).

## Goal

Replace regex-based noqa scanning with tree-sitter comment node traversal.
Tree-sitter parses comments as first-class nodes; walking them avoids matching
inside strings.

## Affected Files

- `src/emend/transform.py` — `_NOQA_RE`, `_extract_noqa_lines()`
- `src/emend/python_plugin.py` — `_NOQA_RE`
- `src/emend/language_plugins.py` — `RegexCommentHandler`

## Implementation Notes

### New API in `emend_core`

Add a Rust-side function (or extend `PyScopeResolver`) to extract noqa
comments from a source string by language:

```rust
// emend_core: new function
pub fn collect_noqa_lines(source: &str, ext: &str) -> Vec<(u32, Option<String>)>;
// Returns: list of (line_number, Option<comma-separated tags>)
// None = bare noqa (suppress all)
// Some("deadcode,unused") = tagged noqa
```

This walks tree-sitter's comment nodes (node type `"comment"` in Python,
`"line_comment"` in Rust, `"comment"` in TypeScript) and checks the comment
text for the noqa pattern.  The key point is that it only checks actual
comment nodes, never string literals or other tokens.

### Python side

Replace `_extract_noqa_lines()` with:

```python
def _extract_noqa_lines(source: str, ext: str = "py") -> set[int]:
    from emend import emend_core
    result: set[int] = set()
    for line_no, tags in emend_core.collect_noqa_lines(source, ext):
        if tags is None or "deadcode" in tags.split(","):
            result.add(line_no)
    return result
```

The `CommentHandler.find_noqa_comments()` interface in `language_plugins.py`
should also delegate to `emend_core.collect_noqa_lines()`:

```python
class TreeSitterCommentHandler(CommentHandler):
    def __init__(self, ext: str) -> None:
        self._ext = ext

    def find_noqa_comments(self, source: str) -> dict[int, set[str] | None]:
        from emend import emend_core
        result: dict[int, set[str] | None] = {}
        for line_no, tags in emend_core.collect_noqa_lines(source, self._ext):
            if tags is None:
                result[line_no] = None
            else:
                result[line_no] = {t.strip() for t in tags.split(",") if t.strip()}
        return result
```

### Fallback

If `emend_core.collect_noqa_lines()` cannot be added immediately, the
existing regex approach can be made safer by checking that the regex match
position is within a comment token.  However, extending `emend_core` is
strongly preferred.

### `RegexCommentHandler` retirement

Once `TreeSitterCommentHandler` covers all languages, `RegexCommentHandler`
can be removed.  `DocCommentHandler` (which extends it) should also be
migrated.

## Tests

- Add a test with a source file containing `"# noqa: deadcode"` inside a
  string literal — verify it does NOT suppress the warning.
- Existing dead-code noqa tests in `test_dead_code.py` must continue to pass.
- Add tests for TypeScript and Rust noqa comments.

## Acceptance Criteria

- [ ] `_NOQA_RE` removed from `transform.py`.
- [ ] `_NOQA_RE` removed from `python_plugin.py`.
- [ ] `RegexCommentHandler` replaced by `TreeSitterCommentHandler` (or
      delegating to `emend_core`).
- [ ] noqa comments inside string literals do not suppress warnings.
- [ ] All existing noqa/dead-code tests pass.
- [ ] New string-literal false-positive test added and passing.
