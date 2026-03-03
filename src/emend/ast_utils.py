"""AST utilities for traversing and finding symbols in Python code."""

import fnmatch

from emend.component_selector import NestedSymbol


def _rust_dict_to_nested_symbol(d: dict) -> NestedSymbol:
    """Convert a Rust symbol dict to a NestedSymbol."""
    children = [_rust_dict_to_nested_symbol(c) for c in d.get("children", [])
                if c.get("kind") not in ("variable", "reference")]
    return NestedSymbol(
        name=d["name"],
        kind=d["kind"],
        line_start=d["line"],
        line_end=d["end_line"],
        col_offset=d.get("col_offset", 0),
        path=list(d.get("path", [])),
        decorators=list(d.get("decorators", [])),
        decorator_line_start=d.get("decorator_line_start"),
        parameters=list(d.get("param_names", [])),
        children=children,
    )


def find_nested_definitions(filepath: str, max_depth: int | None = None) -> list[NestedSymbol]:
    """Walk source and find all class/function definitions with nesting info.

    Uses tree-sitter via the Rust extension for parsing.
    """
    from emend import emend_core

    with open(filepath) as f:
        source = f.read()

    rust_syms = emend_core.collect_symbols_from_str(
        source,
        max_depth=max_depth + 1 if max_depth is not None else None,
    )
    # Filter out variables and references at the top level to match the
    # old LibCST behavior (only function/class definitions).
    return [_rust_dict_to_nested_symbol(d) for d in rust_syms
            if d.get("kind") not in ("variable", "reference")]


def find_symbol_by_path(symbols: list[NestedSymbol], path: list[str]) -> NestedSymbol | None:
    """Find a symbol by its path (e.g., ['MyClass', '_build', 'nested_func'])."""
    if not path:
        return None

    for sym in symbols:
        if sym.name == path[0]:
            if len(path) == 1:
                return sym
            return find_symbol_by_path(sym.children, path[1:])
    return None


def expand_wildcard_path(symbols: list[NestedSymbol], path: list[str]) -> list[NestedSymbol]:
    """Expand wildcard patterns in a symbol path (e.g., ['MyClass', '*', 'handle_*']).

    Returns all symbols matching the pattern.
    """
    if not path:
        return []

    def match_at_level(syms: list[NestedSymbol], remaining_path: list[str]) -> list[NestedSymbol]:
        if not remaining_path:
            return []

        pattern = remaining_path[0]
        matches = []

        for sym in syms:
            if fnmatch.fnmatch(sym.name, pattern):
                if len(remaining_path) == 1:
                    # Last segment - return this symbol
                    matches.append(sym)
                else:
                    # Recurse into children
                    matches.extend(match_at_level(sym.children, remaining_path[1:]))

        return matches

    return match_at_level(symbols, path)


def find_symbol_by_line(symbols: list[NestedSymbol], line: int, line_end: int | None = None) -> NestedSymbol | None:
    """Find the innermost symbol that contains the given line or line range.

    Args:
        symbols: List of symbols to search
        line: Line number to search for
        line_end: Optional end line for range search (defaults to line)

    Returns:
        The innermost symbol containing the line(s), or None if not found
    """
    if line_end is None:
        line_end = line

    def find_innermost(syms: list[NestedSymbol]) -> NestedSymbol | None:
        best_match = None
        for sym in syms:
            if sym.line_start <= line <= sym.line_end and sym.line_start <= line_end <= sym.line_end:
                best_match = sym
                child_match = find_innermost(sym.children)
                if child_match:
                    best_match = child_match
        return best_match

    return find_innermost(symbols)


def get_symbol_source(filepath: str, symbol: NestedSymbol, dedent: bool = False) -> str:
    """Extract the source code for a symbol, optionally dedenting."""
    with open(filepath) as f:
        lines = f.readlines()

    # Include decorator lines if present
    start_line = symbol.line_start
    if symbol.decorator_line_start is not None:
        start_line = symbol.decorator_line_start

    source_lines = lines[start_line - 1:symbol.line_end]
    source = ''.join(source_lines)

    if dedent:
        # Find minimum indentation
        non_empty_lines = [l for l in source_lines if l.strip()]
        if non_empty_lines:
            min_indent = min(len(l) - len(l.lstrip()) for l in non_empty_lines)
            dedented_lines = []
            for line in source_lines:
                if line.strip():
                    dedented_lines.append(line[min_indent:])
                else:
                    dedented_lines.append('\n')
            source = ''.join(dedented_lines)

    return source
