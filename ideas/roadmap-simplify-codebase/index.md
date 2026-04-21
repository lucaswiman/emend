# Codebase simplification roadmap

Goal: reduce total maintained Python lines without changing user-visible behavior. Source audit identified ~3,400–3,700 candidate lines across `src/emend/` (~39.5k LOC).

## Phases

- [x] **Phase A — zero/near-zero risk**
  - [x] A1. Strip `@mcp_app.tool()` decorators from 15 dead MCP tools in `mcp_server.py` (-13 lines; bodies kept because the dispatcher tools and tests still call them as plain Python helpers — see [reduced-savings.md](reduced-savings.md))
  - [x] A2. Extract `cli_error_handler` context manager + inline `_is_source_file_query` (-113 lines across cli_base/cli_edit/cli_analysis/cli_find)
  - [x] A3. Drop unused `tempfile` import + `TraceDatalogConfig` dataclass in `fact_graph.py` (rolled into B2 below)

- [x] **Phase B — refactors covered by tests**
  - [x] B1. Convert `trace_presets.py` from hardcoded Python rules to YAML data files in `src/emend/presets/` (-652 Python lines; 478 lines of YAML + loader added; round-trip verified)
  - [x] B2. Factor `fact_graph.py` `_all_*`/`add_Xs_batch` boilerplate (-51 lines; B2.3 Datalog-query helper skipped — methods diverge too much for clean extraction, see notes)
  - [x] B3. Replace `dsl.py` regex-based ORM resolution with `PyScopeResolver` + `collect_string_literals` (+23 lines net but design-philosophy-compliant; `_CLASS_DEF_RE` retained because `resolve_graphql_links` still uses it)
  - [x] B4. Inline `_extract_root_name` and `prefilter_files_structural` thin wrappers in `transform.py` (-9 lines; `ast.literal_eval`/`ast.parse`/JSONC regex left in place because `emend_core` lacks equivalents — see open question 6)

**Realized total: ~-815 net Python LOC** (1,159 deletions / 344 insertions across 9 files), plus 478 lines of YAML data. Lower than the audit's ~2,400-line estimate for Phases A+B because:
- A1's bodies are reused by the dispatcher tools (only decorators were removable)
- B2's Datalog query strings *are* most of the LOC; helper extraction adds boilerplate that offsets savings
- B3 swapping regex for tree-sitter requires more structural code than the regex it replaces

The pre-implementation audit numbers should be treated as upper bounds.

---

## Phase C — decisions recorded (2026-04-21)

- [ ] C1. `rewrite_engine.py` — **decision: keep, mark experimental; extract `UnionFind` to its own module.** Module is 643 lines with 23 real tests, zero xfails, recent bug-fix activity (commits #152, #164). Hidden CLI (`saturate`). Only external dep is `duplicate.py` using `UnionFind`. Action: extract `UnionFind` to `src/emend/union_find.py`; add experimental docstring to `rewrite_engine.py` and the `saturate` command.
- [x] ~~C2. Merge `flow_ir.py` adapter layer~~ — **marked invalid.** Not pure indirection: 422 lines = ~30% converters + a 188-line `_execute_via_datalog()` that resolves source/sink/sanitizer patterns via `LocationResolver`, builds Datalog tuples, and falls back to the Python engine on failure. Collapsing would duplicate the bridge in `lint.py` and `policy.py`. 12 dedicated tests in `test_flow_ir.py` directly exercise the IR.
- [ ] C3. Unify `DeadCodeCheck` / `DeadCodeConfig` — **decision: unify.** Both are config dataclasses that feed the same `find_dead_code()` in `transform.py:6544`. Pure config-level duplication. Flow-rule engines (lint's `_check_flow_rule` vs `flow_ir.execute_flow_spec`) stay separate — they take genuinely different paths (lint drives `--fix`; policy drives Datalog witness traces).
- [x] ~~C4. `language_plugins.py` plugin protocol~~ — **marked invalid.** Protocol is NOT Python-only: `languages/rust/plugin.py` and `languages/typescript/plugin.py` actively use `TreeSitterImportHandler`, `DocCommentHandler`, `TreeSitterPatternCompiler` stubs. Inlining would only remove `python_plugin.py` (288 lines) while leaving the 716-line `language_plugins.py` intact because 60% is reusable multi-language stubs.
- [x] ~~C5. Extract interprocedural trace~~ — **deferred (no decision requested this session).**

## Phase D — `CommonFlags` Typer dataclass (deferred from A2)

- [ ] D1. Consolidate `--apply` / `--json` / `--output` into a shared `CommonFlags` dataclass across CLI modules (~50)
  - Risk: medium — touches 15+ command signatures.

## Phase E — Rust extension capability gaps (blocks further design-philosophy cleanup)

Phase B4 had to leave several `ast`/regex usages in `transform.py` because `emend_core` doesn't yet expose the needed APIs.

- [ ] E1. `emend_core.parse_string_literal(text, ext) -> str` — unescape a Python/JSON/etc. string-literal source fragment to its value. Half already exists (`rust/src/pattern.rs:572 extract_string_content` — private). Just needs PyO3 wrapper. **Approved this session.**
- [ ] E2. `emend_core.validate_syntax(code, ext, *, mode='expression'|'statement')` — currently called optimistically but the function isn't actually exposed. Needed to delete `_is_valid_replacement`'s `ast.parse` fallback. **Approved this session.**
- [x] ~~E3. JSONC parsing~~ — **skipped.** 100-150 Rust lines + new tree-sitter-json grammar dep to save ~6 Python lines; net negative. Current regex stripping stays.

Each of E1/E2 unlocks 15–25 additional Python lines for deletion.

---

## Open questions — resolved 2026-04-21

1. **`rewrite_engine.py`** → (b) keep, mark experimental; extract `UnionFind` to its own module.
2. **`flow_ir.py`** → marked invalid (not pure indirection; holds 188-line Datalog/Python bridge).
3. **`policy.py` vs `lint.py`** → unify `DeadCodeCheck`/`DeadCodeConfig` only; flow-rule engines stay separate.
4. **`language_plugins.py`** → marked invalid (protocol is actively multi-language via Rust/TypeScript plugins).
5. **Datalog query helper** → skipped (~4 net-line saving; hurts Datalog query readability).
6. **Rust extensions (Phase E)** → do E1 (`parse_string_literal`) and E2 (`validate_syntax`); skip E3 (JSONC) — 100-150 Rust lines for 6 Python lines is net negative.
7. **`_CLASS_DEF_RE`** → defer full GraphQL→tree-sitter conversion; delete dead `_RESOLVER_CLASS_RE` at `dsl.py:148`.

## Files of interest after Phases A+B

| File | Before | After | Δ |
|---|---|---|---|
| `mcp_server.py` | 1952 | 1939 | −13 |
| `cli_analysis.py` | 1105 | 1078 | −27 |
| `cli_edit.py` | 919 | 858 | −61 |
| `cli_find.py` | 767 | 760 | −7 |
| `cli_base.py` | 299 | 281 | −18 |
| `dsl.py` | 1547 | 1570 | +23 |
| `fact_graph.py` | 4477 | 4426 | −51 |
| `trace_presets.py` | 790 | 138 | −652 |
| `transform.py` | 8474 | 8465 | −9 |
| **TOTAL** | **20,330** | **19,515** | **−815** |
| New: `src/emend/presets/*.yaml` | — | 478 lines (data) | — |
