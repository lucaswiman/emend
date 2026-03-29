# Roadmap

**When you complete a task, check off its checkbox.**

---

## Completed

- [x] Phase 1: Impact analysis — `transform.py`, `emend impact`
- [x] Phase 2: Intraprocedural taint — `taint.py`, `emend taint`
- [x] Phase 3: Compliance layer — **Won't do separately**; taint labels cover this
- [x] Phase 4: Stable fact schema — `fact_graph.py`, `emend facts`
- [x] Phase 5: Interprocedural taint — `taint.py`, `emend taint --interprocedural`
- [x] Phase 6: MCP query interface — `mcp_server.py`, `emend mcp`
- [x] Phase 7: Rewrite/equality-saturation experiment — `rewrite_engine.py`, `emend saturate`
- [x] Phase 8: Expert-mode policy/query surfaces — `policy.py`, `emend policy`, `emend query`

---

## Taint Precision Improvements

Spec: [taint-analysis.md](taint-analysis.md)

- [x] Field sensitivity — treat `obj.field` as distinct from `obj` — `taint.py` (`_extract_qualified_identifiers`, dotted assignment detection)
- [ ] Object-sensitive dispatch — resolve `obj.method()` by receiver type, not just name
- [x] High-precision container modeling — track taint through list/dict elements — `taint.py` (`_find_container_mutations`, `_find_for_loops`, subscript-read propagation)
- [x] Framework-specific source/sink/sanitizer rules — Django, Flask, SQLAlchemy, FastAPI — `taint_presets.py`, `emend taint --preset`, YAML `presets:` key

---

## DSL Support for Embedded Languages

Spec: [dsl-support.md](dsl-support.md)

### Current state

Full DSL infrastructure in `dsl.py`: SQL region detection (keyword
heuristics, magic comments), table/column extraction via regex, ORM link
resolution (singularize+PascalCase, `__tablename__` matching), DSL-region
find with `$METAVAR` support (`find_in_dsl()`), regex named group
navigation, and impact analysis integration.  The `emend dsl` command is
hidden (renamed to `dsl-debug`); DSL support is transparently integrated
into `search`, `refs`, `lint`, `impact`, and `editor-server`.  The
`dsl_symbols` and `dsl_links` tables in `parse.db` are populated during
`emend index`.

### Next steps (design fix)

- [x] **Remove the standalone `emend dsl` command** — renamed to `emend dsl-debug` (hidden); `emend dsl` kept as hidden alias
- [x] **Wire DSL symbols into `search`** — `emend search User --include-dsl` finds both `class User` and SQL `FROM users`
- [x] **Wire DSL symbols into `refs`** — `emend refs models.py::User --include-dsl` surfaces SQL table references via ORM link resolution
- [x] **Wire into editor-server** — `dsl_goto_definition` JSON-RPC method resolves cursor in SQL string to ORM model class

### Phase 1: Infrastructure

- [x] Add tree-sitter grammars for SQL, HTML, CSS, Jinja to `emend_core` — `tree-sitter-html`, `tree-sitter-css`, `tree-sitter-sequel` (SQL), `tree-sitter-jinja2`; language configs in `languages/{html,css,sql,jinja2}/config.toml`; parser dispatch in `pattern.rs`; scope resolver support in `scope.rs`; language registry in `language_registry.py`
- [x] Implement injection detection (call-based, magic-comment, SQL keyword heuristics) — `dsl.py` (`detect_dsl_regions`, regex-based; tree-sitter grammars deferred)
- [x] Implement `DslSymbolExtractor` for SQL (tables, columns) — `dsl.py` (`extract_sql_symbols`; CSS/JSX deferred)
- [x] Add `dsl_symbols` table to `parse.db`; wire into `emend index` — `_init_cache_schema()` creates `dsl_symbols` table; `_index_batch()` extracts and stores DSL symbols during indexing

### Phase 2: Link resolution + navigation

- [x] Implement `DslLinkResolver` with strategies: `orm_model`, `orm_column` — `dsl.py` (`resolve_orm_links`; `component_export`, `css_class_usage` deferred)
- [x] Add `dsl_links` table and populate during indexing — `_init_cache_schema()` creates `dsl_links` table with indexes on `target_qn` and `content_hash`
- [x] Wire `--include-dsl` into `search` and `refs` — DSL symbols always included in `search` and `refs` output (cli.py DSL overlay sections)
- [x] Add `dsl_goto_definition` to `editor-server` — `_goto_dsl_fallback()` in `goto_definition` (editor_search.py)

### Phase 3: Pattern matching in DSL regions

- [x] Extend pattern grammar with `--dsl` mode for DSL-specific node types — `search --dsl sql` searches inside DSL regions; `find_in_dsl()` in `dsl.py` with `$METAVAR` support
- [x] Add DSL-aware lint rules to lint engine — `LintRule.dsl` field; `_compile_dsl_pattern()` in `lint.py`; rules with `dsl: sql` in YAML matched against embedded SQL regions
- [x] Implement `find`/`replace` inside DSL regions — `find_in_dsl()` in `dsl.py`; `search --dsl sql 'SELECT $COLS FROM $TABLE'` with metavar captures

### Phase 4: Tier 2 DSLs + deeper integration

- [x] Jinja2/Django template support: variable resolution, block inheritance — `dsl.py` (`extract_jinja_symbols`, `resolve_jinja_links`, `_detect_jinja_regions`), standalone `.html`/`.jinja2`/`.j2` files + embedded Python strings
- [x] GraphQL support: schema-to-resolver linking, query-to-type navigation — `dsl.py` (`extract_graphql_symbols`, `resolve_graphql_links`, `_detect_graphql_regions`), standalone `.graphql`/`.gql` files + embedded Python strings
- [x] Regex named group navigation: `(?P<name>...)` → `.group("name")` call sites — `extract_regex_named_groups()` and `find_regex_group_references()` in `dsl.py`
- [x] `impact` command integration: ORM model changes surface affected SQL queries and JSX call sites — `find_dsl_impact()` in `dsl.py`; `emend impact` outputs `dsl_impacts` in JSON and text modes

---

## Datalog-First Analysis

Spec: [unified-deadcode-datalog.md](unified-deadcode-datalog.md)

Move reasoning and inference to Datalog over the fact graph. Pattern
matching, code transformation, and heuristic filtering (e.g. string
literal dead code suppression) stay in Python/Rust. The schema drops
absolute line numbers from the relational core (moved to `source_loc`
for display) and tags references/calls with their containing CFG block
for exact joins.

### Phase 1: Schema and CFG population

- [ ] Add `cfg_block` relation (`file_path`, `func_qn`, `block_id`, `is_entry`, `is_exit`)
- [ ] Add `decorator_on` relation (`symbol_qn`, `decorator`)
- [ ] Populate `cfg_block` and `cfg_edge` in `build_from_project()`
- [ ] Add `source_loc` relation; move all display positions there

### Phase 2: Block-tagged references

- [ ] Assign `(func_qn, block_id)` to each reference via byte-offset intersection with CFG blocks
- [ ] Same for `call` facts
- [ ] Populate `def_use` from CFG builder's block defs/uses (block IDs, not lines)

### Phase 3: Direct relation queries — `refs`, `callers`, `callees`, `graph`

- [ ] `refs` → Datalog query on `reference` (replace `find_references()` file traversal)
- [ ] `callers` → Datalog query on `call` (replace `find_callers()` file traversal)
- [ ] `callees` → Datalog query on `call` scoped by `func_qn` (replace line-range filtering)
- [ ] `graph` → Datalog query on `call` + Python formatting (replace Rust `collect_callees`)
- [ ] Remove Python traversal code from `transform.py` for these commands

### Phase 4: Unified dead code

- [ ] Implement reachable-block closure + live-reference Datalog query
- [ ] Port entry point heuristics (dunders, decorators, `__all__`, tests) to Datalog rules
- [ ] Wire into `emend deadcode` as default backend (string literal filtering stays as Python post-filter)
- [ ] Switch `cfg --unreachable` to query the fact graph
- [ ] Remove `find_dead_code()` from `transform.py` and `find_unreachable_blocks()` from `cfg.py`

### Phase 5: Taint migration

- [ ] Add `func_summary` relation (param → return/sink flow)
- [ ] Rewrite intraprocedural taint propagation as Datalog over `def_use` (pattern matching stays in Python)
- [ ] Rewrite interprocedural fixed-point as recursive Datalog (replaces Python loop)
- [ ] Migrate flow-based lint rules (`flows-from`/`flows-to`/`not-through`) to same propagation
- [ ] Remove Python taint simulation and fixed-point iteration

### Phase 6: Cleanup

- [ ] Enforce fact-graph-only path for `impact` (remove non-Datalog fallback)
- [ ] Evaluate consolidating `parse.db` (SQLite) and `facts.db` (CozoDB)
- [ ] Update all tests

---

## Reference Documents

- [taint-analysis.md](taint-analysis.md) — deferred taint precision work
- [dsl-support.md](dsl-support.md) — DSL support full spec
- [query-language-for-code-invariants.md](query-language-for-code-invariants.md) — ongoing witness-quality requirement
- [rewrite-and-saturation.md](rewrite-and-saturation.md) — open design questions for the experimental rewrite engine
- [backend-options.md](backend-options.md) — architecture rationale (CozoDB vs egglog)
- [relation-to-existing-tools.md](relation-to-existing-tools.md) — positioning vs Semgrep, CodeQL, Pysa
- [unified-deadcode-datalog.md](unified-deadcode-datalog.md) — unified dead code via Datalog
- [open-questions.md](open-questions.md) — ongoing design trade-offs
