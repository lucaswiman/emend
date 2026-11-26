"""Transform engine for extended selectors.

This package re-exports the names that callers import as
``from emend.transform import X``.  Anything else lives in, and should be
imported from, the submodule that defines it.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# cache.py
# ---------------------------------------------------------------------------
from .cache import (
    _cache_db_dir,
    _knowledge_db_dir,
    _get_worktree_id,
    _init_cache_schema,
)

# ---------------------------------------------------------------------------
# index.py
# ---------------------------------------------------------------------------
from .index import (
    _compute_duplicate_payloads,
    _ensure_cache_ignore_files,
    _ensure_index_fresh,
    _get_cached_qnames,
    _index_batch,
    _scan_manifest,
    get_index_status,
    query_import_graph,
    query_reference_index,
    query_symbol_index,
    warm_caches,
)
from .venv_index import (
    _venv_db_path,
    lookup_venv_symbol,
)

# ---------------------------------------------------------------------------
# project_iter.py
# ---------------------------------------------------------------------------
from .project_iter import (
    _file_to_module,
    _find_project_root,
    _find_source_root,
    _normalize_module_qn,
    detect_project_languages,
    extract_pattern_literals,
    find_pattern_in_project,
    visit_project_ts,
)

from emend.file_collection import (
    collect_all_source_files as _collect_all_source_files,
)

# ---------------------------------------------------------------------------
# components.py
# ---------------------------------------------------------------------------
from .components import (
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
    _get_or_build_fact_graph,
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
    _get_entry_point_config,
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
    _is_test_file,
    _is_test_symbol,
    find_impact,
)

# ---------------------------------------------------------------------------
# rename_move.py
# ---------------------------------------------------------------------------
from .rename_move import (
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
    "_cache_db_dir", "_knowledge_db_dir", "_get_worktree_id", "_init_cache_schema",
    # index
    "_compute_duplicate_payloads", "_ensure_cache_ignore_files",
    "_ensure_index_fresh", "_get_cached_qnames", "_index_batch",
    "_scan_manifest", "_venv_db_path",
    "get_index_status", "lookup_venv_symbol", "query_import_graph", "query_reference_index",
    "query_symbol_index", "warm_caches",
    # project_iter
    "_file_to_module", "_find_project_root", "_find_source_root",
    "_normalize_module_qn", "detect_project_languages",
    "extract_pattern_literals", "find_pattern_in_project",
    "visit_project_ts",
    # file_collection
    "_collect_all_source_files",
    # components
    "add_to_component", "get_component", "remove_component", "set_component",
    # patterns
    "PatternMatch", "_resolve_relative_module", "_substitute_metavars",
    "analyze_imports", "copy_symbol", "find_pattern", "get_symbol_source", "remove_symbol",
    "replace_pattern",
    # refs
    "_get_or_build_fact_graph", "find_callees", "find_callers", "find_references",
    "generate_graph",
    # deadcode
    "DeadBlock", "DeadModule", "DeadSymbol",
    "dead_code_result_details", "dead_code_result_to_dict",
    "_get_entry_point_config",
    "find_dead_code", "safe_delete", "semantic_context",
    # impact
    "ImpactEdge", "ImpactResult", "_is_test_file", "_is_test_symbol", "find_impact",
    # rename_move
    "move_module", "move_symbol", "rename_module", "rename_symbol",
    # dispatch
    "_apply_matching_filter", "cmd_add", "cmd_edit", "cmd_lookup",
    # rust
    "_rust",
]
