"""Python-specific language plugin implementations.

Provides concrete ``ImportHandler``, ``CommentHandler``, and
``PatternCompiler`` for Python source files.  These extract the logic that
was previously inlined in ``transform.py`` and ``lint.py``.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize

from emend.language_plugins import (
    CommentHandler,
    ImportHandler,
    LanguagePlugin,
    PatternCompiler,
)

# Copied from transform.py (_NOQA_RE) to avoid a circular import.
_NOQA_RE = re.compile(r'#\s*noqa\b(?:\s*:\s*(.*))?', re.IGNORECASE)


class PythonImportHandler(ImportHandler):
    """Import handler for Python source files."""

    def extract_imports(self, source: str) -> str:
        """Return all top-level import statements as a single string."""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return ""
        lines = source.splitlines(keepends=True)
        imports = []
        for stmt in tree.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                start = stmt.lineno - 1
                end = stmt.end_lineno
                imports.append("".join(lines[start:end]))
        return "".join(imports)

    def add_import_text(
        self, import_str: str, position: int, source_code: str
    ) -> str:
        """Return *source_code* with *import_str* inserted.

        Raises ``ValueError`` if *source_code* cannot be parsed.
        """
        tree = ast.parse(source_code)  # raises ValueError-wrapped SyntaxError in caller

        lines = source_code.splitlines(keepends=True)
        import_line = import_str.rstrip('\n') + '\n'

        first_import_line = None
        last_import_line = None
        last_future_line = None
        for stmt in tree.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                if first_import_line is None:
                    first_import_line = stmt.lineno
                last_import_line = stmt.end_lineno
                if isinstance(stmt, ast.ImportFrom) and stmt.module == '__future__':
                    last_future_line = stmt.end_lineno

        if position == 0:
            insert_at = (last_future_line or 0)
            lines.insert(insert_at, import_line)
        else:
            if last_import_line is not None:
                lines.insert(last_import_line, import_line)
            else:
                lines.insert(0, import_line)

        return "".join(lines)

    def remove_import(self, source: str, module: str, name: str) -> str:
        raise NotImplementedError("remove_import is not yet implemented for Python")


class PythonCommentHandler(CommentHandler):
    """Comment and docstring handler for Python source files."""

    @property
    def line_comment_prefix(self) -> str:
        return "#"

    def find_docstrings(
        self, source: str, symbol_byte_range: tuple[int, int]
    ) -> list[tuple[int, int, str]]:
        """Return ``(start_byte, end_byte, text)`` for each docstring."""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []

        encoded = source.encode()
        lines = source.splitlines(keepends=True)
        results: list[tuple[int, int, str]] = []

        def _collect(node: ast.AST) -> None:
            body = getattr(node, 'body', None)
            if not isinstance(body, list) or not body:
                return
            first = body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                start_line = first.lineno - 1
                end_line = first.end_lineno
                text = "".join(lines[start_line:end_line])
                start_byte = len("".join(lines[:start_line]).encode())
                end_byte = start_byte + len(text.encode())
                results.append((start_byte, end_byte, text))
            for child in body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    _collect(child)

        _collect(tree)
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
                                    rules.add(r[len("emend:"):])
                            if rules:
                                result[srow] = rules
                            # e.g. "# noqa: E501" with no emend: prefix → no effect
                        else:
                            result[srow] = None  # bare noqa suppresses all
        except tokenize.TokenError:
            pass
        return result

    def rename_in_docstrings(
        self, content: str, old_name: str, new_name: str
    ) -> str | None:
        """Replace *old_name* with *new_name* in all docstrings.

        Returns new content if changes were made, ``None`` otherwise.
        """
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return None

        lines = content.splitlines(keepends=True)
        docstring_ranges: list[tuple[int, int]] = []

        def _collect_docstrings(node: ast.AST) -> None:
            body = getattr(node, 'body', None)
            if not isinstance(body, list) or not body:
                return
            first = body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstring_ranges.append((first.lineno - 1, first.end_lineno))
            for child in body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    _collect_docstrings(child)

        _collect_docstrings(tree)

        if not docstring_ranges:
            return None

        changed = False
        for start_line, end_line in docstring_ranges:
            for i in range(start_line, end_line):
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
