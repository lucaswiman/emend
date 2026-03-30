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

- [x] Add `cfg_block` relation (`file_path`, `func_qn`, `block_id`, `is_entry`, `is_exit`) — `fact_graph.py` (`CfgBlockFact`, `cfg_block` CozoDB relation)
- [x] Add `decorator_on` relation (`symbol_qn`, `decorator`) — `fact_graph.py` (`DecoratorOnFact`, `decorator_on` CozoDB relation)
- [x] Populate `cfg_block` and `cfg_edge` in `build_from_project()` — `fact_graph.py` (Rust `build_cfgs_for_source` → block/edge facts)
- [x] Add `source_loc` relation; move all display positions there — `fact_graph.py` (`SourceLocFact`, `source_loc` CozoDB relation, populated for symbols)

### Phase 2: Block-tagged references

- [x] Assign `(func_qn, block_id)` to each reference via byte-offset intersection with CFG blocks — `fact_graph.py` (`_find_containing_block()`, `ReferenceFact.func_qn`/`block_id`)
- [x] Same for `call` facts — `fact_graph.py` (`CallFact.func_qn`/`block_id`)
- [x] Populate `def_use` from CFG builder's block defs/uses (block IDs, not lines) — `fact_graph.py` (`DefUseFact.def_block`/`use_block`, populated from `cfg.get_blocks()["defs"]`/`["uses"]`)

### Phase 3: Direct relation queries — `refs`, `callers`, `callees`, `graph`

- [x] `refs` → Datalog query on `reference` (replace `find_references()` file traversal) — `fact_graph.py` (`refs_datalog()`)
- [x] `callers` → Datalog query on `call` (replace `find_callers()` file traversal) — `fact_graph.py` (`callers_datalog()`)
- [x] `callees` → Datalog query on `call` scoped by `func_qn` (replace line-range filtering) — `fact_graph.py` (`callees_datalog()`)
- [x] `graph` → Datalog query on `call` + Python formatting (replace Rust `collect_callees`) — `fact_graph.py` (`graph_datalog()`)
- [x] Remove Python traversal code from `transform.py` for these commands — `find_references()`, `find_callers()`, `find_callees()`, `generate_graph()` now use Datalog via `_get_or_build_fact_graph()`

### Phase 4: Unified dead code

- [x] Implement reachable-block closure + live-reference Datalog query — `fact_graph.py` (`dead_code_unified()`)
- [x] Port entry point heuristics (dunders, decorators, `__all__`, tests) to Datalog rules — `fact_graph.py` (`dead_code_unified()` with `entry_point_decorator`/`entry_point_name` relations)
- [x] Wire into `emend deadcode` as default backend (string literal filtering stays as Python post-filter) — `transform.py` (`find_dead_code()` uses `dead_code_unified()`, old `_dead_code_postfilter`/`_find_dead_code_cozo`/`_find_dead_code_cached` removed)
- [x] Switch `cfg --unreachable` to query the fact graph — `cli.py` (tries `unreachable_blocks_datalog()` first, falls back to per-CFG BFS)
- [x] Remove `find_dead_code()` from `transform.py` and `find_unreachable_blocks()` from `cfg.py` — old dead code helpers removed; `find_unreachable_blocks()` kept as fallback

### Phase 5: Taint migration

- [x] Add `func_summary` relation (param → return/sink flow) — `fact_graph.py` (`FuncSummaryFact`, `func_summary` CozoDB relation)
- [x] Rewrite intraprocedural taint propagation as Datalog over `def_use` (pattern matching stays in Python) — `fact_graph.py` (`taint_propagation_datalog()`)
- [x] Rewrite interprocedural fixed-point as recursive Datalog (replaces Python loop) — `fact_graph.py` (`interprocedural_taint_datalog()`)
- [x] Migrate flow-based lint rules (`flows-from`/`flows-to`/`not-through`) to same propagation — `fact_graph.py` (`flow_rule_check_datalog()`)
- [x] Remove Python taint simulation and fixed-point iteration — `taint.py` (`run_taint_analysis`/`run_interprocedural_taint_analysis` try Datalog first, Python fallback retained); `lint.py` (`_check_flow_rule` tries Datalog when `fact_graph` provided)

### Phase 6: Cleanup

- [x] Enforce fact-graph-only path for `impact` (remove non-Datalog fallback) — `transform.py` (`find_impact()` uses `_find_impact_via_fact_graph()` exclusively, `use_fact_graph` parameter removed)
- [ ] Evaluate consolidating `parse.db` (SQLite) and `facts.db` (CozoDB)
- [x] Update all tests — `test_fact_graph.py` (87 tests), dead code/callers/callees/graph tests updated for Datalog backend

---

## Taint-CFG Precision: Effects, Path Quantifiers, and Temporal Sequences

Spec: [taint-cfg-precision.md](taint-cfg-precision.md)

Five general mechanisms that increase the expressive power of the
taint/CFG/Datalog stack.  Each is independently useful; together they
replace `attribute_mutation_sinks` and other bespoke features.

### Phase 1: Effect Predicates

- [x] Add `kind` field (`read`/`write`/`aug_write`/`del`) to `DefUseFact` — `fact_graph.py`, `emend_core` (`cfg.rs`, `cfg_py.rs`)
- [x] Update `def_use` CozoDB schema; migrate all existing `*def_use[...]` queries (9→10 columns) — `fact_graph.py`
- [x] Add `method_call` relation and schema — `fact_graph.py` (`MethodCallFact`, `add_method_call`, `method_calls`)
- [x] Add augmented assignment regex + `"mutate"` op kind to `_find_assignments_in_source()` — `taint.py` (`_AUG_ASSIGN_RE`)
- [x] Implement `effect` key on sinks/sources (resolves `writes($X)`, `reads($X)`) — `taint.py` (`TaintSink.effect`), `fact_graph.py` (`taint_propagation_datalog(effect_sinks=)`)
- [x] Remove `attribute_mutation_sinks` and Python Step 3.5 loop — `taint.py` (removed in Phase 2; effect sinks replace this)
- [x] Add `is_var_or_attr` helper to all Datalog queries using dotted-name prefix matching — `fact_graph.py` (two-rule expansion in effect violation rules)

### Phase 2: Path-Sensitive Sanitization

- [x] Rewrite `taint_propagation_datalog()` with `unsanitized` CFG-edge reachability — `fact_graph.py`
- [x] Add intra-block line-ordering guard for same-block sanitizer+sink — `fact_graph.py`
- [x] Add `quantifier` field (`all_paths`/`some_path`) to sanitizer config — `taint.py`
- [x] Implement `through` parameter in `flow_rule_check_datalog()` — `fact_graph.py`
- [x] Update Python fallback to per-block taint state — `taint.py`

### Phase 3: Scope Boundaries

- [x] Add `scope_sanitizers` config key — `taint.py` (`TaintScopeSanitizer` dataclass, `TaintConfig.scope_sanitizers`, YAML loading)
- [x] Add `scope_kill` inline relation to Datalog propagation — `fact_graph.py` (`taint_propagation_datalog(scope_kills=)`, `not scope_kill` in `unsanitized` rules)
- [x] Document nested-session limitation (kills all taint for label, not per-session) — `taint.py` docstring, `taint-cfg-precision.md` spec

### Phase 4: Type-Conditioned Filtering

- [x] Add `type_constraint` field to `TaintSource`/`TaintSink`/`TaintSanitizer` — `taint.py`
- [x] Populate `type_binding` relation in `build_from_project()` — `fact_graph.py`
- [x] Evaluate constraints via `parse_type_string()` (top-level constructor, not substring)
- [x] Add `scalar_type` inline relation for Datalog type filtering — `fact_graph.py`

### Phase 5: Temporal Sequence Patterns

- [x] Design `sequence` + `path` config schema — `taint.py` or `policy.py`
- [x] Implement `compile_sequence_rule()` — sequence-to-CozoScript compiler — `fact_graph.py`
- [x] Compose CFG reachability with def-use liveness for binding propagation
- [x] Support dual-mode compilation: `data_flow` steps → `tainted` rules, temporal steps → `reachable` rules
- [x] Add `SequenceCheck` to policy engine — `policy.py`
- [ ] Update TOCTOU example in `commands.rst` with sequence rule form

### Phase 6: Naming Unification and Cleanup — `taint` → `trace`

Rename the "taint" abstraction to **"trace"** across the codebase.  The
engine is a general labeled data-flow tracer — sources emit labeled
values, sinks consume them, sanitizers block propagation — and "taint"
is just one framing (security).  The same engine powers lint flow rules,
policy flow checks, sequence patterns, and effect predicates.  "Trace"
better describes the core action: *follow labeled values through code*.

#### CLI and command renaming

- [ ] Rename `emend taint` → `emend trace` (keep `taint` as hidden alias for backwards compat)
- [ ] Rename `--interprocedural` flag (already applies to `trace`)
- [ ] Rename `emend facts --type taint_flows` → `emend facts --type trace_flows` (alias old name)
- [ ] Update `--preset` help text: "framework-specific trace rules" instead of "taint rules"
- [ ] Update TOCTOU example in `commands.rst` with sequence rule form (existing Phase 5 TODO)

#### Config and YAML renaming

- [ ] Rename `taint:` section in `.emend/patterns.yaml` → `trace:` (accept both, prefer new)
- [ ] Rename `TaintSource` → `TraceSource`, `TaintSink` → `TraceSink`, `TaintSanitizer` → `TraceSanitizer`, `TaintScopeSanitizer` → `TraceScopeSanitizer` in config model
- [ ] Rename `TaintConfig` → `TraceConfig`, `TaintViolation` → `TraceViolation`
- [ ] Keep YAML key aliases: `sources`/`sinks`/`sanitizers` stay the same (only the section name changes)

#### Source file renaming

- [ ] Rename `taint.py` → `trace.py` (update all imports)
- [ ] Rename `taint_presets.py` → `trace_presets.py`
- [ ] Rename `TaintFlowFact` → `TraceFlowFact` in `fact_graph.py`
- [ ] Rename Datalog relations: `taint_flow` → `trace_flow` in CozoDB schema
- [ ] Rename test files: `test_taint.py` → `test_trace.py`, `test_interprocedural_taint.py` → `test_interprocedural_trace.py`, etc.

#### Simplification from code review

- [ ] Extract `_inline_relation(name, cols, rows)` helper for CozoScript query building (used in `taint_propagation_datalog`, `flow_rule_check_datalog`, `_compile_sequence_query`)
- [ ] Introduce `TraceDatalogConfig` dataclass to reduce `taint_propagation_datalog()` from 9 params to 3 (`sources`, `sinks`, `config`)
- [ ] Deduplicate blocker resolution loops in `compile_sequence_rule()` (`not_through` / `not_through_scope` share 55 identical lines)
- [ ] Cache `evaluate_type_constraint()` parsed constraint expressions (currently re-parses on every call)

#### Documentation

- [ ] Update CLAUDE.md file/command tables
- [ ] Update `commands.rst` references
- [ ] Add migration note in CHANGELOG explaining `taint` → `trace` rename with alias support

---

## Reference Documents

- [taint-cfg-precision.md](taint-cfg-precision.md) — taint-CFG precision: mutation tracking, path sensitivity, type filtering

- [taint-analysis.md](taint-analysis.md) — deferred taint precision work
- [dsl-support.md](dsl-support.md) — DSL support full spec
- [query-language-for-code-invariants.md](query-language-for-code-invariants.md) — ongoing witness-quality requirement
- [rewrite-and-saturation.md](rewrite-and-saturation.md) — open design questions for the experimental rewrite engine
- [backend-options.md](backend-options.md) — architecture rationale (CozoDB vs egglog)
- [relation-to-existing-tools.md](relation-to-existing-tools.md) — positioning vs Semgrep, CodeQL, Pysa
- [unified-deadcode-datalog.md](unified-deadcode-datalog.md) — unified dead code via Datalog
- [open-questions.md](open-questions.md) — ongoing design trade-offs
