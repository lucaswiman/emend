# emend Development Guide

## File Locations

### Source Code (`src/emend/`)

#### CLI surface

| File | Purpose |
|------|---------|
| `cli.py` | Wires command functions from `cli_*.py` into the Typer `app` and the `edit` / `analyze` / `tool` / `map` subapps. Holds `main()`. Most command bodies live in the `cli_*.py` files. |
| `cli_base.py` | Shared CLI helpers: `app`/`edit_app`/`analyze_app`/`tool_app` Typer instances, the `cli_error_handler` context manager, `ApplyFlag`/`JsonFlag` annotated types, `resolve_files`, `parse_where_clause`, `QueryShape`, top-level `--version` callback. |
| `cli_find.py` | `find` (and hidden aliases `grep`, `search`, `show`, `get`, `lookup`, `ls`) — unified search command. |
| `cli_edit.py` | `set`, `rm`, `delete`, `add`, `replace`, `cp` (copy-to), `rename`, `mv` (move), `batch`, `saturate`. |
| `cli_analysis.py` | `refs`, `graph`, `deadcode`, `impact`, `types`, `trace`, `facts`, `cfg`, `dsl-debug`, `dupes`. |
| `cli_checks.py` | `lint`, `policy`, `check` (the unified runner that calls into `checks/engine.py`). |
| `cli_map.py` | `map add` / `add-module` / `lookup` / `search` / `resolve` / `rm` / `rm-module` / `list-modules` / `update-module`. |
| `cli_tooling.py` | `index`, `editor-search`, `editor-server`, `mcp`. |
| `cli_output.py` | Shared output helpers: `format_json(data)` and `emit_json(data)` for consistent JSON serialisation across CLI commands. |

#### Core engine

| File | Purpose |
|------|---------|
| `transform/` | Core engine package (was `transform.py`). Re-exports the full public surface from 12 submodules so existing `from emend.transform import X` imports continue to work. Submodules: `__init__.py` (re-export facade), `cache.py` (SQLite parse.db and CozoDB facts.db management), `components.py` (component access, modification, and diff generation), `deadcode.py` (dead code detection: symbols, blocks, modules, safe deletion), `dispatch.py` (unified command dispatch: lookup, edit, add), `impact.py` (impact analysis: reverse-caller BFS closure, diff-to-selector mapping), `index.py` (symbol index, QN cache, and venv index management), `patterns.py` (pattern matching, find, replace, copy, and symbol source utilities), `project_iter.py` (project file iteration, pattern search, and module utilities), `refs.py` (reference finding, callers, callees, and call graph generation), `rename_move.py` (symbol and module rename/move operations), `venv_index.py` (separate venv symbol cache backed by `parse_venv.db`). |
| `pattern.py` | Pattern parsing and Rust IR compilation with `$METAVAR` support. |
| `component_selector.py` | Extended selector parser (`file.py::Sym[component][accessor]`). |
| `ast_commands.py` | List-symbols and copy-to commands (uses Rust `emend_core` for symbol collection). |
| `ast_utils.py` | AST traversal utilities backed by `emend_core.collect_symbols_from_str()`. |
| `query.py` | Symbol collection and filtering for `lookup` (uses `PyScopeResolver`). |
| `cfg.py` | Per-function CFG: `build_cfgs_for_source/file()`, `find_unreachable_blocks()`, text/JSON/DOT formatters; wraps `emend_core.PyCfg`. |
| `location_resolver.py` | Pattern match → exact CFG-block resolver: `ResolvedLocation` (frozen dataclass), `LocationResolver` (FactGraph-backed or on-the-fly), `MODULE_LEVEL_FUNC`/`MODULE_LEVEL_BLOCK` sentinels. |

#### Analysis engines

| File | Purpose |
|------|---------|
| `fact_graph.py` | Relational fact model and Datalog query layer. Fact dataclasses (`SymbolFact`, `CallFact`, `ReferenceFact`, `DefUseFact`, `CfgBlockFact`, …); `FactGraph` class with `update_files()`/`remove_files()`/`build_from_project()`; Datalog query methods (`refs_datalog`, `callers_datalog`, `callees_datalog`, `graph_datalog`, `dead_code_unified`, `unreachable_blocks_datalog`, `trace_propagation_datalog`, `interprocedural_trace_datalog`, `flow_rule_check_datalog`); JSON serialisation. |
| `trace.py` | Trace/taint analysis. `TraceConfig`, `TraceSource`, `TraceSink` (optional `effect: writes($X)/reads($X)`), `TraceSanitizer` (`quantifier: all_paths/some_path`), `TraceScopeSanitizer` (scope-bounded kills), `TraceViolation`. Datalog-backed intra- and interprocedural analysis with fixed-point iteration. YAML `presets:` key for auto-loading framework rules. |
| `trace_presets.py` | Framework preset loader: `get_preset()` reads `src/emend/presets/*.yaml` (Flask, Django, SQLAlchemy, FastAPI, Express, Axum, Diesel, Next.js, React, …); `merge_configs()` composes multiple configs. |
| `lint.py` | Lint public API layer (~800 lines). Pattern-based rules, flow rules (`flows-from`/`flows-to`/`not-through`), `deadcode` rule, `duplicate-code` rule. Loads from `.emend/rules.yaml`. Bulk of the implementation now lives in `checks/` submodules; full reduction to a thin shim is deferred post-release. |
| `policy.py` | Policy public API layer (~680 lines). `Policy`, `FlowCheck`, `StructuralCheck`, `TypeCheck`, `DeadCodeCheck`, `DatalogCheck`, `CustomCheck`, `SequenceCheck`. Loads from `.emend/rules.yaml`. Bulk of the implementation now lives in `checks/` submodules; full reduction to a thin shim is deferred post-release. |
| `checks/` | Unified rule engine package (was `checks.py`). Public surface: `CheckViolation`, `run_checks` re-exported from `checks/__init__.py`. Submodules: `engine.py` (unified runner — dispatches to lint + policy, normalises results; CLI `check` and MCP `check` tool entry point), `flow.py` (shared flow IR: `FlowSpec`, `FlowViolation`, `WitnessStep`; `execute_flow_spec()` Datalog-first / Python-fallback bridge), `pattern_rules.py` (pattern-based lint rule matching), `rules_config.py` (YAML config loader: `DeadCodeConfig`, `load_rules_document()`), `structural.py` (structural pattern checks), `types.py` (oracle-driven type constraint checks), `custom.py` (custom expert query checks), `datalog.py` (CozoScript Datalog query checks), `deadcode.py` (dead code detection as a policy check wrapper), `duplicates.py` (duplicate code detection), `sequence.py` (temporal sequence / CFG-reachability checks). |
| `flow_ir.py` | Backward-compat re-export shim (~15 lines). Forwards all public names (`FlowSpec`, `FlowViolation`, `WitnessStep`, `execute_flow_spec`, etc.) from `emend.checks.flow`. Import from `emend.checks.flow` directly in new code. |
| `rules_config.py` | Backward-compat re-export shim (~15 lines). Forwards all public names (`DeadCodeConfig`, `load_rules_document()`, etc.) from `emend.checks.rules_config`. Import from `emend.checks.rules_config` directly in new code. |
| `dsl.py` | Embedded-DSL support: `DslRegion`, `DslSymbol`, `DslLink`, `RegexNamedGroup`. Detects SQL regions (heuristics + magic comments), extracts symbols, resolves ORM links (SQLAlchemy/Django) and regex named-group references, computes DSL impact. |
| `duplicate.py` | Duplicate-code detection: AST canonicalisation, Merkle hashing, sibling-sequence winnowing. Backs `analyze dupes`, lint, and MCP. |
| `duplicate_heuristics.py` | Boilerplate-suppression heuristics for `duplicate.py` (abstract stubs, trivial validators, `__init__` self-assignments, dunder boilerplate, …). |
| `rewrite_engine.py` | **Experimental** equality-saturation rewrites: `EGraph`, `ENode`, `RewriteRule`, `run_saturation()`. Loads `.emend/rewrites.yaml`. CLI: `saturate` (hidden). |
| `union_find.py` | `UnionFind` disjoint-set; used by `rewrite_engine.py` and `duplicate.py`. |
| `type_oracle.py` | Type-inference adapter. `TypeOracle` ABC + `PyreflyAdapter`/`PyrightAdapter`/`TyAdapter` (Python LSPs), `TypeScriptAdapter` (Node Compiler API), `RustAnalyzerAdapter` (LSP). `parse_type_string` parses Python/TS/Rust type syntaxes. `create_type_oracle("auto")` autodetects. Two-tier cache (LRU + SQLite `type_cache` in `parse.db`). |

#### Language plugins

| File | Purpose |
|------|---------|
| `language_registry.py` | Single source of truth for `language ↔ extensions` mapping, populated from `languages/*/config.toml`. `detect_language(path)`, `get_extensions(lang)`, `is_source_file(path)`. |
| `language_plugins.py` | Multi-language plugin protocol: `LanguagePlugin`, `ImportHandler`, `CommentHandler`, `PatternCompiler`. Tree-sitter-backed default implementations consumed by Rust/TypeScript/Python plugins. |
| `python_plugin.py` | Concrete Python `LanguagePlugin` (import handling, noqa parsing, pattern compilation). |
| `presets/` | Framework rule presets as YAML data files (Flask, Django, SQLAlchemy, FastAPI, Express, Axum, Diesel, Next.js, React, sqlx, node-sql, actix-web). Loaded by `trace_presets.get_preset()`. |
| `project_config.py` | Project-level config: `EnvironmentLookupConfig`, loader for `.emend/config.toml` / `pyproject.toml [tool.emend]` / language defaults. |
| `file_collection.py` | Cached project file discovery: `collect_source_files_scandir()`, `detect_project_languages()`. Single code path shared by CLI, lint, duplicates, and MCP. |

#### Editor / agent integration

| File | Purpose |
|------|---------|
| `editor_search.py` | Editor integration: `EditorSearchEngine`, FTS5 trigram index, JSON-RPC server (`run_editor_server`), scoring, partial-pattern normalisation. **3.2k lines.** |
| `mcp/` | MCP (Model Context Protocol) server package. Exposes emend's commands as MCP tools over stdio or SSE; uses `FastMCP`. Optional `mcp` extra. Submodules: `dispatch.py` (`FastMCP` app, profile/schema/run infrastructure), `find.py` (`search` tool), `edit.py` (`transform` tool), `analyze.py` (`references` + `analyze` tools), `checks.py` (`check` tool), `tooling.py` (`facts_query` + `mappings` + `grammar_and_cookbook` tools). |
| `mcp_server.py` | Backward-compat shim (~50 lines) re-exporting from the `mcp/` package. Import from `emend.mcp` directly in new code. |
| `knowledge.py` | Identifier and module mapping store: `MappingStore` (YAML-backed at `.emend/mappings.yaml`), repo checkout helpers, MCP/RPC integration. |

#### Grammars

| File | Purpose |
|------|---------|
| `grammars/selector.lark` | Lark grammar for selector syntax. |
| `grammars/pattern.lark` | Lark grammar for pattern syntax. |
| `grammar_and_cookbook.rst` | Selector/pattern grammar docs and worked-example cookbook. |

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

There are ~120 test files. Most map 1:1 to a command (`test_<command>.py`)
or a module (`test_<module>.py`). Use `ls tests/test_emend/` for the full
list. Conventions:

- `test_<command>.py` — the public CLI behaviour for that command. Start here when changing a command.
- `test_<module>.py` — direct unit tests for `src/emend/<module>.py`.
- `test_<command|module>_<lang>.py` (e.g. `test_lint_typescript.py`, `test_cfg_rust.py`) — language-specific variants. Add one of these when extending a feature beyond Python.
- `test_phaseN*.py` — historical regression suites pinning behaviour from the completed trace/Datalog/CFG roadmaps (the roadmap docs themselves were deleted; see git history). Don't add new tests here; pick a topical name.
- `test_regressions.py`, `test_bug_report_regressions.py` — catch-all bug-report regressions.
- `test_cli_surface_consolidation.py` — pins the visible CLI surface (commands, hidden aliases, help text). Touched when CLI structure changes.
- `test_phase17_remove_legacy_intra.py`, `test_phase12_cutover.py`, … — verify legacy code paths stay deleted; keep them red if a removal regresses.

Notable suites worth knowing:

| Test File | Tests For |
|-----------|-----------|
| `test_fact_graph.py` | `FactGraph` Datalog query layer end-to-end (refs / callers / callees / graph / dead code / unreachable blocks / trace propagation / interprocedural trace / flow rule checks). |
| `test_incremental_facts.py` | `FactGraph.update_files()` / `remove_files()` incremental updates and parity with full rebuild. |
| `test_effect_predicates.py`, `test_path_sensitive_sanitization.py`, `test_scope_boundaries.py` | Trace-CFG effects, path sensitivity, and scope-bounded sanitisers. |
| `test_datalog_trace.py`, `test_interprocedural_trace.py` | Datalog trace and interprocedural fixed-point. |
| `test_location_resolver.py` | Pattern match → CFG location resolution. |
| `test_dsl.py`, `test_dsl_treesitter.py` | DSL detection, ORM link resolution, regex named groups, tree-sitter grammar integration. |
| `test_type_oracle.py`, `test_typeoracle_integration.py` | Type oracle adapters and `:type[X]` / `:returns[X]` pattern constraints. |
| `test_vim_rpc.py` | Vim plugin JSON-RPC protocol. |
| `test_mcp_server.py` | MCP server tool wrappers. |
| `test_cli_surface_consolidation.py` | Snapshot of registered commands and their visibility. |

## Commands

The CLI is structured as four Typer subapps plus a handful of hidden
top-level aliases for muscle memory. The canonical invocation is
`emend <subapp> <command>`; the hidden aliases (`emend grep ...`,
`emend rm ...`, …) call the same functions.

### `emend find` (and aliases `grep`, `search`, `show`, `get`, `lookup`, `ls`)

Unified search. Auto-detects pattern mode (if `$` in query), symbol-lookup
mode, or summary mode (bare file/dir). Flags: `--output={code|location|selector|summary|metadata}`,
`--flat`, `--tree-depth`, `--imported-from`, `--scope-local`, `--matching`,
`--type-engine`, `--include-map`.

### `emend edit <command>`

Modify code. All commands are dry-run by default and support `--apply`.
Hidden top-level aliases exist for most of these.

| Command | Description |
|---|---|
| `set` | Replace a symbol component (e.g. body, decorators, parameters). |
| `rm` (alias `remove`, `delete`) | Remove a symbol or component. |
| `delete` | Safe delete with optional `--cascade` for transitive dead-code removal. Without `--cascade`, equivalent to `rm`. |
| `add` (alias `insert`) | Insert into list components (parameters, decorators, bases, body). |
| `replace` | Pattern-based replacement; `--in` accepts selectors. |
| `cp` (alias `copy`, `copy-to`) | Copy a symbol to another file. |
| `rename` | Rename symbol or module (auto-detects by `::` in selector). Flags: `--docs`, `--no-hierarchy`, `--unsure`. |
| `mv` (alias `move`) | Move a symbol or module, rewriting imports. |
| `batch` | Apply YAML/JSON operation files. |
| `saturate` | **Experimental** equality-saturation rewrites from `.emend/rewrites.yaml`. |

### `emend analyze <command>`

Read-only analysis.

| Command | Description |
|---|---|
| `refs` (alias `references`) | Find references. Flags: `--writes-only`, `--reads-only`, `--calls-only`. |
| `graph` | Call graph in plain / JSON / DOT format. |
| `deadcode` (alias `dead-code`) | Find unreferenced code. Flags: `--kind`, `--include-private`, `--exclude-references-from`, `--no-strings`, `--no-last-reference`, `--all-files`, `--entry-point-decorator`, `--entry-point-name`, `--exclude-path`. |
| `impact` | Reverse-caller closure for a diff or symbol set. Flags: `--diff`, `--output={symbols|tests|graph}`, `--max-depth`. |
| `types` | Inferred types per symbol. Flags: `--name`, `--kind`, `--definitions-only`, `--engine={pyrefly|pyright|ty|typescript|rust-analyzer|auto}`. |
| `trace` | Source-to-sink flow analysis. Flags: `--config`, `--label`, `--trace`, `--interprocedural`, `--max-iterations`, `--preset`. |
| `facts` | Query the FactGraph. Flags: `--type={symbols|calls|references|trace_flows|types|imports}`, `--name`, `--kind`, `--file`, `--symbol`, `--label`, `--transitive`. |
| `cfg` | Per-function CFG. Flags: `--function`, `--format={text|json|dot}`, `--unreachable`. |
| `dsl-debug` | Inspect detected DSL regions, symbols, links, impact. Flags: `--type sql`, `--orm={sqlalchemy|django}`, `--resolve`. |
| `dupes` | Duplicate-code clusters with boilerplate suppression. |

### Top-level checks

| Command | Description |
|---|---|
| `lint` | Pattern + flow + deadcode + duplicate-code rules from `.emend/rules.yaml`. `--fix` auto-applies attached `replace` patterns. |
| `policy` | Declarative policies from `.emend/rules.yaml`: flow, structural, type, deadcode, datalog, custom, sequence checks. |
| `check` | Unified runner that dispatches to both. Preferred for CI. |

### `emend map <command>`

Identifier and module mappings stored in `.emend/mappings.yaml`.
Subcommands: `add`, `add-module`, `lookup`, `search`, `resolve`, `rm`,
`rm-module`, `list-modules`, `update-module`.

### `emend tool <command>`

| Command | Description |
|---|---|
| `index` | Pre-build parse, QN, and type caches in `parse.db`. Flags: `--jobs`. |
| `editor-search` | One-shot JSON search for editor integration. |
| `editor-server` | Long-running JSON-RPC server over stdio for the Vim plugin. |
| `mcp` | Run the MCP server (`--transport=stdio` default; `--transport=sse --port=N`). |

Common flags across mutating commands: `--apply` / `-a` (write changes;
default is dry-run), `--json` (machine-readable output), `--project`
(operate over a project root rather than a single file).

## Architecture

### Roadmaps

There is no active roadmap. Completed roadmaps (simplify-codebase, modularize, the Datalog/CFG/trace cutover, ts-rust-parity, ast-dedup, import-regex, vim-plugin-improvements) were deleted after completion — retrieve them from git history if needed. The `ideas/*.md` files are exploratory notes (`auto-reindex`, `feature-work`, `mcp-review`, `next-analyses`, `querying-rewrite`, `static-analysis-literature-review`, `symbolic`, `FUTURE_WORK`).

### Design Philosophy: Tree-sitter and Configuration over Language-Specific Logic

**This is a fundamental principle of emend's design.** Although Python is the primary language emend is written and tested with, language-specific code must be avoided wherever possible. Prefer:

1. **Tree-sitter via the Rust `emend_core` extension** — Use `PyScopeResolver`, `find_pattern_in_files`, `PyFileTransform`, and other Rust APIs for source analysis and transformation. These work across all supported languages via the same code paths.
2. **Language configuration in `languages/<lang>/config.toml`** — Node types (e.g. `string`, `function_definition`), import syntax, comment prefixes, and other language-specific details belong in TOML configuration, not hard-coded in Python.
3. **The `ImportHandler` / language plugin protocol** — When language-specific behavior is unavoidable, it goes in the plugin interface (`language_plugins.py`), not inline in transform logic.

**Do not** use Python's `ast` module, hand-rolled regexes for parsing source code, or other Python-specific approaches when a tree-sitter-based solution exists or can be built. For example:
- To find string literals in source code, use tree-sitter pattern matching (`{type: "string", value: null}`), not a regex.
- To detect references to a symbol, use `PyScopeResolver.references_in_file()`, not a regex over source lines.
- To get statement boundaries, use `get_statement_ranges()` or tree-sitter node spans, not Python's `ast.parse()`.
- To find function calls and extract arguments, use `find_pattern("$F($ARGS)", file)` or tree-sitter call-node queries, not `re.finditer(r"\b(\w+)\s*\(([^)]*)\)", ...)`.

**Regexes in analysis code are a big code smell** and almost always indicate that the tree-sitter/config.toml design is being violated. Regexes over source code are fragile — they break on nested parentheses, string literals, comments, multi-line expressions, and countless other edge cases that a real parser handles correctly. They should almost always be avoided. If you find yourself writing `re.match(...)` or `re.finditer(...)` to extract structure from source code (call sites, assignments, function signatures, etc.), stop and use tree-sitter instead. The `find_pattern()` API with `$METAVAR` captures provides a clean, language-agnostic way to match structural patterns. Language differences should be encoded as **data** in `config.toml` files and consumed by language-agnostic code. When a Python-only implementation exists and needs to be extended to TypeScript/Rust, the right approach is to **refactor the Python implementation to be language-agnostic** (driven by config data), not to write a parallel implementation for each language. The Datalog/FactGraph layer is intentionally language-independent — only fact *population* changes per language, never the queries or analysis logic.

If the Rust extension lacks a needed capability, extend it rather than working around the gap with Python-specific code.

### Tree-sitter + Rust Backend

All source analysis uses the Rust `emend_core` extension (PyO3/maturin) built on tree-sitter:
- `ast_utils.py` — uses `emend_core.collect_symbols_from_str()`
- `query.py` — uses `PyScopeResolver` for symbol collection and filtering
- `ast_commands.py` — uses `emend_core` for symbol collection with rich metadata
- Supported languages: Python, TypeScript/TSX/JS/JSX, Rust, HTML, CSS, SQL, Jinja2
- Language configs in `languages/{python,typescript,rust,html,css,sql,jinja2}/config.toml`

### Cross-Project Operations

Cross-project functions use `visit_project_ts()` in `transform/project_iter.py`, which iterates project files with parallel read + pre-filtering via the Rust extension:

- `find_references()` — uses `PyScopeResolver.references_in_file()` for scope-aware reference finding
- `rename_symbol()` — uses scope resolver + byte-range edits via `PyFileTransform`
- `move_module()` / `rename_module()` — import rewriting + filesystem operations
- `find_callers()` — uses `references_in_file()` filtered to `kind == "call"`
- `find_callees()` — uses `references_in_file()` + `find_nested_definitions()`
- `generate_graph()` — builds call graph from callers/callees analysis

### Rules and policies

Lint rules and policies live in a single document: `.emend/rules.yaml`
(loader: `checks/rules_config.load_rules_document`). The legacy
`.emend/patterns.yaml` and `.emend/policies.yaml` fallbacks were removed
in roadmap-modularize Phase 4; only `rules.yaml` is accepted.

A rules document mixes the following top-level keys:

- `macros` — named reusable pattern fragments.
- `rules` — pattern lint rules: `find` + optional `not-inside` / `message` / `replace`. `--fix` applies `replace`.
- Flow rules: `flows-from` + `flows-to` + optional `not-through`, executed via `flow_ir.execute_flow_spec` (Datalog-first, falls back to the Python tracker).
- `deadcode` — `DeadCodeConfig`: `entry-point-decorators`, `entry-point-names`, `exclude-paths` (globs).
- `duplicate-code` — `DuplicateCodeConfig` for duplicate detection thresholds (`duplicate` accepted as a legacy alias).
- `policies` — declarative policy checks (flow / structural / type / datalog / custom / sequence / deadcode), each runnable independently.
- `trace` — `labels`, `sources`, `sinks`, `sanitizers`, `scope_sanitizers`, optional `presets:` list to compose framework rules from `src/emend/presets/*.yaml`.

`checks/engine.py` is the unified entry point that loads the document once and
dispatches to lint and policy engines, normalising results into
`CheckViolation`. Prefer `emend check` in CI; `emend lint` and `emend
policy` filter to subsets of the kinds.

### Trace analysis

`trace.py` is Datalog-backed (the Python intraprocedural engine was
removed in the trace roadmap). The
public surface:

- `run_trace_analysis(files, config)` — intraprocedural; iterates files, collects function defs, analyses each plus module-level code via `_run_trace_datalog()`.
- `run_interprocedural_trace_analysis(files, config, max_iterations=…)` — cross-function with fixed-point iteration over `FunctionSummary` records.
- `format_violations(violations, *, format="text"|"json", traces=False)` — output formatting with optional propagation witnesses.

Path-sensitivity, per-variable sanitisation, scope-bounded sanitisers
(e.g. `session.commit()` killing taint within a request scope), and
`writes($X)` / `reads($X)` effect predicates are all expressed as
relations on the `FactGraph` and resolved via Datalog. Adding a new
trace feature usually means: add a fact kind in `fact_graph.py`, a
populator, and a Datalog rule — no Python control-flow code.

### Impact analysis

`find_impact()` in `transform/impact.py`:

- BFS transitive reverse-caller closure via `find_callers()` over the FactGraph.
- `_parse_diff_to_selectors()` maps git diff hunks to symbol selectors using line ranges from `SourceLocFact`.
- Test-file / test-symbol heuristics surface affected tests separately.
- Witness edges record why each symbol is impacted (which caller pulled it in).

### Type oracle

`type_oracle.py` provides `TypeOracle` (abstract) plus adapters:

- `PyreflyAdapter` runs `pyrefly check --debug-info` and parses the JSON binding dump.
- `PyrightAdapter` / `TyAdapter` use the language-server protocol (`pyright-langserver`, `ty lsp`).
- `TypeScriptAdapter` shells to a Node.js helper that drives the TS Compiler API in batch.
- `RustAnalyzerAdapter` is LSP-based.
- `create_type_oracle(engine="auto")` autodetects via config files (`pyrightconfig.json`, `ty.toml`, `pyrefly.toml`, `pyproject.toml [tool.X]`) and PATH.

Pattern constraints `:type[X]` and `:returns[X]` are parsed by the
`ORACLE_TYPE_CONSTRAINT` grammar terminal and post-filtered by
`_filter_matches_by_type_oracle()` in `transform.py`. Lookup falls back
to `_filter_by_returns_with_oracle()` in `query.py` when no annotation
is present.

Two-tier cache: in-memory LRU + SQLite `type_cache` table in
`.emend/cache/parse.db`, keyed by file content hash.

### Dead code detection

`find_dead_code()` in `transform/deadcode.py`:

- Single-pass O(files) analysis via `PyScopeResolver`.
- `_find_source_root()` detects `src/` layout via `pyproject.toml`.
- Entry-point heuristics skip decorated symbols, dunders, tests, and `__all__` members.
- Configurable `entry-point-decorators`, `entry-point-names`, `exclude-paths` (globs) via `.emend/rules.yaml` or CLI flags.
- String-literal scanning for dynamic references (`getattr`, serialisation).
- `git log -S` integration for last-reference tracking.
- `# noqa: emend:deadcode` inline suppression.

### Environment path lookup

`project_config.py` configures lookup of installed dependency sources:

- **Python** — `.venv` / `venv` site-packages.
- **TypeScript / JavaScript** — `node_modules`.
- **Rust** — `target/`.

Enabled via `[environment_lookup]` in `.emend/config.toml`,
`pyproject.toml [tool.emend]`, or per-language defaults. Cached per
language in `parse.db` (`environment_cache` table), keyed by
environment mtime. Used as a fallback in `query_symbol_index()` and
`EditorSearchEngine` when a symbol is not found in the project index.

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

1. Implement the engine function in the appropriate `transform/` submodule (for tree-sitter-based operations) or `ast_commands.py` (for symbol listing). Read-only analysis usually has a counterpart in `fact_graph.py` if it needs Datalog.
2. Add the Typer command function to the appropriate `cli_*.py` file:
   - Editing operations → `cli_edit.py`.
   - Read-only analysis → `cli_analysis.py`.
   - Lint/policy/check → `cli_checks.py`.
   - Tooling (indexes, servers, MCP) → `cli_tooling.py`.
   - Search/lookup → `cli_find.py`.
   - Map operations → `cli_map.py`.
3. Wire it into `cli.py` under the right subapp (`edit_app`, `analyze_app`, `tool_app`, `map_app`) and add any hidden top-level aliases there.
4. If the command should be exposed to LLM clients, add an MCP tool wrapper in the matching `mcp/` submodule (`mcp/dispatch.py` holds the `FastMCP` app and registration).
5. Add tests in `tests/test_emend/test_<command>.py`. For language-specific behaviour, add a `test_<command>_<lang>.py` companion.

> Note: `cli.py` is the canonical (single-source) registration site. Steps 2 and 3 above are kept for clarity but both point to the same place.

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

<roadmaps-and-phasing>
<layout>
Projects in progress are put in the ideas/roadmap/ folder with an index.md with `- [ ] TODOs` for each phase and more detailed instructions in linked markdown files also in the roadmap directory.

Completed roadmaps are deleted (they remain retrievable from git history), but only after the user confirms the deletion.
</layout>

<instructions>
"Implement the next phase." refers to referencing that index.md file, finding the next phase that was not implemented, implementing it, then checking off finished TODOs.

ALWAYS check off TODOs and phases when you complete them. This can waste time in later sessions duplicating work and sow confusion.

When you finish a phase, you should always let the user know if there are any unfinished tasks from earlier phases, including xfailed tests.

You should only xfail tests if the user tells you to. It's preferable to leave red/failing tests so that the CI build demonstrates the current project status.
</instructions>

<planning>
When asked to create a roadmap for something, use that directory structure.
Put it in ideas/roadmap-project-name if there is an active roadmap rn.
</planning>

</roadmaps-and-phasing>

## Environment notes

Do not assume the dependencies will be installed on the active python installation. The venv must be built to include the compiled rust emend_core. THAT LIBRARY IS _REQUIRED_ for `emend` to function. DO NOT hack around its absence, which indicates you are not working in the correct environment. Try `make clean test` to build a functional .venv and run the test suite.

Use `uv` commands for everything. If you do need to manually install a dependency, use `uv pip install` rather than `.venv/bin/pip install`.
