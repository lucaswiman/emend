# Phase 9: DSL Region Detection in `dsl.py`

## Problem

`dsl.py` uses a large number of regexes (~20+) to detect embedded DSL regions
(SQL, Jinja2, GraphQL) inside Python source code:

```python
_SQL_KEYWORD_RE      # SQL keyword heuristic
_MAGIC_COMMENT_RE    # # language = sql
_TRIPLE_DOUBLE_STRING_RE  # """..."""
_TRIPLE_SINGLE_STRING_RE  # '''...'''
_SINGLE_DOUBLE_STRING_RE  # "..."
_SINGLE_SINGLE_STRING_RE  # '...'
_JINJA_EXPR_RE       # {{ ... }}
_JINJA_TAG_RE        # {% ... %}
_JINJA_COMMENT_RE    # {# ... #}
_JINJA_KEYWORD_RE    # Jinja keywords
_GRAPHQL_KEYWORD_RE  # GraphQL keywords
_RENDER_TEMPLATE_RE  # render_template(...)
_KWARG_RE            # keyword argument matching
_GQL_TYPE_DEF_RE     # GraphQL type/interface definitions
_GQL_FIELD_DEF_RE    # GraphQL field definitions
_GQL_QUERY_DEF_RE    # GraphQL query/mutation definitions
_RESOLVER_CLASS_RE   # GraphQL resolver class detection
_RESOLVER_DECORATOR_RE  # GraphQL resolver decorator
_SQL_TABLE_RE        # SQL table name extraction
_SQL_COLUMN_LIST_RE  # SQL column list extraction
```

These regexes extract string literals and analyze their content to determine
whether they contain SQL, Jinja2, or GraphQL.  The string extraction regexes
in particular are fragile:
- `_TRIPLE_DOUBLE_STRING_RE` with `re.DOTALL` greedily matches across multiple
  string literals if there are two `"""` in the file
- None of them handle raw strings (`r"..."`, `rb"..."`)
- None handle implicit string concatenation (`"foo" "bar"`)
- None handle f-strings or byte strings

## Goal

Replace string-literal extraction regexes with tree-sitter-based string node
traversal.  Use the tree-sitter parse tree to find all string literal nodes,
then inspect their content for DSL indicators.

This phase is more complex than the others because:
1. DSL detection is inherently heuristic (no formal grammar demarcates "this
   string is SQL")
2. The tree-sitter grammars for HTML, CSS, SQL, and Jinja2 are already
   integrated into `emend_core` — they should be used here
3. Magic comment detection (`# language = sql`) requires comment node traversal
   (see Phase 6)

## Affected Files

- `src/emend/dsl.py` — all regex constants and `detect_dsl_regions()`,
  `extract_sql_symbols()`, `_detect_sql_string()`, `_detect_jinja_string()`

## Implementation Notes

### String literal extraction

Replace `_TRIPLE_DOUBLE_STRING_RE` / `_TRIPLE_SINGLE_STRING_RE` / etc. with
tree-sitter node traversal.  The Rust extension should expose a function:

```rust
pub fn collect_string_literals(source: &str, ext: &str) 
    -> Vec<(u32, u32, String)>;  // (start_byte, end_byte, content)
```

Or equivalently, use `find_pattern` with string-type patterns:
```python
matches = find_pattern('"""$CONTENT"""', file_path, source_override=source)
matches += find_pattern("'''$CONTENT'''", file_path, source_override=source)
matches += find_pattern('"$CONTENT"', file_path, source_override=source)
matches += find_pattern("'$CONTENT'", file_path, source_override=source)
```

Note: `find_pattern` with string patterns may not currently support extracting
string literal content via `$METAVAR`.  If not, extend `emend_core` with a
dedicated `collect_string_literals()` function.

### Magic comment detection

Replace `_MAGIC_COMMENT_RE` with tree-sitter comment node traversal (see
Phase 6).  Comments containing `# language = sql` should be detected by
walking comment nodes, not by regex on raw source.

### SQL keyword heuristics

`_SQL_KEYWORD_RE` checks whether a string's content looks like SQL.  This
heuristic can remain as a string-content check applied *after* the string
literal has been correctly extracted by tree-sitter.  The regex on the
*content* is acceptable (it's not parsing source structure), but the
extraction of the containing string must use tree-sitter.

### Jinja2 / GraphQL detection

Similarly, `_JINJA_EXPR_RE` etc. are applied to the already-extracted string
content, which is acceptable.  The only change needed is the extraction step.

### Priority within DSL detection

This phase has the most complex refactoring and the lowest risk of
correctness bugs in practice (DSL detection is heuristic anyway).  It can be
split into sub-phases:
- **9a**: Replace string literal extraction regexes with `collect_string_literals`
- **9b**: Replace magic comment regex with tree-sitter comment traversal (can
  share with Phase 6)
- **9c**: Replace SQL table/column extraction regexes with tree-sitter SQL
  grammar queries (tree-sitter SQL grammar is already integrated)

## Tests

- `test_dsl.py` — all DSL detection tests must pass.
- `test_dsl_treesitter.py` — tree-sitter grammar tests must pass.
- Add test: Python file with two `"""` triple-quoted strings, verify only the
  correct one is detected as a DSL region (old regex could merge them).

## Acceptance Criteria

- [x] String literal extraction in `detect_dsl_regions()` uses tree-sitter, not regex.
- [x] Magic comment detection uses comment node traversal.
- [x] All existing DSL tests pass.
- [x] Multi-string false-merge bug fixed and tested.
- [ ] SQL table/column extraction uses tree-sitter SQL grammar (Phase 9c — deferred, content-regex on already-extracted SQL content is acceptable per roadmap notes).
