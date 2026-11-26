"""Native extraction of one exact file revision."""

from __future__ import annotations

from pathlib import Path
from emend.analysis_snapshot import (
    ExtractedFile,
    FileRevision,
)


def _normalize_qn(qn: str) -> str:
    """Normalize language-specific qualified-name separators to dots."""
    qn = qn.replace("'", "").replace('"', "")
    qn = qn.replace("::", ".").replace("/", ".")
    while ".." in qn:
        qn = qn.replace("..", ".")
    return qn.lstrip(".")


def _extract_file_facts(
    revision: FileRevision,
    stored_path: str,
    content: str,
) -> ExtractedFile:
    """Return the canonical native fact batch for one source revision."""
    from emend import emend_core

    from emend.project_config import module_resolution_context

    config = revision.analysis_config
    assert config is not None
    ext = Path(revision.file_path).suffix.lstrip(".") or "py"
    # A relative path import (``./helpers``) is named against these roots, as
    # the configured scope resolver names it.  The crate root is not passed:
    # the linker resolves ``crate::`` paths.
    module_root, _crate_root = module_resolution_context(
        revision.project_root, revision.language, file_path=revision.file_path,
    )
    extracted = emend_core.extract_file_fact_rows(
        content,
        ext,
        revision.file_path,
        stored_path,
        revision.module_name,
        revision.language,
        config.payload,
        revision.project_root,
        str(module_root),
    )
    extracted["rows"]["file_namespace"] = [[stored_path, config.language]]
    for row in extracted["rows"]["dec"]:
        row.append(stored_path)
    return ExtractedFile(
        revision=revision,
        qnames=frozenset(extracted["qnames"]),
        rows=extracted["rows"],
        search_symbols=tuple(extracted["search_symbols"]),
        search_qnames=tuple(extracted["search_qnames"]),
    )
