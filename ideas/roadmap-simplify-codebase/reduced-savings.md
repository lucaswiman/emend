# Why realized savings were lower than the audit estimated

The pre-implementation audit estimated ~2,400 lines of savings across Phases A and B. Actual delta after implementation: −815 net Python LOC. This document records the per-phase reasons so future audits can be more accurate.

## Phase A1 — MCP tools (estimated −800, actual −13)

**Audit assumption**: 15 unused `@mcp_app.tool()` functions could be deleted whole.

**Reality**: The function bodies are still used as plain Python helpers by:
- The discriminated dispatchers (`transform`, `analyze`, `mappings`, `references`) call them internally — e.g. `transform()` delegates to `replace()`, `modify()`, `rename()`, `move()`.
- Tests import them directly — `test_mcp_server.py` imports `trace_analysis`; `test_knowledge.py` imports `map_read`/`map_write`.

**What we did**: Removed the `@mcp_app.tool()` decorators only. The MCP-facing surface area shrank correctly (those names no longer appear in the served schema), but the bodies remain as Python module functions.

**Lesson for future audits**: When the audit says "function X is dead, delete it", grep for direct callers (not just decorator users) before estimating savings. Internal reuse is common.

## Phase B2 — fact_graph.py boilerplate (estimated −300, actual −51)

**Audit assumption**: 13 `_all_*` methods + 13 `add_Xs_batch` methods + 4 Datalog query methods all duplicated trivially-extractable boilerplate.

**Reality**:
- The Datalog query *strings* are the bulk of the LOC, not the surrounding scaffolding. Helper extraction adds ~10–20 lines of overhead that offsets a chunk of the savings.
- Three `_all_*` methods reorder columns between the SELECT and the stored relation — needed an extra optional parameter on the helper.
- `add_calls_batch` writes to 3 separate relations and `add_references_batch` has a conditional second insert — both required preserving inline logic.
- The 4 Datalog query methods (`refs_datalog`, etc.) diverge in return type and dynamic clause building. Extracting a helper would obscure rather than clarify; skipped.

**Lesson**: Helper extraction has a fixed-cost overhead (~5–10 lines per helper) that only pays off when the duplicated block is large. For 5–10-line repetitions, savings are marginal.

## Phase B3 — dsl.py regex → tree-sitter (estimated −80 to −120, actual +23)

**Audit assumption**: Tree-sitter via `PyScopeResolver` would be more concise than regex.

**Reality**: Tree-sitter requires explicit indexing structures (string-literal index, class-children traversal) that take more lines than `re.finditer`. The new code is more correct (no regex limitations on f-strings, multi-line, etc.) and aligned with the design philosophy, but isn't shorter.

**Lesson**: Replacing regexes with tree-sitter is a *correctness* win, not a *line-count* win. Don't conflate them in audits.

## Phase B4 — transform.py ast/regex (estimated −100 to −150, actual −9)

**Audit assumption**: Several `ast.literal_eval` / `ast.parse` / regex usages could be swapped for `emend_core` equivalents.

**Reality**: `emend_core` doesn't expose:
- A function to unescape a string-literal source fragment (needed to replace `ast.literal_eval`)
- `validate_syntax` with `eval`/`exec` mode distinction (needed to replace `ast.parse`)
- A JSONC parser (needed to replace the regex stripping)

The Rust crate has internal helpers (e.g. `extract_string_content` in `rust/src/pattern.rs`) but they aren't bound through PyO3. Extending Rust was out of scope, so the Python regex/ast usages were left in place. See Phase E in `index.md` for the follow-up Rust work.

**What we did**: Inlined two thin wrappers (`_extract_root_name`, `prefilter_files_structural`).

**Lesson**: When the audit assumes a Rust API exists, verify it before estimating. CLAUDE.md's "use Rust instead of Python" directive only saves lines if the Rust APIs are actually exposed.

## Phase B1 — trace_presets to YAML (estimated −600, actual −652)

This phase **outperformed** the audit. The YAML conversion was clean: existing `_trace_config_from_trace_section` parser was reusable, `pyproject.toml` already bundled `src/emend/` recursively (no package data wiring needed), and round-trip equality was straightforward to verify.

**Lesson**: When the audit identifies "data masquerading as code", savings tend to be accurate or better. These are the highest-confidence simplifications.

## Phase A2 — CLI error handler (estimated ~150, actual −113)

Close to estimate. Some blocks were skipped because they had 2-handler shapes (no ValueError) or used custom exit codes; refactoring them would have changed observable behavior.

**Lesson**: "Identical boilerplate" claims should be verified by reading every block, not just sampling a few.
