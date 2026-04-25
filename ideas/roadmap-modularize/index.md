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

- [ ] **Phase 1 — split `transform.py` into a `transform/` package** (highest leverage)
  - See [phase-1-split-transform.md](phase-1-split-transform.md).
  - Goal: 7 sibling modules of <1500 lines each; `transform/__init__.py`
    re-exports the public surface so all callers (CLI, MCP, lint, policy)
    keep working without import changes.
  - Risk: low — splits along function boundaries, no behavior change. Tests
    cover every command surface.

- [ ] **Phase 2 — unify lint/policy/checks/flow_ir into a `checks/` package**
  - See [phase-2-unify-checks.md](phase-2-unify-checks.md).
  - Three engines (`lint.run_lint`, `policy.run_policy_checks`,
    `checks.run_checks`) already share `DeadCodeConfig`, `FlowSpec`, and the
    YAML loader. Collapse into one engine with rule-kind dispatch.
  - Risk: medium — touches CLI dispatch and MCP wrappers, but the lint and
    policy violation types are nearly isomorphic (`checks.CheckViolation`
    already normalises both).

- [ ] **Phase 3 — single-source CLI registration**
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

- [ ] **Phase 4 — drop legacy `patterns.yaml` / `policies.yaml` fallbacks**
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

- [ ] **Phase 5 — purge `editor_search.py` `import ast` sites**
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

- [ ] **Phase 6 — decompose `mcp_server.py` into `mcp/` package**
  - 1,939 lines of MCP tool wrappers. After Phases 1+2, most tools are
    one-liners that re-pack args and call `transform.X` or `checks.X`.
  - Plan: split by domain mirroring the CLI: `mcp/find.py`, `mcp/edit.py`,
    `mcp/analyze.py`, `mcp/checks.py`, `mcp/tooling.py`. A small
    `mcp/dispatch.py` holds the `FastMCP` app and registration table.
  - Risk: low — covered by `test_mcp_server.py`.

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
