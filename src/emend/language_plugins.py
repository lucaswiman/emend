"""Language plugin ABCs and stub implementations.

Each language plugin composes three handlers:
- ``ImportHandler``: extract / add / remove import statements
- ``CommentHandler``: find docstrings and noqa-style comments
- ``PatternCompiler``: compile pattern strings to the Rust IR

Phase 2a defines the ABCs and the ``LanguagePlugin`` dataclass.
Phase 2b provides ``NoOpImportHandler`` and ``RegexCommentHandler`` as generic
stubs used for languages that do not yet have a dedicated plugin.

Phase 2c extracts Python-specific code into ``PythonImportHandler``,
``PythonCommentHandler``, and ``PythonPatternCompiler`` in ``python_plugin.py``.
Phase 2d rewires the original call sites in ``transform.py`` and ``lint.py``
to delegate through the plugin system.

Phase 6 (noqa consolidation): ``NOQA_PATTERN`` is the canonical core regex for
noqa suppression comments.  Importers should build their own ``re.compile``
using this string rather than duplicating the pattern.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Canonical noqa pattern core (Phase 6)
# ---------------------------------------------------------------------------

#: Core regex fragment that matches ``noqa`` followed by an optional tag list.
#: Does **not** include the comment-prefix (``#``, ``//``) — callers add that.
#:
#: Group 1 (optional): the tag string after the colon, e.g. ``"emend:deadcode"``.
NOQA_PATTERN: str = r'noqa\b(?:\s*:\s*(.*))?'


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


class TreeSitterImportHandler(ImportHandler):
    """Import handler using tree-sitter scope resolver for any language.

    Works for any language whose ``config.toml`` defines an ``[imports]``
    section (TypeScript, Rust, Go, etc.).  Uses ``emend_core.PyScopeResolver``
    to identify import bindings and then maps them back to source lines.
    """

    def __init__(self, language: str, extensions: list[str],
                 import_keywords: tuple[str, ...] | None = None) -> None:
        self._language = language
        self._extensions = extensions
        # Keywords that mark import lines in source (used as fallback and
        # for ``add_import_text`` / ``remove_import`` heuristics).
        self._import_keywords: tuple[str, ...] = import_keywords or ("import",)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _ext(self) -> str:
        return self._extensions[0] if self._extensions else "ts"

    def _is_import_line(self, stripped: str) -> bool:
        """Return True if *stripped* (a ``str.strip()``-ed line) looks like an
        import statement for this language."""
        for kw in self._import_keywords:
            if stripped.startswith(kw + " ") or stripped.startswith(kw + "("):
                return True
            # Handle lines that begin with visibility modifiers (e.g. ``pub use``)
            # Use word-boundary check to avoid "use" matching "excuse".
            # Split on whitespace to get words (no regex needed for whole-word check).
            words = stripped.split()
            if stripped.startswith("pub ") and kw in words:
                return True
            if stripped.startswith("export ") and kw in words:
                return True
        # TypeScript/JavaScript re-exports: ``export { X } from 'Y'``
        # These act as imports and should be included.
        if stripped.startswith("export ") and " from " in stripped:
            return True
        return False

    # ------------------------------------------------------------------
    # ImportHandler API
    # ------------------------------------------------------------------

    def extract_imports(self, source: str) -> str:
        """Return all top-level import statements as a single string.

        Tries the scope resolver first for precise results.  When the scope
        resolver does not return any imports (e.g. because the language config
        does not yet fully support import extraction) falls back to
        keyword-based line scanning.
        """
        lines = source.splitlines(keepends=True)
        import_line_indices: set[int] = set()

        # --- Phase 1: try the scope resolver for precision ---------------
        try:
            from emend import emend_core
            ext = self._ext()
            fake_path = "__temp__." + ext
            resolver = emend_core.PyScopeResolver(".", ext)
            resolver.index_file(fake_path, source)
            imports = resolver.imports_in_file(fake_path)
        except Exception:
            imports = []

        if imports:
            module_paths: set[str] = set()
            imported_names: set[str] = set()
            for local_name, module_path, imported_name, _is_star in imports:
                module_paths.add(module_path)
                imported_names.add(local_name)
                if imported_name:
                    imported_names.add(imported_name)

            for i, line in enumerate(lines):
                stripped = line.strip()
                if not stripped or stripped.startswith("//") or stripped.startswith("#"):
                    continue
                if not self._is_import_line(stripped):
                    continue
                for mp in module_paths:
                    if mp in line:
                        import_line_indices.add(i)
                        break
                else:
                    for name in imported_names:
                        if name in line:
                            import_line_indices.add(i)
                            break

        # --- Phase 2: keyword fallback -----------------------------------
        if not import_line_indices:
            for i, line in enumerate(lines):
                stripped = line.strip()
                if not stripped:
                    continue
                if self._is_import_line(stripped):
                    import_line_indices.add(i)

        if not import_line_indices:
            return ""

        # --- Phase 3: expand multi-line imports --------------------------
        expanded: set[int] = set()
        for idx in sorted(import_line_indices):
            expanded.add(idx)
            j = idx + 1
            while j < len(lines):
                sj = lines[j].strip()
                if not sj:
                    break
                if self._is_import_line(sj):
                    break
                preceding = "".join(lines[idx:j])
                if (preceding.count("{") > preceding.count("}")
                        or preceding.count("(") > preceding.count(")")):
                    expanded.add(j)
                    j += 1
                else:
                    break

        return "".join(lines[i] for i in sorted(expanded))

    def add_import_text(
        self, import_str: str, position: int, source_code: str
    ) -> str:
        """Insert *import_str* into *source_code*.

        *position* == 0 inserts before the first import; any other value
        inserts after the last import.
        """
        lines = source_code.splitlines(keepends=True)
        import_line = import_str.rstrip("\n") + "\n"

        first_import_idx: int | None = None
        last_import_idx: int | None = None
        for i, line in enumerate(lines):
            if self._is_import_line(line.strip()):
                if first_import_idx is None:
                    first_import_idx = i
                last_import_idx = i

        if position == 0:
            insert_at = first_import_idx if first_import_idx is not None else 0
            lines.insert(insert_at, import_line)
        else:
            if last_import_idx is not None:
                lines.insert(last_import_idx + 1, import_line)
            else:
                lines.insert(0, import_line)

        return "".join(lines)

    def remove_import(self, source: str, module: str, name: str) -> str:
        """Remove the import of *name* from *module*.

        Uses a simple heuristic: drop lines that contain both *module* and
        *name* and look like imports.

        Module is checked with ``in`` (it may appear as a quoted string or
        path fragment).  Name is checked as a whole word by splitting on
        punctuation/whitespace to avoid false positives on substrings.

        TODO: replace with tree-sitter byte-range editing once ``emend_core``
        exposes an ``import_node_range(source, module, name, ext)`` helper.
        That would correctly handle grouped imports such as
        ``import { A, B } from "mod"`` where only one name must be removed.
        """
        lines = source.splitlines(keepends=True)
        result: list[str] = []
        for line in lines:
            stripped = line.strip()
            # Split on common punctuation as well as whitespace so that
            # ``{ foo }`` yields the word ``foo`` and ``std::io;`` yields
            # both ``std`` and ``io`` (Rust path separator ``::`` acts as
            # a word boundary in identifier matching).
            words = stripped.replace(",", " ").replace("{", " ").replace(
                "}", " ").replace("(", " ").replace(")", " ").replace(
                "'", " ").replace('"', " ").replace("::", " ").replace(
                ";", " ").split()
            if (self._is_import_line(stripped)
                    and module in stripped
                    and name in words):
                continue
            result.append(line)
        return "".join(result)


class RegexCommentHandler(CommentHandler):
    """Generic comment handler for languages with a simple line-comment prefix.

    Recognises ``<prefix> noqa: tag1,tag2`` comments.  Does not handle
    block-style docstrings (returns empty list for ``find_docstrings``).

    The comment prefix can be supplied directly via *prefix*, or derived from
    a language name via *language* (reads ``config.toml``).  When neither is
    given, the prefix defaults to ``"#"``.
    """

    def __init__(
        self,
        prefix: str | None = None,
        *,
        language: str | None = None,
    ) -> None:
        if prefix is None:
            prefix = _get_comment_prefix(language or "")
        self._prefix = prefix
        escaped = re.escape(prefix)
        # Match "noqa: tag1,tag2" with tags
        self._noqa_tagged_pat = re.compile(
            escaped + r"\s*noqa:\s*(\S+)"
        )
        # Match bare "noqa" (no colon, no tags) to suppress all checks
        self._noqa_bare_pat = re.compile(
            escaped + r"\s*noqa\s*$"
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
            m = self._noqa_tagged_pat.search(line)
            if m:
                tags = {t.strip() for t in m.group(1).split(",") if t.strip()}
                result[lineno] = tags
            elif self._noqa_bare_pat.search(line):
                result[lineno] = None  # bare noqa suppresses all
        return result

    def rename_in_docstrings(
        self, content: str, old_name: str, new_name: str
    ) -> str | None:
        return None


class DocCommentHandler(RegexCommentHandler):
    """Extended comment handler with doc comment detection and renaming.

    Supports two doc comment styles:
    - ``"block"``: JSDoc-style ``/** ... */`` blocks (TypeScript/JavaScript)
    - ``"line"``: consecutive ``///`` or ``//!`` lines (Rust)
    """

    # JSDoc: /** ... */ (multiline, non-greedy)
    # TODO(phase-8): replace with tree-sitter ``comment`` node traversal once
    # ``emend_core`` exposes a helper that returns (start_byte, end_byte, text)
    # for all comment nodes in a source string.  JSDoc comments are ``comment``
    # nodes whose text starts with ``/**``; no regex needed at that point.
    _JSDOC_RE = re.compile(r'/\*\*.*?\*/', re.DOTALL)
    # Rust doc comments: consecutive lines starting with /// or //!
    # TODO(phase-8): replace with tree-sitter ``line_comment`` node traversal
    # (node text starting with ``///`` or ``//!``) once emend_core exposes it.
    _RUST_DOC_LINE_RE = re.compile(r'^[ \t]*(?:///|//!)', re.MULTILINE)

    def __init__(
        self,
        prefix: str | None = None,
        doc_style: str = "block",
        *,
        language: str | None = None,
    ) -> None:
        """
        Args:
            prefix: line comment prefix (e.g. ``//``); if omitted, *language*
                is used to read the prefix from ``config.toml``.
            doc_style: ``"block"`` for ``/** */`` style (JS/TS),
                ``"line"`` for ``///`` style (Rust)
            language: language name to look up prefix from config (used when
                *prefix* is not supplied).
        """
        super().__init__(prefix, language=language)
        self._doc_style = doc_style

    def _find_doc_comment_ranges(
        self, source: str
    ) -> list[tuple[int, int, str]]:
        """Return ``(start_byte, end_byte, text)`` for all doc comments."""
        encoded = source.encode("utf-8")
        results: list[tuple[int, int, str]] = []

        if self._doc_style == "block":
            for m in self._JSDOC_RE.finditer(source):
                text = m.group(0)
                start_byte = len(source[:m.start()].encode("utf-8"))
                end_byte = start_byte + len(text.encode("utf-8"))
                results.append((start_byte, end_byte, text))
        else:
            # "line" style: find runs of consecutive /// or //! lines
            lines = source.splitlines(keepends=True)
            i = 0
            while i < len(lines):
                if self._RUST_DOC_LINE_RE.match(lines[i]):
                    start_line = i
                    while i < len(lines) and self._RUST_DOC_LINE_RE.match(lines[i]):
                        i += 1
                    block_text = "".join(lines[start_line:i])
                    start_byte = len("".join(lines[:start_line]).encode("utf-8"))
                    end_byte = start_byte + len(block_text.encode("utf-8"))
                    results.append((start_byte, end_byte, block_text))
                else:
                    i += 1

        return results

    def find_docstrings(
        self, source: str, symbol_byte_range: tuple[int, int]
    ) -> list[tuple[int, int, str]]:
        """Return doc comments that fall within *symbol_byte_range*."""
        all_docs = self._find_doc_comment_ranges(source)
        start, end = symbol_byte_range
        return [
            (ds, de, text)
            for ds, de, text in all_docs
            if ds >= start and de <= end
        ]

    def rename_in_docstrings(
        self, content: str, old_name: str, new_name: str
    ) -> str | None:
        """Replace *old_name* with *new_name* inside doc comments.

        Returns new content if changes were made, ``None`` otherwise.
        """
        all_docs = self._find_doc_comment_ranges(content)
        if not all_docs:
            return None

        # Work on lines for simpler replacement
        lines = content.splitlines(keepends=True)
        # Build a set of line indices that belong to doc comments
        doc_line_indices: set[int] = set()
        running_byte = 0
        byte_to_line: list[int] = []
        for idx, line in enumerate(lines):
            line_bytes = len(line.encode("utf-8"))
            byte_to_line.append(running_byte)
            running_byte += line_bytes

        for ds, de, _text in all_docs:
            for idx, line_start in enumerate(byte_to_line):
                line_end = line_start + len(lines[idx].encode("utf-8"))
                if line_start < de and line_end > ds:
                    doc_line_indices.add(idx)

        changed = False
        word_pat = re.compile(r'\b' + re.escape(old_name) + r'\b')
        for idx in doc_line_indices:
            new_line = word_pat.sub(new_name, lines[idx])
            if new_line != lines[idx]:
                lines[idx] = new_line
                changed = True

        if changed:
            return "".join(lines)
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
            placeholder = f"_META_{mv.name}_"
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
                if mv.name == "_":
                    return {"type": "any_expr"}

                tc = mv.type_constraint
                if tc in ("int", "str", "call", "float", "identifier", "attr", "stmt"):
                    return {"type": "type_constraint", "kind": tc, "name": mv.name}
                if tc and tc.startswith("!"):
                    inner = tc[1:]
                    if inner in ("int", "str", "call", "float", "identifier", "attr", "stmt"):
                        return {"type": "type_constraint", "kind": tc, "name": mv.name}

                if mv.ellipsis:
                    return {"type": "ellipsis", "name": mv.name}
                else:
                    return {"type": "metavar", "name": mv.name}

        # Recursively fixup all fields
        new_ir = {}
        for k, v in ir.items():
            if k in ("name", "module", "asname") and isinstance(v, str) and v in metavar_map:
                # Fields that expect a name string but got a placeholder
                new_ir[k] = metavar_map[v].name
            elif k == "value" and isinstance(v, str) and v in metavar_map:
                # 'value' usually expects an IR dict, but might have got a placeholder string
                # We fix it up by treating it as a Name node and then fixing that
                new_ir[k] = self._fixup_ir({"type": "name", "value": v}, metavar_map)
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

def _get_comment_prefix(language: str) -> str:
    """Return the line-comment prefix for *language*.

    Delegates to ``language_registry.get_comment_prefix``, which reads from
    the language's ``config.toml`` and falls back to ``"#"``.
    """
    try:
        from emend.language_registry import get_comment_prefix
        return get_comment_prefix(language)
    except Exception:
        return "#"


def _load_plugin_file(language: str, plugin_py: "Path") -> "LanguagePlugin | None":
    """Try to load a ``LanguagePlugin`` from a ``plugin.py`` path.

    Returns ``None`` if the file does not exist or has no ``create_plugin``
    function.
    """
    import importlib.util
    if not plugin_py.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        f"emend.plugins.{language}", plugin_py
    )
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "create_plugin"):
            return mod.create_plugin()
    return None


def load_plugin(language: str) -> "LanguagePlugin":
    """Return a ``LanguagePlugin`` for *language*.

    Returns the full Python plugin for ``"python"``; for other languages,
    tries built-in plugin files and entry-point plugins before falling back
    to a generic stub with the correct comment prefix.
    """
    if language == "python":
        from emend.python_plugin import create_python_plugin
        return create_python_plugin()

    from emend.language_registry import _find_languages_dir, _discover_entry_point_languages

    # 1. Check built-in languages directory
    lang_dir = _find_languages_dir()
    if lang_dir:
        plugin = _load_plugin_file(language, lang_dir / language / "plugin.py")
        if plugin is not None:
            return plugin

    # 2. Check entry-point plugins from third-party packages
    ep_langs = _discover_entry_point_languages()
    if language in ep_langs:
        plugin = _load_plugin_file(language, ep_langs[language] / "plugin.py")
        if plugin is not None:
            return plugin

    return LanguagePlugin(
        import_handler=NoOpImportHandler(),
        comment_handler=RegexCommentHandler(language=language),
        pattern_compiler=TreeSitterPatternCompiler(language),
    )
