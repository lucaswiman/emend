"""Transform engine for extended selectors.

This package re-exports the complete public surface from the 10 submodules
so that all existing ``from emend.transform import X`` imports continue to
work unchanged.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# cache.py
# ---------------------------------------------------------------------------
from .cache import (
    _SCHEMA_VERSION,
    _cache_db_dir,
    _knowledge_db_dir,
    _delete_facts_for_file,
    _get_disk_cache,
    _get_facts_db,
    _get_worktree_id,
    _init_cache_schema,
    _open_facts_db,
    _resolve_cache_root,
    _build_facts_db,
    _build_fact_sym_rows,
    _extract_file_facts,
    _facts_db_cache,
)

# ---------------------------------------------------------------------------
# index.py
# ---------------------------------------------------------------------------
from .index import (
    _NOQA_RE,
    _compute_duplicate_payloads,
    _ensure_cache_ignore_files,
    _ensure_index_fresh,
    _extract_all_exports_text,
    _extract_noqa_lines,
    _get_cached_qnames,
    _index_batch,
    _lookup_via_modmap,
    _query_symbol_index_cozo,
    _scan_manifest,
    ManifestScanResult,
    get_index_status,
    query_import_graph,
    query_reference_index,
    query_symbol_index,
    warm_caches,
)
from .venv_index import (
    _build_venv_index,
    _ensure_venv_index,
    _venv_db_path,
    lookup_venv_symbol,
)

# ---------------------------------------------------------------------------
# project_iter.py
# ---------------------------------------------------------------------------
from .project_iter import (
    _METAVAR_RE as _METAVAR_RE_proj,  # noqa: F401 (re-exported as alias)
    _SKIP_DIRS,
    _add_import_text,
    _collect_source_files,
    _collect_source_files_scandir,
    _ext_from_path,
    _file_list_cache,
    _file_to_module,
    _files_importing_module,
    _find_project_root,
    _find_source_root,
    _get_imports,
    _index_prefilter,
    _normalize_module_qn,
    _read_and_filter_py,
    detect_project_languages,
    extract_pattern_literals,
    find_pattern_in_project,
    ProjectPatternMatch,
    visit_project_ts,
)

# Also export the file collection functions that project_iter re-imports
from emend.file_collection import (
    collect_source_files as _collect_source_files_impl,
    collect_source_files_scandir as _collect_source_files_scandir_impl,
    collect_all_source_files as _collect_all_source_files,
    collect_git_tracked_source_files as _collect_git_tracked_source_files,
)

# ---------------------------------------------------------------------------
# components.py
# ---------------------------------------------------------------------------
from .components import (
    _CONTENT_REF_RE,
    _extract_string_content_from_text,
    _generate_diff,
    _raise_component_not_found,
    add_to_component,
    get_component,
    remove_component,
    set_component,
)

# ---------------------------------------------------------------------------
# patterns.py
# ---------------------------------------------------------------------------
from .patterns import (
    PatternMatch,
    _collect_name_contexts,
    _filter_matches_by_import,
    _filter_matches_by_scope_local,
    _filter_matches_by_type_oracle,
    _is_valid_replacement,
    _resolve_relative_module,
    _substitute_metavars,
    analyze_imports,
    copy_symbol,
    find_pattern,
    get_symbol_source,
    remove_symbol,
    replace_pattern,
)

# ---------------------------------------------------------------------------
# refs.py
# ---------------------------------------------------------------------------
from .refs import (
    Callee,
    Reference,
    _fact_graph_cache,
    _get_or_build_fact_graph,
    _rename_in_docstrings,
    find_callees,
    find_callers,
    find_references,
    generate_graph,
)

# ---------------------------------------------------------------------------
# deadcode.py
# ---------------------------------------------------------------------------
from .deadcode import (
    DeadBlock,
    DeadModule,
    DeadSymbol,
    dead_code_result_details,
    dead_code_result_to_dict,
    DeletePlan,
    _ENTRY_POINT_DECORATOR_BASENAMES,
    _ENTRY_POINT_DECORATORS,
    _ENTRY_POINT_NAMES,
    _get_entry_point_config,
    _get_last_reference_commit,
    _is_dunder,
    _is_likely_entry_point,
    _parse_decorator_name,
    _string_literal_filter,
    find_dead_code,
    safe_delete,
    semantic_context,
)

# ---------------------------------------------------------------------------
# impact.py
# ---------------------------------------------------------------------------
from .impact import (
    ImpactEdge,
    ImpactResult,
    _find_impact_via_fact_graph,
    _is_test_file,
    _is_test_symbol,
    _parse_diff_to_changed_files,
    _parse_diff_to_selectors,
    _try_relative,
    find_impact,
)

# ---------------------------------------------------------------------------
# rename_move.py
# ---------------------------------------------------------------------------
from .rename_move import (
    _replace_module_in_strings,
    _rename_module_references,
    _resolve_relative_import_qn,
    _source_has_remaining_refs,
    _split_or_retarget_import,
    _update_imports_for_move,
    move_module,
    move_symbol,
    rename_module,
    rename_symbol,
)

# ---------------------------------------------------------------------------
# dispatch.py
# ---------------------------------------------------------------------------
from .dispatch import (
    _apply_matching_filter,
    _cmd_add_single,
    _cmd_edit_single,
    _cmd_lookup_single_selector,
    _dispatch_with_returns_filter,
    _expand_selector_with_returns_filter,
    _merge_type_filter,
    cmd_add,
    cmd_edit,
    cmd_lookup,
)

# ---------------------------------------------------------------------------
# Rust extension — re-exported for backward-compat
# (e.g. ``from emend.transform import _rust``)
# ---------------------------------------------------------------------------
from emend import emend_core as _rust

__all__ = [
    # cache
    "_SCHEMA_VERSION", "_cache_db_dir", "_knowledge_db_dir", "_delete_facts_for_file",
    "_get_disk_cache", "_get_facts_db", "_get_worktree_id", "_init_cache_schema",
    "_open_facts_db", "_resolve_cache_root", "_build_facts_db", "_build_fact_sym_rows",
    "_extract_file_facts", "_facts_db_cache",
    # index
    "_NOQA_RE", "_compute_duplicate_payloads", "_ensure_cache_ignore_files",
    "_ensure_index_fresh", "_ensure_venv_index", "_extract_all_exports_text",
    "_extract_noqa_lines", "_get_cached_qnames", "_index_batch", "_lookup_via_modmap",
    "_query_symbol_index_cozo", "_scan_manifest", "_venv_db_path", "ManifestScanResult",
    "get_index_status", "lookup_venv_symbol", "query_import_graph", "query_reference_index",
    "query_symbol_index", "warm_caches", "_build_venv_index",
    # project_iter
    "_SKIP_DIRS", "_add_import_text", "_collect_source_files", "_collect_source_files_scandir",
    "_ext_from_path", "_file_list_cache", "_file_to_module", "_files_importing_module",
    "_find_project_root", "_find_source_root", "_get_imports", "_index_prefilter",
    "_normalize_module_qn", "_read_and_filter_py", "detect_project_languages",
    "extract_pattern_literals", "find_pattern_in_project", "ProjectPatternMatch",
    "visit_project_ts",
    # components
    "_CONTENT_REF_RE", "_extract_string_content_from_text", "_generate_diff",
    "_raise_component_not_found", "add_to_component", "get_component", "remove_component",
    "set_component",
    # patterns
    "PatternMatch", "_collect_name_contexts", "_filter_matches_by_import",
    "_filter_matches_by_scope_local", "_filter_matches_by_type_oracle",
    "_is_valid_replacement", "_resolve_relative_module", "_substitute_metavars",
    "analyze_imports", "copy_symbol", "find_pattern", "get_symbol_source", "remove_symbol",
    "replace_pattern",
    # refs
    "Callee", "Reference", "_fact_graph_cache", "_get_or_build_fact_graph",
    "_rename_in_docstrings", "find_callees", "find_callers", "find_references",
    "generate_graph",
    # deadcode
    "DeadBlock", "DeadModule", "DeadSymbol", "DeletePlan",
    "dead_code_result_details", "dead_code_result_to_dict",
    "_ENTRY_POINT_DECORATOR_BASENAMES", "_ENTRY_POINT_DECORATORS", "_ENTRY_POINT_NAMES",
    "_get_entry_point_config", "_get_last_reference_commit", "_is_dunder",
    "_is_likely_entry_point", "_parse_decorator_name", "_string_literal_filter",
    "find_dead_code", "safe_delete", "semantic_context",
    # impact
    "ImpactEdge", "ImpactResult", "_find_impact_via_fact_graph", "_is_test_file",
    "_is_test_symbol", "_parse_diff_to_changed_files", "_parse_diff_to_selectors",
    "_try_relative", "find_impact",
    # rename_move
    "_replace_module_in_strings", "_rename_module_references", "_resolve_relative_import_qn",
    "_source_has_remaining_refs", "_split_or_retarget_import", "_update_imports_for_move",
    "move_module", "move_symbol", "rename_module", "rename_symbol",
    # dispatch
    "_apply_matching_filter", "_cmd_add_single", "_cmd_edit_single",
    "_cmd_lookup_single_selector", "_dispatch_with_returns_filter",
    "_expand_selector_with_returns_filter", "_merge_type_filter", "cmd_add", "cmd_edit",
    "cmd_lookup",
    # rust
    "_rust",
]
