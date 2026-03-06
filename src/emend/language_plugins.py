"""Language plugin ABCs and stub implementations.

Each language plugin composes three handlers:
- ``ImportHandler``: extract / add / remove import statements
- ``CommentHandler``: find docstrings and noqa-style comments
- ``PatternCompiler``: compile pattern strings to the Rust IR

Phase 2a defines the ABCs and the ``LanguagePlugin`` dataclass.
Phase 2b provides ``NoOpImportHandler``, ``RegexCommentHandler``, and
``NoOpPatternCompiler`` as generic stubs used for languages that do not yet
have a dedicated plugin.

Phase 2c (extracting a full ``PythonPlugin``) is out of scope here.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# ABCs
# ---------------------------------------------------------------------------

class ImportHandler(ABC):
    """Abstract interface for import extraction and manipulation."""

    @abstractmethod
    def extract_imports(self, source: str) -> list[tuple[str, str, str | None, bool]]:
        """Return a list of ``(module, name, alias, is_from_import)`` tuples."""
        ...

    @abstractmethod
    def add_import(
        self, source: str, module: str, name: str, alias: str | None = None
    ) -> str:
        """Return *source* with an import for *name* from *module* added."""
        ...

    @abstractmethod
    def remove_import(self, source: str, module: str, name: str) -> str:
        """Return *source* with the import of *name* from *module* removed."""
        ...


class CommentHandler(ABC):
    """Abstract interface for comment and docstring operations."""

    @property
    @abstractmethod
    def line_comment_prefix(self) -> str:
        """The single-line comment prefix (e.g. ``#`` for Python, ``//`` for TS)."""
        ...

    @abstractmethod
    def find_docstrings(
        self, source: str, symbol_byte_range: tuple[int, int]
    ) -> list[tuple[int, int, str]]:
        """Return ``(start_byte, end_byte, text)`` for docstrings in *source*."""
        ...

    @abstractmethod
    def find_noqa_comments(self, source: str) -> dict[int, set[str] | None]:
        """Return ``{line_number: set_of_tags}`` for noqa-style comments.

        A value of ``None`` means "suppress all checks on this line".
        Line numbers are 1-based.
        """
        ...


class PatternCompiler(ABC):
    """Abstract interface for compiling pattern strings to the Rust IR."""

    @abstractmethod
    def compile(self, pattern_str: str, metavar_map: dict) -> dict | None:
        """Return a Rust-IR dict for *pattern_str*, or ``None`` if unsupported."""
        ...


# ---------------------------------------------------------------------------
# LanguagePlugin dataclass
# ---------------------------------------------------------------------------

@dataclass
class LanguagePlugin:
    """Composes the three handlers for a single language."""

    import_handler: ImportHandler
    comment_handler: CommentHandler
    pattern_compiler: PatternCompiler


# ---------------------------------------------------------------------------
# Phase 2b: stub implementations
# ---------------------------------------------------------------------------

class NoOpImportHandler(ImportHandler):
    """Import handler that performs no operations (returns source unchanged)."""

    def extract_imports(self, source: str) -> list[tuple[str, str, str | None, bool]]:
        return []

    def add_import(
        self, source: str, module: str, name: str, alias: str | None = None
    ) -> str:
        return source

    def remove_import(self, source: str, module: str, name: str) -> str:
        return source


class RegexCommentHandler(CommentHandler):
    """Generic comment handler for languages with a simple line-comment prefix.

    Recognises ``<prefix> noqa: tag1,tag2`` comments.  Does not handle
    block-style docstrings (returns empty list for ``find_docstrings``).
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        escaped = re.escape(prefix)
        self._noqa_pat = re.compile(
            escaped + r"\s*noqa:\s*(\S+)"
        )

    @property
    def line_comment_prefix(self) -> str:
        return self._prefix

    def find_docstrings(
        self, source: str, symbol_byte_range: tuple[int, int]
    ) -> list[tuple[int, int, str]]:
        return []

    def find_noqa_comments(self, source: str) -> dict[int, set[str] | None]:
        result: dict[int, set[str] | None] = {}
        for lineno, line in enumerate(source.splitlines(), 1):
            m = self._noqa_pat.search(line)
            if m:
                tags = {t.strip() for t in m.group(1).split(",") if t.strip()}
                result[lineno] = tags
        return result


class NoOpPatternCompiler(PatternCompiler):
    """Pattern compiler that defers to the default Rust IR path (returns None)."""

    def compile(self, pattern_str: str, metavar_map: dict) -> dict | None:
        # Returning None signals callers to use the default compilation path.
        return None


# ---------------------------------------------------------------------------
# Plugin registry and loader
# ---------------------------------------------------------------------------

_COMMENT_PREFIXES: dict[str, str] = {
    "python": "#",
    "typescript": "//",
    "rust": "//",
    "go": "//",
}


def load_plugin(language: str) -> LanguagePlugin:
    """Return a ``LanguagePlugin`` for *language*.

    Currently returns stub implementations for all languages.
    Phase 2c will add ``PythonPlugin`` discovery here.
    """
    prefix = _COMMENT_PREFIXES.get(language, "#")
    return LanguagePlugin(
        import_handler=NoOpImportHandler(),
        comment_handler=RegexCommentHandler(prefix),
        pattern_compiler=NoOpPatternCompiler(),
    )
