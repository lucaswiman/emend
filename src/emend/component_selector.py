"""Extended selector parsing with component access."""
import glob as glob_mod
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from lark import Lark, Transformer, Token
from lark.exceptions import LarkError
import importlib.resources


PSEUDO_CLASS_TO_KIND = {
    "KEYWORD_ONLY": "KEYWORD_ONLY",
    "POSITIONAL_ONLY": "POSITIONAL_ONLY",
    "POSITIONAL_OR_KEYWORD": "POSITIONAL_OR_KEYWORD",
}


class _PseudoClassMarker:
    """Marker to distinguish pseudo_class values from accessor values."""
    def __init__(self, value: str):
        self.value = value


def parse_selector(selector: str) -> tuple[str, str]:
    """Split ``file::symbol`` into ``(file, symbol)``.

    If no ``::`` is present, returns ``(selector, "")`` so callers can treat
    the absence of a symbol part uniformly. Only the first ``::`` is used as a
    separator; further ``::`` occurrences stay in the symbol part.
    """
    file_part, _sep, sym_part = selector.partition("::")
    return file_part, sym_part


@dataclass
class NestedSymbol:
    """Represents a discovered symbol with nesting information."""
    name: str
    kind: str  # 'class', 'function', 'async_function', 'method', 'async_method'
    line_start: int
    line_end: int
    col_offset: int
    path: list[str]  # Full path like ['MyClass', '_build', 'nested_func']
    decorators: list[str] = field(default_factory=list)
    decorator_line_start: int | None = None  # Line number of first decorator
    parameters: list[str] = field(default_factory=list)
    children: list['NestedSymbol'] = field(default_factory=list)


@dataclass
class ExtendedSelector:
    file_path: str
    symbol_path: list[str]
    component: str | None = None
    accessor: str | int | None = None
    pseudo_class: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    type_filter: str | None = None  # e.g. "returns[str]" or "type[Connection]"

    @property
    def language(self) -> str:
        """Return the source language for this selector's file_path.

        Defaults to 'python' if extension is unknown.
        """
        from emend.language_registry import detect_language
        return detect_language(self.file_path) or "python"

    @property
    def line_range(self) -> tuple[int, int] | None:
        """Return (line_start, line_end) tuple if both are set, else None."""
        if self.line_start is not None and self.line_end is not None:
            return (self.line_start, self.line_end)
        return None

    def has_wildcards(self) -> bool:
        """Check if selector contains wildcard patterns."""
        return any('*' in segment for segment in self.symbol_path)

    def has_file_glob(self) -> bool:
        """Check if file_path contains glob wildcards (* or ?)."""
        return '*' in self.file_path or '?' in self.file_path

    def expand_file_glob(self, language: str | None = None) -> list[str]:
        """Expand file_path glob, returning matching source files.

        Args:
            language: Source language to filter by.  ``None`` (the default)
                accepts any supported source language — hard-defaulting to
                Python here made every non-Python glob selector
                (``*.ts::greet``) fail with "No files match".

        Raises FileNotFoundError if no files match.
        """
        from emend.language_registry import is_source_file, matches_language
        candidates = glob_mod.glob(self.file_path, recursive=True)
        if language is None:
            matches = [f for f in candidates if is_source_file(f)]
        else:
            matches = [f for f in candidates if matches_language(f, language)]
        if not matches:
            raise FileNotFoundError(f"No files match: {self.file_path}")
        return sorted(matches)

    def with_file_path(self, new_path: str) -> 'ExtendedSelector':
        """Return a copy with a different file_path."""
        return replace(self, file_path=new_path)


class SelectorTransformer(Transformer):
    """Transform parse tree to ExtendedSelector."""

    def start(self, items):
        return items[0]

    def selector(self, items):
        return items[0]

    def explicit_selector(self, items):
        # Filter out tokens (like ::), keep only transformed rules
        transformed = [item for item in items if not isinstance(item, Token)]

        file_path = transformed[0]
        # symbol_path is optional (for module-level components like imports)
        symbol_path = []
        type_filter = None
        components = []

        rest = transformed[1:]
        # Consume symbol_path (list), then optional type_filter (str), then components (dicts)
        if rest and isinstance(rest[0], list):
            symbol_path = rest[0]
            rest = rest[1:]
        if rest and isinstance(rest[0], str) and not isinstance(rest[0], dict):
            type_filter = rest[0]
            rest = rest[1:]
        components = rest

        if components:
            comp = components[0]
            return ExtendedSelector(
                file_path=file_path,
                symbol_path=symbol_path,
                component=comp["name"],
                accessor=comp.get("accessor"),
                pseudo_class=comp.get("pseudo_class"),
                type_filter=type_filter,
            )
        else:
            return ExtendedSelector(
                file_path=file_path,
                symbol_path=symbol_path,
                type_filter=type_filter,
            )

    def dotted_selector(self, items):
        # Filter out tokens, keep only transformed rules
        transformed = [item for item in items if not isinstance(item, Token)]

        # symbol_path is required for dotted_selector
        symbol_path = transformed[0]
        type_filter = None
        components = []

        rest = transformed[1:]
        if rest and isinstance(rest[0], str) and not isinstance(rest[0], dict):
            type_filter = rest[0]
            rest = rest[1:]
        components = rest

        if components:
            comp = components[0]
            return ExtendedSelector(
                file_path="",  # No explicit file path
                symbol_path=symbol_path,
                component=comp["name"],
                accessor=comp.get("accessor"),
                pseudo_class=comp.get("pseudo_class"),
                type_filter=type_filter,
            )
        else:
            return ExtendedSelector(
                file_path="",
                symbol_path=symbol_path,
                type_filter=type_filter,
            )

    def file_path(self, items):
        return str(items[0])

    def symbol_path(self, items):
        # items will be symbol_segment nodes, extract their values
        result = []
        for item in items:
            if isinstance(item, Token):
                result.append(str(item))
            else:
                # It's a symbol_segment rule result
                result.append(str(item))
        return result

    def symbol_segment(self, items):
        # Extract the token value (WILDCARD or IDENTIFIER)
        return str(items[0])

    def component(self, items):
        result = {"name": str(items[0])}
        if len(items) > 1:
            item = items[1]
            if isinstance(item, _PseudoClassMarker):
                result["pseudo_class"] = item.value
            else:
                result["accessor"] = item
        if len(items) > 2:
            item = items[2]
            if isinstance(item, _PseudoClassMarker):
                result["pseudo_class"] = item.value
            else:
                result["accessor"] = item
        return result

    def type_filter(self, items):
        # Strip leading ':' from the TYPE_FILTER token
        return str(items[0])[1:]

    def pseudo_class(self, items):
        # Strip leading ':' from the PSEUDO_CLASS token and wrap with marker
        return _PseudoClassMarker(str(items[0])[1:])

    def accessor(self, items):
        value = items[0]
        # Try to parse as int, otherwise keep as string
        try:
            return int(value)
        except (ValueError, TypeError):
            return str(value)

    def COMPONENT_NAME(self, token):
        return str(token)

    def IDENTIFIER(self, token):
        return str(token)

    def INT(self, token):
        return int(token)

    def PATH(self, token):
        return str(token)

    def PSEUDO_CLASS(self, token):
        return str(token)


# Load grammar from package
_grammar_text = importlib.resources.read_text("emend.grammars", "selector.lark")
_parser = Lark(
    _grammar_text,
    parser="lalr",
    transformer=SelectorTransformer(),
    start=["explicit_selector", "dotted_selector", "selector"]
)


def parse_extended_selector(selector_str: str) -> ExtendedSelector:
    """Parse extended selector string.

    Args:
        selector_str: Selector in format file.py::Symbol.path[component][accessor]
                      or path.to.file.SomeSymbol
                      or file.py:4 or file.py:4-10 for line-based selectors

    Returns:
        ExtendedSelector object with parsed components
    """
    # Pre-check for line-based selectors (file.py:4 or file.py:4-10)
    # Only match if there's no :: (which indicates a symbol selector)
    if "::" not in selector_str:
        line_match = re.match(r"^(.+):(\d+)(?:-(\d+))?$", selector_str)
        if line_match:
            file_path = line_match.group(1)
            line_start = int(line_match.group(2))
            line_end = int(line_match.group(3)) if line_match.group(3) else line_start
            return ExtendedSelector(
                file_path=file_path,
                symbol_path=[],
                line_start=line_start,
                line_end=line_end,
            )
        # For dotted selectors without ::, use dotted_selector start rule
        try:
            return _parser.parse(selector_str, start="dotted_selector")
        except LarkError:
            # Fall back to default start (selector) which will probably fail but with a better error
            pass

    return _parser.parse(selector_str, start="explicit_selector")
