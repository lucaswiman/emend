"""Language plugin ABCs and stub implementations.

Each language plugin composes three handlers:
- ``ImportHandler``: extract / add / remove import statements
- ``CommentHandler``: find docstrings and noqa-style comments
- ``PatternCompiler``: compile pattern strings to the Rust IR

Phase 2a defines the ABCs and the ``LanguagePlugin`` dataclass.
Phase 2b provides ``NoOpImportHandler``, ``RegexCommentHandler``, and
``NoOpPatternCompiler`` as generic stubs used for languages that do not yet
have a dedicated plugin.

Phase 2c extracts Python-specific code into ``PythonImportHandler``,
``PythonCommentHandler``, and ``PythonPatternCompiler`` in ``python_plugin.py``.
Phase 2d rewires the original call sites in ``transform.py`` and ``lint.py``
to delegate through the plugin system.
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
    def extract_imports(self, source: str) -> str:
        """Return all top-level import statements as a single string."""
        ...

    @abstractmethod
    def add_import_text(
        self, import_str: str, position: int, source_code: str
    ) -> str:
        """Return *source_code* with *import_str* inserted at *position*.

        *position* == 0 means prepend (after ``__future__`` imports);
        *position* == -1 means append (after the last import).
        """
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

    @abstractmethod
    def rename_in_docstrings(
        self, content: str, old_name: str, new_name: str
    ) -> str | None:
        """Replace *old_name* with *new_name* in all docstrings.

        Returns new content if changes were made, ``None`` otherwise.
        """
        ...


class PatternCompiler(ABC):
    """Abstract interface for compiling pattern strings to the Rust IR."""

    @abstractmethod
    def compile(self, pattern_str: str) -> dict | None:
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

    def extract_imports(self, source: str) -> str:
        return ""

    def add_import_text(
        self, import_str: str, position: int, source_code: str
    ) -> str:
        return source_code

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

    def rename_in_docstrings(
        self, content: str, old_name: str, new_name: str
    ) -> str | None:
        return None


class NoOpPatternCompiler(PatternCompiler):
    """Pattern compiler that defers to the default Rust IR path (returns None)."""

    def compile(self, pattern_str: str) -> dict | None:
        # Returning None signals callers to use the default compilation path.
        return None


class TreeSitterPatternCompiler(PatternCompiler):
    """Universal pattern compiler using tree-sitter for any language.

    This compiler works by:
    1. Replacing metavariables ($X, $...ARGS) with valid placeholders.
    2. Parsing the munged string with tree-sitter for the target language.
    3. Converting the tree-sitter CST to the Rust IR dict.
    4. Mapping placeholder nodes back to Metavar and Ellipsis nodes.
    """

    def __init__(self, language: str) -> None:
        self.language = language

    def compile(self, pattern_str: str) -> dict | None:
        from emend import emend_core
        from emend.language_registry import get_extensions
        from emend.pattern import parse_pattern, MetaVar

        # 1. Parse metavariables using existing Lark grammar
        try:
            pattern = parse_pattern(pattern_str)
        except Exception:
            return None

        # 2. Replace metavariables with placeholders
        # We use _build_metavar_map_and_replace logic but we need the map
        # to fixup the result.
        metavar_map: dict[str, MetaVar] = {}
        temp_code = pattern_str

        # Sort by length descending to avoid partial matches
        sorted_mvs = sorted(pattern.metavars, key=lambda mv: (
            -len(f"$...{mv.name}:{mv.type_constraint or ''}"),
            -len(f"$...{mv.name}"),
            -len(f"${mv.name}:{mv.type_constraint or ''}"),
            -len(f"${mv.name}")
        ))

        for mv in sorted_mvs:
            placeholder = f"__META_{mv.name}__"
            metavar_map[placeholder] = mv

            if mv.name == "_":
                pattern_str_meta = "$_"
            elif mv.ellipsis and mv.type_constraint:
                pattern_str_meta = f"$...{mv.name}:{mv.type_constraint}"
            elif mv.ellipsis:
                pattern_str_meta = f"$...{mv.name}"
            elif mv.type_constraint:
                pattern_str_meta = f"${mv.name}:{mv.type_constraint}"
            else:
                pattern_str_meta = f"${mv.name}"

            temp_code = temp_code.replace(pattern_str_meta, placeholder)

        # 3. Parse with tree-sitter via emend_core
        exts = get_extensions(self.language)
        ext = exts[0] if exts else "py"

        try:
            ir = emend_core.compile_pattern_treesitter(temp_code, ext)
        except Exception:
            return None

        # 4. Recursively replace placeholder nodes with Metavar/Ellipsis nodes
        return self._fixup_ir(ir, metavar_map)

    def _fixup_ir(self, ir: dict | list | str, metavar_map: dict[str, MetaVar]):
        if isinstance(ir, list):
            return [self._fixup_ir(item, metavar_map) for item in ir]
        if isinstance(ir, str):
            if ir in metavar_map:
                mv = metavar_map[ir]
                return mv.name
            return ir
        if not isinstance(ir, dict):
            return ir

        # If it's a name node, check if it's a placeholder
        if ir.get("type") == "name":
            val = ir.get("value")
            if isinstance(val, str) and val in metavar_map:
                mv = metavar_map[val]
                tc = mv.type_constraint
                if tc in ("int", "str", "call", "float", "identifier", "attr", "stmt"):
                    return {"type": "type_constraint", "kind": tc, "name": mv.name}
                if tc and tc.startswith("!"):
                    inner = tc[1:]
                    if inner in ("int", "str", "call", "float", "identifier", "attr", "stmt"):
                        return {"type": "type_constraint", "kind": tc, "name": mv.name}

                if mv.name == "_":
                    return {"type": "any_expr"}

                if mv.ellipsis:
                    return {"type": "ellipsis", "name": mv.name}
                else:
                    return {"type": "metavar", "name": mv.name}

        # Recursively fixup all fields
        new_ir = {}
        for k, v in ir.items():
            # Special case for import/import_from names which are strings
            if k == "name" and isinstance(v, str) and v in metavar_map:
                new_ir[k] = metavar_map[v].name
            elif k == "module" and isinstance(v, str) and v in metavar_map:
                new_ir[k] = metavar_map[v].name
            elif k == "asname" and isinstance(v, str) and v in metavar_map:
                new_ir[k] = metavar_map[v].name
            else:
                new_ir[k] = self._fixup_ir(v, metavar_map)

        # Post-process call args for ellipsis
        if new_ir.get("type") == "call" and "args" in new_ir:
            processed_args = []
            for arg in new_ir["args"]:
                if isinstance(arg, dict) and arg.get("type") == "arg":
                    val = arg.get("value")
                    if isinstance(val, dict) and val.get("type") == "ellipsis":
                        processed_args.append(val)
                        continue
                processed_args.append(arg)
            new_ir["args"] = processed_args

            has_ellipsis = any(
                isinstance(arg, dict) and arg.get("type") == "ellipsis"
                for arg in new_ir["args"]
            )
            # In our Rust IR, call has "exact_args" field
            new_ir["exact_args"] = not has_ellipsis

        return new_ir


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

    Returns the full Python plugin for ``"python"``; stubs for other languages.
    """
    if language == "python":
        from emend.python_plugin import create_python_plugin
        return create_python_plugin()

    from emend.language_registry import _find_languages_dir
    lang_dir = _find_languages_dir()
    if lang_dir:
        plugin_py = lang_dir / language / "plugin.py"
        if plugin_py.is_file():
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                f"emend.plugins.{language}", plugin_py
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, "create_plugin"):
                    return mod.create_plugin()

    prefix = _COMMENT_PREFIXES.get(language, "#")
    return LanguagePlugin(
        import_handler=NoOpImportHandler(),
        comment_handler=RegexCommentHandler(prefix),
        pattern_compiler=TreeSitterPatternCompiler(language),
    )
