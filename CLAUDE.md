# emend Development Guide

## File Locations

### Source Code (`src/emend/`)

| File | Purpose |
|------|---------|
| `cli.py` | CLI entry point (Typer), all command definitions |
| `transform.py` | Core engine: lookup, edit, add, find, replace, rename, move, find-references, callers, callees, graph |
| `pattern.py` | LibCST pattern matching with `$METAVAR` support |
| `component_selector.py` | Selector parsing (`file.py::Sym[component][accessor]`) |
| `ast_commands.py` | List-symbols and copy-to commands (LibCST `_ListSymbolsVisitor` with `PositionProvider`) |
| `ast_utils.py` | AST traversal utilities using LibCST (`_NestedDefinitionVisitor` with `PositionProvider`) |
| `query.py` | Symbol collection and filtering for `lookup` (LibCST `_SymbolCollector` with `PositionProvider`) |
| `lint.py` | Lint engine: loads `.emend/patterns.yaml` rules, runs pattern-based linting |
| `grammars/selector.lark` | Lark grammar for selector syntax |
| `grammars/pattern.lark` | Lark grammar for pattern syntax |

### Tests (`tests/test_emend/`)

| Test File | Tests For |
|-----------|-----------|
| `test_add_parameter.py` | `add` command (parameters, decorators, bases) |
| `test_ast_migration.py` | LibCST migration regression tests (ast_utils, query, search --output summary) |
| `test_batch.py` | `batch` command (YAML/JSON operations) |
| `test_callers.py` | `callers` command |
| `test_callees.py` | `find_callees()` in transform.py |
| `test_cli_transform.py` | CLI integration for transform operations |
| `test_component_selector.py` | Extended selector parsing |
| `test_copy_to.py` | `copy-to` command |
| `test_edit.py` | `edit` command |
| `test_file_glob_selectors.py` | File glob selectors, `--matching`, `--output selector`, `--in` selectors, `resolve_files` |
| `test_find.py` | `find` command (pattern matching) |
| `test_find_references_context.py` | `find-references --writes-only` / `--reads-only` |
| `test_imported_from.py` | `find --imported-from` filter |
| `test_line_selector.py` | Line-based selector parsing |
| `test_lint.py` | `lint` command (rules, macros, `--fix`) |
| `test_list_symbols.py` | `search --output summary` command (was list-symbols) |
| `test_lookup.py` | `lookup` command |
| `test_move.py` | `move` command |
| `test_pattern.py` | Pattern parsing and compilation |
| `test_power_features.py` | `--where`, `--scope-local`, enhanced `--inside`/`--not-inside` with patterns |
| `test_primitives_copy_imports.py` | Copy imports using `get` + `add` primitives |
| `test_primitives_transform.py` | Transform references using `find` + `replace --in` |
| `test_query.py` | Symbol query/filtering |
| `test_regressions.py` | Regression tests (scope-aware rename, --docs, signatures) |
| `test_rename_module.py` | `rename-module` command |
| `test_rename_symbol.py` | `rename` / `rename-symbol` command |
| `test_rope_commands.py` | Module-level refactoring commands (move module mode) |
| `test_search.py` | `search` command (unified find/lookup) |
| `test_show.py` | `show` command |
| `test_show_unified.py` | Unified show output |
| `test_transform.py` | Core transform operations |
| `test_transform_comprehensions.py` | Pattern matching in comprehensions |
| `test_transform_decorators.py` | Decorator editing via `edit` command |
| `test_transform_ellipsis_collections.py` | Ellipsis captures in collections |
| `test_transform_fstrings.py` | F-string pattern matching |
| `test_transform_inside.py` | `--inside` / `--not-inside` constraints |
| `test_visit_project.py` | `visit_project()` helper |

## Commands

| Command | Description |
|---------|-------------|
| `search` | Unified search: auto-detects pattern mode (if `$` in query) vs symbol lookup mode vs summary mode (bare file/dir). `--output=code\|location\|selector\|summary\|metadata`, `--flat`, `--tree-depth`, `--imported-from`, `--scope-local`, `--matching`. Also available as: `query`, `show`, `get`, `lookup`, `find` for intuitive workflows |
| `replace` | Replace code patterns (dry-run by default). `--in` supports selectors |
| `edit` | Modify or remove existing symbol components. File globs in selectors |
| `add` | Insert new items into list components. File globs in selectors |
| `copy-to` | Copy a symbol to another file |
| `move` | Move a symbol to another file or a module to another package, updating imports |
| `rename` | Rename a symbol or module across the project (`--docs`, `--no-hierarchy`, `--unsure` for symbols; auto-detects mode by `::` in selector) |
| `refs` | Find all references to a symbol (`--writes-only`, `--reads-only`, `--calls-only` for call sites only) |
| `graph` | Generate a call graph in plain/json/dot format |
| `batch` | Apply batch refactoring from YAML/JSON operation files |
| `lint` | Lint files using pattern rules from `.emend/patterns.yaml` |

## Architecture

### LibCST-Only Backend

All source code now uses LibCST exclusively (no stdlib `ast`). This includes:
- `ast_utils.py` -- `_NestedDefinitionVisitor` with `PositionProvider`
- `query.py` -- `_SymbolCollector` with `PositionProvider`
- `ast_commands.py` -- `_ListSymbolsVisitor` with `PositionProvider`

### Cross-Project Operations

All cross-project functions use the shared `visit_project()` helper in `transform.py`, which encapsulates iterating project files with text-check, parse, and MetadataWrapper setup:

- `find_references()` -- `_ReferenceFinder` CSTVisitor with QualifiedNameProvider + PositionProvider (scope-aware)
- `rename_symbol()` -- `_SymbolRenamer` CSTTransformer with QualifiedNameProvider (scope-aware) + optional `_DocstringRenamer`
- `move_module()` / `rename_module()` -- `_ModuleImportRenamer` + filesystem operations
- `find_callers()` -- finds call sites (not just references) using `ParentNodeProvider`
- `find_callees()` -- analyzes function body to list all called functions
- `generate_graph()` -- builds call graph from callers/callees analysis

### Lint Engine

`lint.py` loads rules from `.emend/patterns.yaml`:
- `macros` section: named reusable pattern fragments
- `rules` section: `find` + optional `not-inside` + `message` + optional `replace`
- `--fix` flag auto-applies associated `replace` patterns

## Running Tests

Tests run in a virtual environment managed by the Makefile.

```bash
# Run all tests (8 parallel workers)
make test

# Run specific test file
make test TESTS=tests/test_emend/test_add_parameter.py

# Run specific test by name
make test TESTS="tests/test_emend/test_copy_imports.py::test_copy_imports_prepend"

# Run tests matching a pattern
make test TESTS="-k default"
```

## Adding Commands

1. Add command implementation in `transform.py` (for LibCST-based operations) or `ast_commands.py` (for AST-based)
2. Import and wire up in `cli.py`:
   - Import the function from `transform.py`
   - Add `@app.command()` definition with Typer annotations
3. Add tests in `tests/test_emend/test_<command>.py`

## Code Conventions

- Most commands follow the dry-run by default pattern (except read-only commands like `search`, `graph`)
- Selectors use the `ExtendedSelector` type from `component_selector.py`
- Tests use `tmp_path` fixture for file operations
- Test functions use descriptive names: `test_<command>_<scenario>`
- Read-only commands that output to stdout use `print(content, end='')` to avoid adding extra newlines
