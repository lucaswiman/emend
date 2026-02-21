"""Extended selector parsing with component access."""
import glob as glob_mod
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from lark import Lark, Transformer, Token
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

    def expand_file_glob(self) -> list[str]:
        """Expand file_path glob, returning matching .py files.

        Raises FileNotFoundError if no files match.
        """
        matches = [
            f for f in glob_mod.glob(self.file_path, recursive=True)
            if f.endswith('.py')
        ]
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
        # Filter out tokens (like ::), keep only transformed rules
        transformed = [item for item in items if not isinstance(item, Token)]

        file_path = transformed[0]
        # symbol_path is optional (for module-level components like imports)
        symbol_path = []
        components = []

        if len(transformed) > 1:
            # Check if next item is symbol_path (list) or component (dict)
            if isinstance(transformed[1], list):
                symbol_path = transformed[1]
                components = transformed[2:]
            else:
                # It's a component, symbol_path is empty
                components = transformed[1:]

        if components:
            comp = components[0]
            return ExtendedSelector(
                file_path=file_path,
                symbol_path=symbol_path,
                component=comp["name"],
                accessor=comp.get("accessor"),
                pseudo_class=comp.get("pseudo_class")
            )
        else:
            return ExtendedSelector(
                file_path=file_path,
                symbol_path=symbol_path
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
_parser = Lark(_grammar_text, parser="lalr", transformer=SelectorTransformer())


def parse_extended_selector(selector_str: str) -> ExtendedSelector:
    """Parse extended selector string.

    Args:
        selector_str: Selector in format file.py::Symbol.path[component][accessor]
                      or file.py:4 or file.py:4-10 for line-based selectors

    Returns:
        ExtendedSelector object with parsed components

    Examples:
        >>> sel = parse_extended_selector("file.py::func")
        >>> sel.file_path
        'file.py'
        >>> sel.symbol_path
        ['func']

        >>> sel = parse_extended_selector("file.py::Class.method[params][ctx]")
        >>> sel.component
        'params'
        >>> sel.accessor
        'ctx'

        >>> sel = parse_extended_selector("file.py:10")
        >>> sel.line_start
        10

        >>> sel = parse_extended_selector("file.py:10-20")
        >>> sel.line_start
        10
        >>> sel.line_end
        20
    """
    # Pre-check for line-based selectors (file.py:4 or file.py:4-10)
    # Only match if there's no :: (which indicates a symbol selector)
    if '::' not in selector_str:
        line_match = re.match(r'^(.+):(\d+)(?:-(\d+))?$', selector_str)
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
    return _parser.parse(selector_str)
