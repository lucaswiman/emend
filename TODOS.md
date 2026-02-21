# emend TODO List

## Bug Fixes / Regressions

All three bugs from the initial LibCST implementation have been fixed.
See `tests/test_emend/test_regressions.py` for regression tests.

- [x] `rename_symbol` and `find_references` are name-based, not scope-aware → **FIXED** via QualifiedNameProvider
- [x] `rename --docs` flag is a no-op → **FIXED** via `_DocstringRenamer`
- [x] `list-symbols` shows incomplete signatures → **FIXED** via full parameter type iteration

---

## Part 1: Simplification and Unification

### 1.1 Drop the Dual AST Backend
- [x] Migrate `find_nested_definitions()` from stdlib `ast` to a LibCST CSTVisitor — **DONE** (`ast_utils.py` uses `_NestedDefinitionVisitor` with `PositionProvider`)
- [x] Migrate `query.py` symbol collection to LibCST (use `PositionProvider`) — **DONE** (`_SymbolCollector` with `PositionProvider`)
- [x] Update `list-symbols` to use LibCST backend (enables richer metadata) — **DONE** (`ast_commands.py` uses `_ListSymbolsVisitor` with `PositionProvider`)
- [x] Update `lookup` filters to use LibCST metadata providers — **DONE**
- [x] Consolidate `NestedSymbol` / `SymbolInfo` / `SymbolFinder` into a single representation — **DONE**
- [x] Write regression tests ensuring `list-symbols` and `lookup` behavior unchanged after migration — **DONE** in `test_ast_migration.py`

### 1.2 Unify the Selector Systems
- [x] Extend `ExtendedSelector` to include `line_range` field (currently in `locator.Selector`) — **DONE** (`ExtendedSelector.line_range` property)
- [x] Remove `locator.py` Selector type (consolidate into `component_selector.py`) — **DONE** (`locator.py` deleted; `NestedSymbol` moved to `component_selector.py`)
- [x] Update all callers of the old `Selector` type to use `ExtendedSelector` — **DONE**
- [x] Write tests for unified selector round-trip parsing — **DONE**
- [x] Ensure parsing of all existing selector syntaxes — **DONE**

### 1.3 Merge `find` and `lookup`
- [x] Add `search` command to CLI that auto-detects mode from query
  - If query contains metavariables (`$X`, `$...Y`) → pattern matching mode
  - If query is a selector path (`MyClass.method`) → symbol lookup mode
  - Combined filters (`--kind`, `--has-decorator`) work in both modes
- [x] Keep `find` and `lookup` as aliases
- [x] Write tests for `search` in pattern mode
- [x] Write tests for `search` in lookup mode
- [x] Write tests for `search` with combined filters

### 1.4 Consolidate Cross-Project Operations (ProjectVisitor)
- [x] Implement `visit_project(selector, visitor_factory, project_path, metadata_providers)` helper
- [x] Refactor `find_references()` to use `visit_project()`
- [x] Refactor `rename_symbol()` to use `visit_project()` — **DONE**
- [x] Refactor `move_symbol()` to use `visit_project()` — **DONE**
- [x] Refactor `move_module()` / `rename_module()` to use `visit_project()` — **DONE**
- [x] Write tests verifying refactored functions behave identically

---

## Part 2: Making emend More Powerful

### 2.1 Scope-Aware Pattern Matching
- [x] Add `--where` flag to `find` command for scope filtering — **DONE** (alias for `--inside` with pattern support)
- [x] Implement `--imported-from <module>` filter (match only names imported from specific module) — **DONE** via `_ImportOriginCollector` + `QualifiedNameProvider`
- [x] Implement `--scope-local` flag (match only names local to current scope) — **DONE** (matches only locally-defined names, excludes imports)
- [x] Integrate `QualifiedNameProvider` into pattern matching when `--imported-from` is used — **DONE**
- [x] Write tests: `find 'json.loads($X)' --imported-from json` matches only actual json.loads — **DONE** in `test_imported_from.py`

### 2.2 Expression Context Filtering
- [x] Add `--writes-only` flag to `find-references` — **DONE**
- [x] Add `--reads-only` flag to `find-references` — **DONE**
- [x] Integrate `ParentNodeProvider` in `_ReferenceFinder` for write context detection — **DONE** (uses `ParentNodeProvider` instead of `ExpressionContextProvider`)
- [x] Write tests for read/write context filtering — **DONE** in `test_find_references_context.py`

### 2.3 Parent-Node-Aware Patterns (Improve --not-inside)
- [x] Add `ParentNodeProvider` integration for context-aware matching — **DONE**
- [x] Support pattern syntax in `--inside` / `--not-inside` (e.g., `--not-inside 'try:'`) — **DONE**
- [x] Allow `--not-inside 'def test_*'` to exclude test functions by name pattern — **DONE**
- [x] Write tests for improved `--inside`/`--not-inside` with patterns — **DONE** in `test_power_features.py`

### 2.4 Cross-Reference Graph
- [x] Add `callers` command: find all callers of a function across the project
- [x] Add `callees` command: find all functions/methods called by a function
- [x] Add `graph` command: produce full dependency graph in DOT / JSON / plain format
- [ ] Integrate `FullRepoManager` + `FullyQualifiedNameProvider`
- [x] Write tests for `callers` command
- [x] Write tests for `callees` command
- [x] Write tests for `graph` command with multiple output formats — **DONE** (plain, JSON, DOT)

### 2.5 Batch Refactoring
- [x] Add `batch` command reading YAML/JSON operation files
- [x] Support `rename` operation type in batch file
- [x] Support `replace` operation type in batch file
- [x] Support `add` operation type in batch file
- [x] Support `edit` operation type in batch file
- [x] Support `remove` operation type in batch file
- [x] Operations apply in order (first rename, then replace matching new name, etc.)
- [x] Add `--apply` flag; default is dry-run showing all diffs
- [x] Write tests for batch operations with YAML input
- [x] Write tests for batch operations with JSON input

### 2.6 Pattern Macros / Lint Rules
- [x] Add `lint` command reading rules from `.emend/patterns.yaml`
- [x] Support `macros` section: named reusable pattern fragments
- [x] Support `rules` section: `find` + optional `not-inside` + `message`
- [x] Expand macro references `{macro_name}` within rule patterns
- [x] Output violations with file/line/column and rule message
- [x] Add `--fix` flag to apply associated `replace` automatically
- [x] Write tests for `lint` with multiple rules
- [x] Write tests for macro expansion
- [x] Write tests for `--fix` mode

---

## Part 3: Pattern Matching Enhancements

### 3.1 Negated Type Constraints
- [x] Add `$X:!int`, `$X:!str`, etc. syntax to `pattern.lark` grammar
- [x] Implement negated constraint matching in `pattern.py` compiler
- [x] Write tests: `find 'range($X:!int)'` matches non-integer args
- [x] Write tests: `find '$A:!call + $B'` excludes function call expressions

### 3.2 Star Expression Patterns
- [x] Add `*$X` (star expression / unpacking) patterns to pattern grammar — **DONE**
- [x] Add `**$X` (double-star / dict-unpacking) patterns — **DONE**
- [x] Support `func(*$ARGS, **$KWARGS)` in function call patterns — **DONE**
- [x] Write tests for star expression matching — **DONE**
- [x] Write tests for double-star dict-unpacking matching — **DONE**

### 3.3 Compound Statement Patterns
- [x] Support `if $COND:` pattern (condition captured, body optional)
- [x] Support `for $VAR in $ITER:` pattern
- [x] Support `while $COND:` pattern
- [x] Support `with $CTX as $VAR:` and `with $CTX:` patterns
- [x] Support `try:` / `except $EXC:` / `except $EXC as $VAR:` patterns
- [x] Support `async for` and `async with` variants — **DONE**
- [x] Write tests for each compound statement type

### 3.4 Decorator Patterns in Find
- [x] Support multi-line `@$DEC\ndef $FUNC($...ARGS):` pattern
- [x] Support multiple decorator patterns: `@$DEC1\n@$DEC2\ndef ...` — **DONE**
- [x] Write tests for decorator + function definition patterns
- [x] Write tests for async decorator patterns — **DONE**

### 3.5 Lambda Body Patterns
- [x] Support `lambda $X: $EXPR` pattern
- [x] Support `lambda $X, $Y: $EXPR` pattern (multiple params)
- [x] Support `lambda *$ARGS: $EXPR` pattern — **DONE**
- [x] Write tests for lambda pattern matching

### 3.6 Dict Patterns with Literal String Keys
- [x] Support `{'name': $NAME, 'age': $AGE}` patterns with literal keys — **DONE**
- [x] Support partial dict matching `{'type': 'user', ...}` (extra keys OK) — **DONE**
- [x] Write tests for exact key dict patterns — **DONE**
- [x] Write tests for partial (spread) dict patterns — **DONE**

### 3.7 Chained Comparison Patterns
- [x] Support `$A < $B < $C` chained comparison patterns — **DONE**
- [x] Support mixed operators: `$A <= $B < $C` — **DONE**
- [x] Write tests for chained comparison matching and replacement — **DONE**

### 3.8 Walrus Operator in Complex Contexts
- [x] Support walrus in comprehensions: `[$X for $VAR in $ITER if ($TARGET := $EXPR)]` — **DONE**
- [x] Support walrus in if: `if ($VAR := $EXPR):` — **DONE**
- [x] Write tests for walrus in comprehension patterns — **DONE**
- [x] Write tests for walrus in conditional patterns — **DONE**

---

## Part 4: Documentation and Developer Experience

### 4.1 Update CLAUDE.md
- [x] Add new commands (search, lint, callers, callees, graph, batch) to command table -- **DONE**
- [x] Update architecture description to reflect visit_project() and lint.py -- **DONE**
- [x] Update test file table for any new test files -- **DONE**

### 4.2 Update Sphinx Docs
- [x] Add documentation for `search` command -- **DONE**
- [x] Add documentation for `lint` command with `.emend/patterns.yaml` format -- **DONE**
- [x] Add documentation for `callers`, `callees`, `graph` commands -- **DONE**
- [x] Add documentation for `batch` command with example YAML/JSON -- **DONE**
- [x] Update pattern-matching reference with new syntax (negated, compound, decorator, lambda) -- **DONE**
- [x] Add examples for pattern macros and lint rules -- **DONE**
- [x] Update recipes with new command examples -- **DONE**
- [x] Fix installation.md (update dependencies) -- **DONE**

### 4.3 Clean Up FUTURE_WORK.md
- [x] Remove items that have been implemented -- **DONE**
- [x] Add new ideas discovered during implementation -- **DONE**
- [x] Update priority ordering based on what was learned -- **DONE**

---

## Implementation Notes

### `$X:stmt` Type Constraint
Currently, `$X:stmt` is accepted by the grammar but not fully implemented.

**Challenge:** The pattern compiler replaces `$X` with `__META_X__` (a valid identifier) to parse as Python code. However, `__META_X__` is an expression, not a statement, so it can't appear at statement-level positions (e.g., function body).

**Recommended approach:** Template-based matching where `$BODY:stmt` is restricted to positions that expect a statement sequence, matched against `body: Sequence[BaseStatement]`. This avoids the parsing problem since the metavar is resolved by the compound statement matcher itself, not in parsed Python.

### Compound Statement Pattern Approach
Compound statements (if/for/while/with/try) require special handling since their bodies are statement sequences. The recommended approach:
1. Parse just the header (condition, target, etc.) as an expression
2. Match the CST node's condition/target fields against the compiled pattern
3. Leave the body unconstrained unless `$BODY:stmt` is specified

### Dict Pattern Matching
Dict patterns with literal keys need exact key matching via LibCST `MatchIfTrue` matchers. Partial patterns using `...` require a special matcher that checks for key presence without requiring exact dict equality.

### Design Rationale

**Why Brackets for Components?**
`file.py::func[params]` not `file.py::func.params` — Avoids ambiguity with symbols named `params`, `returns`, etc. Brackets are visually distinct and not used elsewhere in selectors.

**Why CLI-Driven Over YAML?**
1. **Composability** - Pipes and xargs work naturally
2. **Discoverability** - `--help` shows all options
3. **Iteration speed** - Edit command, re-run immediately
4. **AI-friendly** - Single-line operations are easier to generate correctly

YAML rules can be added later for complex multi-step transforms, but the CLI primitives come first.

**Why Diff-Style Transforms?**
The `-`/`+` prefix is universally understood from version control. It makes intent immediately clear:

```
- old_thing($X)
+ new_thing($X)
```

**Why LibCST Backend?**
1. **Format preservation** - No unwanted style changes
2. **Mature matcher API** - Handles complex patterns
3. **Type-aware** - Can distinguish expressions, statements, etc.
4. **Well-maintained** - Instagram/Meta backed
