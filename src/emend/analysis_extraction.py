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

    config = revision.analysis_config
    assert config is not None
    ext = Path(revision.file_path).suffix.lstrip(".") or "py"
    extracted = emend_core.extract_file_fact_rows(
        content,
        ext,
        revision.file_path,
        stored_path,
        revision.module_name,
        revision.language,
        config.payload,
    )
    return ExtractedFile(
        revision=revision,
        qnames=frozenset(extracted["qnames"]),
        rows=extracted["rows"],
    )
