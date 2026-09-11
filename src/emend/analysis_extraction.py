"""Native extraction of one exact file revision."""

from __future__ import annotations

import hashlib
from emend.analysis_snapshot import ExtractedFile, FileRevision


def _normalize_qn(qn: str) -> str:
    """Normalize language-specific qualified-name separators to dots."""
    qn = qn.replace("'", "").replace('"', "")
    qn = qn.replace("::", ".").replace("/", ".")
    while ".." in qn:
        qn = qn.replace("..", ".")
    return qn.lstrip(".")


def _extract_file_facts(
    abs_path: str,
    rel_path: str,
    ext: str,
    content: str,
    project_root: str,
    module_name: str,
    language: str | None = None,
) -> ExtractedFile:
    """Return the canonical native fact batch for one source revision."""
    from emend import emend_core
    from emend.language_registry import detect_language

    effective_language = language or detect_language(abs_path) or "python"
    extracted = emend_core.extract_file_fact_rows(
        content,
        ext,
        abs_path,
        rel_path,
        project_root,
        module_name,
        effective_language,
    )
    revision = FileRevision.create(
        project_root,
        abs_path,
        hashlib.md5(content.encode(), usedforsecurity=False).hexdigest(),
        ext,
        module_name,
    )
    return ExtractedFile(
        revision=revision,
        qnames=frozenset(extracted["qnames"]),
        rows=extracted["rows"],
    )
