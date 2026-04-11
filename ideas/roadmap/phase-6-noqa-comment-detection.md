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
   per-language-prefix patterns used for TypeScript and Rust, where the
   comment prefix (`#`, `//`) is hardcoded in Python:
   ```python
   self._noqa_tagged_pat = re.compile(escaped + r"\s*noqa:\s*(\S+)")
   self._noqa_bare_pat   = re.compile(escaped + r"\s*noqa\s*$")
   ```

The core regex logic (`noqa:\s*(\S+)`) is fine and intentional — the noqa
format is a stable convention, not source code structure.  The real issues are:

- **The comment prefix is hardcoded in Python** (`#`, `//`) rather than read
  from `config.toml`.  Adding a new language requires changing Python code.
- **The same `_NOQA_RE` is duplicated** in `transform.py` and `python_plugin.py`.
- **`RegexCommentHandler.__init__`** takes a `prefix` argument wired up
  in the plugin registry — the prefix should come from `config.toml` instead.

The false-positive risk (matching `"# noqa"` inside a string literal) is low
priority — in practice no one writes noqa strings as string content in test
files, and tree-sitter comment node extraction would only help if `emend_core`
exposes comment text positions, which it may not.

## Goal

1. Move the comment prefix (and optionally the full noqa pattern) into each
   language's `config.toml` so no Python code needs to know about it.
2. Consolidate the two copies of `_NOQA_RE` into a single location.
3. Keep the regex logic itself — it is appropriate for matching within already-
   identified comment text.

## Affected Files

- `src/emend/transform.py` — `_NOQA_RE` (consolidate, don't delete)
- `src/emend/python_plugin.py` — `_NOQA_RE` (remove duplicate, import from
  one canonical location)
- `src/emend/language_plugins.py` — `RegexCommentHandler` (read prefix from
  config rather than constructor arg)
- `languages/python/config.toml` — add `[comments]` section
- `languages/typescript/config.toml` — add `[comments]` section
- `languages/rust/config.toml` — add `[comments]` section

## Implementation Notes

### `config.toml` changes

Add to each language config:

```toml
# languages/python/config.toml
[comments]
line_prefix = "#"

# languages/typescript/config.toml and languages/rust/config.toml
[comments]
line_prefix = "//"
```

The `noqa` pattern itself (`noqa\b(?:\s*:\s*(.*))?`) is language-agnostic and
stays in Python.  Only the comment delimiter is language-specific data.

### `RegexCommentHandler` change

Remove the `prefix` constructor parameter.  Instead, read it from the language
config:

```python
class RegexCommentHandler(CommentHandler):
    def __init__(self, language: str) -> None:
        config = load_config(language)
        prefix = config.get("comments", {}).get("line_prefix", "#")
        escaped = re.escape(prefix)
        self._noqa_tagged_pat = re.compile(escaped + r"\s*noqa:\s*(\S+)")
        self._noqa_bare_pat   = re.compile(escaped + r"\s*noqa\s*$")
```

### Canonical `_NOQA_RE`

Keep one copy in `transform.py` (or move to a shared `_noqa.py` helper).
Delete the copy in `python_plugin.py` and import from the canonical location.

### Optional: tree-sitter comment nodes

If `emend_core` exposes comment node text and positions (node type `comment`
in Python, `line_comment` in Rust), the scan can be restricted to actual
comment nodes, eliminating any false-positive risk.  This is a nice-to-have
improvement and can be added incrementally without changing the regex logic.

## Tests

- All existing noqa / dead-code tests in `test_dead_code.py` must pass.
- Confirm that a new language added with only `config.toml` (no Python code
  change) correctly picks up its comment prefix for noqa detection.

## Acceptance Criteria

- [x] `_NOQA_RE` has a single canonical definition (not duplicated).
- [x] Comment prefix (`#`, `//`) read from `config.toml` in each language.
- [x] `RegexCommentHandler` no longer takes a hardcoded prefix argument.
- [x] All existing noqa/dead-code tests pass.
