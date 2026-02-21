# Future Work for emend

Ideas for further improving emend.

---

## Completed (kept for reference)

The following have all been implemented:

- **LibCST-only backend** -- All source (`ast_utils.py`, `query.py`, `ast_commands.py`) now uses LibCST. No stdlib `ast`.
- **Unified selectors** -- `ExtendedSelector` is the primary type; `locator.py` removed.
- **All cross-project ops use `visit_project()`** -- `rename_symbol()`, `move_symbol()`, `move_module()`, `rename_module()` all refactored.
- **Scope-aware features** -- `--where`, `--scope-local`, `--imported-from` on `find` command.
- **Expression context filtering** -- `--writes-only` / `--reads-only` on `find-references`.
- **Parent-node-aware patterns** -- `--inside` / `--not-inside` accept patterns like `'def test_*'`, `'try:'`, `'async def fetch_*'`.
- **Cross-reference graph** -- `callers`, `callees`, `graph` commands with plain/json/dot output.
- **Batch refactoring** -- `batch` command with YAML/JSON operation files.
- **Pattern macros / lint rules** -- `lint` command with `.emend/patterns.yaml`.
- **Negated type constraints** -- `$X:!int`, `$X:!str`.
- **Star expression patterns** -- `*$X`, `**$X`, `func(*$ARGS, **$KWARGS)`.
- **Async compound statements** -- `async for`, `async with` patterns.
- **Multiple decorator patterns** -- `@$DEC1\n@$DEC2\ndef ...`, async decorated functions.
- **Lambda star args** -- `lambda *$ARGS: $EXPR`.
- **Dict patterns** -- `{'key': $VAL}` exact and `{'key': $VAL, ...}` partial matching.
- **Chained comparisons** -- `$A < $B < $C`, mixed operators.
- **Walrus operator** -- `if ($VAR := $EXPR):`, walrus in comprehensions.

---

## Remaining Ideas

### Cross-Reference Graph Enhancements

- Integrate `FullRepoManager` + `FullyQualifiedNameProvider` for better cross-file resolution
- More output formats for `graph` command

### Semantic Type Constraints

The current type constraint system (`$X:int`, `$X:str`) checks AST node types. A
richer system could check inferred types using mypy/pyright or LibCST's
`TypeInferenceProvider`.

### `$X:stmt` Type Constraint

Currently accepted by the grammar but not fully implemented. The pattern compiler
replaces `$X` with `__META_X__` (a valid identifier) to parse as Python code, but
this is an expression and cannot appear at statement-level positions. A template-based
approach where `$BODY:stmt` is restricted to positions that expect a statement sequence
would solve this.

---

## Priorities

1. **Semantic type constraints** -- High effort (requires type checker integration),
   very high power gain for type-aware refactoring.

2. **Cross-reference graph enhancements** -- Medium effort, improves accuracy of
   cross-file analysis.

3. **`$X:stmt` type constraint** -- Medium effort, enables body-level pattern matching
   in compound statements.
