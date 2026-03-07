# emend Development Guide

## File Locations

### Source Code (`src/emend/`)

| File | Purpose |
|------|---------|
| `cli.py` | CLI entry point (Typer), all command definitions |
| `transform.py` | Core engine: lookup, edit, add, find, replace, rename, move, find-references, callers, callees, graph, dead-code |
| `pattern.py` | Pattern parsing and Rust IR compilation with `$METAVAR` support |
| `component_selector.py` | Selector parsing (`file.py::Sym[component][accessor]`) |
| `ast_commands.py` | List-symbols and copy-to commands (uses Rust `emend_core` for symbol collection) |
| `ast_utils.py` | AST traversal utilities (uses Rust `emend_core.collect_symbols_from_str()`) |
| `query.py` | Symbol collection and filtering for `lookup` (uses Rust scope resolver) |
| `lint.py` | Lint engine: loads `.emend/patterns.yaml` rules, runs pattern-based linting, dead code detection config |
| `type_oracle.py` | Type inference adapter: `TypeOracle` ABC + `PyreflyAdapter`, `PyrightAdapter`, `TyAdapter`; `parse_type_string`, `TypeDescriptor`, `FileTypes`, `TypeBinding`, `create_type_oracle`, `detect_type_engine`; results cached in `parse.db` (`type_cache` table) |
| `editor_search.py` | Editor integration: `EditorSearchEngine`, FTS5 trigram index, JSON-RPC server (`run_editor_server`), scoring, partial pattern normalization |
| `grammars/selector.lark` | Lark grammar for selector syntax |
| `grammars/pattern.lark` | Lark grammar for pattern syntax |

### Vim Plugin (`vim/`)

| File | Purpose |
|------|---------|
| `plugin/emend.vim` | Plugin entry point: `:Emend`, `:EmendSearch`, `:EmendOutline`, `:EmendRefs` commands |
| `autoload/emend.vim` | RPC client over stdio, server lifecycle, executable detection (`uv tool` / `$PATH`) |
| `autoload/emend/ui.vim` | Split-pane search UI: floating windows (Neovim) / splits (Vim), navigation, preview, cache warming |
| `doc/emend.txt` | Vim help documentation (`:help emend`) |
| `test/emend.vader` | Vader.vim plugin tests |
| `test/run_tests.sh` | Test runner (auto-downloads vader.vim) |

### Tests (`tests/test_emend/`)

| Test File | Tests For |
|-----------|-----------|
| `test_add_parameter.py` | `add` command (parameters, decorators, bases) |
| `test_ast_migration.py` | Tree-sitter migration regression tests (ast_utils, query, search --output summary) |
| `test_batch.py` | `batch` command (YAML/JSON operations) |
| `test_callers.py` | `callers` command |
| `test_dead_code.py` | `dead-code` command (detection, CLI, exclude-refs, strings-as-refs, noqa, git last-reference, lint integration) |
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
| `test_type_oracle.py` | `TypeOracle` unit tests: `parse_type_string`, `TypeDescriptor`, `FileTypes`, cache, parsers, `detect_type_engine`, stress tests, optional pyrefly/pyright integration |
| `test_typeoracle_integration.py` | End-to-end integration: `:type[X]`/`:returns[X]` pattern constraints, oracle-aware lookup, `cmd_edit`/`cmd_add` wiring |
| `test_vim_rpc.py` | Vim plugin JSON-RPC protocol tests: dispatch, search, selector, file_symbols, status, reindex, error handling, serialization |
| `test_visit_project.py` | `visit_project_ts()` helper |

## Commands

| Command | Description |
|---------|-------------|
| `search` | Unified search: auto-detects pattern mode (if `$` in query) vs symbol lookup mode vs summary mode (bare file/dir). `--output=code\|location\|selector\|summary\|metadata`, `--flat`, `--tree-depth`, `--imported-from`, `--scope-local`, `--matching`, `--type-engine`. Also available as: `query`, `show`, `get`, `lookup`, `find` for intuitive workflows |
| `replace` | Replace code patterns (dry-run by default). `--in` supports selectors |
| `edit` | Modify or remove existing symbol components. File globs in selectors |
| `add` | Insert new items into list components. File globs in selectors |
| `copy-to` | Copy a symbol to another file |
| `move` | Move a symbol to another file or a module to another package, updating imports |
| `rename` | Rename a symbol or module across the project (`--docs`, `--no-hierarchy`, `--unsure` for symbols; auto-detects mode by `::` in selector) |
| `refs` | Find all references to a symbol (`--writes-only`, `--reads-only`, `--calls-only` for call sites only) |
| `graph` | Generate a call graph in plain/json/dot format |
| `batch` | Apply batch refactoring from YAML/JSON operation files |
| `lint` | Lint files using pattern rules from `.emend/patterns.yaml` (includes `deadcode` section) |
| `deadcode` | Find potentially dead (unreferenced) code (`--kind`, `--include-private`, `--json`, `--exclude-references-from`, `--no-strings`, `--no-last-reference`, `--all-files`, `--entry-point-decorator`, `--entry-point-name`, `--exclude-path`) |
| `types` | Show inferred types for symbols in a file (`--name`, `--kind`, `--definitions-only`, `--json`, `--engine`) |
| `index` | Pre-build parse, QN-index, and type-cache schema in `parse.db` for faster cross-project operations (`--jobs`) |
| `editor-search` | One-shot JSON search for editor integration (auto-detects symbol/pattern/selector mode) |
| `editor-server` | Long-running JSON-RPC server over stdio for the Vim plugin (methods: search, symbols, pattern, selector, file_symbols, references, status, reindex, shutdown) |

## Architecture

### Tree-sitter + Rust Backend

All source analysis uses the Rust `emend_core` extension (PyO3/maturin) built on tree-sitter:
- `ast_utils.py` — uses `emend_core.collect_symbols_from_str()`
- `query.py` — uses `PyScopeResolver` for symbol collection and filtering
- `ast_commands.py` — uses `emend_core` for symbol collection with rich metadata

### Cross-Project Operations

Cross-project functions use `visit_project_ts()` in `transform.py`, which iterates project files with parallel read + pre-filtering via the Rust extension:

- `find_references()` — uses `PyScopeResolver.references_in_file()` for scope-aware reference finding
- `rename_symbol()` — uses scope resolver + byte-range edits via `PyFileTransform`
- `move_module()` / `rename_module()` — import rewriting + filesystem operations
- `find_callers()` — uses `references_in_file()` filtered to `kind == "call"`
- `find_callees()` — uses `references_in_file()` + `find_nested_definitions()`
- `generate_graph()` — builds call graph from callers/callees analysis

### Lint Engine

`lint.py` loads rules from `.emend/patterns.yaml`:
- `macros` section: named reusable pattern fragments
- `rules` section: `find` + optional `not-inside` + `message` + optional `replace`
- `deadcode` section: enables dead code detection via `DeadCodeConfig` dataclass (supports `entry-point-decorators`, `entry-point-names`, `exclude-paths` with glob patterns)
- `--fix` flag auto-applies associated `replace` patterns

### Type Oracle

`type_oracle.py` provides an abstract `TypeOracle` interface for querying inferred types:
- `PyreflyAdapter` -- runs `pyrefly check --debug-info` and parses the JSON binding dump
- `PyrightAdapter` -- starts `pyright-langserver` via LSP and queries hover for each symbol
- `TyAdapter` -- starts `ty lsp` via LSP and queries hover for each symbol
- `create_type_oracle(engine="auto")` -- factory; autodetects engine from config files and PATH
- `detect_type_engine(project_root)` -- heuristic: config files first (pyrightconfig.json, ty.toml, pyrefly.toml / pyproject.toml sections), then tool availability
- `parse_type_string(raw)` -- parses type strings from any backend into `TypeDescriptor` trees
- `FileTypes` / `TypeBinding` -- structured result types; `FileTypes.build_index()` enables O(1) positional lookup

Pattern constraints `:type[X]` and `:returns[X]` are parsed by the `ORACLE_TYPE_CONSTRAINT` grammar terminal and post-filtered by `_filter_matches_by_type_oracle()` in `transform.py`.  Lookup (`search` / `query_symbols`) uses `_filter_by_returns_with_oracle()` in `query.py` as a fallback when no annotation is present.

Results are cached via a two-tier cache (in-memory LRU + disk SQLite) in the shared `.emend/cache/parse.db` database (`type_cache` table, keyed by file content hash).  All oracle calls check this cache before invoking the underlying type engine.

### Environment Path Lookup

`project_config.py` provides environment-aware symbol lookup via `environment_lookup` configuration:
- **Python**: searches `.venv/venv` site-packages directories for installed package sources (enables "go to definition" for dependencies)
- **TypeScript/JavaScript**: searches `node_modules` for installed package sources
- **Rust**: searches `target/` for compiled dependency and workspace crate sources
- Configuration via `[environment_lookup]` section in language config or project config (`.emend/config.toml`, `pyproject.toml` `[tool.emend]`)
- `enabled` (bool) — toggle environment lookup; `paths` (list[str]) — directories to probe
- Fallback integration in `query_symbol_index()` and `EditorSearchEngine` when symbol not found in project index
- Separate cache per language in `parse.db` (`environment_cache` table), keyed by environment mtime

### Dead Code Detection

`transform.py` contains `find_dead_code()`:
- Single-pass O(files) analysis using `PyScopeResolver`
- `_find_python_source_root()` detects `src/` layout via `pyproject.toml`
- Entry point heuristics skip decorated symbols, dunders, tests, `__all__` members
- Configurable `entry-point-decorators`, `entry-point-names`, and `exclude-paths` (with glob support) via `.emend/patterns.yaml` or CLI flags
- String literal scanning for dynamic references (getattr, serialization)
- `git log -S` integration for last-reference tracking
- `# noqa: emend:deadcode` inline suppression

## Configuration

Project-level settings are loaded from (in priority order, highest wins):
1. ``.emend/config.toml`` in the project root
2. ``pyproject.toml`` under ``[tool.emend]``
3. Language-level defaults from ``languages/<lang>/config.toml``

### Environment Lookup Configuration

Enable symbol lookup in environment paths (venv, node_modules, Cargo build artifacts):

```toml
# pyproject.toml or .emend/config.toml
[environment_lookup]
enabled = true
paths = [".venv", "venv"]  # For Python
paths = ["node_modules"]   # For TypeScript/JavaScript
paths = ["target"]         # For Rust
```

## Running Tests

Tests run in a virtual environment managed by the Makefile.

```bash
# Run all tests (8 parallel workers)
make test

# Run specific test file
make test TESTS=tests/test_emend/test_add_parameter.py

# Run specific test by name
make test TESTS="tests/test_emend/test_primitives_copy_imports.py::test_copy_imports_prepend"

# Run tests matching a pattern
make test TESTS="-k default"
```

## Adding Commands

1. Add command implementation in `transform.py` (for tree-sitter-based operations) or `ast_commands.py` (for symbol listing)
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

## Development Workflow

* Use Red/Green TDD. If writing an automated test is infeasible or redundant, use a manual testing procedure and verify that fails then succeeds.
* Always identify yourself in commit messages (Claude Code, Gemini, Codex, etc.)
* You should use `make test` rather than trying to run tests directly from the environment.

## Environment notes

Do not assume the dependencies will be installed on the active python installation. The venv must be built to include the compiled rust emend_core. THAT LIBRARY IS _REQUIRED_ for `emend` to function. DO NOT hack around its absence, which indicates you are not working in the correct environment. Try `make clean test` to build a functional .venv and run the test suite.

Use `uv` commands for everything. If you do need to manually install a dependency, use `uv pip install` rather than `.venv/bin/pip install`.
