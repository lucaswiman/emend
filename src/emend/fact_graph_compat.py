"""Legacy FactGraph builders at the analysis-owner boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from emend.analysis_store import AnalysisStore
from emend.project_config import find_project_root

if TYPE_CHECKING:
    from emend.fact_graph import FactGraph


def build_from_project(
    cls: type[FactGraph],
    project_path: str,
    language: str | None = None,
    db_path: str | None = None,
    languages: list[str] | None = None,
    include_types: bool = True,
) -> FactGraph:
    """Return a caller-owned view sourced from the project analysis owner."""
    store = AnalysisStore.open(find_project_root(project_path))
    owned = store.query_facts(include_types=include_types, type_engine="auto")
    selected_languages = languages
    if selected_languages is None and language is not None:
        selected_languages = [language]
    selected = None
    if selected_languages is not None:
        wanted = set(selected_languages)
        selected = [
            revision.file_path
            for revision in owned.snapshot.files
            if revision.language in wanted
        ]
    return store.detached_facts(owned, db_path=db_path, file_paths=selected)
