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

The core infrastructure exists in `dsl.py`: SQL region detection (keyword
heuristics, magic comments), table/column extraction via regex, and ORM link
resolution (singularize+PascalCase, `__tablename__` matching).  There is a
standalone `emend dsl` command wired in `cli.py`, but this is **wrong** — DSL
support should be transparent infrastructure integrated into existing commands,
not a separate user-facing command.

### Next steps (design fix)

- [x] **Remove the standalone `emend dsl` command** — renamed to `emend dsl-debug` (hidden); `emend dsl` kept as hidden alias
- [x] **Wire DSL symbols into `search`** — `emend search User --include-dsl` finds both `class User` and SQL `FROM users`
- [x] **Wire DSL symbols into `refs`** — `emend refs models.py::User --include-dsl` surfaces SQL table references via ORM link resolution
- [x] **Wire into editor-server** — `dsl_goto_definition` JSON-RPC method resolves cursor in SQL string to ORM model class

### Phase 1: Infrastructure

- [ ] Add tree-sitter grammars for SQL, HTML, CSS, Jinja to `emend_core`
- [x] Implement injection detection (call-based, magic-comment, SQL keyword heuristics) — `dsl.py` (`detect_dsl_regions`, regex-based; tree-sitter grammars deferred)
- [x] Implement `DslSymbolExtractor` for SQL (tables, columns) — `dsl.py` (`extract_sql_symbols`; CSS/JSX deferred)
- [ ] Add `dsl_symbols` table to `parse.db`; wire into `emend index`

### Phase 2: Link resolution + navigation

- [x] Implement `DslLinkResolver` with strategies: `orm_model`, `orm_column` — `dsl.py` (`resolve_orm_links`; `component_export`, `css_class_usage` deferred)
- [ ] Add `dsl_links` table and populate during indexing
- [ ] Wire `--include-dsl` into `search` and `refs`
- [ ] Add `dsl_goto_definition` to `editor-server`

### Phase 3: Pattern matching in DSL regions

- [ ] Extend pattern grammar with `--dsl` mode for DSL-specific node types
- [ ] Add DSL-aware lint rules to lint engine
- [ ] Implement `find`/`replace` inside DSL regions

### Phase 4: Tier 2 DSLs + deeper integration

- [ ] Jinja2/Django template support: variable resolution, block inheritance
- [ ] GraphQL support: schema-to-resolver linking, query-to-type navigation
- [ ] Regex named group navigation: `(?P<name>...)` → `.group("name")` call sites
- [ ] `impact` command integration: ORM model changes surface affected SQL queries and JSX call sites

---

## Reference Documents

- [taint-analysis.md](taint-analysis.md) — deferred taint precision work
- [dsl-support.md](dsl-support.md) — DSL support full spec
- [query-language-for-code-invariants.md](query-language-for-code-invariants.md) — ongoing witness-quality requirement
- [rewrite-and-saturation.md](rewrite-and-saturation.md) — open design questions for the experimental rewrite engine
- [backend-options.md](backend-options.md) — architecture rationale (CozoDB vs egglog)
- [relation-to-existing-tools.md](relation-to-existing-tools.md) — positioning vs Semgrep, CodeQL, Pysa
- [open-questions.md](open-questions.md) — ongoing design trade-offs
