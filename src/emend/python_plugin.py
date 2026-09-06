"""Python-specific language plugin implementations.

Provides concrete ``ImportHandler``, ``CommentHandler``, and
``PatternCompiler`` for Python source files.  These extract the logic that
was previously inlined in ``transform.py`` and ``lint.py``.
"""
from __future__ import annotations

import io
import logging
import re
import tokenize

from emend.errors import BUG_EXCEPTIONS
from emend.language_plugins import (
    NOQA_PATTERN,
    CommentHandler,
    ImportHandler,
    LanguagePlugin,
    PatternCompiler,
)

logger = logging.getLogger(__name__)

# Build the Python-specific noqa regex from the canonical NOQA_PATTERN.
# Python's tokenize.COMMENT tokens include the leading '#', so we must
# match it here.  (No circular import: language_plugins does not import
# from python_plugin.)
_NOQA_RE = re.compile(r'#\s*' + NOQA_PATTERN, re.IGNORECASE)


def _get_structured_imports(source: str) -> list[dict]:
    """Return structured import dicts using tree-sitter scope resolver.

    Each dict has keys: module, level, names, start_byte, end_byte,
    start_line, end_line, is_plain.

    The Rust scope resolver deliberately omits ``from __future__ import ...``
    statements (they are not runtime dependencies).  This helper re-inserts
    them by scanning for lines that begin with ``from __future__`` so that
    import position detection remains correct for files that use
    ``from __future__ import annotations`` or similar.

    Returns an empty list on failure or if there are no imports.
    """
    from emend import emend_core

    try:
        resolver = emend_core.PyScopeResolver(".", "py")
        structured = resolver.collect_structured_imports_from_source(source, "py") or []
    except Exception:
        logger.debug("Structured import collection failed", exc_info=True)
        return []

    # Re-add __future__ imports that the Rust resolver omits.
    lines = source.splitlines(keepends=True)
    future_entries: list[dict] = []
    for lineno_0idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("from __future__"):
            # Compute byte offsets for this line
            start_byte = len("".join(lines[:lineno_0idx]).encode("utf-8"))
            line_text = line.rstrip("\n")
            end_byte = start_byte + len(line_text.encode("utf-8"))
            future_entries.append({
                "module": "__future__",
                "level": 0,
                "names": [],
                "start_byte": start_byte,
                "end_byte": end_byte,
                "start_line": lineno_0idx,  # 0-indexed, matching StructuredImport
                "end_line": lineno_0idx,     # 0-indexed, matching StructuredImport
                "is_plain": False,
            })

    if not future_entries:
        return structured

    # Merge __future__ entries (they come first) with the resolver results,
    # sorted by start_line to preserve document order.
    merged = future_entries + structured
    merged.sort(key=lambda d: d["start_line"])
    return merged


class PythonImportHandler(ImportHandler):
    """Import handler for Python source files."""

    def extract_imports(self, source: str) -> str:
        """Return all top-level import statements as a single string."""
        imports = _get_structured_imports(source)
        if not imports:
            return ""
        encoded = source.encode("utf-8")
        parts: list[str] = []
        for imp in imports:
            start_b = imp["start_byte"]
            end_b = imp["end_byte"]
            # Include the trailing newline if present
            if end_b < len(encoded) and encoded[end_b : end_b + 1] == b"\n":
                end_b += 1
            parts.append(encoded[start_b:end_b].decode("utf-8"))
        return "".join(parts)

    def add_import_text(
        self, import_str: str, position: int, source_code: str
    ) -> str:
        """Return *source_code* with *import_str* inserted.

        Uses tree-sitter structured imports for position detection.
        """
        imports = _get_structured_imports(source_code)

        lines = source_code.splitlines(keepends=True)
        import_line = import_str.rstrip("\n") + "\n"

        if position == 0:
            imports = [imp for imp in imports
                       if not imp["is_plain"] and imp.get("module") == "__future__"]
        insert_at = imports[-1]["end_line"] + 1 if imports else 0
        lines.insert(insert_at, import_line)

        return "".join(lines)

    def remove_import(self, source: str, module: str, name: str | None) -> str:
        """Return *source* with the import of *name* from *module* removed.

        If *name* is ``None``, the entire ``import <module>`` or
        ``from <module> import ...`` statement is removed.

        If *name* is given, only that name is removed from a
        ``from <module> import <name1>, <name2>`` statement.  When *name*
        is the only imported name the whole statement is removed.

        Uses tree-sitter structured import data (byte offsets) for all edits —
        no ``ast.parse`` or hand-rolled regex.
        """
        imports = _get_structured_imports(source)
        if not imports:
            return source

        encoded = source.encode("utf-8")

        # Collect replacements as (start_byte, end_byte_excl_newline, replacement)
        # We'll apply them in reverse order to preserve earlier byte offsets.
        replacements: list[tuple[int, int, str]] = []

        for imp in imports:
            # Determine whether this import statement matches the requested module.
            if imp["is_plain"]:
                # ``import foo`` or ``import foo as bar`` — module name is in names
                names: list[tuple[str, str | None]] = imp["names"]
                # For plain imports, ``module`` is the top-level package name.
                matching = [n for n, _a in names if n == module or n.split(".")[0] == module]
                if not matching:
                    continue

                if name is not None:
                    # Plain ``import X`` statements don't have individual names
                    # to remove — only remove the whole line when name matches.
                    if name not in matching:
                        continue
                # Remove the entire statement (whole line including newline).
            else:
                # ``from foo import bar`` — module is imp["module"]
                if imp["module"] != module:
                    continue

                if name is not None:
                    imp_names: list[tuple[str, str | None]] = imp["names"]
                    target_names = [n for n, _a in imp_names if n == name]
                    if not target_names:
                        continue

                    remaining = [(n, a) for n, a in imp_names if n != name]
                    if remaining:
                        # Rebuild the import line with the name removed.
                        def _alias_str(n: str, a: str | None) -> str:
                            return f"{n} as {a}" if a else n

                        # Preserve indentation of the original line.
                        start_line0 = imp["start_line"]  # 0-indexed
                        lines = source.splitlines(keepends=True)
                        orig_line = lines[start_line0] if start_line0 < len(lines) else ""
                        indent = orig_line[: len(orig_line) - len(orig_line.lstrip())]
                        new_stmt = (
                            f"{indent}from {module} import "
                            + ", ".join(_alias_str(n, a) for n, a in remaining)
                        )

                        start_b = imp["start_byte"]
                        end_b = imp["end_byte"]
                        # Include trailing newline in the replaced range.
                        if end_b < len(encoded) and encoded[end_b : end_b + 1] == b"\n":
                            end_b += 1

                        replacements.append((start_b, end_b, new_stmt + "\n"))
                        continue
                    # else: name was the only one — fall through to remove whole line

            # Remove the whole import statement (including trailing newline).
            start_b = imp["start_byte"]
            end_b = imp["end_byte"]
            if end_b < len(encoded) and encoded[end_b : end_b + 1] == b"\n":
                end_b += 1
            replacements.append((start_b, end_b, ""))

        if not replacements:
            return source

        # Apply replacements in reverse byte order to preserve earlier offsets.
        replacements.sort(key=lambda x: x[0], reverse=True)
        for start_b, end_b, repl in replacements:
            encoded = encoded[:start_b] + repl.encode("utf-8") + encoded[end_b:]

        return encoded.decode("utf-8")


def _find_docstring_ranges(source: str) -> list[tuple[int, int]]:
    """Return ``(start_line_0idx, end_line_0idx)`` (0-indexed, inclusive) for each docstring.

    Uses tree-sitter scope resolver to identify function/class/module bodies,
    then ``find_pattern`` to locate string literals at the first statement
    position of each body.
    """
    from emend import emend_core
    from emend.transform import find_pattern

    try:
        resolver = emend_core.PyScopeResolver(".", "py")
        resolver.index_file("__temp__.py", source)
        scopes = resolver.scopes_in_file("__temp__.py")
    except Exception:
        logger.debug("Scope collection failed for docstring detection", exc_info=True)
        return []

    try:
        str_matches = find_pattern("$X:str", "__temp__.py", source_override=source)
    except BUG_EXCEPTIONS:
        raise
    except Exception:
        logger.debug("String-literal pattern match failed for docstring detection", exc_info=True)
        return []

    # Build a dict from (1-indexed line) -> list of PatternMatch for strings
    str_by_line: dict[int, list] = {}
    for m in str_matches:
        if m.line is not None:
            str_by_line.setdefault(m.line, []).append(m)

    results: list[tuple[int, int]] = []
    seen: set[int] = set()

    for scope_kind, scope_start, scope_end, _bindings in scopes:
        # scope_start and scope_end are 0-indexed
        if scope_kind == "Module":
            # Module docstring is at line 0 (0-indexed) = line 1 (1-indexed)
            first_body_line_1idx = 1
        else:
            # Function/Class: body starts one line after the def/class line
            # scope_start is 0-indexed; convert to 1-indexed and add 1 for body
            first_body_line_1idx = scope_start + 2

        matches_on_first = str_by_line.get(first_body_line_1idx, [])
        for m in matches_on_first:
            if m.line in seen:
                continue
            seen.add(m.line)
            # Convert to 0-indexed
            start_0idx = m.line - 1
            end_0idx = (m.end_line or m.line) - 1
            results.append((start_0idx, end_0idx))

    return results


class PythonCommentHandler(CommentHandler):
    """Comment and docstring handler for Python source files."""

    @property
    def line_comment_prefix(self) -> str:
        return "#"

    def find_docstrings(
        self, source: str, symbol_byte_range: tuple[int, int]
    ) -> list[tuple[int, int, str]]:
        """Return ``(start_byte, end_byte, text)`` for each docstring."""
        ranges = _find_docstring_ranges(source)
        if not ranges:
            return []

        lines = source.splitlines(keepends=True)
        results: list[tuple[int, int, str]] = []

        for start_0idx, end_0idx in ranges:
            if start_0idx < 0 or start_0idx >= len(lines):
                continue
            text = "".join(lines[start_0idx : end_0idx + 1])
            start_byte = len("".join(lines[:start_0idx]).encode("utf-8"))
            end_byte = start_byte + len(text.encode("utf-8"))
            results.append((start_byte, end_byte, text))

        return results

    def find_noqa_comments(self, source: str) -> dict[int, set[str] | None]:
        """Return ``{line: tags}`` for ``# noqa`` comments via the tokenizer."""
        result: dict[int, set[str] | None] = {}
        try:
            tokens = tokenize.generate_tokens(io.StringIO(source).readline)
            for tok_type, tok_string, (srow, _), _, _ in tokens:
                if tok_type == tokenize.COMMENT:
                    m = _NOQA_RE.search(tok_string)
                    if m:
                        rules_str = m.group(1)
                        if rules_str:
                            rules = set()
                            for r in rules_str.split(","):
                                r = r.strip()
                                if r.startswith("emend:"):
                                    rules.add(r[len("emend:") :])
                            if rules:
                                result[srow] = rules
                            # e.g. "# noqa: E501" with no emend: prefix → no effect
                        else:
                            result[srow] = None  # bare noqa suppresses all
        except (tokenize.TokenError, IndentationError):
            pass
        return result

    def rename_in_docstrings(
        self, content: str, old_name: str, new_name: str
    ) -> str | None:
        """Replace *old_name* with *new_name* in all docstrings.

        Returns new content if changes were made, ``None`` otherwise.
        """
        ranges = _find_docstring_ranges(content)
        if not ranges:
            return None

        lines = content.splitlines(keepends=True)

        changed = False
        for start_0idx, end_0idx in ranges:
            for i in range(start_0idx, end_0idx + 1):
                if i < len(lines) and old_name in lines[i]:
                    lines[i] = lines[i].replace(old_name, new_name)
                    changed = True

        if changed:
            return "".join(lines)
        return None


class PythonPatternCompiler(PatternCompiler):
    """Pattern compiler for Python, delegating to ``_compile_python_pattern_to_rust_ir``."""

    def compile(self, pattern_str: str) -> dict | None:
        from emend.pattern import _compile_python_pattern_to_rust_ir

        return _compile_python_pattern_to_rust_ir(pattern_str)


def create_python_plugin() -> LanguagePlugin:
    """Return a fully-wired ``LanguagePlugin`` for Python."""
    return LanguagePlugin(
        import_handler=PythonImportHandler(),
        comment_handler=PythonCommentHandler(),
        pattern_compiler=PythonPatternCompiler(),
    )
