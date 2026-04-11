# Phase 5: Config-Driven Export Detection

## Problem

`language_registry.py:285-333` implements `detect_exported_names()` using
regex patterns stored in the language's `config.toml`:

```toml
# languages/typescript/config.toml
[exports]
export_patterns = [
    '^\s*export\s+(?:default\s+)?(?:function\*?\s+|class\s+|...)(\w+)',
]
named_export_pattern = '^\s*export\s*\{([^}]+)\}'
```

These TOML-stored regexes are then compiled at runtime and applied to raw
source text.  While the patterns are at least stored as data (not hardcoded
in Python), the mechanism is still regex-over-source.  Problems:
- `export_patterns` can miss `export default` anonymous classes/functions
- `named_export_pattern` uses simplistic `[^}]+` that breaks on nested objects
- The patterns need to be maintained separately from the tree-sitter grammar
- Adding a new language requires writing correct regexes, not just specifying
  a grammar node type

## Goal

Replace regex-based export detection with tree-sitter node queries.  The
config should specify the tree-sitter node type for export statements (already
present in `config.toml` as `export_statement = "export_statement"`), and
the Python code should walk that node type to extract exported names.

## Affected Files

- `src/emend/language_registry.py` — `_get_export_patterns()`,
  `_export_pattern_cache`, `detect_exported_names()`
- `languages/typescript/config.toml` — `export_patterns`, `named_export_pattern`
  (to be removed)
- `languages/rust/config.toml` — similar patterns if present

## Implementation Notes

### New approach

`detect_exported_names(content, language)` should use `collect_symbols_from_str`
or `PyScopeResolver` to get symbols, then filter by visibility:

```python
def detect_exported_names(content: str, language: str) -> set[str]:
    if language == "python":
        return set()  # handled separately via __all__
    config = load_config(language)
    exports_cfg = config.get("exports", {})
    if exports_cfg.get("public_by_default", False):
        # All non-private symbols are exported (Rust pub rules handled by
        # the scope resolver; for "public by default" languages return everything
        # non-prefixed-with-underscore)
        syms = emend_core.collect_symbols_from_str(content, ext=_lang_to_ext(language))
        private_prefix = exports_cfg.get("private_prefix", "_")
        return {s["name"] for s in syms if not s["name"].startswith(private_prefix)}

    # For TypeScript/JS: walk explicit `export` statements
    # Use find_pattern to match export declarations
    from emend.transform import find_pattern
    export_node_type = exports_cfg.get("export_statement", "export_statement")
    # Pattern: "export $DECL" covers function/class/const/let/var/interface/enum
    # Pattern: "export { $NAMES }" covers named re-exports
    exported: set[str] = set()
    for pattern in ["export $DECL", "export { $NAMES }", "export default $DECL"]:
        # Use a temp file path for find_pattern
        ...
    return exported
```

The exact implementation depends on what `find_pattern` / the tree-sitter
query API can express.  The key requirement is: **no regex patterns in
`config.toml`**.  The `export_patterns` and `named_export_pattern` keys should
be removed from all `config.toml` files.

### Config changes

Remove from `languages/typescript/config.toml`:
```toml
export_patterns = [...]      # DELETE
named_export_pattern = '...' # DELETE
```

The `export_statement = "export_statement"` key (a tree-sitter node type) can
stay or be repurposed to guide the tree-sitter query.

### Rust exports

Rust uses `pub` visibility rather than `export`.  The scope resolver already
tracks visibility.  `detect_exported_names` for Rust should use
`collect_symbols_from_str` and filter for symbols with `pub` visibility.
Check if the symbol metadata returned by `collect_symbols_from_str` includes
a `visibility` or `is_public` field; if not, extend `emend_core`.

## Tests

- `test_fact_graph.py` — exported QN tests
- Add tests in `tests/test_emend/test_language_registry.py` (new file if
  needed):
  - `export function foo()` — simple export
  - `export { foo, bar }` — named exports block
  - `export { foo as f }` — aliased export
  - `export default class {}` — anonymous default export
  - Non-exported `function internal()` — must not appear in result

## Acceptance Criteria

- [x] `_get_export_patterns()` removed.
- [x] `_export_pattern_cache` removed.
- [x] `export_patterns` and `named_export_pattern` keys removed from all `config.toml` files.
- [x] `detect_exported_names()` uses tree-sitter node types / `find_pattern`.
- [x] All existing export detection tests pass.
- [x] New edge-case export tests added and passing.
