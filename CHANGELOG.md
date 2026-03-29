# Changelog

## Unreleased

## 0.3.0

### Features

#### Impact Analysis (`impact` command)

- **New `impact` command**: Compute the transitive set of impacted symbols from a change via reverse-caller BFS closure
  - `emend impact mymodule.py::func` — from a selector
  - `emend impact --diff HEAD` — from a git diff (auto-maps changed lines to symbols)
  - `--output symbols` (default), `tests`, or `graph` for witness edges
  - `--json` for structured output, `--max-depth` to limit traversal

#### Taint Analysis (`taint` command)

- **New `taint` command**: Intraprocedural taint analysis tracking value flow from sources to sinks within individual functions
  - Configurable via `taint` section in `.emend/patterns.yaml` with `sources`, `sinks`, `sanitizers`, and `labels`
  - Propagates taint through variable assignments; sanitizers remove taint before sink checks
  - `--trace` shows full propagation path from source to sink
  - `--label` filters to a specific taint label; `--json` for structured output

#### Flow-Based Lint Rules

- **New `flows-from` / `flows-to` / `not-through` lint rule predicates**: Define data-flow lint rules in `.emend/patterns.yaml` that detect when values matching a source pattern reach a sink pattern without passing through a sanitizer
  - Intraprocedural analysis within each function body
  - `FlowWitness` traces show source, propagation chain, and sink
  - Integrates with existing `# noqa` suppression and `--rule` filtering

#### Unified Module Mapping System (`map` command)

- **New `map` command**: Unified identifier and module mappings with subcommands:
  - `add`, `add-module` — create mappings
  - `lookup`, `search` — query mappings
  - `resolve`, `resolve-file` — resolve identifiers to file paths with import-aware Tier 3 resolution
  - `rm`, `rm-module`, `list-modules`, `update-module` — manage mappings
- **Smart module resolution**: Follows re-exports at any depth, resolves plain module paths to `__init__.py`, handles snake_case file names
- **Dotted selector support**: Extended selectors with dot notation (e.g., `module.submodule::Symbol[component]`) for navigating nested structures and re-exports

#### Vim Plugin Improvements

- **`:EmendGoto` fixes**: Smart navigation with local variable support and file context passing for import-aware resolution
- **Enhanced `:Emend` search**:
  - File path search via FTS5 trigram index with fuzzy subsequence matching
  - Auto-detection of file-like queries (`/` or known extensions)
  - File results displayed with distinct styling and higher scores
- **Improved C-Space completion**: Better filtering and result scoring
- **Selection highlighting**: Fixed caret display during navigation (`j`/`k`/`C-n`/`C-p`) with extmark-based selection

#### Multi-Language Environment Lookup

- **Environment lookup generalization**: Renamed `venv_lookup` to `environment_lookup` to support all languages
- **TypeScript/JavaScript support**: `node_modules` directory lookup for installed npm packages
- **Rust support**: `target/` directory lookup for Cargo build artifacts and workspace crates
- **Language-specific defaults**: Each language (`languages/python/config.toml`, `languages/typescript/config.toml`, `languages/rust/config.toml`) now includes environment lookup configuration with appropriate default paths

### Configuration Changes

- Renamed project config section from `[venv_lookup]` to `[environment_lookup]`
  - Old function names (`get_venv_lookup_config`, `resolve_venv_site_packages`) deprecated but remain for backward compatibility
  - New functions: `get_environment_lookup_config()`, `resolve_environment_path()` in `project_config.py`

---

## 0.2.0

### Features

#### Lint Engine

- **`# noqa` inline suppression** (PR #6): suppress violations per-line with `# noqa`, `# noqa: emend:rule-name`, or mixed with other linter tags (`# noqa: E501, emend:no-print`).
- **pre-commit integration** (PR #4): emend can run as a pre-commit hook via `.pre-commit-hooks.yaml`. Add `emend-lint` to your `.pre-commit-config.yaml`.

#### Pattern Replace: `${NAME.content}` String Interpolation (PR #10)

Replace patterns can now extract the inner content of a captured string literal by appending `.content`:

```bash
# Captured $X = "foo" → substitutes foo (quotes stripped)
emend replace 'Union["$X", $Y]' '${X.content} | $Y' src/ --apply
```

Use `${NAME.content}` in a replacement template to strip surrounding quotes from a captured string node. If the captured node is not a string literal, the replacement is skipped for that match. Useful for migrating `Union["X", Y]` (deferred-annotation style) to PEP 604 `X | Y` union syntax.

#### `emend deadcode` detection

* Traces references to find unreferenced symbols.
* False positive reduction:
  * Uses heuristics to see where symbols may be referenced as strings.
  * Special-cases common framework decorator names like `@post`.
* Finds last reference in git.
* May be used in lint, but is too slow to use as a pre-commit hook on large projects.

#### Performance

Refactored much of the codebase to use Rust, tree-sitter, and Python free-threading.

#### Complete tree-sitter migration

LibCST has been fully replaced by the Rust `emend_core` extension built on tree-sitter. All pattern matching, symbol collection, scope resolution, and code transformation now runs through the Rust backend.

- **Removed `parse_cache`** — the LibCST parse cache table is no longer needed; all caching is content-hash based via the scope resolver and symbol index.
- **Removed all LibCST visitors and transformers** — `PatternFinder`, `ConstrainedPatternFinder`, `PatternReplacer`, `_SymbolRenamer`, `ComponentSetter`, `ComponentAdder`, `ComponentRemover`, `SymbolRemover`, `_ReferenceFinder`, `_CallerFilter`, `_CalleeCollector`, `_ImportOriginCollector`, `_BulkReferenceFinder`, and others have been replaced by Rust equivalents.
- **Rust structural matcher** covers all Python expression and statement types: assignments, comprehensions, f-strings, imports, compound statements (`if`/`while`/`for`/`with`/`try`), lambda, walrus, and more.
- **`PyScopeResolver`** provides qualified name resolution, reference finding, and dead code analysis entirely in Rust.
- **`PyFileTransform`** handles all code mutations via non-overlapping byte-range edits.

#### Language configuration reorganization

Language-specific configuration is now organized in per-language directories under `languages/`:

```
languages/
├── python/
│   ├── config.toml      # Scope resolver config
│   └── symbols.scm      # Tree-sitter symbol query
└── typescript/
    └── config.toml      # Scope resolver config
```

Documentation now includes a guide for adding new language support.

---

## 0.1.0 (2026-02-22)

Initial public release.

### Features

#### CLI Consolidation

The command surface was unified to reduce cognitive overhead:

- **Merged** `find`, `lookup`, `list-symbols`, `query`, `show`, `get` → unified `search` command that auto-detects mode from the query (`$` → pattern mode; `::` → lookup mode; bare path → summary mode). All old names remain as aliases.
- **Merged** `callers`, `find-references` → `refs`. Added `--calls-only` to filter to call sites; `--writes-only` and `--reads-only` for read/write context.
- **Merged** `rename-symbol` and `rename-module` → `rename`. Mode auto-detected: selector with `::` renames a symbol; bare path renames a module.
- **Merged** `move-module` → `move`. Same auto-detection.
- **Unified** `--output` flag: `code`, `location`, `selector`, `summary`, `metadata`, `json`, `count`, with optional `::flat` and `::dedent` modifiers.
- **Unified** `--where` flag for scope/structural constraints.

#### Documentation

- Full Sphinx documentation at [lucaswiman.github.io/emend](https://lucaswiman.github.io/emend/) covering commands, patterns, selectors, linting, installation, quickstart, and recipes.
- GitHub Pages CI/CD for automatic doc publishing.

#### Versioning

- Git tag-based versioning via `hatch-vcs`; no hardcoded version numbers.

### Known Limitations / Future Work

See [`TODOS.md`](TODOS.md) and [`ideas/FUTURE_WORK.md`](ideas/FUTURE_WORK.md). Short summary:

- `$X:stmt` type constraint is parsed by the grammar but not fully implemented.
- `graph`, `callers`, and `refs` use per-file `QualifiedNameProvider`; switching to `FullRepoManager` + `FullyQualifiedNameProvider` would improve cross-file name resolution
