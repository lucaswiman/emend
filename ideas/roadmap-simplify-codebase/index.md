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

## Phase C — needs design decisions (see Open Questions below)

- [ ] C1. Decide fate of `rewrite_engine.py` (~600)
- [ ] C2. Merge `flow_ir.py` adapter layer into `lint.py` + `policy.py` (~300)
- [ ] C3. Unify `policy.py` ↔ `lint.py` check hierarchy (`DeadCodeCheck`/`DeadCodeConfig`, `FlowCheck`/flow-rule `LintRule`) (~100)
- [ ] C4. Decide whether `language_plugins.py` plugin protocol is justified given Python is the only language plugin (~200–300)
- [ ] C5. Extract interprocedural trace from `trace.py` into its own module; move `_compile_dsl_pattern` from `lint.py` to `dsl.py` (~100–200)

## Phase D — `CommonFlags` Typer dataclass (deferred from A2)

- [ ] D1. Consolidate `--apply` / `--json` / `--output` into a shared `CommonFlags` dataclass across CLI modules (~50)
  - Risk: medium — touches 15+ command signatures.

## Phase E — Rust extension capability gaps (blocks further design-philosophy cleanup)

Phase B4 had to leave several `ast`/regex usages in `transform.py` because `emend_core` doesn't yet expose the needed APIs. To finish the design-philosophy migration, Rust would need:

- [ ] E1. `emend_core.parse_string_literal(text) -> str` — unescape a Python/JSON/etc. string-literal source fragment to its value. Needed to delete `_extract_string_content_from_text` (uses `ast.literal_eval`).
- [ ] E2. `emend_core.validate_syntax(code, ext, *, mode='expression'|'statement')` — currently called optimistically but the function isn't actually exposed. Needed to delete `_is_valid_replacement`'s `ast.parse` fallback.
- [ ] E3. JSONC parsing (comments + trailing commas) — currently done with regex in `transform.py:_load_tsconfig`. Could go through tree-sitter JSON grammar if we register one.

Each of these would unlock 15–25 additional Python lines for deletion.

---

## Open questions for next session

These determine the scope of Phase C. Please answer before that phase starts:

1. **`rewrite_engine.py` (`emend saturate`)** — is the equality-saturation rewrite engine real product surface area, or experimental scaffolding that can be deleted? Only callers found are the `saturate` CLI command and a `UnionFind` import in `duplicate.py`. Options:
   - (a) Delete entirely (~600 lines saved); move `UnionFind` into `duplicate.py`. Removes the `saturate` CLI command.
   - (b) Keep but mark experimental; hide from `--help`; possibly move to an `experimental/` subpackage.
   - (c) Keep as-is.

2. **`flow_ir.py` adapter layer** — do you agree with collapsing it into the two callers (`lint.py` + `policy.py`)? It's a 422-line indirection that mostly converts dataclasses. Concern: if a future check engine needs the IR, we'll re-create it.

3. **`policy.py` vs `lint.py` overlap** — happy to unify `DeadCodeCheck`/`DeadCodeConfig` and the flow-check hierarchies into a shared model? Or are the two systems intentionally distinct (lint = developer linting, policy = CI gates)?

4. **`language_plugins.py` plugin protocol** — given `python_plugin.py` is the only implementation, the plugin protocol exists for a multi-language future that lives in Rust+TOML now. Should we:
   - (a) Inline `python_plugin.py` into `transform.py` and delete the protocol/registry (~200–300 lines).
   - (b) Keep the protocol as documented extension point.

5. **Phase B2.3 (Datalog query helper)** — this was skipped because `refs_datalog`/`callers_datalog`/`callees_datalog`/`graph_datalog` have meaningfully different shapes (different return types, dynamic clause building, branching on `file_path`). If you want it pushed through anyway, possible to extract a narrower helper that only covers the two near-identical methods (`callers_datalog`/`callees_datalog`). Estimated saving: ~5–8 lines. Worth it?

6. **Rust extensions for design-philosophy compliance (Phase E)** — willing to extend `emend_core` to add `parse_string_literal`, `validate_syntax` (with mode), and tree-sitter JSONC? Each unlocks 15–25 Python lines + removes a CLAUDE.md design-philosophy violation. The Rust work is small but non-trivial.

7. **`_CLASS_DEF_RE` in `dsl.py`** — Phase B3 left this regex in place because `resolve_graphql_links` still uses it. Want a follow-up phase to convert GraphQL link resolution to tree-sitter as well? Estimated saving: ~10 lines.

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
