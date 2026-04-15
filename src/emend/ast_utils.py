"""AST utilities for traversing and finding symbols in Python code."""

import ast
import fnmatch
from pathlib import Path
from typing import Callable, Optional, Tuple

from emend import emend_core
from emend.component_selector import NestedSymbol

# Re-export tree-sitter node types from emend_core so callers can do:
#   from emend.ast_utils import PyNode, PyTree, parse_source, parse_file
PyNode = emend_core.PyNode
PyTree = emend_core.PyTree
parse_source = emend_core.parse_source
parse_file = emend_core.parse_file


def get_imports(filepath: str) -> list[dict]:
    """Extract all imports from a Python file.

    Returns:
        List of dicts:
        - { 'module': 'foo.bar', 'name': 'Baz', 'asname': 'B', 'level': 0 }
        - { 'module': 'foo.bar', 'name': '*', 'level': 0 }  (for star import)
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except Exception:
        return []

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "module": alias.name,
                    "name": None,
                    "asname": alias.asname,
                    "level": 0
                })
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            level = node.level
            for alias in node.names:
                imports.append({
                    "module": module,
                    "name": alias.name,
                    "asname": alias.asname,
                    "level": level
                })
    return imports


def resolve_through_reexports(
    file_path: str,
    symbol_name: str,
    resolve_module_cb: Callable[[str, int, str], Optional[str]],
    visited: Optional[set] = None,
    depth: int = 0
) -> Optional[Tuple[str, int]]:
    """Follow re-exports (star imports or explicit imports) to find the actual definition.

    Args:
        file_path: Current file to search in.
        symbol_name: Name of the symbol to find.
        resolve_module_cb: Callback (module_name, level, current_file) -> file_path.
        visited: Set of visited file paths to prevent infinite loops.
        depth: Current recursion depth.

    Returns:
        Tuple of (file_path, line_number) or None if not found.
    """
    if visited is None:
        visited = set()

    file_path_abs = str(Path(file_path).resolve())
    if file_path_abs in visited or depth > 10:
        return None
    visited.add(file_path_abs)

    if not Path(file_path).is_file():
        return None

    try:
        definitions = find_nested_definitions(file_path)
        symbol = find_symbol_by_path(definitions, [symbol_name])
        if symbol:
            return file_path, symbol.line_start
    except Exception:
        pass

    # Not defined here. Check imports.
    imports = get_imports(file_path)
    
    # 1. Check explicit re-exports: from module import symbol [as alias]
    for imp in imports:
        # Match if either the name or the alias matches what we're looking for
        if imp["name"] == symbol_name or (imp["asname"] and imp["asname"] == symbol_name):
            # If it was aliased, we're looking for the original name in the target module
            target_symbol = imp["name"] if imp["asname"] == symbol_name else symbol_name
            target_file = resolve_module_cb(imp["module"], imp["level"], file_path)
            
            if target_file:
                # If target_file is a directory, check if the target_symbol matches a file or submodule.
                is_module = False
                if Path(target_file).is_dir():
                    candidate = Path(target_file) / (target_symbol + ".py")
                    if candidate.is_file():
                        target_file = str(candidate)
                        is_module = True
                    else:
                        init_py = Path(target_file) / "__init__.py"
                        if init_py.is_file():
                            target_file = str(init_py)
                elif Path(target_file).suffix == ".py" and Path(target_file).stem == target_symbol:
                    is_module = True
                
                result = resolve_through_reexports(
                    target_file, target_symbol, resolve_module_cb, visited, depth + 1
                )
                if result:
                    return result
                
                # If it didn't find a nested symbol but target_file exists, 
                # and it's a module we were looking for, return it.
                if is_module and Path(target_file).is_file():
                    return target_file, 1

    # 2. Check star imports: from module import *
    for imp in imports:
        if imp["name"] == "*":
            target_file = resolve_module_cb(imp["module"], imp["level"], file_path)
            if target_file:
                if Path(target_file).is_dir():
                    init_py = Path(target_file) / "__init__.py"
                    if init_py.is_file():
                        target_file = str(init_py)

                # Recursively search in the target file.
                result = resolve_through_reexports(
                    target_file, symbol_name, resolve_module_cb, visited, depth + 1
                )
                if result:
                    return result

    return None


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
    with open(filepath) as f:
        source = f.read()

    ext = Path(filepath).suffix.lstrip('.') or 'py'
    rust_syms = emend_core.collect_symbols_from_str(
        source,
        max_depth=max_depth + 1 if max_depth is not None else None,
        ext=ext,
    )
    # Filter out variables and references at the top level
    # to return only function/class definitions.
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
