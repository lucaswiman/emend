# Phase 1 — Split `transform.py` into a package

## Why

`src/emend/transform.py` is 8,449 lines (twice the next-largest file) and
holds at least seven unrelated responsibilities. Symptoms:

- Cold-read time for newcomers is hostile.
- Most CLI commands import a single function from it but pull in the whole
  module's import graph (sqlite3, ast, tree-sitter, fact_graph, …).
- Diffs are noisy — a one-line change to dead-code detection looks similar
  to a one-line change to rename in `git log --stat`.

The functions cluster into clean groups (verified by reading the top-level
defs at lines 43–8421, see also `git grep -n "^def \|^class " transform.py`).

## Target layout

```
src/emend/transform/
├── __init__.py          # Re-exports the public surface for backward compat
├── cache.py             # parse.db / fact-DB plumbing
├── index.py             # Symbol/reference indexing, manifest scan, warm
├── project_iter.py      # find_pattern_in_project, visit_project_ts, file scanning
├── components.py        # get/set/add/remove component
├── patterns.py          # find_pattern, replace_pattern, filters, copy_symbol
├── refs.py              # find_references, find_callers, find_callees, generate_graph
├── deadcode.py          # find_dead_code, semantic_context, safe_delete
├── impact.py            # find_impact, _parse_diff_to_selectors
├── rename_move.py       # rename_symbol, move_symbol, move_module, rename_module
└── dispatch.py          # cmd_lookup, cmd_edit, cmd_add (CLI entry points)
```

`__init__.py` re-exports everything currently importable from
`emend.transform` so existing imports (`from emend.transform import find_pattern`)
keep working unchanged. This is non-negotiable — `cli.py`, `mcp_server.py`,
`lint.py`, `policy.py`, `editor_search.py`, and the test suite all import
directly.

## Function → module mapping

Approximate line ranges in the current `transform.py`:

| Module | Functions / classes | Lines |
|---|---|---|
| `cache.py` | `_resolve_cache_root`, `_cache_db_dir`, `_knowledge_db_dir`, `_get_worktree_id`, `_init_cache_schema`, `_get_disk_cache`, `_open_facts_db`, `_get_facts_db`, `_delete_facts_for_file`, `_build_fact_sym_rows`, `_extract_file_facts`, `_build_facts_db` | 43–1214 |
| `index.py` | `_get_cached_qnames`, `_extract_all_exports_text`, `_extract_noqa_lines`, `_index_batch`, `ManifestScanResult`, `_scan_manifest`, `_ensure_index_fresh`, `query_symbol_index`, `_query_symbol_index_cozo`, `_lookup_via_modmap`, `_venv_db_path`, `_ensure_venv_index`, `_build_venv_index`, `lookup_venv_symbol`, `query_reference_index`, `query_import_graph`, `get_index_status`, `warm_caches`, `_ensure_cache_ignore_files`, `_compute_duplicate_payloads` | 1215–2974 |
| `project_iter.py` | `_ext_from_path`, `extract_pattern_literals`, `ProjectPatternMatch`, `find_pattern_in_project`, `_index_prefilter`, `_read_and_filter_py`, `_find_project_root`, `_find_source_root`, `_normalize_module_qn`, `_file_to_module`, `_files_importing_module`, `visit_project_ts`, `_get_imports`, `_add_import_text` | 2975–3618 |
| `components.py` | `_raise_component_not_found`, `get_component`, `_generate_diff`, `set_component`, `add_to_component`, `remove_component`, `_extract_string_content_from_text` | 3619–4132 |
| `patterns.py` | `PatternMatch`, `_filter_matches_by_import`, `_filter_matches_by_scope_local`, `_filter_matches_by_type_oracle`, `find_pattern`, `remove_symbol`, `get_symbol_source`, `_collect_name_contexts`, `_resolve_relative_module`, `analyze_imports`, `copy_symbol`, `_is_valid_replacement`, `_substitute_metavars`, `replace_pattern` | 4133–5072 |
| `refs.py` | `Reference`, `_rename_in_docstrings`, `_get_or_build_fact_graph`, `find_references`, `Callee`, `find_callers`, `find_callees`, `generate_graph` | 5073–5384 |
| `deadcode.py` | `DeadSymbol`, `DeadBlock`, `DeadModule`, `_get_entry_point_config`, `_is_dunder`, `_is_likely_entry_point`, `_get_last_reference_commit`, `_string_literal_filter`, `find_dead_code`, `Danger`, `DataFlow`, `SideEffect`, `CallerInfo`, `TestInfo`, `SemanticContext`, `semantic_context`, `DeletePlan`, `safe_delete` | 5385–7041 |
| `impact.py` | `ImpactEdge`, `ImpactResult`, `_parse_diff_to_changed_files`, `_parse_diff_to_selectors`, `_is_test_file`, `_is_test_symbol`, `_find_impact_via_fact_graph`, `_try_relative`, `find_impact`, `_parse_decorator_name` | 5660–6141 (currently interleaved with deadcode; see note) |
| `rename_move.py` | `rename_symbol`, `move_symbol`, `_source_has_remaining_refs`, `_split_or_retarget_import`, `_update_imports_for_move`, `_resolve_relative_import_qn`, `_replace_module_in_strings`, `_rename_module_references`, `move_module`, `rename_module` | 7042–7809 |
| `dispatch.py` | `_cmd_lookup_single_selector`, `cmd_lookup`, `_apply_matching_filter`, `_merge_type_filter`, `_expand_selector_with_returns_filter`, `_dispatch_with_returns_filter`, `_cmd_edit_single`, `cmd_edit`, `_cmd_add_single`, `cmd_add` | 7810–8449 |

Note: the current ordering interleaves `find_dead_code` (5528) with
`Impact*` (5660). Move the impact block to its own module; this is
defensible because they are conceptually distinct — `find_dead_code` is
forward analysis, `find_impact` is reverse-caller closure.

## Execution plan

1. **Create the package directory** `src/emend/transform/` and add an empty
   `__init__.py`.
2. **Extract one module at a time, in dependency order**: cache → index →
   project_iter → components → patterns → refs → deadcode → impact →
   rename_move → dispatch. For each:
   - Move the functions verbatim. No rewrites.
   - Update internal imports inside the new module.
   - Add `from emend.transform.<new_module> import *` to
     `transform/__init__.py`.
   - Run `make test`. The test suite must stay green.
3. **Delete the original `transform.py`** once `__init__.py` re-exports
   everything.
4. **Verify external import sites** still resolve via the package
   `__init__.py`. Run a one-shot grep:
   `grep -rn "from emend.transform import\|from emend\.transform\." src/ tests/`
   to confirm no caller broke.

## Acceptance criteria

- [ ] Every `make test` target passes after each module extraction.
- [ ] No file in `src/emend/transform/` exceeds 1,500 lines.
- [ ] `from emend.transform import X` still works for every `X` that was
      importable before the split.
- [ ] `git log --follow` works on each new module (use `git mv -k` style
      moves where possible; failing that, accept the history rewrite — the
      change should be one commit per extracted module so blame is
      preserved within the commit).
- [ ] No new external dependencies added.

## Caveats

- `transform.py` has many private `_helper` functions used only by one
  public function. Move them with their consumer.
- A few helpers (e.g. `_find_project_root`) are used by multiple groups.
  They go in the lowest-dependency module (`project_iter.py` for that one)
  and other modules import from there.
- Two name collisions to watch:
  - `Reference` in `refs.py` and `ReferenceFact` in `fact_graph.py` — these
    already coexist; no change needed.
  - `find_dead_code` is imported by `lint.py`, `policy.py`, and
    `cli_analysis.py`. Confirm `transform/__init__.py` re-exports it.

## Estimated diff size

- ~8,400 lines moved (line-for-line; should show up as renames in `git`).
- ~50–80 lines of new `__init__.py` re-exports.
- ~30 lines of fixed-up internal imports inside extracted modules.
- Zero behavior change.
