# Modularize codebase roadmap

Goal: reduce surface area of the largest modules and consolidate parallel
engines, so future changes touch fewer lines and the structure is
self-explanatory. Companion to `ideas/roadmap-simplify-codebase/` (which is
done): that one squeezed lines out; this one moves them.

Source state at start (commit on `claude/refactor-and-update-docs-8colR`):

| File | LOC | Notes |
|---|---|---|
| `transform.py` | 8,449 | Core engine; 6+ unrelated responsibilities |
| `fact_graph.py` | 4,426 | Big but coherent; **leave alone** (Phase B2 audit) |
| `editor_search.py` | 3,161 | 6 inline `import ast` sites |
| `trace.py` | 2,571 | OK but some Phase-1..18 history baked in |
| `mcp_server.py` | 1,939 | Mostly thin wrappers around CLI commands |
| `cli_analysis.py` | 1,058 | Mixes `@app.command` + named exports for `cli.py` |
| `cli_edit.py` | 827 | Same dual registration pattern |
| `cli_find.py` | 759 | Same dual registration pattern |
| `lint.py` | 1,108 | Shares `DeadCodeConfig`/`FlowSpec` with policy/checks |
| `policy.py` | 1,064 | Same |
| `checks.py` | 195 | Unified runner; imports from both above |
| `flow_ir.py` | 422 | The Datalog ↔ Python flow bridge both call |

## Phases

- [x] **Phase 1 — split `transform.py` into a `transform/` package** (highest leverage)
  - See [phase-1-split-transform.md](phase-1-split-transform.md).
  - Goal: 7 sibling modules of <1500 lines each; `transform/__init__.py`
    re-exports the public surface so all callers (CLI, MCP, lint, policy)
    keep working without import changes.
  - Risk: low — splits along function boundaries, no behavior change. Tests
    cover every command surface.
  - **Status**: split landed in commits 2250088 / 9727a5e on
    `claude/modularize-phase-1-split-transform`; follow-ups 3b380b0,
    070c747, 3811e08, 684f674 fixed missing imports
    (`re`, `lru_cache`, `emend_core as _rust`), restored a dropped
    `@dataclass` on `PatternMatch`, relocated misplaced
    semantic-context constants from `impact.py` to `deadcode.py`, and
    re-targeted three `test_language_plugin_bugs.py` patches that no
    longer reach the now-submodule-local symbols. `make test`: 3060
    passed, 2 failed (both `test_knowledge.py::TestMCPTools` — pre-
    existing pydantic / Python 3.14 incompatibility, fails on the
    original `transform.py` too).
  - **Follow-up resolved**: venv-index block extracted to
    `transform/venv_index.py` (289 LOC); `_index_batch` split into
    `_check_cache_hits` / `_write_index_rows` helpers. `transform/index.py`
    now 1558 LOC.

- [x] **Phase 2 — unify lint/policy/checks/flow_ir into a `checks/` package**
  - See [phase-2-unify-checks.md](phase-2-unify-checks.md).
  - Three engines (`lint.run_lint`, `policy.run_policy_checks`,
    `checks.run_checks`) already share `DeadCodeConfig`, `FlowSpec`, and the
    YAML loader. Collapse into one engine with rule-kind dispatch.
  - Risk: medium — touches CLI dispatch and MCP wrappers, but the lint and
    policy violation types are nearly isomorphic (`checks.CheckViolation`
    already normalises both).
  - **Status**: landed on `claude/modularize-agent-swarm-e0nzk` in commits
    91ab117 (2a), df3a508 (2b), 2ce213f (2c), cd3b245 (2d).
    Stage 2a: `src/emend/checks/` package created; `checks.py` converted
    to package (`checks/engine.py`); `rules_config.py` and `flow_ir.py`
    reduced to one-line shims re-exporting from `checks/rules_config.py`
    and `checks/flow.py`. Stage 2b: per-kind modules created
    (`pattern_rules.py`, `structural.py`, `types.py`, `deadcode.py`,
    `datalog.py`, `custom.py`, `sequence.py`, `duplicates.py`); `lint.py`
    reduced to 805 LOC (from 1108) by importing shared types/helpers from
    `checks/`; `policy.py` reduced to 658 LOC (from 1064) by importing check
    types from `checks/`. Stage 2c: `checks/engine.py` gains `mode` parameter
    (`lint`/`policy`/`all`) and `LINT_KINDS`/`POLICY_KINDS` constants;
    `cli_checks.py` lint_cmd and policy_cmd now route through `run_checks`.
    Schema decision: per-kind dispatch (not unified schema), matching the
    existing two-document model (rules + policies). Stage 2d: dead `lint()`
    and `check_policies()` MCP helpers removed; `check` MCP tool gains
    `mode` parameter. `make test`: 3060 passed, 3 skipped, 1 xfailed.
  - **Open follow-up**: Stage 2e (fully reducing `lint.py` and `policy.py`
    to <100-line shims by removing `run_lint`/`run_policy_checks`
    implementation bodies) is deferred until the next release per plan.

- [x] **Phase 3 — single-source CLI registration**
  - Today every `cli_*.py` file declares `@app.command("name", hidden=True)`
    *and* `cli.py` re-registers each function under namespaced subapps
    (`edit set`, `analyze refs`, etc.). Two registration sites means renaming
    a command requires changes in two places.
  - Plan: command modules export plain functions; `cli.py` is the only file
    that calls `app.command(...)`. The hidden top-level aliases live in a
    single dict in `cli.py`.
  - Drop `cli.py`'s re-exports of `parse_where_clause`, `resolve_files`,
    `QueryShape`, `_reject_file_glob`, etc. (verify no external importers
    first; these look like accidental exports from when `cli.py` was monolithic).
  - Risk: low — pure mechanical refactor; covered by `test_cli_surface_consolidation.py`.
  - **Status**: landed on `claude/modularize-agent-swarm-e0nzk`. All
    `@app.command` / `@edit_app.command` / `@analyze_app.command` /
    `@tool_app.command` / `@map_app.command` decorators removed from
    `cli_*.py`; `cli.py` now drives registration through a single
    `_COMMANDS` table of `_CmdEntry` records (subapp, name, fn, hidden,
    no_args_is_help, aliases). `cli.py` `__all__` reduced to
    `{"app", "main"}`; internal callers updated to import helpers from
    `cli_base` instead of the old re-exports. Hidden alias `dsl-debug`
    re-added after a missed alias broke `test_dsl.py::TestDslDebugCommand`.
    `make test`: 3060 passed, 3 skipped, 1 xfailed.

- [x] **Phase 4 — drop legacy `patterns.yaml` / `policies.yaml` fallbacks**
  - Canonical config is `.emend/rules.yaml` (added in
    `roadmap-simplify-codebase` Phase B/C). Legacy paths are still threaded
    as `fallbacks=(LEGACY_PATTERNS_PATH, LEGACY_POLICIES_PATH)` through six
    files (`lint.py`, `policy.py`, `cli_checks.py`, `cli_analysis.py`,
    `mcp_server.py`, `rules_config.py`).
  - Plan: pick a deprecation cutoff; emit a one-time warning when a fallback
    is used; in the next release, delete the `fallbacks=` plumbing and the
    `LEGACY_*` constants.
  - Risk: low for the code; **user-visible** — confirm with the user before
    starting, and document in `CHANGELOG.md`.
  - **Status**: hard-removed (no deprecation shim) per user request.
    `LEGACY_PATTERNS_PATH` and `LEGACY_POLICIES_PATH` constants deleted from
    `checks/rules_config.py` and the shim `rules_config.py`. `load_rules_document`
    and `resolve_rules_path` simplified to drop `fallbacks=` parameter entirely.
    Call-sites updated in `lint.py`, `policy.py`, `cli_checks.py`,
    `cli_analysis.py`, `trace.py`, `checks/pattern_rules.py`, `mcp/analyze.py`.
    Help text updated in `cli_checks.py`, `cli_analysis.py`, `mcp/analyze.py`.
    Docstrings updated in `trace.py`, `trace_presets.py`, `presets/__init__.py`.
    Tests: `test_load_rules_falls_back_to_rules_yaml` deleted from `test_lint.py`;
    `test_load_unified_rules_top_level_deadcode` in `test_policy.py` updated to
    pass `rules.yaml` path directly instead of relying on fallback.
    CHANGELOG entry added. `make test`: baseline had 37 pre-existing failures
    (test_list_symbols, test_map_unified, test_move, test_phase9_parity);
    post-change results match baseline (no new failures from this phase).

- [x] **Phase 5 — purge `editor_search.py` `import ast` sites**
  - 6 inline `import ast as _ast` calls (lines 2357, 2424, 2459, 2497, 2524,
    2574) that parse Python source ad-hoc. Each is a design-philosophy
    violation per `CLAUDE.md`'s "Tree-sitter and Configuration" section.
  - Replace each with `emend_core.collect_symbols_from_str()` or a targeted
    tree-sitter query via `PyTree`/`PyNode` (the latter was added in
    `rust/src/tree_py.rs`).
  - May need a small `emend_core` capability extension if existing APIs
    don't cover one of the use cases — note the gap if so (à la Phase E).
  - Risk: medium — `editor_search` is hot path for vim plugin; verify with
    `test_editor_search.py`, `test_editor_search_files.py`, and
    `test_vim_rpc.py`.
  - **Status**: landed alongside Phase 3 on
    `claude/modularize-agent-swarm-e0nzk`. Top-level `import ast` and the
    six inline `_ast` sites in `editor_search.py` removed; replaced with
    `emend_core.parse_source(...)` plus a small `_walk_nodes` PyNode
    DFS helper at module scope. Affected helpers:
    `_attribute_completions_from_local`, `_class_member_completions`,
    `_class_member_name`, `_find_enclosing_scope_node`,
    `_infer_receiver_target`, and `_complete_self_in_class`. No new
    `emend_core` API needed — `parse_source`, `PyTree`, and `PyNode`
    already covered every site. `test_class_member_name_consistent_ast_module`
    in `test_editor_search.py` rewritten as
    `test_class_member_name_uses_tree_sitter` (still verifies the
    sync/async `def` name extraction). `make test`: 3060 passed,
    3 skipped, 1 xfailed.

- [x] **Phase 6 — decompose `mcp_server.py` into `mcp/` package**
  - 1,939 lines of MCP tool wrappers. After Phases 1+2, most tools are
    one-liners that re-pack args and call `transform.X` or `checks.X`.
  - Plan: split by domain mirroring the CLI: `mcp/find.py`, `mcp/edit.py`,
    `mcp/analyze.py`, `mcp/checks.py`, `mcp/tooling.py`. A small
    `mcp/dispatch.py` holds the `FastMCP` app and registration table.
  - Risk: low — covered by `test_mcp_server.py`.
  - **Status**: landed on `claude/modularize-agent-swarm-e0nzk`. `mcp_server.py`
    reduced from 1,868 lines to 48-line backward-compat shim. New package:
    `mcp/__init__.py` (91 lines, re-exports), `mcp/dispatch.py` (185 lines,
    `FastMCP` instance + profile/schema/run infrastructure), `mcp/find.py`
    (255 lines, `search` tool), `mcp/edit.py` (258 lines, `transform` tool
    + `replace`/`modify`/`rename`/`move` helpers), `mcp/analyze.py` (586
    lines, `references` + `analyze` tools + all analysis helpers),
    `mcp/checks.py` (56 lines, `check` tool), `mcp/tooling.py` (361 lines,
    `facts_query` + `mappings` + `grammar_and_cookbook` tools + helpers).
    All `from emend.mcp_server import X` callers unchanged. Tool names,
    parameter shapes, and registration order identical to before.
    `make test`: 3060 passed, 3 skipped, 1 xfailed.

## Out of scope (already evaluated)

- **`fact_graph.py`** — 4,426 lines but mostly Datalog query strings.
  `roadmap-simplify-codebase` Phase B2 confirmed that splitting hurts
  readability of the relational schema. Leave alone.
- **`flow_ir.py`** — `roadmap-simplify-codebase` Phase C2 marked invalid;
  contains the 188-line Python ↔ Datalog bridge, not pure indirection.
  Survives Phase 2 of this roadmap as `checks/flow.py`.
- **`rewrite_engine.py`** — Phase C1 already extracted `UnionFind` and
  marked it experimental. No further work.
- **`language_plugins.py`** — Phase C4 marked invalid (multi-language by
  design via Rust + TypeScript plugins).

## Sequencing notes

Phases 1 and 2 are independent. Phase 3 should follow Phase 1 (some of the
re-exports come from `transform.py` indirectly via CLI). Phase 6 should
follow Phase 2 (MCP wrappers will collapse further once `checks.py` is the
single entry point). Phase 4 stands alone. Phase 5 is independent.

Recommended order: **1 → 3 → 2 → 6 → 5 → 4** (biggest win first; Phase 4
last because it's user-visible and benefits from a quiet release).
